"""Starts one of the three processes inside the container:  python entrypoint.py bot.py | api_server.py | webhook_server.py

On Railway every variable was set on the service. On AWS they arrive in two forms, and this turns both back into
the plain environment the code has always read, then becomes the process itself (same PID, signals pass through):

  APP_ENV_JSON   one secret holding every app variable as a JSON object ({"DISCORD_TOKEN": "...", ...}). A variable
                 already set in the environment wins, so a single one can be overridden on the task.
  DB_HOST, DB_NAME, DB_USER, DB_PASSWORD   become DATABASE_URL (TLS required), unless DATABASE_URL is set.
  REDIS_HOST                               becomes REDIS_URL (TLS), unless REDIS_URL is set.

Nothing is printed but the names of what was set. With none of these present it changes nothing, so the same image
runs under docker compose or anywhere else that sets the variables directly."""
import json
import os
import sys
from urllib.parse import quote

ALLOWED = {"bot.py", "api_server.py", "webhook_server.py", "migrate_data.py"}


def main() -> None:
    script = sys.argv[1] if len(sys.argv) > 1 else "bot.py"
    if script not in ALLOWED:
        sys.exit(f"entrypoint: {script!r} is not one of {sorted(ALLOWED)}")

    added = []
    raw = os.environ.pop("APP_ENV_JSON", "")
    if raw.strip():
        try:
            values = json.loads(raw)
        except ValueError:
            sys.exit("entrypoint: APP_ENV_JSON is not valid JSON")
        if not isinstance(values, dict):
            sys.exit("entrypoint: APP_ENV_JSON must be a JSON object of NAME -> value")
        for name, value in values.items():
            if value is None or name in os.environ:
                continue
            os.environ[name] = value if isinstance(value, str) else json.dumps(value)
            added.append(name)

    if not os.environ.get("DATABASE_URL") and os.environ.get("DB_HOST"):
        user = quote(os.environ.get("DB_USER", ""), safe="")
        password = quote(os.environ.get("DB_PASSWORD", ""), safe="")
        os.environ["DATABASE_URL"] = f"postgresql://{user}:{password}@{os.environ['DB_HOST']}:{os.environ.get('DB_PORT', '5432')}/{os.environ.get('DB_NAME', 'support')}?sslmode=require"
        added.append("DATABASE_URL")
    if not os.environ.get("REDIS_URL") and os.environ.get("REDIS_HOST"):
        os.environ["REDIS_URL"] = f"rediss://{os.environ['REDIS_HOST']}:{os.environ.get('REDIS_PORT', '6379')}/0"
        added.append("REDIS_URL")
    for name in ("DB_PASSWORD", "DB_USER"):
        os.environ.pop(name, None)

    print(f"entrypoint: starting {script} with {len(added)} variables set from the secret store: {', '.join(sorted(added)) or 'none'}", flush=True)
    os.execv(sys.executable, [sys.executable, script, *sys.argv[2:]])


if __name__ == "__main__":
    main()
