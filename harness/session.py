"""Session logic — PURE functions over lists.

No Modal, no filesystem, no network. That's deliberate: if session logic
depended on Modal, harness/ wouldn't be portable and the architectural
claim would be false. Storage lives in infra/session_store.py.

Model:
    turns.jsonl   one JSON line per turn
    each line     {turn_id, at, prompt, messages: [...], stop_reason, usage}

rebuild_messages() is a projection over that log — replay it and you have
the full context, which is how a session survives sandbox recycling.
"""

from __future__ import annotations

from typing import Any


def _blocks(msg: dict) -> list[dict]:
    c = msg.get("content")
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def tool_use_ids(msg: dict) -> list[str]:
    return [b["id"] for b in _blocks(msg)
            if b.get("type") == "tool_use" and b.get("id")]


def tool_result_ids(msg: dict | None) -> set[str]:
    if not msg:
        return set()
    return {b["tool_use_id"] for b in _blocks(msg)
            if b.get("type") == "tool_result" and b.get("tool_use_id")}


def rebuild_messages(turns: list[dict]) -> list[dict]:
    """Replay the log into a message array ready to send to the model."""
    out: list[dict] = []
    for t in turns:
        out.extend(t.get("messages") or [])
    repaired, _ = repair_messages(out)
    return repaired


def repair_messages(messages: list[dict]) -> tuple[list[dict], list[str]]:
    """Guarantee every tool_use has a matching tool_result.

    ⭐ THE FUNCTION THAT PREVENTS PERMANENT SESSION CORRUPTION.

    A cancelled turn or a dead container can leave a tool_use block with no
    result. The API rejects that on the NEXT request — and every request
    after it, forever. T2.5's validator only detected the problem; this
    fixes it by synthesising the missing results.

    Returns (repaired_messages, orphaned_ids_that_were_fixed).
    """
    out: list[dict] = []
    orphans: list[str] = []
    i = 0

    while i < len(messages):
        msg = messages[i]
        out.append(msg)
        ids = tool_use_ids(msg)

        if not ids:
            i += 1
            continue

        nxt = messages[i + 1] if i + 1 < len(messages) else None
        answered = tool_result_ids(nxt)
        missing = [x for x in ids if x not in answered]

        synth = [
            {
                "type": "tool_result",
                "tool_use_id": x,
                "content": "Tool result unavailable: the turn was "
                           "interrupted before this tool finished.",
                "is_error": True,
            }
            for x in missing
        ]

        if nxt is not None and answered:
            # There IS a results message — merge the missing ones into it.
            if synth:
                orphans.extend(missing)
                out.append({**nxt, "content": _blocks(nxt) + synth})
            else:
                out.append(nxt)
            i += 2
            continue

        # No results message at all (turn died right after the tool call).
        if synth:
            orphans.extend(missing)
            out.append({"role": "user", "content": synth})
        i += 1

    return out, orphans


def validate_messages(messages: list[dict]) -> dict:
    """Read-only check. Used in tests and to prove cancellation is clean."""
    pending: set[str] = set()
    for m in messages:
        for b in _blocks(m):
            if b.get("type") == "tool_use":
                pending.add(b.get("id"))
            elif b.get("type") == "tool_result":
                pending.discard(b.get("tool_use_id"))
    return {"valid": not pending, "orphaned_tool_use_ids": sorted(pending)}


def transcript(turns: list[dict]) -> list[dict]:
    """Flat ordered transcript for the UI to rehydrate from.

    Deliberately NOT the raw message array — the UI wants readable turns
    with tool activity summarised, not tool_use blocks to interpret.
    """
    items: list[dict] = []
    for t in turns:
        items.append({"role": "user", "text": t.get("prompt", ""),
                      "turn_id": t.get("turn_id"), "at": t.get("at")})
        text_parts, tools = [], []
        for m in t.get("messages") or []:
            if m.get("role") != "assistant":
                continue
            for b in _blocks(m):
                if b.get("type") == "text" and b.get("text", "").strip():
                    text_parts.append(b["text"])
                elif b.get("type") == "tool_use":
                    tools.append(b.get("name"))
        items.append({
            "role": "assistant",
            "text": "".join(text_parts),
            "tools_used": tools,
            "turn_id": t.get("turn_id"),
            "stop_reason": t.get("stop_reason"),
            "usage": t.get("usage"),
        })
    return items


def count_context(messages: list[dict]) -> int:
    """Rough character count — a cheap proxy for 'is context getting big?'
    without an API round trip. Real token counts come from the adapter."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        else:
            for b in _blocks(m):
                total += len(str(b.get("text") or b.get("content") or ""))
    return total