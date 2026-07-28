"""Turn tracing. A turn is a trace; each model/tool call is a span.

The tracer is a PURE CONSUMER of the event stream — it never touches the
loop. That falls straight out of having defined the event vocabulary first
(T2.1): everything observability needs is already on the wire.

    tracer = Tracer(turn_id=..., session_id=..., user_id=...)
    async for ev in run_turn(...):
        tracer.observe(ev)
        forward(ev)
    trace = tracer.finish()
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pricing import estimate_cost

# Anything longer gets clipped before it lands in a trace file. Traces get
# screenshotted in demos; a user's uploaded contract should not be in them.
MAX_LOGGED_VALUE = 300
SECRET_HINTS = ("key", "token", "secret", "password", "authorization", "api")


def redact(obj: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "<deep>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(h in str(k).lower() for h in SECRET_HINTS):
                out[k] = "<redacted>"
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, depth + 1) for v in obj[:20]]
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_LOGGED_VALUE else (
            obj[:MAX_LOGGED_VALUE] + f"...<+{len(obj) - MAX_LOGGED_VALUE} chars>")
    return obj


@dataclass
class Span:
    kind: str                    # generation | tool | error | structured
    name: str
    ms: int = 0
    data: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)


class Tracer:
    def __init__(self, turn_id: str, session_id: str = "", user_id: str = "",
                 agent: str = "A", trace_dir: str | None = "/traces"):
        self.turn_id = turn_id
        self.session_id = session_id
        self.user_id = user_id
        self.agent = agent
        self.trace_dir = trace_dir
        self.started_at = time.time()
        self.spans: list[Span] = []
        self._tool_starts: dict[str, float] = {}
        self.totals = {"input_tokens": 0, "output_tokens": 0,
                       "cache_read": 0, "cache_write": 0, "cost_usd": 0.0}
        self.by_model: dict[str, dict] = {}
        self.pricing_verified = True
        self.pricing_notes: set[str] = set()
        self.stop_reason: str | None = None
        self.ttft_ms: int | None = None

    # ── the entire integration surface ────────────────────────────────────
    def observe(self, ev) -> None:
        """Consume one event. Mutates Usage/TurnEnd to backfill cost, so the
        client sees real dollars instead of the null you've been getting."""
        t = ev.type

        if t == "usage":
            c = estimate_cost(
                ev.model,
                input_tokens=ev.input_tokens,
                output_tokens=ev.output_tokens,
                cache_read=ev.cache_read_input_tokens,
                cache_write=ev.cache_creation_input_tokens,
            )
            ev.cost_usd = c["cost_usd"]          # backfill for the UI badge
            if not c["pricing_verified"]:
                self.pricing_verified = False
            self.pricing_notes.add(c["pricing_note"])

            self.totals["input_tokens"] += ev.input_tokens
            self.totals["output_tokens"] += ev.output_tokens
            self.totals["cache_read"] += ev.cache_read_input_tokens
            self.totals["cache_write"] += ev.cache_creation_input_tokens
            self.totals["cost_usd"] += (c["cost_usd"] or 0.0)

            m = self.by_model.setdefault(
                ev.model, {"calls": 0, "input": 0, "output": 0,
                           "cache_read": 0, "cache_write": 0, "cost_usd": 0.0,
                           "ms": 0})
            m["calls"] += 1
            m["input"] += ev.input_tokens
            m["output"] += ev.output_tokens
            m["cache_read"] += ev.cache_read_input_tokens
            m["cache_write"] += ev.cache_creation_input_tokens
            m["cost_usd"] += (c["cost_usd"] or 0.0)
            m["ms"] += ev.ms

            self.spans.append(Span(kind="generation", name=ev.model, ms=ev.ms,
                                   data={"input": ev.input_tokens,
                                         "output": ev.output_tokens,
                                         "cache_read": ev.cache_read_input_tokens,
                                         "cache_write": ev.cache_creation_input_tokens,
                                         "cost_usd": c["cost_usd"]}))

        elif t == "tool_start":
            self._tool_starts[ev.tool_call_id] = time.time()
            self.spans.append(Span(kind="tool_start", name=ev.name,
                                   data={"args": redact(ev.args_preview)}))

        elif t == "tool_end":
            self.spans.append(Span(kind="tool", name=ev.name, ms=ev.ms,
                                   data={"ok": ev.ok,
                                         "summary": redact(ev.summary)}))

        elif t == "structured_block":
            self.spans.append(Span(kind="structured", name=ev.kind,
                                   data={"payload": redact(ev.payload)}))

        elif t == "error":
            self.spans.append(Span(kind="error", name=ev.code,
                                   data={"message": redact(ev.message)}))

        elif t == "turn_end":
            self.stop_reason = ev.stop_reason
            self.ttft_ms = ev.ttft_ms
            ev.cost_usd = round(self.totals["cost_usd"], 6)

    # ── output ────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        tool_ms = sum(s.ms for s in self.spans if s.kind == "tool")
        model_ms = sum(s.ms for s in self.spans if s.kind == "generation")
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "agent": self.agent,
            "started_at": self.started_at,
            "total_ms": int((time.time() - self.started_at) * 1000),
            "ttft_ms": self.ttft_ms,
            "stop_reason": self.stop_reason,
            "totals": {**self.totals,
                       "cost_usd": round(self.totals["cost_usd"], 6)},
            "by_model": self.by_model,
            # Where the wall clock actually went. On Day 7 this tells you
            # whether latency is the model's fault or your tools'.
            "latency_breakdown": {"model_ms": model_ms, "tool_ms": tool_ms},
            "pricing_verified": self.pricing_verified,
            "pricing_notes": sorted(self.pricing_notes),
            "spans": [{"kind": s.kind, "name": s.name, "ms": s.ms,
                       "at": s.at, **s.data} for s in self.spans],
        }

    def finish(self) -> dict:
        trace = self.to_dict()
        if self.trace_dir:
            try:
                d = Path(self.trace_dir)
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{self.turn_id}.json").write_text(json.dumps(trace, default=str))
                # Append-only index so listing doesn't require reading
                # every trace file.
                with open(d / "index.jsonl", "a") as f:
                    f.write(json.dumps({
                        "turn_id": self.turn_id, "user_id": self.user_id,
                        "session_id": self.session_id, "agent": self.agent,
                        "at": self.started_at, "stop_reason": self.stop_reason,
                        "cost_usd": trace["totals"]["cost_usd"],
                        "total_ms": trace["total_ms"],
                    }) + "\n")
            except Exception as e:
                # Never let observability break the request it observes.
                trace["persist_error"] = f"{type(e).__name__}: {e}"
        return trace