"""Web search, provider-agnostic.

Runs in the API PROCESS, never the sandbox — the search key is a
credential and credentials stay in the orchestrator.

Returns METADATA ONLY: title, url, snippet. ~400-600 tokens for five
results. Fetching a page is a separate deliberate step. Auto-fetching every
result would put ~12k tokens of page text into context per search, whether
or not any of it was needed — which is precisely the failure this project
exists to fix.

Three providers behind one interface for the same reason the model adapter
exists: the brief says no single-provider lock-in.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from typing import Any

SNIPPET_CHARS = 240          # ~60 tokens each; 5 results ~= 400 tokens

# Source quality tiers. A general search for "illinois eviction notice"
# ranks law-firm marketing above the statute; legal answers must not cite
# SEO content as authority.
PRIMARY_PATTERNS = (
    r"\.gov$", r"\.gov/", r"\.us$", r"\.us/", r"\.mil$",
    r"ilga\.gov", r"legis\.", r"legislature\.", r"courts?\.",
    r"law\.cornell\.edu", r"govinfo\.gov", r"uscourts\.gov",
)
SECONDARY_PATTERNS = (
    r"\.edu$", r"\.edu/", r"americanbar\.org", r"nolo\.com",
    r"justia\.com", r"casetext\.com", r"courtlistener\.com",
)


def source_tier(url: str) -> str:
    u = (url or "").lower()
    if any(re.search(p, u) for p in PRIMARY_PATTERNS):
        return "primary"        # statutes, courts, government
    if any(re.search(p, u) for p in SECONDARY_PATTERNS):
        return "secondary"      # academic, bar associations, legal databases
    return "tertiary"           # blogs, marketing, general web


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    tier: str = "tertiary"

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(text: str, limit: int = SNIPPET_CHARS) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "..."


# ── providers ─────────────────────────────────────────────────────────────
class TavilyProvider:
    name = "tavily"

    def __init__(self, api_key: str):
        self.key = api_key

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.tavily.com/search", json={
                "api_key": self.key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                # We do NOT want raw content here — that's the whole point.
                # Fetching is a separate, explicit step.
                "include_raw_content": False,
                "include_answer": False,
            })
            r.raise_for_status()
            data = r.json()
        return [
            SearchResult(title=x.get("title", ""), url=x.get("url", ""),
                         snippet=_clean(x.get("content", "")),
                         tier=source_tier(x.get("url", "")))
            for x in data.get("results", [])
        ]


class BraveProvider:
    name = "brave"

    def __init__(self, api_key: str):
        self.key = api_key

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={"X-Subscription-Token": self.key,
                         "Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        return [
            SearchResult(title=x.get("title", ""), url=x.get("url", ""),
                         snippet=_clean(x.get("description", "")),
                         tier=source_tier(x.get("url", "")))
            for x in (data.get("web", {}).get("results") or [])[:max_results]
        ]


class SerperProvider:
    name = "serper"

    def __init__(self, api_key: str):
        self.key = api_key

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://google.serper.dev/search",
                             headers={"X-API-KEY": self.key},
                             json={"q": query, "num": max_results})
            r.raise_for_status()
            data = r.json()
        return [
            SearchResult(title=x.get("title", ""), url=x.get("link", ""),
                         snippet=_clean(x.get("snippet", "")),
                         tier=source_tier(x.get("link", "")))
            for x in (data.get("organic") or [])[:max_results]
        ]


PROVIDERS = [
    ("TAVILY_API_KEY", TavilyProvider),
    ("BRAVE_API_KEY", BraveProvider),
    ("SERPER_API_KEY", SerperProvider),
]


def get_provider():
    """Whichever key is present wins. Swapping providers is adding a secret
    and removing another — no code change."""
    for env, cls in PROVIDERS:
        key = os.environ.get(env)
        if key:
            return cls(key)
    raise RuntimeError(
        "No search provider configured. Set one of: "
        + ", ".join(e for e, _ in PROVIDERS))


def available_providers() -> list[str]:
    return [cls.name for env, cls in PROVIDERS if os.environ.get(env)]


# ── the tool-facing call ──────────────────────────────────────────────────
async def web_search(query: str, max_results: int = 5,
                     prefer_official: bool = True) -> dict:
    provider = get_provider()
    try:
        results = await provider.search(query, max_results=max_results)
    except Exception as e:
        return {"ok": False, "provider": provider.name,
                "error": f"{type(e).__name__}: {e}", "results": []}

    if prefer_official:
        # Stable sort by tier: statutes and court sites above marketing.
        order = {"primary": 0, "secondary": 1, "tertiary": 2}
        results.sort(key=lambda r: order.get(r.tier, 3))

    return {"ok": True, "provider": provider.name, "query": query,
            "results": [r.to_dict() for r in results]}


def format_for_model(payload: dict) -> str:
    """Compact rendering. Every character here is re-sent on every
    subsequent loop iteration, so it stays terse."""
    if not payload.get("ok"):
        return (f"SEARCH FAILED ({payload.get('error')}). "
                f"Do not answer from memory — tell the user the search "
                f"could not be completed.")
    if not payload["results"]:
        return "No results. Try different search terms."

    lines = []
    for i, r in enumerate(payload["results"], 1):
        mark = {"primary": "[OFFICIAL]", "secondary": "[LEGAL-DB]",
                "tertiary": ""}.get(r["tier"], "")
        lines.append(f"{i}. {mark} {r['title']}\n   {r['url']}\n   {r['snippet']}")
    lines.append("\nCall web_fetch on the URLs you need. Prefer [OFFICIAL] "
                 "sources for statutory or procedural claims.")
    return "\n".join(lines)