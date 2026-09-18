"""Behaviour of :class:`quarry.subagent_report.SubagentReport`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from quarry.api.capture_ingest import SESSION_ID_PREFIX_LEN
from quarry.memory_types import MemoryType
from quarry.subagent_report import SubagentReport
from quarry.transcript_reader import TranscriptReader

if TYPE_CHECKING:
    from pathlib import Path


def _turn(role: str, text: str) -> dict[str, object]:
    return {
        "type": role,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _transcript(path: Path, *records: dict[str, object]) -> TranscriptReader:
    path.write_text("\n".join(json.dumps(r) for r in records))
    return TranscriptReader(path)


def _report(reader: TranscriptReader, handle: str = "rmh") -> SubagentReport:
    return SubagentReport.from_transcript(
        reader,
        handle=handle,
        agent_id="a0f13948344b777d1",
        parent_session_id="304fdeb9-parent",
        agent_type="rmh",
    )


class TestFromTranscript:
    def test_text_is_the_last_assistant_turn(self, tmp_path: Path) -> None:
        reader = _transcript(
            tmp_path / "t.jsonl",
            _turn("user", "do it"),
            _turn("assistant", "working"),
            _turn("user", "and?"),
            _turn("assistant", "Done: 3 files changed"),
        )
        assert _report(reader).text == "Done: 3 files changed"

    def test_no_assistant_turn_is_empty(self, tmp_path: Path) -> None:
        reader = _transcript(tmp_path / "t.jsonl", _turn("user", "hello"))
        report = _report(reader)
        assert report.is_empty
        assert report.summary == ""

    def test_missing_transcript_is_empty(self, tmp_path: Path) -> None:
        assert _report(TranscriptReader(tmp_path / "missing.jsonl")).is_empty

    def test_artifacts_come_from_the_whole_transcript(self, tmp_path: Path) -> None:
        reader = _transcript(
            tmp_path / "t.jsonl",
            _turn("user", "fix bead quarry-fbj9 please"),
            _turn("assistant", "Fixed."),
        )
        assert "quarry-fbj9" in _report(reader).artifacts_header

    def test_runaway_report_keeps_head_and_caps_tail(self, tmp_path: Path) -> None:
        huge = "x" * (SubagentReport.MAX_CHARS + 500)
        reader = _transcript(tmp_path / "t.jsonl", _turn("assistant", huge))
        text = _report(reader).text
        assert text.startswith("x" * SubagentReport.MAX_CHARS)
        assert text.endswith("[truncated 500 chars]")


class TestSummary:
    def test_first_non_empty_line_capped(self, tmp_path: Path) -> None:
        first = "A" * 200
        reader = _transcript(
            tmp_path / "t.jsonl", _turn("assistant", f"\n\n{first}\nsecond line")
        )
        assert _report(reader).summary == "A" * SubagentReport.SUMMARY_CHARS


class TestRememberRequest:
    def test_name_uses_the_shared_prefix_width(self, tmp_path: Path) -> None:
        reader = _transcript(tmp_path / "t.jsonl", _turn("assistant", "ok"))
        req = _report(reader).remember_request()
        prefix = "a0f13948344b777d1"[:SESSION_ID_PREFIX_LEN]
        assert req.name == f"subagent-{prefix}-report"

    def test_routes_to_the_handle_as_an_observation(self, tmp_path: Path) -> None:
        reader = _transcript(tmp_path / "t.jsonl", _turn("assistant", "ok"))
        req = _report(reader).remember_request()
        assert req.collection == ""  # daemon routes handle -> memory-<handle>
        assert req.agent_handle == "rmh"
        assert req.memory_type == MemoryType.OBSERVATION.value
        assert req.overwrite is True
        assert req.format_hint == "markdown"
        assert req.summary == "ok"

    def test_document_carries_provenance_and_report(self, tmp_path: Path) -> None:
        reader = _transcript(
            tmp_path / "t.jsonl",
            _turn("user", "see PR #519"),
            _turn("assistant", "Reviewed. No findings."),
        )
        doc = _report(reader).document()
        assert doc.startswith("# Subagent report — rmh (agent a0f13948344b777d1)")
        assert "Parent session: 304fdeb9-parent" in doc
        assert "agent_type: rmh" in doc
        assert "## Session Artifacts" in doc
        assert "PRs: #519" in doc
        assert doc.rstrip().endswith("## Final report\nReviewed. No findings.")

    def test_document_omits_artifacts_block_when_none(self, tmp_path: Path) -> None:
        reader = _transcript(tmp_path / "t.jsonl", _turn("assistant", "plain"))
        assert "## Session Artifacts" not in _report(reader).document()
