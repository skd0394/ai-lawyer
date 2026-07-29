"""Citation tracking and post-turn auditing.

The spec's requirement is not "mention a URL". It is:

    every legal answer cites its sources with URLs
    citations carry a user visible confidence signal
    UNVERIFIED CITATIONS ARE NEVER PRESENTED AS AUTHORITATIVE

The third clause cannot be satisfied by prompting alone — a model will
happily produce a plausible URL it never opened. So the check is
mechanical: extract every URL from the assistant's prose and compare it
against what was actually retrieved this session.

A URL in the answer that is not in the registry is a GHOST CITATION.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict

# Trailing punctuation is deliberately excluded — "see (https://x.gov/a)."
URL_RE = re.compile(r"https?://[^\s\)\]\}<>\"'`]+")

# Models write URLs inside markdown emphasis: **https://x.gov/a**. Left
# attached, the trailing markers cause FALSE POSITIVES — a legitimately
# fetched URL fails to match its registry entry and looks like a ghost.
_TRAILING = "*_~`.,;:!?\"')]}>"


def clean_url(u: str) -> str:
    return (u or "").rstrip(_TRAILING)

CONFIDENCE_LABEL = {
    "verified": "source located and read",
    "partial": "source retrieved, quotes not fully verified",
    "unverified": "could not verify — please check this yourself",
}


@dataclass
class CitationRecord:
    url: str
    title: str = ""
    confidence: str = "unverified"
    verified_quotes: list[str] = field(default_factory=list)
    handle: str | None = None
    fetched_at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        return CONFIDENCE_LABEL.get(self.confidence, self.confidence)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        return d


class CitationRegistry:
    """One per session. Lives in ctx so tool handlers can add to it."""

    def __init__(self) -> None:
        self._by_url: dict[str, CitationRecord] = {}

    def add(self, rec: CitationRecord) -> CitationRecord:
        existing = self._by_url.get(rec.url)
        # Keep the strongest confidence we ever achieved for a URL.
        rank = {"verified": 2, "partial": 1, "unverified": 0}
        if existing and rank.get(existing.confidence, 0) >= rank.get(rec.confidence, 0):
            return existing
        self._by_url[rec.url] = rec
        return rec

    def from_fetch(self, r: dict) -> CitationRecord:
        return self.add(CitationRecord(
            url=r.get("url", ""),
            title=r.get("title", ""),
            confidence=r.get("confidence", "unverified"),
            verified_quotes=list(r.get("verified_quotes") or []),
            handle=r.get("handle"),
        ))

    def all(self) -> list[CitationRecord]:
        return list(self._by_url.values())

    def get(self, url: str) -> CitationRecord | None:
        url = clean_url(url)
        if url in self._by_url:
            return self._by_url[url]
        # Tolerate trailing-slash and fragment differences.
        norm = url.rstrip("/").split("#")[0]
        for u, rec in self._by_url.items():
            if u.rstrip("/").split("#")[0] == norm:
                return rec
        return None

    # ── the mechanical check ──────────────────────────────────────────────
    def audit(self, answer_text: str) -> dict:
        cited_urls = list(dict.fromkeys(
            clean_url(u) for u in URL_RE.findall(answer_text or "")))

        ghosts, presented = [], []
        for u in cited_urls:
            rec = self.get(u)
            if rec is None:
                # ⭐ A URL the model produced without ever fetching it.
                ghosts.append(u)
            else:
                presented.append(rec.to_dict())

        unverified = [p for p in presented if p["confidence"] != "verified"]
        fetched_not_cited = [
            r.to_dict() for r in self.all()
            if not any(p["url"] == r.url for p in presented)
        ]

        return {
            "kind": "citation_audit",
            "sources_fetched": len(self.all()),
            "urls_in_answer": len(cited_urls),
            "citations": presented,
            # The two findings that matter:
            "ghost_citations": ghosts,
            "unverified_presented": unverified,
            "fetched_but_not_cited": fetched_not_cited,
            "clean": not ghosts and not unverified,
        }


def audit_warning(audit: dict) -> str | None:
    """Human-readable warning for the UI, or None if the answer is clean."""
    bits = []
    if audit["ghost_citations"]:
        bits.append(
            f"{len(audit['ghost_citations'])} URL(s) appear in this answer "
            f"that were never retrieved: "
            f"{', '.join(audit['ghost_citations'][:3])}. Treat them as "
            f"unverified.")
    if audit["unverified_presented"]:
        bits.append(
            f"{len(audit['unverified_presented'])} cited source(s) could not "
            f"be fully verified.")
    return " ".join(bits) if bits else None