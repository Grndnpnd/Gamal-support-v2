# Bankr Discord Support Bot (Gamal)

A production-ready Discord support bot for the Bankr platform. Uses semantic search over the Bankr documentation to answer user questions, escalates unresolvable issues to [Plain](https://plain.com) support tickets, and maintains a two-way bridge between Discord ticket threads and Plain so agents can reply without users ever leaving Discord.

Includes an admin panel for live operational control (response overrides, busy mode), automatic LLM failover, and a full analytics dashboard.

---

## What it does

- **Answers Bankr questions** — semantic search (ChromaDB + MiniLM) finds relevant doc chunks, an LLM generates grounded answers
- **Proactive outreach** — detects support intent in channels and offers help without being @mentioned
- **Smart escalation** — offers to open a Plain ticket only when it genuinely can't help, not automatically
- **Discord ticket threads** — creates a private thread on the user's message when a ticket opens; user replies forward to Plain
- **Two-way relay** — agent replies in Plain post back to the Discord thread via webhook
- **Thread cleanup** — 10-second countdown deletion when tickets close, cancellable with `!keep`
- **Multilingual** — English, Simplified Chinese, Korean
- **Agent API** — exposes `/query` and `/query-docs` endpoints so other agents can query the knowledge base
- **Admin panel** — web UI for response overrides, busy mode, and analytics (see below)
- **Doc overrides** — admins can intercept matching questions with a custom message (e.g. during outages)
- **Busy mode** — suppresses passive proactive offers to staff during high-traffic windows
- **LLM failover** — automatically falls back to a secondary provider if the primary LLM is down
- **Analytics** — logs every interaction to Postgres; dashboard shows volume, token usage, resolution rates, and a documentation-gap report

---

## The five operational features

Beyond the core support bot, the admin panel (`/admin`) provides:

### Doc overrides

An override intercepts incoming messages that match any configured keyword and replies with an admin-set message instead of the normal doc-based answer. The main use case is incidents: during a service outage, post an override with keywords like `401, login, can't sign in` and a message like *"We've temporarily paused the service — we'll announce when it's back."* Any matching user gets that instead of the bot trying (and failing) to help.

Each override has an optional **"also offer a ticket"** flag — when on, the bot follows the override message with the standard "want a ticket?" prompt so users can still escalate. Overrides support time windows (start/end) and an enable/disable toggle. Managed from the admin panel; stored in Redis.

### Busy mode

When enabled, the bot stops sending **passive proactive offers** to members holding configured staff roles (Moderator, Support, etc). The point is high-traffic windows: when the support team is answering customers live in channels, the bot shouldn't keep jumping in on the team's own messages. Staff can always still @mention the bot directly — only the uninvited proactive path is suppressed. A toggle in the admin panel, off by default.

### LLM failover

The bot's primary LLM is the Bankr LLM Gateway. If a request to Bankr fails (outage, auth error, timeout), the bot transparently retries against a fallback provider (Ollama Cloud) so a Bankr outage degrades to "answers come from the backup model" instead of "every user gets an error." If both providers fail, the user gets a graceful degraded-mode message. Fully automatic — no admin action needed.

### Analytics & stats

Every handled interaction is logged to PostgreSQL — the question, an LLM-assigned topic tag, how it was resolved, token usage, latency, and whether it hit a documentation gap. The `/admin/stats` dashboard shows headline numbers, charts (message volume, resolution breakdown, token usage over time), and a **most-asked-topics / documentation-gap report** that highlights questions the docs don't answer well. Time-filterable by preset (24h/7d/30d) or arbitrary date range. Rows are auto-pruned after 90 days.

### Admin panel

A password-protected web UI at `/admin` (served by the `api` service) that ties the above together — override management, the busy-mode toggle, and the stats dashboard. Single shared password, signed session cookies.

---

## Architecture

```
Discord message
      │
      ├── @mention / reply to bot  →  handle directly
      ├── Active conversation      →  continue conversation
      └── Passive monitoring       →  detect support intent
                                         │
                                         ├── busy mode on + staff role  →  ignore
                                         └── score ≥ 2  →  proactive offer
      │
      ▼
Doc override check (Redis)  →  if a keyword matches, reply with override message, stop
      │
      ▼
SemanticDocsManager   ←  ChromaDB + all-MiniLM-L6-v2
      │
      ▼
LLM Router            →  Bankr LLM Gateway (primary)
      │                  └── on failure: Ollama Cloud / Gemma (fallback)
      │
      ├── [SUGGEST_ESCALATE]  →  "try this, reply if it doesn't work"
      ├── [NEEDS_TICKET]      →  "want me to open a ticket?"
      ├── [TOPIC: x]          →  stripped, logged for analytics
      └── Normal response     →  reply in Discord
            │
            ├── Conversation logged to Postgres (stats)
            │
            └── Ticket opened
                    │
                    ├── Plain thread created (GraphQL API)
                    ├── Discord thread created on original message
                    ├── Plain ↔ Discord link stored in Redis
                    │
                    └── Agent replies in Plain
                              │
                              ▼
                        webhook_server.py  ←  Plain webhook POST (HMAC verified)
                              │
                              ▼
                        Discord ticket thread  (reply posted)
```

---

## File structure

```
├── bot.py                 Main Discord bot
├── webhook_server.py      Plain webhook receiver → Discord relay
├── api_server.py          Agent API + admin panel (/admin/*)
├── admin_routes.py        Admin panel routes — overrides, busy mode, stats
├── plain_client.py        Plain GraphQL API client
├── shared.py              Shared SemanticDocsManager + Bankr LLM client
├── llm_router.py          LLM failover router (Bankr primary, Ollama Cloud fallback)
├── llm_response.py        LLMResponse — str subclass carrying token usage
├── db.py                  PostgreSQL analytics layer
├── redis_map.py           Plain↔Discord thread map (Redis/memory)
├── redis_overrides.py     Doc override storage + matching (Redis/memory)
├── redis_settings.py      Admin-tunable bot settings, e.g. busy mode (Redis/memory)
├── requirements.txt       Python dependencies
├── Procfile               Railway service definitions
├── .env.example           Environment variable template
└── SKILL.md               Example agent skill (reference only)
```

---

## Services

The deployment runs four Railway services plus two databases, all from this one repo:

| Service | Start command | Purpose | Public URL |
|---|---|---|---|
| `bot` | `python bot.py` | The Discord bot | No |
| `webhook` | `python webhook_server.py` | Plain → Discord reply relay | Yes |
| `api` | `python api_server.py` | Agent API + admin panel | Yes |
| Redis | (Railway managed) | Thread map, overrides, settings | — |
| Postgres | (Railway managed) | Analytics / stats | — |

---

## Prerequisites

Before you start you will need accounts and credentials for:

| Service | What you need | Where to get it |
|---|---|---|
| Discord | Bot token + app | discord.com/developers |
| Bankr | LLM Gateway API key + credits | bankr.bot/api |
| Ollama Cloud | API key (for LLM failover) | ollama.com/settings/keys |
| Plain | Machine user API key | plain.com → Settings → Machine Users |
| GitHub | Account + repo | github.com |
| Railway | Account | railway.app |

---

## Part 1 — Discord bot setup

### 1.1 Create the application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** in the top right
3. Give it a name — e.g. `Bankr Support` — and click **Create**

### 1.2 Create the bot user

1. In the left sidebar click **Bot**
2. Click **Add Bot** → **Yes, do it!**
3. Under **Token** click **Reset Token** then **Copy** — save this, you will need it as `DISCORD_TOKEN`
4. Scroll down to **Privileged Gateway Intents** and enable both:
   - **Message Content Intent**
   - **Server Members Intent**
5. Click **Save Changes**

### 1.3 Set bot permissions

1. In the left sidebar click **OAuth2** → **URL Generator**
2. Under **Scopes** check: `bot` and `applications.commands`
3. Under **Bot Permissions** check:
   - Read Messages / View Channels
   - Send Messages
   - Send Messages in Threads
   - Create Public Threads
   - Manage Threads
   - Read Message History
   - Add Reactions
4. Copy the generated URL at the bottom
5. Open the URL in your browser and invite the bot to your server

---

## Part 2 — Bankr LLM Gateway setup

### 2.1 Create an API key

1. Go to [bankr.bot/api](https://bankr.bot/api)
2. Click **Generate API Key**
3. Give it a name like `Support Bot`
4. Make sure **LLM Gateway** is toggled on
5. Copy the key — it starts with `bk_` — save it as `BANKR_LLM_KEY`

### 2.2 Add LLM credits

The gateway requires a credit balance. New accounts start at $0 and will return a `402` error on every LLM call until you top up.

1. Go to [bankr.bot/llm?tab=credits](https://bankr.bot/llm?tab=credits)
2. Add at least $5–10 to start
3. Optionally enable **auto top-up** so you never run dry in production

---

## Part 3 — Ollama Cloud setup (LLM failover)

The bot uses Ollama Cloud as an automatic fallback if the Bankr gateway is unavailable. This is optional — if you skip it, the bot still works, but a Bankr outage will take the bot down instead of degrading gracefully.

1. Go to [ollama.com](https://ollama.com) and sign in or create an account
2. Go to [ollama.com/settings/keys](https://ollama.com/settings/keys) and create an API key
3. Save it as `OLLAMA_CLOUD_KEY`
4. The default fallback model is `gemma4:31b-cloud` — a fast, general-purpose cloud model. You can override it with `OLLAMA_FALLBACK_MODEL` if you prefer a different one.

---

## Part 4 — Plain setup

### 4.1 Create a Machine User

1. Go to your Plain workspace → **Settings** → **Machine Users**
2. Click **Add Machine User**
3. **Name:** `Discord Bot` (internal label)
4. **Public name:** `Bankr Support` (shown to customers)
5. Click **Save**

### 4.2 Create an API key

1. Click into the machine user you just created
2. Click **Add API Key**
3. Name it `Discord Bot Key`
4. Select these permissions:
   - `customer:create`, `customer:edit`, `customer:read`
   - `thread:create`, `thread:read`, `thread:reply`
   - `threadField:create`, `threadField:update`
   - `threadFieldSchema:create`, `threadFieldSchema:delete`, `threadFieldSchema:edit`, `threadFieldSchema:read`
   - `label:read`
5. Click **Create**
6. **Copy the key immediately** — you cannot see it again after navigating away. Save it as `PLAIN_API_KEY`

### 4.3 Create Thread Fields

The bot stores Discord channel and message IDs on each Plain thread so agents can see where tickets came from.

1. In Plain go to **Settings** → **Thread Fields** → **Add Field**
2. Create these two fields:

**Field 1:** name `Discord Channel ID`, key `discord_channel_id` (must be exact), type `Text`

**Field 2:** name `Discord Message ID`, key `discord_message_id` (must be exact), type `Text`

### 4.4 Create a Label (optional but recommended)

1. Go to **Settings** → **Labels** → **Add Label**
2. Name it `Discord`
3. Copy the label type ID (looks like `lt_01xxx`) — save it as `PLAIN_LABEL_TYPE_ID`

### 4.5 Set up request signing (recommended)

The webhook server verifies that incoming webhooks genuinely came from Plain using an HMAC signature.

1. In Plain go to **Settings** → **Request signing**
2. Copy the signing secret — save it as `PLAIN_WEBHOOK_SECRET`
3. If you skip this, the webhook server still runs but logs `Plain signature verification: DISABLED` and accepts unsigned requests. Setting it is recommended for production.

---

## Part 5 — Set up the GitHub repository

### 5.1 Install Git

Download and install Git from [git-scm.com/downloads](https://git-scm.com/downloads). Verify:

```powershell
git --version
```

### 5.2 Configure Git

```powershell
git config --global user.name "Your Name"
git config --global user.email "your@email.com"
```

### 5.3 Create a GitHub repository

1. Go to [github.com](https://github.com) and sign up or log in
2. Click **+** → **New repository**
3. Name it, set it **Private**, do **not** initialize with a README
4. Copy your repo URL

### 5.4 Push your code

In your project folder:

```powershell
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/yourusername/your-repo.git
git push -u origin main
```

Use a **Personal Access Token** (github.com/settings/tokens, `repo` scope) as the password.

---

## Part 6 — Deploy to Railway

### 6.1 Create a Railway account

Go to [railway.app](https://railway.app) and sign up — **Continue with GitHub** is easiest.

### 6.2 Create a project and connect your repo

1. Click **New Project** → **Deploy from GitHub repo**
2. If prompted, configure the GitHub App and select your repo
3. Railway creates one service and tries to deploy — **click Stop/Cancel** on that deployment; configuration comes first

### 6.3 Add Redis

1. Click **+ New** → **Database** → **Add Redis**
2. Once provisioned, Railway auto-injects `REDIS_URL` into services that reference it

### 6.4 Add PostgreSQL (for analytics)

1. Click **+ New** → **Database** → **PostgreSQL**
2. Once provisioned, note the service name (usually `Postgres`)
3. The `bot` and `api` services will reference its `DATABASE_URL` — see below

### 6.5 Configure Service 1 — bot

This is the service Railway created from your repo in step 6.2.

1. Click the service tile → **Settings** → rename to `bot`
2. **Deploy** → **Custom Start Command**: `python bot.py`
3. **Variables** tab → add the variables (see the env reference below). At minimum:
   `DISCORD_TOKEN`, `BANKR_LLM_KEY`, `BANKR_LLM_MODEL`, `BANKR_LLM_URL`, `DOCS_URL`,
   `PLAIN_API_KEY`, `PLAIN_LABEL_TYPE_ID`, `OLLAMA_CLOUD_KEY`, `OLLAMA_FALLBACK_MODEL`,
   `REDIS_URL`, `DATABASE_URL`, `MOD_ROLE_NAME`
4. For `DATABASE_URL`, use a reference: `${{Postgres.DATABASE_URL}}` (replace `Postgres` with your actual Postgres service name)
5. Do **not** generate a public domain for this service
6. Click **Deploy**

### 6.6 Configure Service 2 — webhook

1. **+ New** → **GitHub Repo** → select your repo
2. **Settings** → rename to `webhook` → **Custom Start Command**: `python webhook_server.py`
3. **Variables**: `DISCORD_TOKEN`, `WEBHOOK_PORT`, `PLAIN_WEBHOOK_SECRET`, `REDIS_URL`
4. **Settings** → **Networking** → **Generate Domain** — copy this URL for Part 7
5. Click **Deploy**

### 6.7 Configure Service 3 — api

1. **+ New** → **GitHub Repo** → select your repo
2. **Settings** → rename to `api` → **Custom Start Command**: `python api_server.py`
3. **Variables**: `BANKR_LLM_KEY`, `BANKR_LLM_MODEL`, `BANKR_LLM_URL`, `DOCS_URL`,
   `OLLAMA_CLOUD_KEY`, `OLLAMA_FALLBACK_MODEL`, `API_SERVER_KEY`, `API_SERVER_PORT`,
   `REDIS_URL`, `DATABASE_URL`, `ADMIN_PASSWORD`, `ADMIN_SESSION_SECRET`
4. For `DATABASE_URL`, use the same `${{Postgres.DATABASE_URL}}` reference
5. **Settings** → **Networking** → **Generate Domain** — this is also where the admin panel lives (`/admin`)
6. Click **Deploy**

### 6.8 Confirm all services are healthy

Check each service's **Logs** tab for a clean startup:

**bot logs:**
```
Logged in as Bankr Support#xxxx
Using model: gemini-3-flash via Bankr LLM Gateway (key set)
Plain integration: ENABLED
Thread map: Redis ✅
LLM router ready — primary: Bankr gateway, fallback: Ollama Cloud (gemma4:31b-cloud)
Postgres connected — conversations table ready, stats logging ENABLED
Docs ready.
```

**webhook logs:**
```
Plain webhook server listening on port 8080
Plain signature verification: ENABLED
Thread map: Redis ✅
```

**api logs:**
```
API server starting — pre-loading docs...
Docs ready.
Admin panel mounted at /admin
Postgres connected — conversations table ready, stats logging ENABLED
API server ready on port 8000
```

---

## Part 7 — Connect Plain webhooks

1. In Plain go to **Settings** → **Webhooks** → **Add webhook target**
2. **Name:** `Discord Reply Bridge`
3. **URL:** `https://<your-webhook-domain>.up.railway.app/plain-webhook`
4. **Events** — select: `thread.chat_sent`, `thread.email_sent`, `thread.thread_status_transitioned`
5. Click **Save** and confirm it shows as **Active**

---

## Part 8 — Access the admin panel

1. Go to `https://<your-api-domain>.up.railway.app/admin/login`
2. Sign in with the `ADMIN_PASSWORD` you set on the `api` service
3. You'll land on **Bot Controls** — override management and the busy-mode toggle
4. The **Stats** tab shows the analytics dashboard (it will be empty until conversations accrue)

Share the admin password with your team via a password manager. The session lasts 12 hours.

---

## Part 9 — Smoke test

**Test 1 — Bot responds.** @mention the bot with a Bankr question. Expect a grounded answer.

**Test 2 — Intent detection.** Post a support-like message in a monitored channel without mentioning the bot. Expect a proactive offer.

**Test 3 — Ticket creation.** Trigger an escalation, reply `yes` when asked. Expect a Plain thread + a Discord ticket thread.

**Test 4 — Two-way relay.** Reply as an agent in Plain. Expect the reply in the Discord ticket thread.

**Test 5 — Thread deletion.** Type `!close` in a ticket thread. Expect a 10-second countdown, then deletion.

**Test 6 — API server.** `curl https://<your-api-domain>.up.railway.app/health` → `{"status":"ok","docs_ready":true}`

**Test 7 — Override.** In the admin panel, create an override with a test keyword. Send a matching message in Discord. Expect the override message instead of a doc answer.

**Test 8 — Busy mode.** Enable busy mode, have a staff-role member post a passive support message. Expect the bot to stay silent. Have a non-staff account do the same — expect a proactive offer.

**Test 9 — Stats.** Visit `/admin/stats`. After some traffic, expect headline numbers, charts, and the topics table to populate.

---

## Updating the bot

```powershell
git add .
git commit -m "describe your change"
git push
```

Railway watches `main` and automatically redeploys all services on every push.

---

## Environment variable reference

| Variable | Required | Service(s) | Default | Description |
|---|---|---|---|---|
| `DISCORD_TOKEN` | Yes | bot, webhook | — | Discord bot token |
| `BANKR_LLM_KEY` | Yes | bot, api | — | Bankr API key with LLM Gateway enabled |
| `BANKR_LLM_MODEL` | | bot, api | `gemini-3-flash` | Bankr model to use |
| `BANKR_LLM_URL` | | bot, api | `https://llm.bankr.bot` | Bankr gateway base URL |
| `OLLAMA_CLOUD_KEY` | | bot, api | — | Ollama Cloud API key (LLM failover). If unset, no fallback |
| `OLLAMA_CLOUD_URL` | | bot, api | `https://ollama.com` | Ollama Cloud base URL |
| `OLLAMA_FALLBACK_MODEL` | | bot, api | `gemma4:31b-cloud` | Fallback model |
| `PLAIN_API_KEY` | Yes | bot | — | Plain machine user API key |
| `PLAIN_LABEL_TYPE_ID` | | bot | blank | Plain label ID for Discord tickets |
| `PLAIN_WEBHOOK_SECRET` | | webhook | blank | Plain HMAC signing secret. If set, webhooks are verified |
| `REDIS_URL` | Yes (prod) | all | — | Redis connection URL (Railway auto-provides) |
| `DATABASE_URL` | | bot, api | — | Postgres URL for analytics. If unset, stats logging is off |
| `STATS_RETENTION_DAYS` | | bot | `90` | Days of stats history kept before pruning |
| `DOCS_URL` | | bot, api | Bankr docs | URL to fetch documentation from |
| `DOCS_REFRESH_HOURS` | | bot, api | `6` | How often to re-index docs |
| `MONITORED_CHANNEL_IDS` | | bot | all | Comma-separated channel IDs to monitor |
| `MOD_ROLE_NAME` | | bot | `Moderator` | Staff role name(s), comma-separated. Used for ticket thread visibility and as the busy-mode default |
| `CONVERSATION_TTL_MINUTES` | | bot | `30` | Inactivity timeout for conversations |
| `REFLAG_COOLDOWN_MINUTES` | | bot | `15` | Cooldown before re-flagging the same user |
| `WEBHOOK_PORT` | | webhook | `8080` | Port for the webhook server |
| `WEBHOOK_SERVER_URL` | | bot | blank | Internal URL of the webhook service (for cross-process `!keep`) |
| `API_SERVER_KEY` | Yes | api | — | Bearer token for the agent API |
| `API_SERVER_PORT` | | api | `8000` | Port for the agent API server |
| `API_RATE_LIMIT_RPM` | | api | `60` | API requests per minute per IP |
| `ADMIN_PASSWORD` | Yes (for panel) | api | — | Password for the admin panel login |
| `ADMIN_SESSION_SECRET` | | api | random | Signing secret for admin session cookies. Set explicitly so sessions survive restarts |

---

## Bot commands

| Command | Effect |
|---|---|
| `!close` | Close the current ticket / end conversation |
| `!done` | Same as `!close` |
| `!keep` | Cancel a pending thread deletion countdown |
| `thanks` / `thank you` | Ends the conversation gracefully |

---

## Permissions summary

**Discord bot permissions:** Read Messages / View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Manage Threads, Read Message History, Add Reactions

**Discord privileged intents:** Message Content Intent, Server Members Intent

**Plain API permissions:** `customer:create` `customer:edit` `customer:read` `thread:create` `thread:read` `thread:reply` `threadField:create` `threadField:update` `threadFieldSchema:*` `label:read`

---

## Troubleshooting

**Bot not responding in Discord**
Check the `bot` service logs. Common causes: wrong/missing `DISCORD_TOKEN`, Message Content Intent not enabled, bot not invited with correct permissions.

**`401` errors in bot logs**
`BANKR_LLM_KEY` is wrong, missing, or LLM Gateway is not enabled on the key.

**`402` errors in bot logs**
Bankr LLM credit balance is $0. Top up at bankr.bot/llm.

**`500` / `auth_error` from the Bankr gateway**
A Bankr-side upstream issue. If `OLLAMA_CLOUD_KEY` is set, the bot automatically falls back to Ollama Cloud and keeps working — check logs for `Fallback (Ollama Cloud ...) answered`. If failover is not configured, set `OLLAMA_CLOUD_KEY` to add resilience.

**Bot replies show a `[TOPIC: ...]` tag**
The topic tag the LLM appends for analytics should be stripped before the user sees it. If it leaks through, the model is formatting the tag unexpectedly — report it; it's a quick fix to the strip regex.

**`Postgres connected` not appearing in logs**
`DATABASE_URL` is unset or the reference didn't resolve. The bot still runs fine (stats just don't record). Check the variable is set on the `bot` and `api` services and that the `${{Postgres.DATABASE_URL}}` reference matches the actual Postgres service name.

**Stats dashboard is empty**
Expected if Postgres was added recently — there is no historical backfill, data accrues from when logging went live. If it stays empty despite traffic, check the `bot` logs for `log_conversation failed` lines.

**Plain tickets not creating**
Check `PLAIN_API_KEY` is set with the required permissions, and that the two Thread Fields (`discord_channel_id`, `discord_message_id`) exist in Plain.

**Agent replies not reaching Discord**
The Plain → Discord relay requires Redis. Check `REDIS_URL` is set on both `bot` and `webhook`, both show `Thread map: Redis ✅`, and the Plain webhook URL matches the `webhook` service domain.

**Webhook returns 403**
`PLAIN_WEBHOOK_SECRET` is set but the incoming request isn't signed correctly — confirm the secret matches the one in Plain's Settings → Request signing.

**Admin panel won't accept the password**
Confirm `ADMIN_PASSWORD` is set on the `api` service. If logins drop after every redeploy, set `ADMIN_SESSION_SECRET` explicitly so it doesn't regenerate on restart.

**`Thread map: in-memory only` warning**
`REDIS_URL` is not set on that service.

**Docs not loading**
`DOCS_URL` is unreachable or returns non-200. Check the URL is correct and public.
