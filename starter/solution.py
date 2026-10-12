"""
Starter solution — deliberately naive. It works, and it scores badly on purpose.

It keeps only the LATEST copy of each page and returns the paragraph that shares the most
words with the question. It never notices that pages disagree, ignores `as_of`, and
returns no conflicts. Your job is to do much better.

Keep the `build()` function and the three methods; change everything else.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

WORD = re.compile(r"[a-z0-9$%.]+")


class NaiveKnowledge:
    def __init__(self):
        self.pages = {}  # url -> (snapshot_id, markdown)

    def ingest(self, snapshot: Snapshot) -> None:
        for p in snapshot.pages:
            self.pages[p.url] = (snapshot.snapshot_id, p.markdown)

    def answer(self, question, as_of=None):
        q = set(WORD.findall(question.lower()))
        best, best_score = None, 0
        for url, (sid, md) in self.pages.items():
            for para in md.split("\n\n"):
                s = len(q & set(WORD.findall(para.lower())))
                if s > best_score:
                    best, best_score = (url, sid, para.strip()), s
        if not best:
            return Answer(text="I don't know.", confident=False)
        url, sid, para = best
        return Answer(text=para[:600], sources=[Source(url=url, snapshot_id=sid)])

    def conflicts(self):
        return []


def build():
    return NaiveKnowledge()
