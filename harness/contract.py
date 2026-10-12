"""
The submission contract. Every team's solution MUST follow this exactly.

A submission is a folder containing `solution.py` that defines:

    def build() -> KnowledgeSystem

The harness then does, in order:

    ks = build()
    for snap in snapshots (oldest -> newest):
        ks.ingest(Snapshot(...))
    for q in eval questions:
        ks.answer(q.question, as_of=q.as_of)       # -> Answer
    ks.conflicts()                                  # -> list[Conflict]

Only the Python standard library types below cross the boundary, so a
solution can use any stack internally (vector DB, graph, LLM, rules...).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    markdown: str          # page content as markdown
    fetched_at: str        # ISO-8601 timestamp of the crawl


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str       # e.g. "01_2026-06-02_baseline"
    taken_on: str          # ISO date, e.g. "2026-06-02"
    pages: list[Page]      # the FULL website as of this pull


@dataclass
class Source:
    url: str
    snapshot_id: Optional[str] = None
    quote: Optional[str] = None   # short supporting excerpt (optional, helps scoring)


@dataclass
class Answer:
    text: str                                   # the member-facing answer
    sources: list[Source] = field(default_factory=list)
    confident: bool = True                      # False = "I don't know / please contact us"
    # Set when the system knows the sources disagree and why it picked a value:
    conflict_note: Optional[str] = None


@dataclass
class Conflict:
    topic: str                     # e.g. "30-year fixed mortgage APR"
    values: list[str]              # the competing values, e.g. ["6.125%", "6.375%"]
    sources: list[Source]          # where each value came from
    resolution: Optional[str] = None   # the value the system decided is correct, if any
    reason: Optional[str] = None       # why (newer page, official rate sheet, expired promo...)


@runtime_checkable
class KnowledgeSystem(Protocol):
    def ingest(self, snapshot: Snapshot) -> None:
        """Called once per snapshot, oldest first. Must finish before the next call."""

    def answer(self, question: str, as_of: Optional[str] = None) -> Answer:
        """Answer a member question.

        as_of=None  -> answer with what is true as of the LATEST snapshot.
        as_of=date  -> answer as the website stood on that date (ISO date).
        """

    def conflicts(self) -> list[Conflict]:
        """Every place the website contradicts itself (across or within snapshots)."""
