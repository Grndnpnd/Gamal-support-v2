"""Copies this system's data from the old host to the new one: every Postgres table and every Redis key.
Run once, at the switch, as a one-off task on the new host (python entrypoint.py migrate_data.py), with the old
host's services already stopped so nothing changes underneath it.

  SOURCE_DATABASE_URL   the old Postgres            DATABASE_URL   the new one (set by the entrypoint)
  SOURCE_REDIS_URL      the old Redis               REDIS_URL      the new one
  MIGRATE_MODE          check (default): count both sides and change nothing
                        copy: copy, then count both sides again
  MIGRATE_REPLACE       1 to empty a destination table that already has rows, or overwrite existing keys.
                        Without it a non-empty destination table stops the copy: nothing is ever merged.

It prints table names, key counts and row counts. It never prints a row, a key's value or an address.
Redis is copied by type (string, hash, list, set, sorted set) with each key's time to live, not with DUMP/RESTORE,
so the two sides do not have to be the same Redis version. A key of another type is counted and reported, not
copied. The exit code is 0 only when both sides match."""
import asyncio
import os
import sys

MODE = os.getenv("MIGRATE_MODE", "check")
REPLACE = os.getenv("MIGRATE_REPLACE", "") == "1"
BATCH = 1000


def say(*a):
    print("migrate:", *a, flush=True)


async def tables(conn):
    rows = await conn.fetch("select table_name from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE' order by table_name")
    return [r["table_name"] for r in rows]


async def columns(conn, table):
    rows = await conn.fetch("select column_name from information_schema.columns where table_schema = 'public' and table_name = $1 order by ordinal_position", table)
    return [r["column_name"] for r in rows]


def ident(name):
    return '"' + name.replace('"', '""') + '"'


async def postgres():
    import asyncpg
    src_url, dst_url = os.getenv("SOURCE_DATABASE_URL", ""), os.getenv("DATABASE_URL", "")
    if not src_url or not dst_url:
        say("postgres: skipped (SOURCE_DATABASE_URL or DATABASE_URL is not set)"); return True
    src = await asyncpg.connect(src_url, timeout=30); dst = await asyncpg.connect(dst_url, timeout=30)
    ok = True
    try:
        if MODE == "copy":
            # The destination gets the app's own tables first, exactly as the app would make them at its first start.
            import db
            await dst.execute(db._SCHEMA); await dst.execute(db._SIGNIN_SCHEMA)
        have = set(await tables(dst))
        for t in await tables(src):
            n_src = await src.fetchval(f"select count(*) from {ident(t)}")
            if t not in have:
                say(f"postgres: {t}: {n_src} rows on the old side, NO such table on the new side"); ok = ok and n_src == 0; continue
            n_dst = await dst.fetchval(f"select count(*) from {ident(t)}")
            if MODE == "copy":
                if n_dst and not REPLACE:
                    say(f"postgres: {t}: the new side already has {n_dst} rows; not touched (set MIGRATE_REPLACE=1 to empty it first)"); ok = False; continue
                cols = [c for c in await columns(src, t) if c in set(await columns(dst, t))]
                collist = ", ".join(ident(c) for c in cols)
                async with dst.transaction():
                    if n_dst: await dst.execute(f"truncate table {ident(t)}")
                    async with src.transaction():
                        batch = []
                        async for row in src.cursor(f"select {collist} from {ident(t)}", prefetch=BATCH):
                            batch.append(tuple(row))
                            if len(batch) >= BATCH: await dst.copy_records_to_table(t, records=batch, columns=cols); batch = []
                        if batch: await dst.copy_records_to_table(t, records=batch, columns=cols)
                    # Serial columns carry on after the highest copied id, not from 1.
                    for c in cols:
                        seq = await dst.fetchval("select pg_get_serial_sequence($1, $2)", f"public.{t}", c)
                        if seq: await dst.execute(f"select setval($1, coalesce((select max({ident(c)}) from {ident(t)}), 0) + 1, false)", seq)
                n_dst = await dst.fetchval(f"select count(*) from {ident(t)}")
            same = n_src == n_dst
            ok = ok and same
            say(f"postgres: {t}: old {n_src} rows, new {n_dst} rows{'' if same else '   <-- DIFFERENT'}")
    finally:
        await src.close(); await dst.close()
    return ok


async def copy_key(src, dst, key, kind):
    """One key, by type. Returns False for a type this does not copy."""
    if kind == b"string": await dst.set(key, await src.get(key))
    elif kind == b"hash":
        h = await src.hgetall(key)
        if h: await dst.hset(key, mapping=h)
    elif kind == b"list":
        items = await src.lrange(key, 0, -1)
        if items: await dst.rpush(key, *items)
    elif kind == b"set":
        members = await src.smembers(key)
        if members: await dst.sadd(key, *members)
    elif kind == b"zset":
        pairs = await src.zrange(key, 0, -1, withscores=True)
        if pairs: await dst.zadd(key, {m: s for m, s in pairs})
    else: return False
    ttl = await src.pttl(key)
    if ttl and ttl > 0: await dst.pexpire(key, ttl)
    return True


async def redis(src=None, dst=None):
    if src is None:
        import redis.asyncio as aioredis
        src_url, dst_url = os.getenv("SOURCE_REDIS_URL", ""), os.getenv("REDIS_URL", "")
        if not src_url or not dst_url:
            say("redis: skipped (SOURCE_REDIS_URL or REDIS_URL is not set)"); return True
        src = aioredis.from_url(src_url, socket_connect_timeout=30, socket_timeout=60); dst = aioredis.from_url(dst_url, socket_connect_timeout=30, socket_timeout=60)
    n_dst0 = await dst.dbsize()
    counts, skipped, copied = {}, {}, 0
    if MODE == "copy" and n_dst0 and not REPLACE:
        say(f"redis: the new side already has {n_dst0} keys; not touched (set MIGRATE_REPLACE=1 to overwrite keys of the same name)")
        return False
    async for key in src.scan_iter(count=500):
        kind = await src.type(key)
        counts[kind] = counts.get(kind, 0) + 1
        if MODE != "copy": continue
        if await dst.exists(key): await dst.delete(key)
        if await copy_key(src, dst, key, kind): copied += 1
        else: skipped[kind] = skipped.get(kind, 0) + 1
    n_src, n_dst = await src.dbsize(), await dst.dbsize()
    by = ", ".join(f"{k.decode()} {v}" for k, v in sorted(counts.items())) or "none"
    say(f"redis: old {n_src} keys ({by}); new {n_dst} keys" + (f"; copied {copied}" if MODE == "copy" else ""))
    for k, v in skipped.items(): say(f"redis: {v} keys of type {k.decode()} were NOT copied")
    # Keys that expire between the two counts make the old side smaller, never the new side larger than the old was.
    return not skipped and (MODE != "copy" or (n_dst >= copied and copied >= n_src - 5))


async def main():
    if MODE not in ("check", "copy"): sys.exit("migrate: MIGRATE_MODE must be check or copy")
    say(f"mode {MODE}{' (replace)' if REPLACE else ''}")
    ok_pg = await postgres()
    ok_redis = await redis()
    say("done: both sides match" if ok_pg and ok_redis else "done: NOT matching, see above" if MODE == "copy" else "done (check only: differences are expected before a copy)")
    sys.exit(0 if (ok_pg and ok_redis) or MODE == "check" else 1)


if __name__ == "__main__":
    asyncio.run(main())
