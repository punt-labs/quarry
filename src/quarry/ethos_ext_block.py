"""The versioned quarry memory guide spliced into an ethos identity's ext file."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self, final

MEMORY_GUIDE_HEADER: Final = "## Memory (quarry guide v2)"

# The block ethos injects at SessionStart, PreCompact, and SubagentStart. Its
# first line is the version header the refresh keys on; the body says WHEN to
# remember (the five moments), what never to remember, and why the handle is
# always the agent's own statement.
_GUIDE_TEMPLATE: Final = f"""\
{MEMORY_GUIDE_HEADER}

You have persistent memory in quarry. It is cross-project and cross-machine:
what you remember here is findable from any repo, by you and by teammates.

Collection: "{{memory_collection}}"  ·  Handle: "{{handle}}"

Recall — before answering a why/how/what-did-we-decide question:
  find(query, agent_handle="{{handle}}")   only your own memories
  find(query)                       everything: teammates' memories, lessons, captures

Persist — call remember at these moments, not at the end and not never:
  1. you found a non-obvious root cause or gotcha           memory_type="fact"
  2. a design decision was ratified, with its reason         memory_type="fact"
  3. you worked out a repeatable how-to                      memory_type="procedure"
  4. you formed a judgement you will want to revisit         memory_type="opinion"
  5. before submitting a mission result, one note on what
     you would tell yourself next time                       memory_type="observation"

  remember(content, document_name="<topic>-<slug>", agent_handle="{{handle}}",
           memory_type=..., summary="<one line>")

Always pass agent_handle="{{handle}}". The daemon cannot infer your identity — a
subagent's working directory resolves to the repo's leader, not to you.

Do not remember progress narration, file contents, tool output, or anything
already in the repo. A rule the whole project should follow is a lesson:
learn(lesson) — project-scoped, no handle, retrieval preference.

Memory types: fact = objective, verifiable · observation = neutral summary ·
procedure = how-to · opinion = subjective assessment with confidence.
Memories decay with a 30-day half-life; lessons and documents do not.
"""

_KEY_LINE: Final = re.compile(r"^session_context:(?P<value>.*)$")
_LITERAL_INDICATOR: Final = re.compile(r"^\s*\|[+-]?\s*$")


class BlockState(StrEnum):
    """What the refresh finds at the ``session_context`` key."""

    ABSENT = "absent"
    STALE = "stale"
    CURRENT = "current"
    CUSTOM = "custom"


@final
@dataclass(frozen=True, slots=True)
class SessionContextBlock:
    """The ``session_context`` entry of an ext file, located on the raw text.

    The file is never round-tripped through ``yaml.dump`` — that would destroy
    comments and key order — so the block is a line range: the key line
    through the last indented-or-blank line of its literal scalar. A key whose
    value is not a literal block (an inline string) is a hand-authored entry
    and is classified :attr:`BlockState.CUSTOM`, never rewritten.
    """

    _lines: tuple[str, ...]
    # The key line's index; ``None`` is the documented "no session_context key
    # in this file" contract, not a failure.
    _start: int | None
    _end: int

    @classmethod
    def locate(cls, raw: str) -> Self:
        """Find the block in *raw*; line endings are kept so a splice preserves them."""
        lines = tuple(raw.splitlines(keepends=True))
        for index, line in enumerate(lines):
            if _KEY_LINE.match(line.rstrip("\r\n")):
                return cls(lines, index, cls._block_end(lines, index))
        return cls(lines, None, len(lines))

    @staticmethod
    def _block_end(lines: tuple[str, ...], start: int) -> int:
        """Return the exclusive end: the first column-0 line after the key."""
        end = start + 1
        while end < len(lines) and (lines[end].strip() == "" or lines[end][0] == " "):
            end += 1
        return end

    @property
    def state(self) -> BlockState:
        """Classify the block by its first body line."""
        if self._start is None:
            return BlockState.ABSENT
        if not self._is_literal():
            return BlockState.CUSTOM
        first = self._first_body_line()
        if first == MEMORY_GUIDE_HEADER:
            return BlockState.CURRENT
        if first.startswith("## Memory"):
            return BlockState.STALE
        return BlockState.CUSTOM

    def _is_literal(self) -> bool:
        """Return whether the key's value is a ``|`` literal block indicator."""
        if self._start is None:
            return False
        match = _KEY_LINE.match(self._lines[self._start].rstrip("\r\n"))
        return (
            match is not None and _LITERAL_INDICATOR.match(match["value"]) is not None
        )

    def _first_body_line(self) -> str:
        """Return the first non-blank body line, stripped, or ``""``."""
        if self._start is None:
            return ""
        for line in self._lines[self._start + 1 : self._end]:
            if line.strip():
                return line.strip()
        return ""

    @property
    def needs_guide(self) -> bool:
        """Return whether the refresh should write the current guide here."""
        return self.state in (BlockState.ABSENT, BlockState.STALE)

    def with_guide(self, handle: str, memory_collection: str) -> str:
        """Return the file text with the current guide appended or spliced in.

        An absent block is appended after the existing content; a stale block
        is replaced in place, so keys before and after it are untouched.
        """
        fragment = self.render(handle, memory_collection)
        if self._start is None:
            # The leading newline both completes an unterminated last line and
            # separates the block from a terminated one, as the v1 append did.
            return "".join(self._lines) + "\n" + fragment
        before = "".join(self._lines[: self._start])
        after = "".join(self._lines[self._end :])
        return before + fragment + after

    @staticmethod
    def render(handle: str, memory_collection: str) -> str:
        """Return the ``session_context: |`` fragment for *handle*.

        Each body line is indented two spaces as a YAML literal block scalar
        requires; the fragment ends with a newline so a following key starts
        at column 0.
        """
        body = _GUIDE_TEMPLATE.format(
            handle=handle, memory_collection=memory_collection
        )
        indented = "\n".join(
            f"  {line}" if line else "" for line in body.rstrip("\n").splitlines()
        )
        return f"session_context: |\n{indented}\n"
