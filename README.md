# Bankr Discord Support Bot

A production-ready Discord support bot for the Bankr platform. Uses semantic search over the Bankr documentation to answer user questions, escalates unresolvable issues to [Plain](https://plain.com) support tickets, and maintains a two-way bridge between Discord ticket threads and Plain so agents can reply without users ever leaving Discord.

---

## What it does

- **Answers Bankr questions** — semantic search (ChromaDB + MiniLM) finds relevant doc chunks, Bankr LLM Gateway generates grounded answers
- **Proactive outreach** — detects support intent in channels and offers help without being @mentioned
- **Smart escalation** — offers to open a Plain ticket only when it genuinely can't help, not automatically
- **Discord ticket threads** — creates a private thread on the user's message when a ticket opens; user replies forward to Plain
- **Two-way relay** — agent replies in Plain post back to the Discord thread via webhook
- **Thread cleanup** — 10-second countdown deletion when tickets close, cancellable with `!keep`
- **Multilingual** — English, Simplified Chinese, Korean
- **Agent API** — exposes `/query` and `/query-docs` endpoints so other agents can query the knowledge base

---

## Architecture

```
Discord message
      │
      ├── @mention / reply to bot  →  handle directly
      ├── Active conversation      →  continue conversation
      └── Passive monitoring       →  detect support intent
                                         │
                                         └── score ≥ 2  →  proactive offer
      │
      ▼
SemanticDocsManager   ←  ChromaDB + all-MiniLM-L6-v2
      │
      ▼
Bankr LLM Gateway     ←  gemini-3-flash (OpenAI-compatible)
      │
      ├── [SUGGEST_ESCALATE]  →  "try this, reply if it doesn't work"
      ├── [NEEDS_TICKET]      →  "want me to open a ticket?"
      └── Normal response     →  reply in Discord
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
                        webhook_server.py  ←  Plain webhook POST
                              │
                              ▼
                        Discord ticket thread  (reply posted)
```

---

## File structure

```
├── bot.py                 Main Discord bot
├── webhook_server.py      Plain webhook receiver → Discord relay
├── api_server.py          Agent API (query-docs, query-llm, query)
├── plain_client.py        Plain GraphQL API client
├── shared.py              Shared SemanticDocsManager + LLM client
├── redis_map.py           Shared Plain↔Discord thread map (Redis/memory)
├── requirements.txt       Python dependencies
├── Procfile               Railway service definitions
├── .env.example           Environment variable template
└── SKILL.md               OpenClaw skill for the agent API
```

---

## Prerequisites

Before you start you will need accounts and credentials for:

| Service | What you need | Where to get it |
|---|---|---|
| Discord | Bot token + app | discord.com/developers |
| Bankr | LLM Gateway API key + credits | bankr.bot/api |
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
   - ✅ **Message Content Intent**
   - ✅ **Server Members Intent**
5. Click **Save Changes**

### 1.3 Set bot permissions

1. In the left sidebar click **OAuth2** → **URL Generator**
2. Under **Scopes** check: `bot` and `applications.commands`
3. Under **Bot Permissions** check:
   - ✅ Read Messages / View Channels
   - ✅ Send Messages
   - ✅ Send Messages in Threads
   - ✅ Create Public Threads
   - ✅ Manage Threads
   - ✅ Read Message History
   - ✅ Add Reactions
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

## Part 3 — Plain setup

### 3.1 Create a Machine User

1. Go to your Plain workspace → **Settings** → **Machine Users**
2. Click **Add Machine User**
3. **Name:** `Discord Bot` (internal label)
4. **Public name:** `Bankr Support` (shown to customers)
5. Click **Save**

### 3.2 Create an API key

1. Click into the machine user you just created
2. Click **Add API Key**
3. Name it `Discord Bot Key`
4. Select these permissions:
   - ✅ `customer:create`
   - ✅ `customer:edit`
   - ✅ `customer:read`
   - ✅ `thread:create`
   - ✅ `thread:read`
   - ✅ `thread:reply`
   - ✅ `threadField:create`
   - ✅ `threadField:update`
   - ✅ `threadFieldSchema:create`
   - ✅ `threadFieldSchema:delete`
   - ✅ `threadFieldSchema:edit`
   - ✅ `threadFieldSchema:read`
   - ✅ `label:read`
5. Click **Create**
6. **Copy the key immediately** — you cannot see it again after navigating away. Save it as `PLAIN_API_KEY`

### 3.3 Create Thread Fields

The bot stores Discord channel and message IDs on each Plain thread so agents can see where tickets came from.

1. In Plain go to **Settings** → **Thread Fields** → **Add Field**
2. Create these two fields:

**Field 1:**
- Field name: `Discord Channel ID`
- Key: `discord_channel_id` ← must be exact
- Type: `Text`
- Save

**Field 2:**
- Field name: `Discord Message ID`
- Key: `discord_message_id` ← must be exact
- Type: `Text`
- Save

### 3.4 Create a Label (optional but recommended)

Labels let your team filter Discord tickets in Plain.

1. Go to **Settings** → **Labels** → **Add Label**
2. Name it `Discord`
3. After saving, copy the label type ID from the URL or detail panel — looks like `lt_01xxx`
4. Save it as `PLAIN_LABEL_TYPE_ID`

---

## Part 4 — Set up the GitHub repository

If you have never used Git before, follow these steps exactly.

### 4.1 Install Git

Download and install Git from [git-scm.com/downloads](https://git-scm.com/downloads). During installation accept all defaults.

Verify it installed:

```powershell
git --version
```

### 4.2 Configure Git with your identity

```powershell
git config --global user.name "Your Name"
git config --global user.email "your@email.com"
```

### 4.3 Create a GitHub account and repository

1. Go to [github.com](https://github.com) and sign up or log in
2. Click the **+** in the top right → **New repository**
3. Name it `bankr-support-bot`
4. Set it to **Private**
5. Do **not** check "Initialize with README" — you already have files
6. Click **Create repository**
7. GitHub will show you a page with setup commands — copy your repo URL, it looks like `https://github.com/yourusername/bankr-support-bot.git`

### 4.4 Initialize and push your code

Open a terminal in your project folder (the folder containing `bot.py`) and run these commands one at a time:

```powershell
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/yourusername/bankr-support-bot.git
git push -u origin main
```

When it asks for your GitHub credentials, use your GitHub username and a **Personal Access Token** — not your password. Generate one at [github.com/settings/tokens](https://github.com/settings/tokens) → **Generate new token (classic)** → check `repo` scope → copy the token and use it as your password.

### 4.5 Verify

Go to `https://github.com/yourusername/bankr-support-bot` in your browser. You should see all your files listed.

---

## Part 5 — Deploy to Railway

### 5.1 Create a Railway account

Go to [railway.app](https://railway.app) and sign up. The easiest option is **Continue with GitHub** — this lets Railway access your repos directly without needing to enter credentials manually.

### 5.2 Create a new project and connect your repo

1. Click **New Project**
2. Click **Deploy from GitHub repo**
3. If prompted, click **Configure GitHub App** — this opens a GitHub permissions page. Click **Only select repositories**, choose `bankr-support-bot`, and click **Save**. You'll be sent back to Railway.
4. Your repo will now appear in the list — click it
5. Railway creates one service from your repo and immediately tries to deploy it. **Click the red Stop/Cancel button** on the deployment — you need to finish configuration before it runs

### 5.3 Add Redis

Add the database before touching any service settings so the URL is ready to copy.

1. In your Railway project click **+ New** (top right of the project canvas)
2. Click **Database** → **Add Redis**
3. Railway provisions Redis in about 10 seconds — you'll see it appear as a tile on the canvas
4. Click into the Redis tile → click the **Variables** tab
5. Find `REDIS_URL` in the list and click the copy icon next to it — keep this somewhere handy, you'll paste it into all three services

### 5.4 Configure Service 1 — bot

This is the service Railway already created from your repo in step 5.2.

1. Click the service tile on the canvas
2. Click the **Settings** tab
3. Under **Service Name** change it to `bot`
4. Scroll down to the **Deploy** section → find **Custom Start Command** → click **Add start command** and enter:
   ```
   python bot.py
   ```
5. Scroll down to **Source** and confirm it shows your GitHub repo connected to the `main` branch
6. Click the **Variables** tab → click **New Variable** and add each of these one at a time:

```
DISCORD_TOKEN            = (your Discord bot token from Part 1)
BANKR_LLM_KEY            = (your bk_... key from Part 2)
BANKR_LLM_MODEL          = gemini-3-flash
BANKR_LLM_URL            = https://llm.bankr.bot
DOCS_URL                 = https://docs.bankr.bot/llms-full.txt
DOCS_REFRESH_HOURS       = 6
PLAIN_API_KEY            = (your Plain API key from Part 3)
PLAIN_LABEL_TYPE_ID      = (your lt_... label ID, or leave blank)
MONITORED_CHANNEL_IDS    = (comma-separated Discord channel IDs, or leave blank for all channels)
CONVERSATION_TTL_MINUTES = 30
REFLAG_COOLDOWN_MINUTES  = 15
REDIS_URL                = (paste the Redis URL you copied in step 5.3)
```

7. Do **not** click Generate Domain for this service — it connects outbound to Discord and doesn't need a public URL
8. Click **Deploy** — watch the logs tab to confirm it starts cleanly (see expected logs in step 5.7)

### 5.5 Configure Service 2 — webhook

1. Click **+ New** on the project canvas → **GitHub Repo**
2. Select your `bankr-support-bot` repo from the list
3. Railway creates a new service tile — click into it
4. Click **Settings** → change the service name to `webhook`
5. Scroll to **Deploy** → **Custom Start Command** → enter:
   ```
   python webhook_server.py
   ```
6. Scroll to **Source** and confirm the repo and `main` branch are connected
7. Click the **Variables** tab → add:

```
DISCORD_TOKEN        = (same Discord bot token)
WEBHOOK_PORT         = 8080
PLAIN_WEBHOOK_SECRET = (leave blank)
REDIS_URL            = (same Redis URL)
```

8. Click the **Settings** tab → scroll to **Networking** → click **Generate Domain**
9. Railway gives you a public URL like `webhook-production-xxxx.up.railway.app` — **copy this**, you need it in Part 6
10. Click **Deploy**

### 5.6 Configure Service 3 — api

1. Click **+ New** on the project canvas → **GitHub Repo**
2. Select your `bankr-support-bot` repo again
3. Click into the new service tile
4. Click **Settings** → rename to `api`
5. Scroll to **Deploy** → **Custom Start Command** → enter:
   ```
   python api_server.py
   ```
6. Scroll to **Source** and confirm repo and `main` branch
7. Click **Variables** tab → add:

```
BANKR_LLM_KEY      = (same bk_... key)
BANKR_LLM_MODEL    = gemini-3-flash
BANKR_LLM_URL      = https://llm.bankr.bot
DOCS_URL           = https://docs.bankr.bot/llms-full.txt
DOCS_REFRESH_HOURS = 6
API_SERVER_KEY     = (generate one by running this locally:  python -c "import secrets; print(secrets.token_hex(32))")
API_SERVER_PORT    = 8000
REDIS_URL          = (same Redis URL)
```

8. If you want external agents to reach the API: **Settings** → **Networking** → **Generate Domain**
9. Click **Deploy**

### 5.7 Confirm all three services are healthy

Each service has a **Deployments** tab and a **Logs** tab. Click into each one and confirm the logs show a clean startup — this is what healthy looks like:

**bot logs:**
```
Logged in as Bankr Support#xxxx
Using model: gemini-3-flash via Bankr LLM Gateway (key set)
Plain integration: ENABLED
Thread map: Redis ✅
Fetching docs from https://docs.bankr.bot/llms-full.txt
Docs ready.
```

**webhook logs:**
```
Plain webhook server listening on port 8080
Thread map: Redis ✅
```

**api logs:**
```
API server starting — pre-loading docs...
Docs ready.
API server ready on port 8000
```

**If the deployment fails** — click the failed deployment → **View Logs** — the error will be in the last few lines. Common causes:
- `ModuleNotFoundError` — a package is missing from `requirements.txt`
- `KeyError` or `ValueError` on startup — a required environment variable is missing
- `Thread map: in-memory only` warning — `REDIS_URL` is not set on that service

**If it deploys but immediately crashes** — check the Variables tab and make sure every required variable has a value, not a blank.

---

## Part 6 — Connect Plain webhooks

Plain needs to know where to send agent reply notifications.

1. In Plain go to **Settings** → **Webhooks** → **Add webhook target**
2. **Name:** `Discord Reply Bridge`
3. **URL:** `https://webhook-production-xxxx.up.railway.app/plain-webhook`
   Replace `webhook-production-xxxx` with the actual domain from Step 5.5
4. **Events** — select these three:
   - ✅ `thread.chat_sent`
   - ✅ `thread.email_sent`
   - ✅ `thread.thread_status_transitioned`
5. Click **Save** and make sure it shows as **Active**

---

## Part 7 — Smoke test

Run through these tests in order to confirm everything is working end to end.

**Test 1 — Bot responds**

Go to your Discord server and @mention the bot with a Bankr question:
```
@Bankr Support how do I set up a DCA?
```
Expected: bot replies with a grounded answer within a few seconds and shows the `!close` footer on the first message.

**Test 2 — Intent detection**

Post a message in a monitored channel without mentioning the bot:
```
my wallet balance isn't showing up correctly
```
Expected: bot proactively offers help.

**Test 3 — Ticket creation**

Trigger an escalation by describing something the bot can't diagnose. Wait for the bot to ask if you want a ticket, reply `yes`.
Expected: a Plain thread appears in your Plain workspace and a Discord ticket thread opens on your message.

**Test 4 — Two-way relay**

In Plain, open the ticket thread that was just created and reply as an agent.
Expected: the reply appears in the Discord ticket thread within a second or two. This test confirms Redis is working correctly between the `bot` and `webhook` services.

**Test 5 — Thread deletion**

In the Discord ticket thread type `!close`.
Expected: confirmation message, then `🗑️ This thread will be deleted in 10 seconds. Reply !keep to cancel.`, then thread deleted after 10 seconds.

**Test 6 — API server**

```bash
curl https://your-api-domain.up.railway.app/health
```
Expected: `{"status":"ok","docs_ready":true}`

---

## Updating the bot

When you make code changes locally and want to deploy:

```powershell
git add .
git commit -m "describe your change"
git push
```

Railway watches your repo and automatically redeploys all three services on every push to `main`.

---

## Environment variable reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | Discord bot token from Developer Portal |
| `BANKR_LLM_KEY` | ✅ | — | Bankr API key with LLM Gateway enabled |
| `BANKR_LLM_MODEL` | | `gemini-3-flash` | Model to use ($0.50/M in, $3.00/M out) |
| `BANKR_LLM_URL` | | `https://llm.bankr.bot` | Bankr gateway base URL |
| `PLAIN_API_KEY` | ✅ | — | Plain machine user API key |
| `PLAIN_LABEL_TYPE_ID` | | blank | Plain label ID for Discord tickets |
| `REDIS_URL` | ✅ prod | — | Redis connection URL (auto-set by Railway) |
| `DOCS_URL` | | Bankr docs | URL to fetch documentation from |
| `DOCS_REFRESH_HOURS` | | `6` | How often to re-index docs |
| `MONITORED_CHANNEL_IDS` | | all | Comma-separated Discord channel IDs to monitor |
| `CONVERSATION_TTL_MINUTES` | | `30` | Inactivity timeout for conversations |
| `REFLAG_COOLDOWN_MINUTES` | | `15` | Cooldown before re-flagging same user |
| `WEBHOOK_PORT` | | `8080` | Port for webhook server |
| `PLAIN_WEBHOOK_SECRET` | | blank | Plain webhook signature secret |
| `API_SERVER_KEY` | ✅ | — | Bearer token for agent API |
| `API_SERVER_PORT` | | `8000` | Port for agent API server |
| `API_RATE_LIMIT_RPM` | | `60` | API requests per minute per IP |

---

## Bot commands

Users can type these in any conversation or ticket thread:

| Command | Effect |
|---|---|
| `!close` | Close the current ticket / end conversation |
| `!done` | Same as `!close` |
| `!keep` | Cancel a pending thread deletion countdown |
| `thanks` / `thank you` | Ends the conversation gracefully |

---

## Permissions summary

**Discord bot permissions required:**
- Read Messages / View Channels
- Send Messages
- Send Messages in Threads
- Create Public Threads
- Manage Threads
- Read Message History
- Add Reactions

**Discord privileged intents required:**
- Message Content Intent
- Server Members Intent

**Plain API permissions required:**
- `customer:create` `customer:edit` `customer:read`
- `thread:create` `thread:read` `thread:reply`
- `threadField:create` `threadField:update`
- `threadFieldSchema:create` `threadFieldSchema:delete` `threadFieldSchema:edit` `threadFieldSchema:read`
- `label:read`

---

## Troubleshooting

**Bot not responding in Discord**
Check the `bot` service logs in Railway. Most common causes:
- `DISCORD_TOKEN` is wrong or missing
- Message Content Intent is not enabled in the Discord Developer Portal
- Bot was not invited to the server with the correct permissions

**`401` errors in bot logs**
`BANKR_LLM_KEY` is wrong, missing, or LLM Gateway is not enabled on the key. Go to bankr.bot/api and verify.

**`402` errors in bot logs**
LLM credit balance is $0. Top up at bankr.bot/llm.

**Plain tickets not creating**
Check `PLAIN_API_KEY` is set and has all the required permissions listed above. Also verify the two Thread Fields (`discord_channel_id` and `discord_message_id`) exist in Plain Settings → Thread Fields.

**Agent replies not reaching Discord**
The Plain → Discord relay requires Redis to be working. Check:
1. `REDIS_URL` is set on both the `bot` and `webhook` services
2. Both services show `Thread map: Redis ✅` in their startup logs
3. The Plain webhook URL in Settings → Webhooks matches the `webhook` service domain exactly

**`Thread map: in-memory only` warning**
`REDIS_URL` is not set on that service. Go to Railway → that service → Variables and add it.

**Docs not loading**
The `DOCS_URL` is unreachable or returns a non-200 response. The bot will log `Failed to fetch docs: HTTP xxx`. Check the URL is correct and publicly accessible.

**Rate limit on API server**
Default is 60 requests/minute per IP. Increase by setting `API_RATE_LIMIT_RPM` to a higher value on the `api` service.
