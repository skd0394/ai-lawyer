"""Agent B's structured-interaction tools.

These are ordinary tools with halting=True. The loop already knows how to
break on them and discard trailing output (built on Day 2), so the
"stops cleanly with no trailing text" requirement needs no new machinery.

Validation happens HERE, in code. "Ask at most four questions" and "always
include help text" are enforced by rejecting the call back to the model,
not by hoping the prompt holds.
"""

from __future__ import annotations

from typing import Any

MAX_QUESTIONS_PER_BATCH = 4
MIN_HELP_TEXT_CHARS = 25

QUESTION_TYPES = ("text", "long_text", "choice", "multi_choice",
                  "number", "date", "yes_no")

ASK_QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "context": {
            "type": "string",
            "description": "one sentence of orientation shown above the form",
        },
        "questions": {
            "type": "array",
            "description": f"{MAX_QUESTIONS_PER_BATCH} maximum",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string",
                           "description": "stable snake_case key"},
                    "prompt": {"type": "string"},
                    "type": {"type": "string", "enum": list(QUESTION_TYPES)},
                    "options": {"type": "array", "items": {"type": "string"},
                                "description": "for choice / multi_choice"},
                    "help_text": {"type": "string",
                                  "description": "why this is being asked "
                                                 "and what turns on it"},
                    "required": {"type": "boolean"},
                },
                "required": ["id", "prompt", "type", "help_text"],
            },
        },
    },
    "required": ["questions"],
}


def validate_questions(args: dict) -> tuple[list[dict], list[str]]:
    """Returns (clean_questions, errors). Errors go back to the model as a
    tool error so it can correct itself."""
    errors: list[str] = []
    qs = args.get("questions") or []

    if not isinstance(qs, list) or not qs:
        return [], ["`questions` must be a non-empty array"]

    if len(qs) > MAX_QUESTIONS_PER_BATCH:
        errors.append(
            f"{len(qs)} questions asked; the maximum is "
            f"{MAX_QUESTIONS_PER_BATCH}. Ask the most important "
            f"{MAX_QUESTIONS_PER_BATCH} now and the rest in a later batch.")

    clean, seen = [], set()
    for i, q in enumerate(qs[:MAX_QUESTIONS_PER_BATCH]):
        if not isinstance(q, dict):
            errors.append(f"question {i} is not an object")
            continue

        qid = (q.get("id") or "").strip()
        if not qid:
            errors.append(f"question {i} has no id")
            continue
        if qid in seen:
            errors.append(f"duplicate question id '{qid}'")
            continue
        seen.add(qid)

        qtype = (q.get("type") or "text").strip()
        if qtype not in QUESTION_TYPES:
            errors.append(f"'{qid}': unknown type '{qtype}'. "
                          f"Use one of: {', '.join(QUESTION_TYPES)}")
            continue

        help_text = (q.get("help_text") or "").strip()
        if len(help_text) < MIN_HELP_TEXT_CHARS:
            # The spec requires non-lawyers to understand what is being
            # asked AND why. A one-liner restating the question fails that.
            errors.append(
                f"'{qid}': help_text is missing or too short. Explain why "
                f"you are asking and what turns on the answer, in plain "
                f"English.")
            continue

        options = q.get("options") or []
        if qtype in ("choice", "multi_choice"):
            if len(options) < 2:
                errors.append(f"'{qid}': {qtype} needs at least 2 options")
                continue
        elif qtype == "yes_no":
            options = ["Yes", "No", "Not sure"]
        else:
            options = []

        clean.append({
            "id": qid,
            "prompt": (q.get("prompt") or "").strip(),
            "type": qtype,
            "options": options,
            "help_text": help_text,
            "required": bool(q.get("required", True)),
        })

    return clean, errors


def already_answered(questions: list[dict], collected: dict) -> list[str]:
    return [q["id"] for q in questions if q["id"] in (collected or {})]


# ── history compaction ────────────────────────────────────────────────────
def compact_question_history(messages: list[dict],
                             collected: dict) -> tuple[list[dict], int]:
    """Collapse fully-answered question batches out of history.

    An ask_questions call is ~1,000 output tokens of prompts, options and
    help text. That block then sits in history and is re-sent as INPUT on
    every subsequent turn.

    ⚠️ The obvious implementation is WRONG. Rewriting the tool_use `input`
    to something compact leaves a malformed example of the call in history,
    and the model imitates it — we observed it calling ask_questions with
    {"_answered": [...]}, which is the compaction shape, not the schema.
    History is few-shot context; do not put invalid examples in it.

    So instead we remove the tool_use/tool_result PAIR entirely and leave a
    plain assistant note. No tool_use means no tool_result obligation, so
    the session stays valid, nothing malformed is modelled, and the saving
    is larger.

    Returns (messages, chars_saved).
    """
    def _blocks(m):
        c = m.get("content")
        return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []

    out: list[dict] = []
    saved = 0
    i = 0

    while i < len(messages):
        msg = messages[i]
        blocks = _blocks(msg)

        uses = [b for b in blocks if b.get("type") == "tool_use"]
        is_lone_ask = (msg.get("role") == "assistant"
                       and len(uses) == 1
                       and uses[0].get("name") == "ask_questions")

        if is_lone_ask:
            qs = (uses[0].get("input") or {}).get("questions") or []
            ids = [q.get("id") for q in qs if isinstance(q, dict) and q.get("id")]
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            nxt_blocks = _blocks(nxt) if nxt else []
            results_only = bool(nxt_blocks) and all(
                b.get("type") == "tool_result" for b in nxt_blocks)
            answers_this = {b.get("tool_use_id") for b in nxt_blocks} == {
                uses[0].get("id")}

            # Only collapse a batch every question of which is answered,
            # and only when the very next message is just its result.
            if ids and all(x in collected for x in ids) and results_only \
                    and answers_this:
                before = len(str(msg)) + len(str(nxt))
                note = {"role": "assistant",
                        "content": [{"type": "text",
                                     "text": f"[Asked and received answers "
                                             f"for: {', '.join(ids)}]"}]}
                out.append(note)
                saved += before - len(str(note))
                i += 2
                continue

        out.append(msg)
        i += 1

    return out, saved