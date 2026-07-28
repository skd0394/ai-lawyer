"""Event vocabulary — the contract between adapter, loop, and UI.

Two tiers:
  ADAPTER -> LOOP   TextDelta, ToolUseStart, ToolUseEnd, Usage, StreamEnd
  LOOP -> BROWSER   TurnStart, TextDelta, ToolStart, ToolEnd, StructuredBlock,
                    Citation, Usage, Compaction, ErrorEvent, TurnEnd

Nothing in this file imports a provider SDK. That is deliberate and is the
thing that makes model-agnosticism real rather than claimed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from typing import Any, ClassVar


@dataclass
class Event:
    """Base. `type` is a ClassVar so it is not a constructor argument,
    but to_dict() puts it on the wire — the UI switches on it."""

    type: ClassVar[str] = "event"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type
        return d

    def to_sse(self) -> str:
        # The two trailing newlines terminate an SSE frame. Omit one and
        # the browser buffers forever waiting for the rest. Classic bug.
        return f"data: {json.dumps(self.to_dict(), default=str)}\n\n"


# ── Adapter -> Loop ───────────────────────────────────────────────────────

@dataclass
class TextDelta(Event):
    """One fragment of assistant prose. Also forwarded to the browser."""
    type: ClassVar[str] = "text_delta"
    text: str


@dataclass
class ToolUseStart(Event):
    """Model has begun requesting a tool. Args are NOT known yet — they
    stream in afterwards as partial JSON."""
    type: ClassVar[str] = "tool_use_start"
    id: str
    name: str


@dataclass
class ToolUseEnd(Event):
    """Tool request complete, args parsed. The loop acts on this."""
    type: ClassVar[str] = "tool_use_end"
    id: str
    name: str
    args: dict


@dataclass
class Usage(Event):
    """Token accounting for ONE model call. The scoreboard.

    cache_read is billed at a large discount; cache_creation at a small
    premium. Tracking them separately is why you can report honest costs
    on Day 7 instead of guessing from raw totals.
    """
    type: ClassVar[str] = "usage"
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float | None = None
    ms: int = 0

    @property
    def total_input(self) -> int:
        return (self.input_tokens
                + self.cache_read_input_tokens
                + self.cache_creation_input_tokens)


@dataclass
class StreamEnd(Event):
    """One model call finished. NOT the same as the turn finishing —
    a turn is many calls."""
    type: ClassVar[str] = "stream_end"
    stop_reason: str | None = None


# ── Loop -> Browser ───────────────────────────────────────────────────────

@dataclass
class TurnStart(Event):
    type: ClassVar[str] = "turn_start"
    turn_id: str
    session_id: str
    agent: str = "A"


@dataclass
class ToolStart(Event):
    """Emitted when execution begins. This is what powers the live
    'Searching Illinois eviction notice...' chips in the UI — the single
    most convincing thing in the demo."""
    type: ClassVar[str] = "tool_start"
    tool_call_id: str
    name: str
    args_preview: str = ""


@dataclass
class ToolEnd(Event):
    type: ClassVar[str] = "tool_end"
    tool_call_id: str
    name: str
    ok: bool
    summary: str = ""
    ms: int = 0


@dataclass
class StructuredBlock(Event):
    """Agent B's native UI payloads: question forms, file requests,
    findings panels, terminal handoffs."""
    type: ClassVar[str] = "structured_block"
    kind: str          # question_form | file_request | findings | handoff | conclusion
    payload: dict = field(default_factory=dict)


@dataclass
class Citation(Event):
    """confidence is user-visible and required by the spec:
    'source located and read' vs 'couldn't fully verify, check yourself'."""
    type: ClassVar[str] = "citation"
    url: str
    title: str = ""
    confidence: str = "unverified"     # verified | unverified
    quote: str = ""


@dataclass
class Compaction(Event):
    type: ClassVar[str] = "compaction"
    tokens_before: int
    tokens_after: int


@dataclass
class ErrorEvent(Event):
    """Spec: 'provider errors surface cleanly instead of hanging.'
    An error is an EVENT, not an exception that kills the stream."""
    type: ClassVar[str] = "error"
    code: str
    message: str
    retryable: bool = False


@dataclass
class TurnEnd(Event):
    type: ClassVar[str] = "turn_end"
    stop_reason: str   # stop | awaiting_user | cancelled | max_iterations | error
    total_ms: int = 0
    ttft_ms: int | None = None
    billed_input_tokens: int = 0
    billed_output_tokens: int = 0
    cost_usd: float = 0.0