"""
Load rag/chunks.jsonl into Pinecone (one record per chunk, no duplicates).

Each record uses the chunk's own id, so running this again OVERWRITES instead of duplicating.
The chunk text is stored in metadata["text"], which is what n8n's Pinecone Vector Store node reads.

Setup (once):
    pip install openai pinecone

Run (Windows Command Prompt, from the kit folder):
    set OPENAI_API_KEY=sk-...
    set PINECONE_API_KEY=pcsk_...
    python rag\\load_pinecone.py --clear          # wipe the index, then load everything
    python rag\\load_pinecone.py                  # load / overwrite without wiping

Options:
    --index bankgpt      Pinecone index name (default: bankgpt)
    --batch 100          chunks per OpenAI / Pinecone call
    --clear              delete every record in the index first
    --dry-run            read the file and report, without calling OpenAI or Pinecone
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL = "text-embedding-3-small"          # 1536 dimensions, matches the bankgpt index


def load_chunks(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def with_retry(fn, what: str, tries: int = 8):
    delay = 5
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:                     # rate limits, timeouts, brief outages
            msg = str(e)
            if attempt == tries:
                raise
            print(f"    {what} failed ({msg[:120]}); retrying in {delay}s [{attempt}/{tries}]", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(HERE / "chunks.jsonl"))
    ap.add_argument("--index", default="bankgpt")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = load_chunks(Path(args.file))
    ids = {r["id"] for r in rows}
    print(f"{len(rows)} chunks in {args.file} ({len(ids)} unique ids)")
    if args.dry_run:
        return

    for var in ("OPENAI_API_KEY", "PINECONE_API_KEY"):
        if not os.getenv(var):
            sys.exit(f"Please set {var} first (see the top of this file).")

    from openai import OpenAI
    from pinecone import Pinecone

    oai = OpenAI()
    index = Pinecone(api_key=os.environ["PINECONE_API_KEY"]).Index(args.index)
    stats = index.describe_index_stats()
    print(f"index '{args.index}': {stats.get('total_vector_count', '?')} records, dimension {stats.get('dimension', '?')}")
    if stats.get("dimension") not in (None, 1536):
        sys.exit("This index is not 1536-dimensional; it does not match text-embedding-3-small.")

    if args.clear:
        print("clearing the index ...", flush=True)
        try:
            index.delete(delete_all=True)
        except Exception as e:
            if "not found" not in str(e).lower():        # an already-empty namespace is fine
                raise
        for _ in range(30):
            time.sleep(2)
            if index.describe_index_stats().get("total_vector_count", 0) == 0:
                break
        print("  index is empty")

    t0 = time.time()
    done = 0
    for start in range(0, len(rows), args.batch):
        batch = rows[start:start + args.batch]
        texts = [r["text"] for r in batch]
        emb = with_retry(lambda: oai.embeddings.create(model=MODEL, input=texts), "OpenAI embeddings")
        vectors = []
        for r, e in zip(batch, emb.data):
            meta = {k: v for k, v in r.items() if k != "id" and v not in (None, "")}
            vectors.append({"id": r["id"], "values": e.embedding, "metadata": meta})
        with_retry(lambda: index.upsert(vectors=vectors), "Pinecone upsert")
        done += len(batch)
        rate = done / max(1e-6, time.time() - t0)
        print(f"  {done}/{len(rows)} loaded  ({rate:.0f} chunks/s, ~{(len(rows) - done) / max(rate, 1e-6):.0f}s left)", flush=True)

    time.sleep(5)
    print(f"done in {time.time() - t0:.0f}s. Pinecone now reports "
          f"{index.describe_index_stats().get('total_vector_count', '?')} records "
          f"(it can take a minute to catch up; expect {len(ids)}).")


if __name__ == "__main__":
    main()
