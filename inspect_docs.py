"""
inspect_docs.py
---------------
Diagnostic script — tells us what the Bankr docs structure actually looks
like so we can pick the right granularity for the propose pipeline.

Run: railway run python inspect_docs.py
"""
import asyncio, os, sys, re
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
from shared import SemanticDocsManager
import article_sync


async def main():
    docs = SemanticDocsManager()
    await docs.ensure_ready()
    text = docs.raw_content
    print(f"Total chars: {len(text):,}")
    print(f"Total lines: {text.count(chr(10)):,}")

    # Count headers by level
    HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
    matches = list(HEADER_RE.finditer(text))
    by_level = Counter(len(m.group(1)) for m in matches)
    print(f"\nHeaders by level:")
    for lvl in sorted(by_level):
        print(f"  H{lvl}: {by_level[lvl]:>5}")
    print(f"  total: {len(matches):,}")

    # Are some headers actually inside code blocks? Check by looking for ```
    # before each header
    code_block_state = False
    in_code_count = 0
    line_no = 0
    pos = 0
    for line in text.split("\n"):
        line_no += 1
        if line.strip().startswith("```"):
            code_block_state = not code_block_state
        elif code_block_state and HEADER_RE.match(line):
            in_code_count += 1
    print(f"\nHeaders that are actually inside ``` code blocks: {in_code_count}")

    # Show distribution of section body sizes
    secs = article_sync.parse_sections(text)
    body_sizes = sorted(len(s.body) for s in secs if s.header)
    if body_sizes:
        n = len(body_sizes)
        print(f"\nSection body sizes (chars), n={n}:")
        print(f"  min   = {body_sizes[0]:,}")
        print(f"  p25   = {body_sizes[n//4]:,}")
        print(f"  p50   = {body_sizes[n//2]:,}")
        print(f"  p75   = {body_sizes[3*n//4]:,}")
        print(f"  max   = {body_sizes[-1]:,}")
        tiny = sum(1 for s in body_sizes if s < 200)
        print(f"  sections < 200 chars (likely too granular): {tiny}")

    # Sample headers by level so we can see what's actually being captured
    print("\nSample H1 headers (first 20):")
    h1s = [m.group(2) for m in matches if len(m.group(1)) == 1][:20]
    for h in h1s:
        print(f"  • {h!r}")

    print("\nSample H2 headers (first 20):")
    h2s = [m.group(2) for m in matches if len(m.group(1)) == 2][:20]
    for h in h2s:
        print(f"  • {h!r}")

    print("\nSample H3 headers (first 20):")
    h3s = [m.group(2) for m in matches if len(m.group(1)) == 3][:20]
    for h in h3s:
        print(f"  • {h!r}")

    # Count "top-level" sections — H1+H2 only
    top_level = sum(1 for m in matches if len(m.group(1)) <= 2)
    print(f"\nIf we limited diff to H1+H2 only: {top_level} sections")


if __name__ == "__main__":
    asyncio.run(main())
