"""Anthropic implementation of ModelAdapter."""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from ..events import Event, StreamEnd, TextDelta, ToolUseEnd, ToolUseStart, Usage
from .base import ToolResult

# Retry only on transient failures. A 400 (bad request) is YOUR bug and
# retrying it just wastes time and money.
RETRYABLE = ("rate_limit", "overloaded", "api_connection", "internal_server",
             "timeout", "503", "529", "429", "500")


def _is_retryable(exc: Exception) -> bool:
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(k in blob for k in RETRYABLE)


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, max_retries: int = 3):
        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        self._max_retries = max_retries

    # ── 1. stream ─────────────────────────────────────────────────────────
    async def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        system: Any = None,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[Event]:
        kwargs: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens, "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        attempt = 0
        while True:
            emitted = False          # have we yielded anything downstream?
            t0 = time.perf_counter()
            try:
                # `partial` accumulates tool arguments. They arrive as
                # FRAGMENTS OF A JSON STRING, not as objects:
                #   {"que    ry": "Illi    nois evic    tion"}
                # Parsing before content_block_stop => JSONDecodeError.
                # This is the #1 streaming-tool-use bug.
                partial: dict[int, dict] = {}

                async with self._client.messages.stream(**kwargs) as stream:
                    async for ev in stream:
                        t = ev.type

                        if t == "content_block_start":
                            cb = ev.content_block
                            if cb.type == "tool_use":
                                partial[ev.index] = {
                                    "id": cb.id, "name": cb.name, "json": ""
                                }
                                emitted = True
                                yield ToolUseStart(id=cb.id, name=cb.name)

                        elif t == "content_block_delta":
                            d = ev.delta
                            if d.type == "text_delta":
                                emitted = True
                                yield TextDelta(text=d.text)
                            elif d.type == "input_json_delta":
                                if ev.index in partial:
                                    partial[ev.index]["json"] += d.partial_json

                        elif t == "content_block_stop":
                            p = partial.pop(ev.index, None)
                            if p is not None:
                                raw = p["json"].strip()
                                try:
                                    args = json.loads(raw) if raw else {}
                                except json.JSONDecodeError:
                                    # Return it as a tool error the model can
                                    # see and retry, rather than crashing.
                                    args = {"_parse_error": True, "_raw": raw}
                                yield ToolUseEnd(id=p["id"], name=p["name"],
                                                 args=args)

                    final = await stream.get_final_message()

                u = final.usage
                yield Usage(
                    model=final.model,
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    cache_read_input_tokens=getattr(
                        u, "cache_read_input_tokens", 0) or 0,
                    cache_creation_input_tokens=getattr(
                        u, "cache_creation_input_tokens", 0) or 0,
                    ms=int((time.perf_counter() - t0) * 1000),
                )
                yield StreamEnd(stop_reason=final.stop_reason)
                return

            except Exception as exc:
                # Once we've streamed tokens to the user we CANNOT silently
                # retry — they'd see the answer restart mid-sentence. Only
                # retry a stream that failed before producing anything.
                if emitted or attempt >= self._max_retries or not _is_retryable(exc):
                    raise
                attempt += 1
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.5)
                await asyncio.sleep(backoff)

    # ── 2 & 3. message construction (provider-specific, lives here) ───────
    def assistant_message(self, text: str, tool_calls: list[ToolUseEnd]) -> dict:
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        for c in tool_calls:
            content.append({"type": "tool_use", "id": c.id,
                            "name": c.name, "input": c.args})
        # An assistant turn may never be empty.
        return {"role": "assistant", "content": content or [{"type": "text",
                                                             "text": " "}]}

    def tool_result_message(self, results: list[ToolResult]) -> dict:
        # Anthropic nests tool results in a USER message. OpenAI uses a
        # separate 'tool' role. Exactly the difference the loop shouldn't know.
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    "is_error": not r.ok,
                }
                for r in results
            ],
        }

    # ── 4. count_tokens ───────────────────────────────────────────────────
    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[dict],
        system: Any = None,
        tools: list[dict] | None = None,
    ) -> int:
        """Measure before sending. This powers scripts/budget.py, which
        enforces the <1500 system-prompt / <1200 tool-definition limits."""
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        r = await self._client.messages.count_tokens(**kwargs)
        return r.input_tokens