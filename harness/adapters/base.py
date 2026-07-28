"""The ModelAdapter contract.

An adapter does four things:
  1. stream()               provider stream -> our Event vocabulary
  2. assistant_message()    our events -> a provider-shaped assistant message
  3. tool_result_message()  tool outputs -> a provider-shaped user message
  4. count_tokens()         measure before sending (budget enforcement)

2 and 3 matter more than they look. Message SHAPES are provider-specific
(Anthropic nests tool_result blocks inside a user message; OpenAI uses a
separate 'tool' role). If the loop built messages, the loop would know
about providers. Keeping construction here is what lets loop.py stay
completely provider-blind.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable

from ..events import Event, ToolUseEnd


class ToolResult:
    """Neutral tool outcome. Deliberately not provider-shaped."""

    def __init__(self, call_id: str, name: str, content: str, ok: bool = True):
        self.call_id = call_id
        self.name = name
        self.content = content
        self.ok = ok


@runtime_checkable
class ModelAdapter(Protocol):
    name: str

    def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        system: Any = None,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[Event]:
        """Yield TextDelta / ToolUseStart / ToolUseEnd / Usage / StreamEnd."""
        ...

    def format_tools(self, defs: list[dict], cache_last: bool = False) -> list[dict] | None:
        """Neutral [{name, description, schema}] -> provider tool format.

        Anthropic wants `input_schema`; OpenAI nests under
        `function.parameters`. Same reason message construction lives here:
        the loop must not know which provider it is talking to.
        """
        ...

    def format_system(self, system: Any, cache: bool = False) -> Any:
        """Plain system text -> provider format, optionally cache-marked."""
        ...

    def assistant_message(self, text: str, tool_calls: list[ToolUseEnd]) -> dict:
        ...

    def tool_result_message(self, results: list[ToolResult]) -> dict:
        ...

    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[dict],
        system: Any = None,
        tools: list[dict] | None = None,
    ) -> int:
        ...