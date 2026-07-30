"""The agent loop. Provider-blind, law-blind, reusable by both agents.

Grep this file for 'anthropic' or 'legal' — you should find neither. That
is the whole architectural claim, and it's testable.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, AsyncIterator, Callable

from .adapters.base import ModelAdapter, ToolResult
from .events import (
    Compaction, ErrorEvent, Event, StreamEnd, StructuredBlock, TextDelta,
    ToolEnd, ToolStart, ToolUseEnd, ToolUseStart, TurnEnd, TurnStart, Usage,
)
from .tools import ToolRegistry, args_preview


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _resolve(registry):
    """`registry` may be a ToolRegistry or a CALLABLE returning one.

    The callable form exists because a tool can change what tools should
    exist — Agent B's advance_phase does exactly that. Resolving once per
    turn meant the model was told it had new tools while the loop still
    held the old set, and every call failed with "unknown tool".

    Note this can invalidate the tool cache breakpoint when the set
    changes. That is correct: different tools are a different prefix.
    """
    return registry() if callable(registry) else registry


async def run_turn(
    *,
    adapter: ModelAdapter,
    model: str,
    messages: list[dict],
    registry: ToolRegistry,
    system: Any = None,
    ctx: Any = None,
    max_iterations: int = 12,
    # 4096 truncates legal documents mid-draft. A real operating agreement
    # is 3,000-6,000 words. You are billed for tokens GENERATED, not for
    # the ceiling, so a high limit costs nothing until it is used.
    max_tokens: int = 16000,
    cancel_check: Callable[[], bool] | None = None,
    turn_id: str | None = None,
    session_id: str = "",
    agent: str = "A",
    cache: bool = False,
) -> AsyncIterator[Event]:
    """Run one turn to completion, yielding events as they happen.

    NOTE: `messages` is mutated in place. The caller persists it after the
    turn — that's how a session survives sandbox recycling.

    Stop reasons:
      stop            model finished talking
      awaiting_user   a halting tool fired (Agent B asked something)
      cancelled       user hit cancel
      max_iterations  safety cap — always surfaced, never silent
      error           provider failed after retries
    """
    turn_id = turn_id or uuid.uuid4().hex[:12]
    t_start = time.perf_counter()
    ttft_ms: int | None = None
    billed_in = billed_out = 0
    stop_reason = "max_iterations"

    # Both breakpoints sit on content that is byte-identical for the whole
    # session, so iterations 2..N read them from cache instead of paying
    # full price to reprocess the same bytes every time.
    system_fmt = adapter.format_system(system, cache=cache)

    yield TurnStart(turn_id=turn_id, session_id=session_id, agent=agent)

    for _iteration in range(max_iterations):
        # Cancel between iterations. Checking mid-stream would mean
        # abandoning a partially-appended message and corrupting history.
        if cancel_check and cancel_check():
            stop_reason = "cancelled"
            break

        # Re-resolved every iteration: a tool may have changed which tools
        # should exist.
        current = _resolve(registry)
        tool_defs = adapter.format_tools(current.definitions(),
                                         cache_last=cache)

        text_parts: list[str] = []
        tool_calls: list[ToolUseEnd] = []
        api_stop: str | None = None

        try:
            async for ev in adapter.stream(
                model=model, messages=messages, system=system_fmt,
                tools=tool_defs, max_tokens=max_tokens,
            ):
                if isinstance(ev, TextDelta):
                    if ttft_ms is None:
                        ttft_ms = _ms(t_start)
                    text_parts.append(ev.text)
                    yield ev
                elif isinstance(ev, ToolUseStart):
                    if ttft_ms is None:
                        ttft_ms = _ms(t_start)
                    # internal: args aren't known yet, nothing to show
                elif isinstance(ev, ToolUseEnd):
                    tool_calls.append(ev)
                elif isinstance(ev, Usage):
                    billed_in += ev.total_input
                    billed_out += ev.output_tokens
                    yield ev
                elif isinstance(ev, StreamEnd):
                    # The PROVIDER's stop reason, distinct from the loop's.
                    # "max_tokens" here means the model was cut off
                    # mid-sentence — silently discarding it is how a
                    # truncated contract reaches a user looking complete.
                    api_stop = ev.stop_reason
        except Exception as e:
            # Spec: "provider errors surface cleanly instead of hanging."
            # An error is an EVENT. The stream closes tidily and the session
            # stays valid, because we haven't appended anything yet.
            yield ErrorEvent(code="provider_error",
                             message=f"{type(e).__name__}: {e}",
                             retryable=True)
            stop_reason = "error"
            break

        text = "".join(text_parts)
        messages.append(adapter.assistant_message(text, tool_calls))

        if api_stop == "max_tokens":
            yield ErrorEvent(
                code="output_truncated",
                message=(f"The model hit the {max_tokens}-token output limit "
                         f"and was cut off mid-generation. The result is "
                         f"incomplete."),
                retryable=True)

        if not tool_calls:
            stop_reason = "truncated" if api_stop == "max_tokens" else "stop"
            break

        # ── Execute tools ─────────────────────────────────────────────────
        results: list[ToolResult] = []
        halted = False

        for call in tool_calls:
            if halted:
                # CRITICAL: every tool_use block must get a matching
                # tool_result or the NEXT request 400s and the session is
                # permanently broken. When a halting tool ends the turn,
                # remaining calls still need an answer — a synthetic one.
                results.append(ToolResult(
                    call_id=call.id, name=call.name,
                    content="Skipped: the turn ended when a prior tool "
                            "halted it.", ok=True))
                continue

            yield ToolStart(tool_call_id=call.id, name=call.name,
                            args_preview=args_preview(call.args))
            t0 = time.perf_counter()
            out = await current.dispatch(call.name, call.args, ctx)
            elapsed = _ms(t0)
            yield ToolEnd(tool_call_id=call.id, name=call.name, ok=out.ok,
                          summary=out.summary, ms=elapsed)

            if out.payload is not None:
                yield StructuredBlock(
                    kind=out.payload.get("kind", call.name),
                    payload=out.payload)

            # Forward anything the tool produced. The loop does not inspect
            # these — citations are an agent concern, not a harness one.
            for extra in (out.events or []):
                yield extra

            results.append(ToolResult(call_id=call.id, name=call.name,
                                      content=out.content, ok=out.ok))

            if current.is_halting(call.name):
                halted = True

        messages.append(adapter.tool_result_message(results))

        if halted:
            # Agent B: "when it asks or concludes, it stops cleanly with no
            # trailing text or extra tool calls." Anything the model might
            # have said after the halting call never gets requested.
            stop_reason = "awaiting_user"
            break

        if cancel_check and cancel_check():
            stop_reason = "cancelled"
            break

    yield TurnEnd(
        stop_reason=stop_reason,
        total_ms=_ms(t_start),
        ttft_ms=ttft_ms,
        billed_input_tokens=billed_in,
        billed_output_tokens=billed_out,
    )