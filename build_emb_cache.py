"""
build_emb_cache.py
------------------
Run once while the image is built: fetch the docs as they are now, embed every chunk, and write the result to the
image's embedding cache (see emb_cache.py). A container started from the image then has almost every vector it
needs before it has computed anything, so its first start on a new host takes seconds, not minutes.

If the docs cannot be fetched at build time the image is built without the file and still works: a start computes
what it needs, as it always did. This script never fails the build.
"""
import sys
import urllib.request


def main() -> int:
    try:
        import emb_cache
        from shared import DOCS_URL, EMBED_MODEL, SemanticDocsManager
        req = urllib.request.Request(DOCS_URL, headers={"User-Agent": "bankr-support-build"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8", "replace")
        docs = SemanticDocsManager()
        chunks = docs._chunk_text(text)
        vectors = []
        for i in range(0, len(chunks), 64):
            vectors.extend(docs._ef(chunks[i:i + 64]))
        n = emb_cache.save_file(chunks, vectors, EMBED_MODEL)
        print(f"build_emb_cache: {n} chunks embedded into {emb_cache.EMB_CACHE_FILE}")
    except Exception as e:  # noqa: BLE001 - by design: never fail the build
        print(f"build_emb_cache: skipped ({type(e).__name__}: {e}); starts will compute embeddings themselves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
