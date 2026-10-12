#!/usr/bin/env python3
"""
Plug a submission in and score it.

    python harness/run_eval.py \
        --submission path/to/team_folder \
        --snapshots participant/snapshots \
        --eval participant/eval/public_eval.jsonl \
        [--conflicts-gold internal/eval/conflicts_gold.json] \
        [--out results/team_x]

On the first run the snapshot zips in snapshots_zip/ are unpacked into the --snapshots folder.

Participants run it against the public eval; we run the same command against
internal/eval/internal_eval.jsonl + conflicts_gold.json to rank teams.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib.util
import json
import sys
import time
import traceback
import zipfile
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from contract import Answer, KnowledgeSystem, Page, Snapshot, Source  # noqa: E402
from scoring import score_conflicts, score_item, summarize  # noqa: E402

ANSWER_TIMEOUT_S = 60
INGEST_LIMIT_S = 30 * 60


def ensure_snapshots(root: Path) -> None:
    """First run: unpack snapshots_zip/*.zip into snapshots/ (folders that already exist are kept)."""
    zip_dir = next((d for d in (root.parent / "snapshots_zip", root) if d.is_dir() and any(d.glob("*.zip"))), None)
    if zip_dir is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    todo = [z for z in sorted(zip_dir.glob("*.zip")) if not (root / z.stem / "manifest.json").exists()]
    if todo:
        print(f"first run: unpacking {len(todo)} snapshot zip(s) from {zip_dir} into {root}", flush=True)
    for z in todo:
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():  # refuse anything that would land outside the snapshots folder
                if (root / name).resolve().is_relative_to(root.resolve()) is False:
                    raise SystemExit(f"unsafe path in {z.name}: {name}")
            zf.extractall(root)


def load_snapshots(root: Path) -> list[Snapshot]:
    ensure_snapshots(root)
    snaps = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists()):
        manifest = json.loads((d / "manifest.json").read_text())
        pages = [
            Page(
                url=p["url"],
                title=p.get("title", ""),
                markdown=(d / p["file"]).read_text(encoding="utf-8"),
                fetched_at=p.get("fetched_at", manifest["taken_on"]),
            )
            for p in manifest["pages"]
        ]
        snaps.append(Snapshot(manifest["snapshot_id"], manifest["taken_on"], pages))
    snaps.sort(key=lambda s: s.taken_on)
    return snaps


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def to_answer(ans) -> Answer:
    """Accept our Answer, a dict, or ANY object with the same fields (e.g. the candidate's own copy
    of contract.Answer) without losing sources/confidence."""
    if isinstance(ans, Answer):
        return ans
    if isinstance(ans, str) or ans is None:
        return Answer(text=ans or "")
    srcs = []
    for s in _get(ans, "sources", []) or []:
        url = s if isinstance(s, str) else _get(s, "url")
        if url:
            srcs.append(Source(url=str(url), snapshot_id=_get(s, "snapshot_id") if not isinstance(s, str) else None,
                               quote=_get(s, "quote") if not isinstance(s, str) else None))
    conf = _get(ans, "confident", True)
    return Answer(text=str(_get(ans, "text", "") or ""), sources=srcs,
                  confident=bool(True if conf is None else conf), conflict_note=_get(ans, "conflict_note"))


def load_submission(folder: Path) -> KnowledgeSystem:
    sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location("solution", folder / "solution.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ks = mod.build()
    missing = [m for m in ("ingest", "answer", "conflicts") if not callable(getattr(ks, m, None))]
    if missing:
        raise SystemExit(f"Submission does not follow the contract; missing: {missing}")
    return ks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--submission", required=True, type=Path)
    ap.add_argument("--snapshots", type=Path, default=Path("snapshots"),
                    help="snapshot folders; unpacked automatically from ../snapshots_zip on first run")
    ap.add_argument("--eval", required=True, type=Path)
    ap.add_argument("--conflicts-gold", type=Path)
    ap.add_argument("--out", type=Path, default=Path("results"))
    ap.add_argument("--repeat-sample", type=int, default=20, help="questions re-asked to test consistency (0 = off)")
    ap.add_argument("--judge", action="store_true", help="add an LLM judge for free-text items (needs ANTHROPIC_API_KEY)")
    args = ap.parse_args()

    items = [json.loads(l) for l in args.eval.read_text().splitlines() if l.strip()]
    snaps = load_snapshots(args.snapshots)
    ks = load_submission(args.submission)

    t0 = time.time()
    for s in snaps:
        print(f"ingest {s.snapshot_id} ({len(s.pages)} pages)", flush=True)
        ks.ingest(s)
    ingest_s = time.time() - t0

    results = []
    pool = cf.ThreadPoolExecutor(max_workers=1)
    for it in items:
        t = time.time()
        try:
            ans = pool.submit(ks.answer, it["question"], it.get("as_of")).result(timeout=ANSWER_TIMEOUT_S)
            ans = to_answer(ans)
            err = None
        except Exception as e:  # timeouts and crashes score zero, run continues
            ans, err = Answer(text="", confident=False), f"{type(e).__name__}: {e}"
            traceback.print_exc()
        r = score_item(it, ans, judge=args.judge)
        r.update(latency_s=round(time.time() - t, 2), error=err, answer=asdict(ans))
        results.append(r)

    # Consistency: ask a deterministic sample of questions a second time.
    repeats = []
    for it in items[:: max(1, len(items) // args.repeat_sample)] if args.repeat_sample else []:
        try:
            a2 = pool.submit(ks.answer, it["question"], it.get("as_of")).result(timeout=ANSWER_TIMEOUT_S)
            text = a2.text if isinstance(a2, Answer) else (a2.get("text", "") if isinstance(a2, dict) else str(a2))
        except Exception:
            text = ""
        repeats.append({"id": it["id"], "text": text})

    conflict_report = None
    if args.conflicts_gold:
        try:
            submitted = ks.conflicts()
        except Exception as e:
            print(f"conflicts() failed: {e}")
            submitted = []
        gold = json.loads(args.conflicts_gold.read_text())
        conflict_report = score_conflicts(gold, submitted)

    report = summarize(results, conflict_report, repeats)
    report["ingest_seconds"] = round(ingest_s, 1)
    report["ingest_over_limit"] = ingest_s > INGEST_LIMIT_S

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "per_question.jsonl").write_text("\n".join(json.dumps(r) for r in results))
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "by_category"}, indent=2))
    print("by category:")
    for cat, v in report["by_category"].items():
        print(f"  {cat:24s} {v['score']:.2f}  (n={v['n']})")
    pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
