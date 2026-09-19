"""A subagent's final report, distilled from its transcript into a memory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.api import RememberRequest
from quarry.api.capture_ingest import SESSION_ID_PREFIX_LEN
from quarry.artifacts import SessionArtifacts
from quarry.memory_types import MemoryType

if TYPE_CHECKING:
    from quarry.transcript_reader import TranscriptReader


@final
@dataclass(frozen=True, slots=True)
class SubagentReport:
    """The report a subagent handed its parent, ready to file as a memory.

    No model distils anything: the subagent already wrote its own summary as
    its final assistant turn (a worker's result summary, a reviewer's
    findings). That turn, headed by the same ``## Session Artifacts`` block
    the raw capture carries, is the memory — short, classified, attributed.
    """

    # The report is a summary by construction; a cap keeps a runaway final
    # turn from becoming a second transcript-sized document.
    MAX_CHARS: ClassVar[int] = 32_000
    SUMMARY_CHARS: ClassVar[int] = 120

    handle: str
    agent_id: str
    parent_session_id: str
    agent_type: str
    artifacts_header: str
    text: str

    @classmethod
    def from_transcript(
        cls,
        reader: TranscriptReader,
        *,
        handle: str,
        agent_id: str,
        parent_session_id: str,
        agent_type: str,
    ) -> Self:
        """Read the final assistant turn and the artifacts from *reader*."""
        return cls(
            handle=handle,
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            agent_type=agent_type,
            artifacts_header=SessionArtifacts.from_text(reader.text()).format_header(),
            text=cls._head_capped(reader.last_assistant_text()),
        )

    @classmethod
    def _head_capped(cls, text: str) -> str:
        """Keep the first ``MAX_CHARS`` and say how much of the tail was cut."""
        if len(text) <= cls.MAX_CHARS:
            return text
        cut = len(text) - cls.MAX_CHARS
        return f"{text[: cls.MAX_CHARS]}\n[truncated {cut} chars]"

    @property
    def is_empty(self) -> bool:
        """Return whether the transcript held no assistant text to file."""
        return not self.text

    @property
    def summary(self) -> str:
        """Return the first non-empty report line, capped for the summary column."""
        for line in self.text.splitlines():
            if line.strip():
                return line.strip()[: self.SUMMARY_CHARS]
        return ""

    @property
    def document_name(self) -> str:
        """Return ``subagent-<id8>-report`` — the raw capture's key width, one id."""
        return f"subagent-{self.agent_id[:SESSION_ID_PREFIX_LEN]}-report"

    def document(self) -> str:
        """Render the memory's markdown body."""
        head = (
            f"# Subagent report — {self.handle} (agent {self.agent_id})\n"
            f"Parent session: {self.parent_session_id} · "
            f"agent_type: {self.agent_type}\n"
        )
        artifacts = f"\n{self.artifacts_header}\n" if self.artifacts_header else ""
        return f"{head}{artifacts}\n## Final report\n{self.text}\n"

    def remember_request(self) -> RememberRequest:
        """Return the ``remember`` body: an observation in ``memory-<handle>``.

        ``collection`` stays empty so the daemon's routing rule (handle →
        ``memory-<handle>``) applies, and ``overwrite`` is on so a repeated stop
        for the same agent replaces its report rather than duplicating it.
        """
        return RememberRequest(
            name=self.document_name,
            content=self.document(),
            format_hint="markdown",
            overwrite=True,
            agent_handle=self.handle,
            memory_type=MemoryType.OBSERVATION.value,
            summary=self.summary,
        )
