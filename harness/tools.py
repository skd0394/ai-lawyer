"""Tool registry — name -> (schema, handler, metadata).

MERN analogy: Express router + Zod. definitions() is the OpenAPI spec you
publish to the model; dispatch() is router.handle().

THE INVARIANT: dispatch() never raises. A tool error is information the
model can act on. An exception kills the turn.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ToolOut:
    ok: bool = True
    content: str = ""              # what the MODEL sees
    summary: str = ""              # short label for the UI chip
    payload: dict | None = None    # structured block for the UI (Agent B)


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    fn: Callable[[dict, Any], Awaitable[ToolOut] | ToolOut]
    # Halting tools END THE TURN when called. Agent B's ask_questions,
    # request_file and the two terminal tools. Day 5 uses this; the
    # mechanism belongs in the harness so both agents share it.
    halting: bool = False
    timeout: int = 60
    # Context control. An uncapped tool result is how a 200-page PDF ends
    # up costing you six times its size across loop iterations.
    max_result_chars: int = 8000


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None):
        self._specs: dict[str, ToolSpec] = {}
        for s in specs or []:
            self.add(s)

    def add(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def tool(self, name: str, description: str, schema: dict, **kw):
        """Decorator form. Handlers take (args: dict, ctx) -> ToolOut.

        Note it's (args, ctx), not (**args). Models occasionally emit keys
        that aren't in your schema; **args would TypeError on those, and a
        crash is worse than a handler that ignores an unknown field.
        """
        def deco(fn):
            self.add(ToolSpec(name=name, description=description,
                              schema=schema, fn=fn, **kw))
            return fn
        return deco

    # ── What the model sees ───────────────────────────────────────────────
    def definitions(self) -> list[dict]:
        """NEUTRAL form. The adapter converts to provider shape — tool
        definition formats differ between providers just like messages do."""
        return [
            {"name": s.name, "description": s.description, "schema": s.schema}
            for s in self._specs.values()
        ]

    def names(self) -> list[str]:
        return list(self._specs)

    def is_halting(self, name: str) -> bool:
        s = self._specs.get(name)
        return bool(s and s.halting)

    def subset(self, names: list[str]) -> "ToolRegistry":
        """Agent B gates tools by consultation phase. In TERMINAL phase the
        model literally cannot emit a question, because the tool isn't there.
        Structural enforcement beats asking the model nicely."""
        return ToolRegistry([self._specs[n] for n in names if n in self._specs])

    # ── Execution ─────────────────────────────────────────────────────────
    async def dispatch(self, name: str, args: dict, ctx: Any = None) -> ToolOut:
        spec = self._specs.get(name)
        if spec is None:
            # Tell the model what IS available so it can self-correct.
            return ToolOut(ok=False, summary=f"unknown tool {name}",
                           content=f"ERROR: no tool named '{name}'. "
                                   f"Available: {', '.join(self._specs)}")

        if args.get("_parse_error"):
            return ToolOut(ok=False, summary="malformed arguments",
                           content="ERROR: your tool arguments were not valid "
                                   "JSON. Please call the tool again.")
        try:
            # Async handlers run directly. SYNC handlers go through a thread
            # for two reasons:
            #   1. timeout protection — a sync call invoked inline would run
            #      to completion before wait_for could ever apply
            #   2. the event loop — sync network I/O (e.g. sandbox.exec) would
            #      block EVERY concurrent request in this container
            # Caveat: a timed-out thread cannot be killed; it keeps running
            # in the background. We stop waiting, we don't stop the work.
            # Acceptable here; a production version would use cancellable
            # subprocess calls instead.
            if inspect.iscoroutinefunction(spec.fn):
                coro = spec.fn(args, ctx)
            else:
                coro = asyncio.to_thread(spec.fn, args, ctx)
            result = await asyncio.wait_for(coro, timeout=spec.timeout)
            if not isinstance(result, ToolOut):
                result = ToolOut(content=str(result))
        except asyncio.TimeoutError:
            return ToolOut(ok=False, summary="timed out",
                           content=f"ERROR: {name} timed out after "
                                   f"{spec.timeout}s.")
        except Exception as e:
            # Deliberately broad. The model gets a readable error and can
            # recover; the turn survives.
            return ToolOut(ok=False, summary=f"{type(e).__name__}",
                           content=f"ERROR: {name} failed: "
                                   f"{type(e).__name__}: {e}")

        # Hard cap with an EXPLICIT marker, so the model knows there's more
        # rather than silently reasoning over a truncated document.
        if len(result.content) > spec.max_result_chars:
            kept = result.content[: spec.max_result_chars]
            result.content = (
                f"{kept}\n\n[TRUNCATED at {spec.max_result_chars} chars. "
                f"{len(result.content) - spec.max_result_chars} more remain. "
                f"Request a specific section if you need it.]"
            )
        if not result.summary:
            result.summary = (result.content[:80] or "ok")
        return result


def args_preview(args: dict, limit: int = 120) -> str:
    """For the UI chip: 'Searching: Illinois commercial eviction notice'."""
    try:
        s = json.dumps(args, default=str)
    except Exception:
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "..."