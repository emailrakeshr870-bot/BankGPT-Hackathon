"""
See what Pinecone actually returns for a question (the same search n8n does).

    set OPENAI_API_KEY=...
    set PINECONE_API_KEY=...
    python rag\\probe.py "How much is a five-year car loan for a 2025 vehicle?"
    python rag\\probe.py "lobby hours" --snap 5 --top 30
    python rag\\probe.py "30-year mortgage rate" --types rates,product,promo

Prints, in rank order: score, page type, effective/published date, URL and the first content line.
Lines marked >>> contain any of the --find strings (e.g. --find 5.39%,5.34%).
"""
from __future__ import annotations

import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--index", default="bankgpt")
    ap.add_argument("--snap", default="5", help="snapshot number 1-5 (5 = latest)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--types", default="", help="comma list of page_type to restrict to, e.g. rates,product,promo")
    ap.add_argument("--find", default="", help="comma list of strings to highlight, e.g. 5.39%%,5.34%%")
    args = ap.parse_args()
    for var in ("OPENAI_API_KEY", "PINECONE_API_KEY"):
        if not os.getenv(var):
            sys.exit(f"Please set {var} first.")

    from openai import OpenAI
    from pinecone import Pinecone

    vec = OpenAI().embeddings.create(model="text-embedding-3-small", input=[args.question]).data[0].embedding
    flt = {f"snap_{args.snap}": {"$eq": "true"}}
    if args.types:
        flt = {"$and": [flt, {"page_type": {"$in": [t.strip() for t in args.types.split(",")]}}]}
    index = Pinecone(api_key=os.environ["PINECONE_API_KEY"]).Index(args.index)
    res = index.query(vector=vec, top_k=args.top, filter=flt, include_metadata=True)
    finds = [f.strip() for f in args.find.split(",") if f.strip()]
    print(f'Q: {args.question}   filter={flt}\n')
    for i, m in enumerate(res.get("matches", []), 1):
        md = m.get("metadata") or {}
        text = md.get("text", "")
        body = text.split("\n\n", 1)[-1].strip().replace("\n", " ")
        date = md.get("effective_date") or md.get("published_date") or ""
        mark = ">>>" if any(f in text for f in finds) else "   "
        print(f"{mark}{i:>3}. {m['score']:.3f}  {md.get('page_type', '?'):<10} {date:<10}  {md.get('url', '')[22:80]}")
        print(f"          {body[:150]}")


if __name__ == "__main__":
    main()
