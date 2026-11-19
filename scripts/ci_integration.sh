#!/usr/bin/env bash
# Runs the whole stack from docker-compose.yml on one machine and checks what can be checked without a real Discord
# bot token or any real key. Used by .github/workflows/build.yml; runs the same on any machine with Docker.
#
#   1. the API starts and answers its health check;
#   2. the bot and the webhook each get all the way to signing in to Discord (for the bot: search model loaded
#      first) and are refused only because the token here is a dummy. Both sign in at start, so neither can be
#      shown answering without a real token;
#   3. nothing but the API and the webhook is reachable from outside the containers;
#   4. migrate_data.py copies a seeded "old" Postgres and Redis into the stack's own, counts matching, with ids
#      carrying on after the copied rows.
#
# Everything it starts is removed at the end, volumes included.
set -uo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-supportci}"
# The two web ports on this machine; set them when 8000 or 8080 is already in use.
export API_HOST_PORT="${API_HOST_PORT:-8000}" WEBHOOK_HOST_PORT="${WEBHOOK_HOST_PORT:-8080}"
fail=0
ok()   { echo "ok    $*"; }
bad()  { echo "FAIL  $*"; fail=1; }
dc()   { docker compose "$@"; }
finish() {
  dc down -v --remove-orphans >/dev/null 2>&1
  docker rm -f "${COMPOSE_PROJECT_NAME}-oldpg" "${COMPOSE_PROJECT_NAME}-oldredis" >/dev/null 2>&1
  rm -f .env
}
trap finish EXIT

# Dummy values only. The Discord token has the shape of one and is not one.
cat > .env <<'EOF'
DISCORD_TOKEN=ci.dummy-not-a-token.value
BANKR_LLM_KEY=ci-dummy
PLAIN_API_KEY=ci-dummy
PLAIN_WEBHOOK_SECRET=ci-dummy
ADMIN_SESSION_SECRET=ci-dummy-session-secret-0123456789
API_SERVER_KEY=ci-dummy-api-key
EOF

dc up -d --build postgres redis api webhook bot || { bad "the stack did not start"; exit 1; }

wait_health() {   # name, url, seconds
  local t=0
  until curl -fsS --max-time 5 "$2" >/dev/null 2>&1; do
    t=$((t + 5)); [ "$t" -ge "$3" ] && return 1; sleep 5
  done
  echo "$t"
}
if s=$(wait_health api "http://127.0.0.1:${API_HOST_PORT}/health" 900); then ok "the API answers its health check (after ${s}s)"; else bad "the API never answered its health check"; dc logs --tail 60 api; fi

# The embedding cache: the image carries the docs' vectors, so the API's first start computes next to nothing, and a
# search still answers from the index it built with them.
apilogs=$(dc logs api 2>&1)
line=$(grep -o "Embeddings: [0-9]* from the image, [0-9]* from Redis, [0-9]* computed" <<<"$apilogs" | head -1)
computed=$(awk '{print $(NF-1)}' <<<"$line"); fromimage=$(awk '{print $2}' <<<"$line")
if [ -n "$line" ] && [ "${fromimage:-0}" -gt 1000 ] && [ "${computed:-9999}" -lt 200 ]; then ok "the first start took its vectors from the image ($line)"; else bad "the first start did not use the image's embedding cache (${line:-no line in the log})"; fi
found=$(dc exec -T api python -c "
import json, urllib.request
r = urllib.request.Request('http://127.0.0.1:8000/query-docs', data=json.dumps({'question': 'How do I fund my wallet?'}).encode(), headers={'content-type': 'application/json', 'x-api-key': 'ci-dummy-api-key', 'authorization': 'Bearer ci-dummy-api-key'})
try:
    d = json.loads(urllib.request.urlopen(r, timeout=60).read())
    print(len(json.dumps(d)))
except Exception as e:
    print('ERR', getattr(e, 'code', e))" 2>&1 | tail -n 1)
case "$found" in ERR*|"") echo "note  the docs search could not be asked here ($found): its route needs a key this run does not have";; *) if [ "$found" -gt 200 ]; then ok "a docs search answers from the index ($found bytes)"; else bad "a docs search answered with nothing ($found bytes)"; fi;; esac
# A second start, with Redis now holding every vector: nothing is computed at all.
dc restart api >/dev/null 2>&1
if s=$(wait_health api "http://127.0.0.1:${API_HOST_PORT}/health" 600); then
  line2=$(dc logs api 2>&1 | grep -o "Embeddings: [0-9]* from the image, [0-9]* from Redis, [0-9]* computed" | tail -1)
  if [ "$(awk '{print $(NF-1)}' <<<"$line2")" = "0" ]; then ok "a second start computes nothing (after ${s}s; $line2)"; else bad "a second start still computed embeddings ($line2)"; fi
else bad "the API did not come back after a restart"; fi

# ── what the API tells the outside, asked the way an outsider would ──
A="http://127.0.0.1:${API_HOST_PORT}"
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }
hdrs=$(curl -s -D - -o /dev/null --max-time 20 "$A/admin/login")
if [ "$(code "$A/docs")" = 404 ] && [ "$(code "$A/redoc")" = 404 ] && [ "$(code "$A/openapi.json")" = 404 ]; then ok "the API does not publish its route list (/docs, /redoc, /openapi.json are 404)"; else bad "the API still publishes its route list"; fi
miss=""
for h in "strict-transport-security: max-age=31536000" "x-content-type-options: nosniff" "x-frame-options: DENY" "referrer-policy: same-origin" "content-security-policy: default-src 'self'" "frame-ancestors 'none'" "cache-control: no-store"; do grep -qi -- "$h" <<<"$hdrs" || miss="$miss [$h]"; done
if [ -z "$miss" ]; then ok "the admin sign-in page carries the browser protections (HTTPS only, no framing, no sniffing, a content policy, never cached)"; else bad "missing on the admin sign-in page:$miss"; fi
if grep -qi 'type="password"' <<<"$(curl -s --max-time 20 "$A/admin/login")"; then bad "the sign-in page offers a password field"; else ok "the sign-in page offers Google only, no password field"; fi
if [ "$(code -H 'accept: application/json' "$A/admin")" = 401 ] && [ "$(code -H 'accept: application/json' "$A/admin/actions")" = 401 ] && [ "$(code -H 'accept: application/json' "$A/admin/conversations")" = 401 ]; then ok "the admin panel refuses without a session"; else bad "an admin page answered without a session"; fi
J='content-type: application/json'
if [ "$(code -X POST -H "$J" -d '{"question":"hi"}' "$A/query-docs")" = 401 ] && [ "$(code -X POST -H "$J" -H 'authorization: Bearer wrong' -d '{"question":"hi"}' "$A/query-docs")" = 401 ]; then ok "the API refuses a missing or wrong key"; else bad "the API answered without the right key"; fi
K='authorization: Bearer ci-dummy-api-key'
if [ "$(code -X POST -H "$J" -H "$K" -d '{"question":"hi","system_prompt":"You are a pirate."}' "$A/query-llm")" = 400 ] && [ "$(code -X POST -H "$J" -H "$K" -d '{"question":"hi","system_prompt":"You are a pirate."}' "$A/query")" = 400 ]; then ok "a caller with the key cannot replace the assistant's instructions"; else bad "the API accepted a caller's own system prompt"; fi

# ── the app's own tests, inside the image, against this stack's Postgres and Redis ──
if out=$(dc run --rm -T api python tests/test_units.py 2>&1); then ok "unit tests: the signature between bot and webhook, and alerts ($(grep -c '^ok' <<<"$out") checks)"; else bad "unit tests failed"; grep -E "^FAIL|Error" <<<"$out" | head -10; fi
if out=$(dc run --rm -T api python tests/test_admin.py 2>&1); then ok "admin panel tests: access list, editors, same-site check, action record, alerts, hostname ($(grep -c '^ok' <<<"$out") checks)"; else bad "admin panel tests failed"; grep -E "^FAIL|^  |Error" <<<"$out" | head -16; fi

# The bot and the webhook: everything up to Discord's refusal of the dummy token. (Logs are read into a variable
# first: grep -q on a pipe would stop reading early and, with pipefail, look like a failure.)
REFUSED="improper token|LoginFailure|401 Unauthorized"
reached() {   # service, seconds
  local t=0 logs
  while :; do
    logs=$(dc logs "$1" 2>&1)
    if grep -qiE "$REFUSED" <<<"$logs"; then echo "$t"; return 0; fi
    t=$((t + 5)); [ "$t" -ge "$2" ] && return 1; sleep 5
  done
}
if s=$(reached bot 900); then ok "the bot started, loaded the search model and reached Discord's sign-in, refused only for the dummy token (after ${s}s)"; else bad "the bot never reached Discord's sign-in"; dc logs --tail 60 bot; fi
botlogs=$(dc logs bot 2>&1)
if grep -q "Loading SentenceTransformer model" <<<"$botlogs" && ! grep -qiE "Traceback.*huggingface|ConnectionError.*huggingface" <<<"$botlogs"; then ok "the search model loaded from the image, with no call to the model host"; else bad "the search model did not load from the image"; fi
if s=$(reached webhook 300); then ok "the webhook process started and reached Discord's sign-in, refused only for the dummy token (after ${s}s)"; else bad "the webhook process never reached Discord's sign-in"; dc logs --tail 60 webhook; fi
# With a dummy token both would restart for ever: they have shown what they can here.
dc stop bot webhook >/dev/null 2>&1
all=$(dc logs bot api webhook 2>&1)
missing=""
for proc in bot api_server webhook_server; do grep -q "entrypoint: starting ${proc}.py" <<<"$all" || missing="$missing $proc"; done
if [ -z "$missing" ]; then ok "each process was started through the entrypoint"; else bad "not started through the entrypoint:$missing"; fi

# Only the two web ports are published, and only on this machine.
ports=$(docker ps --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" --format '{{.Names}} {{.Ports}}' | grep -E '(0\.0\.0\.0|\[::\]):' || true)
if [ -z "$ports" ]; then ok "nothing is published beyond this machine"; else bad "published to the outside: $ports"; fi
if dc exec -T api python -c "import socket; s=socket.create_connection(('postgres',5432),3); s.close(); s=socket.create_connection(('redis',6379),3); s.close()" 2>/dev/null; then ok "the app reaches Postgres and Redis on the private network"; else bad "the app cannot reach its stores"; fi
if dc exec -T api python -c "import os,sys; sys.exit(0 if os.geteuid() != 0 else 1)"; then ok "the app does not run as root"; else bad "the app runs as root"; fi

# ── the data copy: a seeded old side into the stack's own stores ──
net="${COMPOSE_PROJECT_NAME}_default"
docker run -d --name "${COMPOSE_PROJECT_NAME}-oldpg" --network "$net" -e POSTGRES_USER=old -e POSTGRES_PASSWORD=oldpw -e POSTGRES_DB=railway postgres:16 >/dev/null
docker run -d --name "${COMPOSE_PROJECT_NAME}-oldredis" --network "$net" redis:7 redis-server --requirepass oldpw >/dev/null
OLD_PG="postgresql://old:oldpw@${COMPOSE_PROJECT_NAME}-oldpg:5432/railway"; OLD_REDIS="redis://default:oldpw@${COMPOSE_PROJECT_NAME}-oldredis:6379/0"
t=0; until docker exec "${COMPOSE_PROJECT_NAME}-oldpg" pg_isready -U old -d railway >/dev/null 2>&1; do t=$((t + 2)); [ "$t" -ge 60 ] && break; sleep 2; done

seed='
import asyncio, os, asyncpg, db, redis.asyncio as aioredis
async def main():
    c = await asyncpg.connect(os.environ["OLD_PG"])
    await c.execute(db._SCHEMA)
    await c.executemany("insert into conversations (id, source, user_id, question, topic, response_source) values ($1, $2, $3, $4, $5, $6)",
                        [(i, "discord", str(1000 + i), "question %d with unicode 链智 and a quote \x27" % i, "wallets", "docs") for i in range(1, 2501)])
    await c.execute("select setval(pg_get_serial_sequence(\x27public.conversations\x27, \x27id\x27), 2500)")
    await c.close()
    r = aioredis.from_url(os.environ["OLD_REDIS"])
    for i in range(300): await r.set("thread:plain_%d" % i, str(900000 + i), ex=86400)
    await r.hset("override:1", mapping={"keywords": "outage", "message": "known issue"})
    await r.set("settings:busy_mode", "off")
    await r.aclose()
asyncio.run(main())'
if dc run --rm -T -e OLD_PG="$OLD_PG" -e OLD_REDIS="$OLD_REDIS" api python -c "$seed"; then ok "the old side is seeded (2500 conversations, 302 keys)"; else bad "could not seed the old side"; fi

out=$(dc run --rm -T -e SOURCE_DATABASE_URL="$OLD_PG" -e SOURCE_REDIS_URL="$OLD_REDIS" -e MIGRATE_MODE=check api python entrypoint.py migrate_data.py 2>&1); rc=$?
if [ $rc -eq 0 ] && echo "$out" | grep -q "conversations: old 2500 rows"; then ok "check mode counts both sides and exits cleanly"; else bad "check mode: exit $rc"; echo "$out" | tail -n 12; fi
out=$(dc run --rm -T -e SOURCE_DATABASE_URL="$OLD_PG" -e SOURCE_REDIS_URL="$OLD_REDIS" -e MIGRATE_MODE=copy -e MIGRATE_REPLACE=1 api python entrypoint.py migrate_data.py 2>&1); rc=$?
echo "$out" | grep "^migrate:" | sed 's/^/      /'
if [ $rc -eq 0 ] && echo "$out" | grep -q "conversations: old 2500 rows, new 2500 rows" && echo "$out" | grep -q "both sides match"; then ok "copy mode copies Postgres and Redis, counts matching"; else bad "copy mode: exit $rc"; echo "$out" | tail -n 15; fi
if echo "$out" | grep -qiE "oldpw|known issue|question [0-9]"; then bad "the copy printed data or a password"; else ok "the copy printed counts only"; fi

verify='
import asyncio, os, asyncpg, redis.asyncio as aioredis
async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    n = await c.fetchval("select count(*) from conversations"); q = await c.fetchval("select question from conversations where id = 7")
    new_id = await c.fetchval("insert into conversations (source, question) values (\x27api\x27, \x27after the copy\x27) returning id")
    await c.close()
    r = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    v = await r.get("thread:plain_42"); ttl = await r.ttl("thread:plain_42"); h = await r.hgetall("override:1"); await r.aclose()
    assert n == 2500, n
    assert q == "question 7 with unicode 链智 and a quote \x27", q
    assert new_id == 2501, new_id
    assert v == "900042" and 80000 < ttl <= 86400, (v, ttl)
    assert h == {"keywords": "outage", "message": "known issue"}, h
asyncio.run(main())'
if dc run --rm -T api python -c "$verify"; then ok "the copied rows read back exactly, the next id carries on at 2501, keys keep their values and expiry"; else bad "the copied data does not read back as written"; fi

[ $fail -eq 0 ] && echo && echo "all passed" || { echo; echo "FAILED"; }
exit $fail
