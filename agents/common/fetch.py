"""Extract-then-discard page fetching.

⭐ THE CENTREPIECE OF THE COST STORY.

Naive:  web_fetch returns 12,000 tokens of page text into the message
        array. Five more loop iterations follow. Cost: 12k x 6 = 72,000
        input tokens for ONE page.

Here:   the sandbox fetches, cleans, and caches the full text. A CHEAP
        model extracts only the passages relevant to the stated purpose
        (~600 tokens) plus verbatim quotes. Cost: 600 x 6 = 3,600 tokens.

The full text never enters the model's context. It lives on the volume,
retrievable by handle if the agent genuinely needs more.

Quote verification is mechanical, not prompted: the extractor returns exact
substrings and we check `quote in text`. A quote that doesn't match is
dropped and the citation is marked unverified. That is what makes the
confidence signal mean something.
"""

from __future__ import annotations

import json
import re
from typing import Any

EXTRACT_MODEL = "claude-haiku-4-5-20251001"   # mechanical work, cheap model
EXTRACT_MAX_CHARS = 2600                      # ~600 tokens
SOURCE_WINDOW = 60000                         # chars of source shown to Haiku

# Below this, extraction COSTS MORE THAN IT SAVES. Summarising a page that
# is already smaller than the summary overhead is pure loss — plus a model
# call and ~3s of latency for nothing. Measured: a 1,895-char statute page
# came out 2x MORE expensive through the extraction path.
SMALL_PAGE_CHARS = 4500

EXTRACT_PROMPT = """You are extracting from a source page for a legal research assistant.

PURPOSE: {purpose}

Return ONLY a JSON object, no markdown fences, no commentary:
{{
  "relevant": true or false,
  "outline": ["up to 10 section headings present in the source"],
  "extract": "<= 500 words of the passages that bear on PURPOSE. Quote statutory or regulatory text VERBATIM. Do not summarise away specific numbers, deadlines, or section numbers. If the source does not address PURPOSE, say so in one sentence.",
  "quotes": ["up to 5 EXACT substrings copied character-for-character from the source that support the extract"]
}}

The quotes are checked against the source programmatically. A quote that is
not an exact substring will be discarded, so copy them precisely."""


def _parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _normalise(s: str) -> str:
    """Whitespace-insensitive comparison. Extraction models reflow line
    breaks; that should not fail an otherwise exact quote."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def verify_quotes(quotes: list[str], source: str) -> tuple[list[str], list[str]]:
    """Mechanical check. `quote in text`, nothing more."""
    hay = _normalise(source)
    good, bad = [], []
    for q in (quotes or [])[:5]:
        if len(_normalise(q)) < 15:
            continue                    # too short to be evidence
        (good if _normalise(q) in hay else bad).append(q)
    return good, bad


async def fetch_and_extract(*, worker, url: str, purpose: str,
                            client, model: str = EXTRACT_MODEL) -> dict:
    """Returns a COMPACT result plus the usage of the extraction call."""
    r = worker.call("fetch_url", {"url": url}, timeout=90)

    if not r.get("ok"):
        return {"fetched": False, "url": url,
                "error": r.get("error", "worker call failed"),
                "confidence": "unverified"}

    res = r["result"]
    if not res.get("fetched"):
        # A failed fetch is not a dead end — it is a CONFIDENCE SIGNAL.
        # The spec wants "couldn't fully verify, check this yourself".
        return {"fetched": False, "url": url,
                "error": res.get("error", "fetch failed"),
                "confidence": "unverified"}

    source = res.get("text") or ""

    # ── short-circuit: the page is already small ──────────────────────────
    if len(source) <= SMALL_PAGE_CHARS and not res.get("text_truncated"):
        return {
            "fetched": True,
            "url": res.get("final_url", url),
            "title": res.get("title", ""),
            "handle": res.get("handle"),
            "kind": res.get("kind"),
            "total_chars": res.get("total_chars", 0),
            "outline": [ln.strip().lstrip("#").strip()
                        for ln in source.splitlines()
                        if ln.strip().startswith("#")][:10],
            "extract": source,
            # The model has the actual page text, so every word is
            # trivially "verified" — there is nothing to check it against
            # except itself.
            "verified_quotes": [],
            "unverified_quotes": [],
            "relevant": True,
            "confidence": "verified",
            "short_circuited": True,
            "extraction_usage": {"skipped": "page under "
                                            f"{SMALL_PAGE_CHARS} chars"},
        }

    msg = EXTRACT_PROMPT.format(purpose=purpose) + \
        f"\n\nSOURCE ({res.get('title')}, {res.get('final_url')}):\n" + \
        source[:SOURCE_WINDOW]

    usage = {}
    try:
        resp = await client.messages.create(
            model=model, max_tokens=1500,
            messages=[{"role": "user", "content": msg}])
        raw = "".join(b.text for b in resp.content if b.type == "text")
        parsed = _parse_json(raw)
        usage = {"model": model,
                 "input_tokens": resp.usage.input_tokens,
                 "output_tokens": resp.usage.output_tokens}
    except Exception as e:
        parsed = {}
        usage = {"error": f"{type(e).__name__}: {e}"}

    extract = (parsed.get("extract") or "")[:EXTRACT_MAX_CHARS]
    good, bad = verify_quotes(parsed.get("quotes") or [], source)

    # "verified" requires BOTH: the page was retrieved AND at least one
    # quote is an exact substring of what was retrieved.
    confidence = "verified" if (res.get("fetched") and good) else "partial"
    if not extract:
        confidence = "unverified"

    return {
        "fetched": True,
        "url": res.get("final_url", url),
        "title": res.get("title", ""),
        "handle": res.get("handle"),
        "kind": res.get("kind"),
        "total_chars": res.get("total_chars", 0),
        "outline": (parsed.get("outline") or [])[:10],
        "extract": extract,
        "verified_quotes": good,
        "unverified_quotes": bad,
        "relevant": bool(parsed.get("relevant", bool(extract))),
        "confidence": confidence,
        "extraction_usage": usage,
    }


def format_for_model(r: dict) -> str:
    """What actually enters the model's context. Target: under 800 tokens."""
    if not r.get("fetched"):
        return (f"COULD NOT RETRIEVE {r.get('url')}\n"
                f"Reason: {r.get('error')}\n"
                f"Any claim resting on this source is UNVERIFIED and must be "
                f"presented to the user as such — do not state it as "
                f"established law.")

    parts = [
        f"SOURCE: {r['title']}",
        f"URL: {r['url']}",
        f"CONFIDENCE: {r['confidence']}"
        + ("  (page retrieved and quotes verified against it)"
           if r["confidence"] == "verified"
           else "  (retrieved, but quotes could not be verified — do not "
                "present as authoritative)"),
    ]
    if r.get("outline") and not r.get("short_circuited"):
        parts.append("SECTIONS: " + " | ".join(r["outline"][:8]))
    if r.get("short_circuited"):
        parts.append("(page returned in full — it was short enough that "
                     "summarising it would have cost more than it saved)")
    parts.append("\nCONTENT:\n" + (r.get("extract") or "(nothing relevant)"))

    # Quotes are NOT reprinted. They are already inside CONTENT verbatim,
    # and duplicating them was costing ~400 tokens per fetch. They are kept
    # in the returned dict for the citation record and for computing
    # confidence — the model only needs the VERDICT, not the evidence.
    nv, nb = len(r.get("verified_quotes") or []), len(r.get("unverified_quotes") or [])
    if nv or nb:
        line = f"\nQUOTE CHECK: {nv} passage(s) matched the live page"
        if nb:
            line += (f"; {nb} did NOT match and were discarded — treat any "
                     f"claim resting on them as unverified")
        parts.append(line + ".")
    if not r.get("short_circuited"):
        parts.append(
            f"\n[Full page is {r.get('total_chars', 0)} chars, cached as "
            f"handle '{r.get('handle')}'. Call read_cached_page for more.]")
    return "\n".join(parts)