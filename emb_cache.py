"""
emb_cache.py
------------
Embeddings by content, so a start computes only what it has not seen.

Indexing the docs means turning ~2,300 chunks of text into vectors. On a full machine that is a minute or two; on a
small container (a quarter of a core) it is a quarter of an hour, at every start and at every docs refresh, for the
bot and the API each. The vector for a chunk depends only on the model and the chunk's text, so it is kept:

  1. in a file built into the image (EMB_CACHE_FILE, written at build by build_emb_cache.py), so the very first
     start on a new host is fast too;
  2. in Redis (when REDIS_URL is set), shared by the bot and the API, so whichever meets a new chunk first computes
     it once for both.

Only chunks found in neither are computed. A cached vector is byte-for-byte what the model produced, so search
results are the same with or without the cache. Nothing here can stop indexing: if the file is missing or Redis
does not answer, the chunks are simply computed as before.
"""
import hashlib
import logging
import os

log = logging.getLogger(__name__)

EMB_CACHE_FILE = os.getenv("EMB_CACHE_FILE", "/opt/models/emb-cache.npz")
REDIS_URL = os.getenv("REDIS_URL", "")
PREFIX = "emb:1:"            # bump the number if the stored form ever changes
TTL_SECONDS = 45 * 86400     # a chunk no start has asked for in 45 days is forgotten
BATCH = 64

_file: dict | None = None


def key_of(model: str, chunk: str) -> str:
    return hashlib.sha256(f"{model}\n{chunk}".encode("utf-8")).hexdigest()


def _from_file() -> dict:
    """The image's own cache: key -> vector. Loaded once; empty if the file is not there or not readable."""
    global _file
    if _file is None:
        _file = {}
        try:
            import numpy as np
            with np.load(EMB_CACHE_FILE, allow_pickle=False) as z:
                _file = dict(zip((str(k) for k in z["keys"]), z["vecs"]))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"Embedding cache file not usable ({e}); continuing without it")
    return _file


def _redis():
    if not REDIS_URL:
        return None
    try:
        import redis
        return redis.Redis.from_url(REDIS_URL, socket_connect_timeout=5, socket_timeout=10)
    except Exception as e:
        log.warning(f"Embedding cache: Redis not usable ({e}); continuing without it")
        return None


def embed_with_cache(chunks: list[str], embed, model: str, client=None) -> list:
    """One vector per chunk, in order. `embed` is the model's own function (list of texts -> list of vectors) and
    is called only for chunks that are in neither cache. `client` is a Redis client, for tests."""
    import numpy as np

    keys = [key_of(model, c) for c in chunks]
    out: list = [None] * len(chunks)
    n_file = n_redis = 0

    held = _from_file()
    for i, k in enumerate(keys):
        v = held.get(k)
        if v is not None:
            out[i] = v
            n_file += 1

    r = client if client is not None else _redis()
    if r is not None:
        try:
            want = [i for i, v in enumerate(out) if v is None]
            for a in range(0, len(want), 500):
                part = want[a:a + 500]
                for i, raw in zip(part, r.mget([PREFIX + keys[i] for i in part])):
                    if raw:
                        out[i] = np.frombuffer(raw, dtype=np.float32)
                        n_redis += 1
            # Everything this start uses stays remembered, wherever it came from.
            pipe = r.pipeline(transaction=False)
            for i, v in enumerate(out):
                if v is not None:
                    pipe.set(PREFIX + keys[i], np.asarray(v, dtype=np.float32).tobytes(), ex=TTL_SECONDS)
            pipe.execute()
        except Exception as e:
            log.warning(f"Embedding cache: Redis read failed ({e}); computing what is missing")
            r = None

    missing = [i for i, v in enumerate(out) if v is None]
    for a in range(0, len(missing), BATCH):
        part = missing[a:a + BATCH]
        vecs = embed([chunks[i] for i in part])
        for i, v in zip(part, vecs):
            out[i] = np.asarray(v, dtype=np.float32)
        if r is not None:
            # Written batch by batch, so a start that is cut short does not lose what it had already computed.
            try:
                pipe = r.pipeline(transaction=False)
                for i in part:
                    pipe.set(PREFIX + keys[i], out[i].tobytes(), ex=TTL_SECONDS)
                pipe.execute()
            except Exception as e:
                log.warning(f"Embedding cache: Redis write failed ({e})")
                r = None

    log.info(f"Embeddings: {n_file} from the image, {n_redis} from Redis, {len(missing)} computed")
    return [np.asarray(v, dtype=np.float32).tolist() for v in out]


def save_file(chunks: list[str], vectors: list, model: str, path: str = "") -> int:
    """Write the image's cache file (used at build)."""
    import numpy as np
    path = path or EMB_CACHE_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    np.savez(path, keys=np.array([key_of(model, c) for c in chunks]), vecs=np.asarray(vectors, dtype=np.float32))
    return len(chunks)
