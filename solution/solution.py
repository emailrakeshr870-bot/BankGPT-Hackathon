"""
BankGPT submission: answers come from the n8n + Pinecone RAG workflow.

    answer()     POSTs {"question", "as_of"} to the n8n webhook "bankgpt-answer" and turns the
                 reply {answer, sources, confident, conflict_note} into an Answer.
                 - Same question + same date -> same answer (cached), so repeats are consistent.
                 - If n8n is unreachable, too slow, or returns garbage, the local rule-based
                   engine (local_engine.py) answers instead, so no question scores zero.
    conflicts()  computed locally from the snapshots by local_engine.py.
    ingest()     records each snapshot (dates -> snapshot ids for sources) and feeds the local engine.

Settings (environment variables win over bankgpt_config.json next to this file):
    BANKGPT_WEBHOOK_URL   e.g. http://localhost:5679/webhook/bankgpt-answer   (production URL)
    BANKGPT_WEBHOOK_KEY   the value of the X-BankGPT-Key header set in n8n's "BankGPT webhook secret"
    BANKGPT_TIMEOUT       seconds to wait for n8n before falling back (default 15)
    BANKGPT_MODE          "n8n" (default), "local" (never call n8n) or "n8n-only" (no fallback; for testing)

Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(next(d / "harness" for d in HERE.parents
                            if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402
import local_engine  # noqa: E402

DONT_KNOW = "I don't know. Please call one of our branches."
HEADER_NAME = "X-BankGPT-Key"


def _settings() -> dict:
    cfg = {}
    f = HERE / "bankgpt_config.json"
    if f.exists():
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    def get(env, key, default=""):
        return os.getenv(env) or cfg.get(key) or default
    return {
        "url": get("BANKGPT_WEBHOOK_URL", "webhook_url"),
        "key": get("BANKGPT_WEBHOOK_KEY", "webhook_key"),
        "timeout": float(get("BANKGPT_TIMEOUT", "timeout", 15)),
        "mode": str(get("BANKGPT_MODE", "mode", "n8n")).lower(),
    }


class BankGPT:
    def __init__(self):
        self.cfg = _settings()
        self.local = local_engine.build()
        self.snaps: list[tuple[str, str]] = []          # (taken_on, snapshot_id), oldest first
        self.cache: dict[tuple[str, str], Answer] = {}
        self.fail_streak = 0                             # stop calling n8n after repeated failures
        self.stats = {"n8n": 0, "local": 0, "cache": 0}

    # ---------------------------------------------------------------- ingest
    def ingest(self, snapshot: Snapshot) -> None:
        self.snaps.append((snapshot.taken_on, snapshot.snapshot_id))
        self.snaps.sort()
        self.local.ingest(snapshot)

    def _snapshot_for(self, as_of: str | None) -> str | None:
        if not self.snaps:
            return None
        if not as_of:
            return self.snaps[-1][1]
        older = [sid for d, sid in self.snaps if d <= as_of[:10]]
        return older[-1] if older else self.snaps[0][1]

    # ---------------------------------------------------------------- answer
    def answer(self, question: str, as_of: str | None = None) -> Answer:
        key = (re.sub(r"\s+", " ", (question or "").strip().lower()), (as_of or "").strip())
        if key in self.cache:
            self.stats["cache"] += 1
            return self.cache[key]
        ans = None
        mode = self.cfg["mode"]
        if mode != "local" and self.cfg["url"] and self.fail_streak < 3:
            ans = self._ask_n8n(question, as_of)
            if ans is None and mode == "n8n-only":
                ans = Answer(text="", confident=False)
        if ans is None:
            ans = self.local.answer(question, as_of)
            self.stats["local"] += 1
        else:
            self.stats["n8n"] += 1
            if ans.confident and len(ans.sources) < 6:
                # add the pages the local engine would cite (cheap, no network) as extra sources
                try:
                    have = {s.url.split("#")[0].rstrip("/") for s in ans.sources}
                    for s in self.local.answer(question, as_of).sources or []:
                        u = s.url.split("#")[0].rstrip("/")
                        if u not in have and len(ans.sources) < 6:
                            have.add(u)
                            ans.sources.append(s)
                except Exception:
                    pass
        self.cache[key] = ans
        return ans

    def _ask_n8n(self, question: str, as_of: str | None) -> Answer | None:
        body = json.dumps({"question": question, "as_of": as_of or ""}).encode("utf-8")
        req = urllib.request.Request(self.cfg["url"], data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        if self.cfg["key"]:
            req.add_header(HEADER_NAME, self.cfg["key"])
        data = None
        self.last_raw = ""
        for attempt in range(2):                         # one quick retry on connection hiccups
            try:
                with urllib.request.urlopen(req, timeout=self.cfg["timeout"]) as r:
                    self.last_raw = r.read().decode("utf-8", "replace")
                    data = json.loads(self.last_raw or "null")
                break
            except (urllib.error.HTTPError, ValueError) as e:   # 4xx/5xx or bad JSON: don't retry
                print(f"[bankgpt] n8n error: {e}", file=sys.stderr)
                break
            except Exception as e:                       # timeout / connection refused
                print(f"[bankgpt] n8n unreachable ({type(e).__name__}: {e})", file=sys.stderr)
                if attempt == 0 and "timed out" not in str(e).lower() and not isinstance(e, TimeoutError):
                    time.sleep(0.3)
                    continue
                break
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict) and isinstance(data.get("json"), dict):
            data = data["json"]
        if not isinstance(data, dict) or not isinstance(data.get("answer"), str) or not data["answer"].strip():
            self.fail_streak += 1
            return None
        self.fail_streak = 0
        return self._to_answer(data, as_of)

    def _to_answer(self, d: dict, as_of: str | None) -> Answer:
        text = d["answer"].strip()
        confident = d.get("confident") is not False
        if re.search(r"\bi don'?t know\b", text, re.I):
            confident = False
        snap_id = self._snapshot_for(d.get("as_of") or as_of)
        sources = []
        if confident:
            seen = set()
            for u in d.get("sources") or []:
                if isinstance(u, str) and u.startswith("http") and u not in seen:
                    seen.add(u)
                    sources.append(Source(url=u, snapshot_id=snap_id))
        note = d.get("conflict_note")
        note = note.strip() if isinstance(note, str) and note.strip() else None
        return Answer(text=text, sources=sources, confident=confident, conflict_note=note)

    # ---------------------------------------------------------------- conflicts
    def conflicts(self) -> list[Conflict]:
        return self.local.conflicts()


def build():
    return BankGPT()


if __name__ == "__main__":
    # Quick check of the webhook, no snapshots needed:
    #   python solution/solution.py "How much is a five-year car loan for a 2025 vehicle?"
    #   python solution/solution.py "What was the 10-month jumbo certificate special?" 2026-05-31
    if len(sys.argv) < 2:
        sys.exit('usage: python solution/solution.py "question" [as_of YYYY-MM-DD]')
    ks = BankGPT()
    cfg = ks.cfg
    print(f"webhook: {cfg['url'] or '(not set)'}   key: {'set' if cfg['key'] else 'NOT set'}   mode: {cfg['mode']}")
    if not cfg["url"]:
        sys.exit("Set BANKGPT_WEBHOOK_URL (or webhook_url in solution/bankgpt_config.json) first.")
    t = time.time()
    a = ks._ask_n8n(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"{time.time() - t:.1f} s")
    if a is None:
        print("n8n did not return an answer. What n8n sent back:")
        print((getattr(ks, "last_raw", "") or "(empty reply)")[:1500])
    else:
        print(a.text)
        print("confident:", a.confident, "| conflict_note:", a.conflict_note)
        for s in a.sources:
            print("  source:", s.url)
