"""
Ask n8n only the questions you pick and score them, without loading the snapshots.
Costs a few model calls instead of a full run.

    python dev\\quick_check.py dup-049 dup-088 dup-104
    python dev\\quick_check.py --failed res_n8n4          (re-ask everything that scored < 1 in that run)
    python dev\\quick_check.py --eval dev\\paraphrase_eval.jsonl q-003

Uses the same BANKGPT_WEBHOOK_URL / BANKGPT_WEBHOOK_KEY settings as solution.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "solution"))
from scoring import score_item  # noqa: E402
import solution  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--eval", default=str(ROOT / "eval" / "public_eval.jsonl"))
    ap.add_argument("--failed", help="folder of an earlier run; re-ask the questions that scored < 1")
    args = ap.parse_args()

    ids = set(args.ids)
    if args.failed:
        for line in open(Path(args.failed) / "per_question.jsonl", encoding="utf-8"):
            r = json.loads(line)
            if r.get("score", 1) < 1:
                ids.add(r["id"])
    items = [json.loads(l) for l in open(args.eval, encoding="utf-8") if l.strip()]
    items = [it for it in items if it["id"] in ids]
    if not items:
        sys.exit("No matching question ids.")

    ks = solution.BankGPT()
    if not ks.cfg["url"]:
        sys.exit("Set BANKGPT_WEBHOOK_URL first.")
    total = 0.0
    for it in items:
        t = time.time()
        ans = ks._ask_n8n(it["question"], it.get("as_of"))
        dt = time.time() - t
        if ans is None:
            ans = solution.Answer(text="", confident=False)
        r = score_item(it, ans)
        total += r["score"]
        print(f"{it['id']}  score {r['score']:.2f}  {dt:.1f}s  missed {r['missed']}  trap {r['trap_hits']}")
        print(f"   Q: {it['question']}" + (f"  (as of {it['as_of']})" if it.get("as_of") else ""))
        print(f"   A: {ans.text[:400]}")
        print(f"   sources: {[s.url for s in ans.sources][:4]}\n")
    print(f"{len(items)} questions, average score {total / len(items):.2f}")


if __name__ == "__main__":
    main()
