"""Tests for the backfill-sessions command and supporting functions."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.atomic_file import AtomicFile
from quarry.backfill import Backfill, BackfillConfig, BackfillRuntime
from quarry.backfill_mapping import ProjectMapping, ProjectMappingResolver
from quarry.backfill_project import BackfillRun
from quarry.backfill_report import BackfillReport
from quarry.backfill_session import BackfillSession
from quarry.backfill_stats import BackfillStats, BackfillTally
from quarry.backfill_unindexable import UnindexableTranscripts
from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.config import Settings
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.queued_ingest import QueuedIngest, QueuePolicy
from quarry.daemon.tasks import TaskState
from quarry.db.chunk_catalog import ChunkCatalog
from quarry.db.facade import Database
from quarry.results import DocumentSummary
from quarry.stop_signal import StopSignal
from quarry.sync_registry import DirectoryRegistration
from quarry.transcript_read import TranscriptRead
from quarry.transcript_reader import TranscriptReader
from tests.queue_fixtures import ctx_with_queue, writer_for
from tests.transcript_writers import DirectWriter, RecordingWriter, runtime_over

runner = CliRunner()


# ---------------------------------------------------------------------------
# BackfillConfig
# ---------------------------------------------------------------------------


class TestBackfillConfig:
    def test_construction_defaults(self) -> None:
        cfg = BackfillConfig()
        assert cfg.dry_run is False
        assert cfg.collection_override is None
        assert cfg.project_filter is None
        assert cfg.limit is None

    def test_construction_explicit(self) -> None:
        cfg = BackfillConfig(
            dry_run=True,
            collection_override="captures",
            project_filter="/Users/me/proj",
            limit=50,
        )
        assert cfg.dry_run is True
        assert cfg.collection_override == "captures"
        assert cfg.project_filter == "/Users/me/proj"
        assert cfg.limit == 50

    @pytest.mark.parametrize(
        ("limit", "processed", "reached"),
        [(None, 99, False), (0, 99, False), (3, 2, False), (3, 3, True), (3, 4, True)],
    )
    def test_the_limit_is_reached_by_that_many_transcripts(
        self, limit: int | None, processed: int, *, reached: bool
    ) -> None:
        assert BackfillConfig(limit=limit).reached_by(processed) is reached

    def test_frozen(self) -> None:
        cfg = BackfillConfig()
        with pytest.raises(AttributeError):
            cfg.dry_run = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# encode_project_path
# ---------------------------------------------------------------------------


class TestEncodeProjectPath:
    def test_simple_path(self) -> None:
        assert ProjectMappingResolver.encode("/Users/jdoe/code") == "-Users-jdoe-code"

    def test_path_with_hyphens(self) -> None:
        result = ProjectMappingResolver.encode("/Users/jdoe/Coding/punt-labs/quarry")
        assert result == "-Users-jdoe-Coding-punt-labs-quarry"

    def test_root_path(self) -> None:
        assert ProjectMappingResolver.encode("/") == "-"

    def test_single_component(self) -> None:
        assert ProjectMappingResolver.encode("/home") == "-home"

    @pytest.mark.parametrize(
        ("path", "encoded"),
        [
            (
                "/home/u/Coding/punt-labs/beadle/.claude/worktrees/mcp-serve",
                "-home-u-Coding-punt-labs-beadle--claude-worktrees-mcp-serve",
            ),
            ("/home/u/my.project", "-home-u-my-project"),
            ("/home/u/my_project", "-home-u-my-project"),
            ("/home/u/My Project", "-home-u-My-Project"),
            ("/home/u/caf\u00e9", "-home-u-caf-"),
        ],
        ids=["dot-dir", "dotted", "underscored", "spaced", "non-ascii"],
    )
    def test_every_character_but_a_letter_or_digit_becomes_a_dash(
        self, path: str, encoded: str
    ) -> None:
        assert ProjectMappingResolver.encode(path) == encoded


# ---------------------------------------------------------------------------
# build_project_mappings
# ---------------------------------------------------------------------------


class TestBuildProjectMappings:
    def test_matches_registration_to_directory(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        encoded = "-Users-jdoe-Coding-punt-labs-quarry"
        (projects_dir / encoded).mkdir(parents=True)

        reg = DirectoryRegistration(
            directory="/Users/jdoe/Coding/punt-labs/quarry",
            collection="quarry",
            registered_at="2025-01-01T00:00:00",
        )

        with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", projects_dir):
            mappings = ProjectMappingResolver.resolve_all([reg])

        assert len(mappings) == 1
        assert mappings[0].collection == "quarry"
        assert mappings[0].captures_collection == "quarry-captures"
        assert mappings[0].encoded_dir == encoded

    def test_skips_unmatched_registration(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)

        reg = DirectoryRegistration(
            directory="/nonexistent/project",
            collection="nonexistent",
            registered_at="2025-01-01T00:00:00",
        )

        with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", projects_dir):
            mappings = ProjectMappingResolver.resolve_all([reg])

        assert len(mappings) == 0

    def test_no_projects_dir(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "does-not-exist"

        with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", projects_dir):
            mappings = ProjectMappingResolver.resolve_all([])

        assert mappings == []


# ---------------------------------------------------------------------------
# list_transcript_files
# ---------------------------------------------------------------------------


class TestListTranscriptFiles:
    def test_lists_jsonl_files(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        project = projects_dir / "my-project"
        project.mkdir(parents=True)
        (project / "aaa11111.jsonl").write_text("{}")
        (project / "bbb22222.jsonl").write_text("{}")
        (project / "not-jsonl.txt").write_text("ignore")

        with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", projects_dir):
            mapping = ProjectMapping("my-project", "", "")
            files = mapping.transcript_files()

        assert len(files) == 2
        assert all(f.suffix == ".jsonl" for f in files)

    def test_empty_directory(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        project = projects_dir / "empty"
        project.mkdir(parents=True)

        with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", projects_dir):
            mapping = ProjectMapping("empty", "", "")
            files = mapping.transcript_files()

        assert files == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)

        with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", projects_dir):
            mapping = ProjectMapping("nonexistent", "", "")
            files = mapping.transcript_files()

        assert files == []


# ---------------------------------------------------------------------------
# BackfillSession.name
# ---------------------------------------------------------------------------


class TestBackfillSessionName:
    def test_uses_stable_session_prefix(self, tmp_path: Path) -> None:
        """The document name is the stable ``session-<id[:8]>`` — no mtime — so
        it matches the compaction hook's name and one document exists per
        session regardless of who wrote it first."""
        name = "1e7aa08d-c485-45d1-8228-54d1a375c812.jsonl"
        transcript = tmp_path / name
        transcript.write_text("{}")

        result = BackfillSession(transcript, "", "").name

        assert result == "session-1e7aa08d"

    def test_name_matches_compaction_hook_so_one_doc_in_both_orders(
        self, tmp_path: Path
    ) -> None:
        """Backfill derives the SAME document_name the compaction hook files
        under (``session-<id[:8]>``), so a session yields ONE document in either
        write order: hook-first -> backfill's exact-name check skips it;
        backfill-first -> the hook's overwrite=True replaces the same name."""
        session_id = "1e7aa08d-c485-45d1-8228-54d1a375c812"
        transcript = tmp_path / f"{session_id}.jsonl"
        transcript.write_text("{}")

        backfill_name = BackfillSession(transcript, "", "").name
        # The capture route (compaction hook path) files under session-<id[:8]>.
        hook_name = f"session-{session_id[:8]}"

        assert backfill_name == hook_name


# ---------------------------------------------------------------------------
# Backfill.sessions integration
# ---------------------------------------------------------------------------


def _make_transcript(path: Path, messages: list[dict[str, object]]) -> None:
    """Write a minimal JSONL transcript file."""
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")


def _user_message(text: str) -> dict[str, object]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_message(text: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _setup_registry(
    registry_path: Path,
    project_path: str,
    collection: str,
) -> None:
    """Create a registry and register a project directory."""
    from quarry.sync_registry import SyncRegistry

    Path(project_path).mkdir(parents=True, exist_ok=True)
    conn = SyncRegistry(registry_path)
    try:
        conn.register_directory(Path(project_path), collection)
    finally:
        conn.close()


def _make_settings(db_path: Path, registry_path: Path) -> Settings:
    """Load settings with overridden paths."""
    settings = Settings.load().resolve_db_paths(None)
    return settings.model_copy(
        update={
            "lancedb_path": db_path,
            "registry_path": registry_path,
        }
    )


def _make_env(tmp_path: Path) -> dict[str, Path]:
    """Set up a minimal backfill environment.

    Uses a project directory inside tmp_path so the resolved path is
    predictable (avoids macOS /tmp -> /private/tmp symlink issues).
    """
    # The "real" project directory that will be registered
    real_project = tmp_path / "myproject"
    real_project.mkdir()

    # Encode the resolved project path the same way Claude Code does
    encoded = ProjectMappingResolver.encode(str(real_project.resolve()))

    projects_dir = tmp_path / ".claude" / "projects"
    project_dir = projects_dir / encoded
    project_dir.mkdir(parents=True)

    transcript = project_dir / "abcd1234-0000-0000-0000-000000000000.jsonl"
    _make_transcript(
        transcript,
        [_user_message("hello"), _assistant_message("hi there")],
    )

    db_path = tmp_path / "lancedb"
    db_path.mkdir()
    registry_path = tmp_path / "registry.db"

    return {
        "projects_dir": projects_dir,
        "project_dir": project_dir,
        "real_project": real_project,
        "transcript": transcript,
        "db_path": db_path,
        "registry_path": registry_path,
    }


def _run(
    env: dict[str, Path],
    writer: RecordingWriter | DirectWriter,
    config: BackfillConfig | None = None,
    stop: StopSignal | None = None,
) -> BackfillStats:
    """Backfill *env* through *writer*, reading the env's (empty) database."""
    database = Database.connect(env["db_path"])
    runtime = runtime_over(database, writer, env["registry_path"], stop)
    with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", env["projects_dir"]):
        return Backfill.sessions(config or BackfillConfig(), runtime)


def _registered_env(tmp_path: Path) -> dict[str, Path]:
    env = _make_env(tmp_path)
    _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")
    return env


def _stored_doc(timestamp: str) -> dict[str, object]:
    return {
        "document_name": "session-abcd1234",
        "document_path": "",
        "collection": "myproject-captures",
        "total_pages": 1,
        "chunk_count": 5,
        "indexed_pages": 1,
        "ingestion_timestamp": timestamp,
    }


class TestBackfillSessions:
    def test_dry_run_no_writes(self, tmp_path: Path) -> None:
        writer = RecordingWriter()
        stats = _run(_registered_env(tmp_path), writer, BackfillConfig(dry_run=True))

        assert writer.calls == []
        assert stats.would_ingest == 1
        assert stats.ingested == 0
        assert stats.skipped_existing == 0

    def test_skip_unregistered_projects(self, tmp_path: Path) -> None:
        from quarry.sync_registry import SyncRegistry

        env = _make_env(tmp_path)
        # Initialize empty registry (no registrations)
        SyncRegistry(env["registry_path"]).close()

        stats = _run(env, RecordingWriter())

        assert stats.ingested == 0
        assert stats.skipped_unregistered == 1

    def test_dedup_skips_existing(self, tmp_path: Path) -> None:
        writer = RecordingWriter()
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            return_value=[_stored_doc("2999-01-01T00:00:00")],
        ):
            stats = _run(_registered_env(tmp_path), writer)

        assert writer.calls == []
        assert stats.ingested == 0
        assert stats.skipped_existing == 1

    def test_a_session_that_grew_after_it_was_stored_is_ingested_again(
        self, tmp_path: Path
    ) -> None:
        """The compaction hook stores the session up to that moment; the rest of
        the conversation reaches the transcript afterwards, and backfill must file
        it over the partial document instead of skipping on its mere existence."""
        writer = RecordingWriter()
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            return_value=[_stored_doc("2000-01-01T00:00:00")],
        ):
            stats = _run(_registered_env(tmp_path), writer)

        [call] = writer.calls
        assert call.name == "session-abcd1234"
        assert call.collection == "myproject-captures"
        assert stats.ingested == 1
        assert stats.skipped_existing == 0

    def test_the_snapshot_is_taken_before_the_transcript_is_read(
        self, tmp_path: Path
    ) -> None:
        """The stored time is when backfill read the transcript, not when it stored."""
        writer = RecordingWriter()
        read_at: list[datetime] = []
        real_text = TranscriptReader.text

        def timed_text(self: TranscriptReader) -> str:
            read_at.append(datetime.now(UTC))
            return real_text(self)

        with patch.object(TranscriptReader, "text", timed_text):
            _run(_registered_env(tmp_path), writer)

        assert writer.calls[0].snapshot <= read_at[0]

    def test_an_ingest_failure_reports_the_class_not_the_message(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        writer = RecordingWriter(OSError("/home/jane/secret-token-path"))

        stats = _run(env, writer)

        assert stats.errors == [f"{env['transcript'].name}: internal error: OSError"]

    def test_a_transcript_the_queue_never_admitted_is_a_failure_with_its_reason(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        writer = RecordingWriter(BackfillWriteError("ingest queue full; not admitted"))

        stats = _run(env, writer)

        assert stats.ingested == 0
        assert stats.errors == [
            f"{env['transcript'].name}: ingest queue full; not admitted"
        ]

    def test_a_transcript_is_recorded_as_stored_only_after_the_write_succeeds(
        self, tmp_path: Path
    ) -> None:
        bad, good = tmp_path / "bad", tmp_path / "good"
        bad.mkdir()
        good.mkdir()
        with patch("quarry.backfill_project.StoredSessions.record") as record:
            _run(_registered_env(bad), RecordingWriter(OSError("x")))
            record.assert_not_called()
            _run(_registered_env(good), RecordingWriter())
            record.assert_called_once()

    def test_limit_flag(self, tmp_path: Path) -> None:
        env = _registered_env(tmp_path)
        # Add a second transcript
        second = env["project_dir"] / "ef567890-0000-0000-0000-000000000000.jsonl"
        _make_transcript(
            second,
            [
                _user_message("second session"),
                _assistant_message("ok"),
            ],
        )

        stats = _run(env, RecordingWriter(), BackfillConfig(dry_run=True, limit=1))

        assert stats.would_ingest == 1

    def test_collection_override(self, tmp_path: Path) -> None:
        writer = RecordingWriter()
        stats = _run(
            _registered_env(tmp_path),
            writer,
            BackfillConfig(collection_override="my-override"),
        )

        assert stats.ingested == 1
        assert [call.collection for call in writer.calls] == ["my-override"]

    def test_empty_transcript_skipped(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path)
        # Overwrite with empty content
        env["transcript"].write_text("")
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")
        writer = RecordingWriter()

        stats = _run(env, writer)

        assert writer.calls == []
        assert stats.ingested == 0
        assert stats.skipped_empty == 1

    def test_ingested_text_is_scrubbed(self, tmp_path: Path) -> None:
        """Transcript text is scrubbed before store — the DES-036 leak lock.

        Backfill's job passes a content_scrubber, no manual pre-scrub, so the
        pipeline redacts the content before store.  A secret token and an email in
        a transcript must never reach the searchable LanceDB store (retrievable via
        /v1/search).  Patching the store boundary asserts the REAL pipeline scrub
        ran.
        """
        from quarry.ingestion.streaming import DocumentStreamer

        fake_pat = "ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"
        email = "leak.user@evil-example.com"
        env = _registered_env(tmp_path)
        _make_transcript(
            env["transcript"],
            [_user_message(f"my token is {fake_pat} and my email is {email}")],
        )
        settings = _make_settings(env["db_path"], env["registry_path"])
        writer = DirectWriter(Database.connect(env["db_path"]), settings)

        stored: list[object] = []

        def _capture_build_chunks(
            _self: DocumentStreamer, pages: list[object], _labels: object
        ) -> list[object]:
            stored.extend(pages)
            return []

        with (
            patch.object(DocumentStreamer, "build_chunks", _capture_build_chunks),
            patch("quarry.db.chunk_store.ChunkStore.delete_document"),
        ):
            _run(env, writer)

        assert stored, "the job never reached the chunker"
        joined = " ".join(page.text for page in stored)  # type: ignore[attr-defined]
        assert fake_pat not in joined
        assert email not in joined
        assert "[REDACTED:gh-pat]" in joined
        assert "[REDACTED:email]" in joined


def _write_sessions(env: dict[str, Path], *stems: str, text: str = "hello") -> None:
    """Add a transcript with *text* in it for each *stem* (its session id prefix)."""
    for stem in stems:
        _make_transcript(
            env["project_dir"] / f"{stem}-0000-0000-0000-000000000000.jsonl",
            [_user_message(text), _assistant_message("ok")],
        )


def _add_project(env: dict[str, Path], tmp_path: Path, name: str) -> Path:
    """Register a second project, with one transcript, under its own collection."""
    from quarry.sync_registry import SyncRegistry

    project = tmp_path / name
    project.mkdir()
    directory = env["projects_dir"] / ProjectMappingResolver.encode(
        str(project.resolve())
    )
    directory.mkdir()
    _make_transcript(
        directory / "feed0001-0000-0000-0000-000000000000.jsonl",
        [_user_message("other project"), _assistant_message("ok")],
    )
    conn = SyncRegistry(env["registry_path"])
    try:
        conn.register_directory(project, name)
    finally:
        conn.close()
    return project


def _library_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("quarry.backfill")]


class TestUnindexableTranscripts:
    def test_unindexable_ones_ahead_of_good_ones_do_not_consume_the_limit(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "aaaa0001", "aaaa0002", "aaaa0003", "bbbb0001")
        nothing = BackfillWriteError("nothing was stored").as_outcome(
            WriteOutcome.UNINDEXABLE
        )
        writer = RecordingWriter(
            errors_by_name={f"session-aaaa000{i}": nothing for i in (1, 2, 3)}
        )

        stats = _run(env, writer, BackfillConfig(limit=1))

        assert stats.skipped_empty == 3
        assert stats.ingested == 1  # the first good one; limit 1 stops there
        assert [c.name for c in writer.calls] == [
            "session-aaaa0001",
            "session-aaaa0002",
            "session-aaaa0003",
            "session-abcd1234",
        ]

    def test_a_second_run_does_not_retry_the_unchanged_ones(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "aaaa0001")
        nothing = BackfillWriteError("nothing was stored").as_outcome(
            WriteOutcome.UNINDEXABLE
        )
        _run(env, RecordingWriter(errors_by_name={"session-aaaa0001": nothing}))

        again = RecordingWriter()
        stats = _run(env, again)

        assert "session-aaaa0001" not in [c.name for c in again.calls]
        assert stats.skipped_empty == 1
        assert stats.failed == 0

    def test_a_transcript_that_changed_is_tried_again(self, tmp_path: Path) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "aaaa0001")
        nothing = BackfillWriteError("nothing was stored").as_outcome(
            WriteOutcome.UNINDEXABLE
        )
        _run(env, RecordingWriter(errors_by_name={"session-aaaa0001": nothing}))
        changed = env["project_dir"] / "aaaa0001-0000-0000-0000-000000000000.jsonl"
        later = changed.stat().st_mtime + 60
        os.utime(changed, (later, later))

        again = RecordingWriter()
        _run(env, again)

        assert "session-aaaa0001" in [c.name for c in again.calls]

    def test_a_dry_run_records_nothing(self, tmp_path: Path) -> None:
        env = _registered_env(tmp_path)
        env["transcript"].write_text("")

        stats = _run(env, RecordingWriter(), BackfillConfig(dry_run=True))

        assert stats.skipped_empty == 1
        assert not env["registry_path"].with_name("backfill-unindexable.json").exists()


class TestEmptyAndUnreadable:
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_they_are_counted_apart_in_a_real_run_and_a_dry_run(
        self, tmp_path: Path, *, dry_run: bool
    ) -> None:
        env = _registered_env(tmp_path)
        (env["project_dir"] / "dddd0001-0000.jsonl").write_text("")
        (env["project_dir"] / "eeee0001-0000.jsonl").mkdir()  # not a readable file

        stats = _run(env, RecordingWriter(), BackfillConfig(dry_run=dry_run))

        assert stats.skipped_empty == 1
        assert stats.skipped_unreadable == 1
        assert stats.would_ingest + stats.ingested == 1


class TestStopping:
    def test_a_stop_between_transcripts_ends_the_run_with_the_rest_not_attempted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002")
        runtime_stop = StopSignal()
        writer = RecordingWriter(
            after_write=lambda: runtime_stop.set("daemon shutting down")
        )

        with caplog.at_level(logging.INFO):
            stats = _run(env, writer, stop=runtime_stop)

        assert stats.ingested == 1
        assert stats.not_attempted == 2
        assert stats.errors == ["backfill stopped: daemon shutting down"]
        loud = [r for r in _library_records(caplog) if r.levelno >= logging.WARNING]
        assert len(loud) == 1  # one line, with the count: not a traceback per file
        assert "2 not attempted" in loud[0].getMessage()
        assert loud[0].exc_info is None

    def test_a_writer_that_says_the_run_cannot_go_on_stops_it(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002")
        writer = RecordingWriter(
            BackfillWriteError("daemon shutting down").as_outcome(WriteOutcome.ABORTED)
        )

        stats = _run(env, writer)

        assert len(writer.calls) == 1  # no waiting out each of the others
        assert stats.ingested == 0
        assert stats.failed == 0
        assert stats.not_attempted == 3
        assert stats.errors == ["backfill stopped: daemon shutting down"]

    def test_a_queue_that_stays_full_stops_the_run_after_a_few(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002", "bbbb0003", "bbbb0004")
        writer = RecordingWriter(
            BackfillWriteError("ingest queue full").as_outcome(
                WriteOutcome.NOT_ADMITTED
            )
        )

        stats = _run(env, writer)

        assert len(writer.calls) == BackfillTally.MAX_UNADMITTED
        assert stats.failed == BackfillTally.MAX_UNADMITTED
        assert stats.not_attempted == 2
        assert stats.errors[0] == "backfill stopped: ingest queue stayed full"

    def test_a_store_that_takes_a_transcript_resets_the_count(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002", "bbbb0003", "bbbb0004")
        full = BackfillWriteError("ingest queue full").as_outcome(
            WriteOutcome.NOT_ADMITTED
        )
        writer = RecordingWriter(
            errors_by_name={
                "session-abcd1234": full,
                "session-bbbb0001": full,
                "session-bbbb0003": full,
                "session-bbbb0004": full,
            }
        )

        stats = _run(env, writer)

        assert stats.ingested == 1  # bbbb0002 got in, so the run went on
        assert stats.not_attempted == 0


class TestOneLinePerFailure:
    def test_a_refusal_is_one_warning_with_its_message_and_no_traceback(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)
        writer = RecordingWriter(BackfillWriteError("collection is read-only"))

        with caplog.at_level(logging.INFO):
            _run(env, writer)

        [line] = [
            r
            for r in _library_records(caplog)
            if env["transcript"].name in r.getMessage()
        ]
        assert line.levelno == logging.WARNING
        assert "collection is read-only" in line.getMessage()
        assert line.exc_info is None

    def test_any_other_failure_is_one_error_with_its_class_and_no_message(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)
        writer = RecordingWriter(OSError("/home/jane/hidden-path"))

        with caplog.at_level(logging.INFO):
            _run(env, writer)

        [line] = [
            r
            for r in _library_records(caplog)
            if env["transcript"].name in r.getMessage()
        ]
        assert line.levelno == logging.ERROR
        assert "OSError" in line.getMessage()
        assert line.exc_info is not None
        shown = logging.Formatter().formatException(line.exc_info)
        assert "hidden-path" not in line.getMessage() + shown
        assert "hidden-path" not in caplog.text

    def test_a_capture_file_that_could_not_be_written_is_a_failure(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        writer = RecordingWriter()

        with patch("quarry.capture.CaptureWriter.write", return_value=False):
            stats = _run(env, writer)

        assert writer.calls == []  # not handed on as if it had been written
        assert stats.failed == 1
        assert stats.errors == [
            f"{env['transcript'].name}: capture file could not be written"
        ]


class TestTheRunReport:
    def test_a_clean_run_logs_one_info_summary(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            _run(_registered_env(tmp_path), RecordingWriter())

        [summary] = [
            r for r in _library_records(caplog) if " ingested," in r.getMessage()
        ]
        assert summary.levelno == logging.INFO
        assert "1 ingested, 0 skipped, 0 failed, 0 not" in summary.getMessage()

    def test_a_run_that_fell_short_logs_the_summary_as_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001")
        writer = RecordingWriter(errors_by_name={"session-bbbb0001": OSError("x")})

        with caplog.at_level(logging.INFO):
            _run(env, writer)

        [summary] = [
            r for r in _library_records(caplog) if " ingested," in r.getMessage()
        ]
        assert summary.levelno == logging.WARNING
        assert "1 ingested, 0 skipped, 1 failed, 0 not" in summary.getMessage()


class TestProjectsAreIsolated:
    def test_a_store_read_failure_on_one_project_leaves_the_others_running(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)
        project = _add_project(env, tmp_path, "other")
        real = ChunkCatalog.list_documents

        def broken_for_the_first(
            self: ChunkCatalog, **kwargs: Any
        ) -> list[DocumentSummary]:
            if kwargs.get("collection_filter") == "myproject-captures":
                msg = "/home/jane/hidden-path"
                raise RuntimeError(msg)
            return real(self, **kwargs)

        writer = RecordingWriter()
        with (
            patch.object(ChunkCatalog, "list_documents", broken_for_the_first),
            caplog.at_level(logging.INFO),
        ):
            stats = _run(env, writer)

        assert [c.collection for c in writer.calls] == ["other-captures"]
        assert stats.ingested == 1
        assert stats.not_examined == 1
        assert len(stats.errors) == 1
        assert stats.errors[0].endswith(": internal error: RuntimeError")
        assert str(env["real_project"]) in stats.errors[0]
        assert "hidden-path" not in caplog.text
        assert project.exists()


_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _state_file(env: dict[str, Path]) -> Path:
    return env["registry_path"].with_name("backfill-unindexable.json")


def _bump_mtime(path: Path) -> None:
    """Make *path* look newer, as if it had been appended to."""
    later = path.stat().st_mtime + 60
    os.utime(path, (later, later))


def _recorded(env: dict[str, Path]) -> dict[str, int]:
    state = _state_file(env)
    return json.loads(state.read_text()) if state.exists() else {}


class TestAReadFaultIsNotAnEmptyTranscript:
    def test_a_read_error_is_unreadable_never_recorded_and_retried_next_run(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)
        real_read_text = Path.read_text

        def emfile(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.suffix == ".jsonl":
                raise OSError(errno.EMFILE, "Too many open files")
            return real_read_text(self, *args, **kwargs)

        with (
            patch.object(Path, "read_text", emfile),
            caplog.at_level(logging.WARNING),
        ):
            stats = _run(env, RecordingWriter())

        assert stats.skipped_unreadable == 1
        assert stats.skipped_empty == 0
        assert _recorded(env) == {}
        assert "EMFILE" in caplog.text
        again = RecordingWriter()
        _run(env, again)
        assert [call.name for call in again.calls] == ["session-abcd1234"]


class TestATranscriptThatChangedIsNotRecorded:
    def test_text_appended_between_the_read_and_the_record_is_not_skipped_next_time(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        env["transcript"].write_text("")
        real_text = TranscriptReader.text

        def empty_then_appended(self: TranscriptReader) -> str:
            real_text(self)
            _bump_mtime(env["transcript"])  # a session appends after the read
            return ""

        with patch.object(TranscriptReader, "text", empty_then_appended):
            _run(env, RecordingWriter())

        assert _recorded(env) == {}

    def test_the_same_holds_for_a_worker_that_found_nothing_to_index(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        nothing = BackfillWriteError("nothing was stored").as_outcome(
            WriteOutcome.UNINDEXABLE
        )
        writer = RecordingWriter(
            errors_by_name={"session-abcd1234": nothing},
            after_write=lambda: _bump_mtime(env["transcript"]),
        )

        _run(env, writer)

        assert _recorded(env) == {}


class TestAFailedSaveNeverDecidesTheRun:
    def test_the_stats_come_back_with_one_non_fatal_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)
        env["transcript"].write_text("")  # something to remember, so a save happens

        with (
            patch.object(
                AtomicFile, "replace", side_effect=OSError(errno.ENOSPC, "full")
            ),
            caplog.at_level(logging.WARNING),
        ):
            stats = _run(env, RecordingWriter())

        assert stats.errors == ["unindexable cache not saved: ENOSPC"]
        assert not stats.stored_nothing
        assert "ENOSPC" in caplog.text

    def test_a_save_in_the_cleanup_does_not_mask_a_fault_in_the_run(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)

        with (
            patch.object(BackfillRun, "process", side_effect=RuntimeError("the run")),
            pytest.raises(RuntimeError, match="the run"),
        ):
            _run(env, RecordingWriter())


class TestTheNotAdmittedStreakIsConsecutive:
    @staticmethod
    def _full() -> BackfillWriteError:
        return BackfillWriteError("ingest queue full").as_outcome(
            WriteOutcome.NOT_ADMITTED
        )

    def test_a_store_result_in_between_ends_the_streak(self, tmp_path: Path) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002", "bbbb0003")
        nothing = BackfillWriteError("nothing was stored").as_outcome(
            WriteOutcome.UNINDEXABLE
        )
        writer = RecordingWriter(
            errors_by_name={
                "session-abcd1234": self._full(),
                "session-bbbb0001": self._full(),
                "session-bbbb0002": nothing,
                "session-bbbb0003": self._full(),
            }
        )

        stats = _run(env, writer)

        assert stats.stopped == ""
        assert stats.failed == 3

    def test_a_failure_that_never_reached_the_queue_does_not_end_it(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002", "bbbb0003", "bbbb0004")
        writer = RecordingWriter(
            errors_by_name={
                "session-abcd1234": self._full(),
                "session-bbbb0002": self._full(),
                "session-bbbb0003": self._full(),
            }
        )
        real = BackfillSession.write_capture_file

        def capture_fails_for_the_second(session: BackfillSession) -> bool:
            return False if session.name == "session-bbbb0001" else real(session)

        with patch.object(
            BackfillSession, "write_capture_file", capture_fails_for_the_second
        ):
            stats = _run(env, writer)

        assert stats.stopped == "ingest queue stayed full"
        assert stats.not_attempted == 1  # bbbb0004 was never tried
        assert "session-bbbb0004" not in [call.name for call in writer.calls]


class TestAHungWorkerStopsTheRun:
    async def test_the_run_stops_after_the_first_job_that_never_finishes(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002")
        ctx = ctx_with_queue(tmp_path / "queue")
        stop = StopSignal()
        policy = QueuePolicy(
            admit_timeout_s=1.0, finish_timeout_s=0.3, grace_s=0.5, reserve=0
        )
        writer = writer_for(ctx, asyncio.get_running_loop(), stop, policy)
        runtime = BackfillRuntime(
            Database.connect(env["db_path"]), writer, env["registry_path"], stop
        )
        ran: list[str] = []

        async def hangs(job: ScrubbedIngestJob, _ctx: object, _state: object) -> None:
            ran.append(job.name)

        with (
            patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", env["projects_dir"]),
            patch.object(ScrubbedIngestJob, "run", hangs),
        ):
            stats = await asyncio.to_thread(
                Backfill.sessions, BackfillConfig(), runtime
            )

        assert ran == ["session-abcd1234"]  # not one orphan per transcript
        assert stats.failed == 0
        assert stats.not_attempted == 3
        assert stats.errors[0].startswith(
            "backfill stopped: ingest did not finish within"
        )

    async def test_a_cancelled_loop_is_a_shutdown_not_a_cancellederror(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        ctx = ctx_with_queue(tmp_path / "queue")
        stop = StopSignal()
        writer = writer_for(ctx, asyncio.get_running_loop(), stop)
        runtime = BackfillRuntime(
            Database.connect(env["db_path"]), writer, env["registry_path"], stop
        )

        async def cancelled(_self: QueuedIngest, _job: object) -> TaskState:
            raise asyncio.CancelledError

        with (
            patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", env["projects_dir"]),
            patch.object(QueuedIngest, "run", cancelled),
        ):
            stats = await asyncio.to_thread(
                Backfill.sessions, BackfillConfig(), runtime
            )

        assert stats.errors == ["backfill stopped: daemon shutting down"]
        assert "Cancelled" not in " ".join(stats.errors)


class TestNotAttemptedCountsOnlyWhatStillNeededWork:
    def test_current_and_known_unindexable_transcripts_are_not_counted(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002", "bbbb0003")
        quiet = env["project_dir"] / "bbbb0002-0000-0000-0000-000000000000.jsonl"
        cache = UnindexableTranscripts(_state_file(env))
        cache.record(TranscriptRead(quiet, quiet.stat().st_mtime_ns, _NOW))
        cache.save()
        current = {
            "document_name": "session-bbbb0001",
            "document_path": "",
            "collection": "myproject-captures",
            "total_pages": 1,
            "chunk_count": 1,
            "indexed_pages": 1,
            "ingestion_timestamp": "2999-01-01T00:00:00",
        }
        stop = StopSignal()
        writer = RecordingWriter(after_write=lambda: stop.set("task cancelled"))

        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            return_value=[current],
        ):
            stats = _run(env, writer, stop=stop)

        # abcd1234 was written; bbbb0001 is current, bbbb0002 known unindexable.
        assert stats.ingested == 1
        assert stats.not_attempted == 1  # only bbbb0003 was left to do

    def test_a_project_the_run_never_reached_is_not_examined(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _add_project(env, tmp_path, "other")
        stop = StopSignal()
        stop.set("daemon shutting down")

        stats = _run(env, RecordingWriter(), stop=stop)

        assert stats.not_examined == 2
        assert stats.not_attempted == 0
        assert "2 not examined" in BackfillReport(stats).summary()


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestBackfillCLI:
    def test_help_text(self) -> None:
        result = runner.invoke(app, ["backfill-sessions", "--help"])
        assert result.exit_code == 0
        assert "backfill" in result.output.lower()
        assert "--dry-run" in result.output
        assert "--collection" in result.output
        assert "--project" in result.output
        assert "--limit" in result.output


class TestBackfillLimitDefaultIsAll:
    """The ``limit`` default is 0 ("all") on both the CLI and the wire contract.

    The exact divergence this feature removed was a non-zero default acting as a
    silent transcript cap. These assertions fail if a regression re-introduces
    one — e.g. ``limit: int = 500`` on ``BackfillRequest`` or a Typer default of
    500 on the ``--limit`` option — before it can reach a user.
    """

    def test_request_contract_default(self) -> None:
        from quarry.api import BackfillRequest

        assert BackfillRequest().limit == 0

    def test_cli_option_default(self) -> None:
        import inspect

        from quarry.cli_maintenance import MaintenanceCli

        params = inspect.signature(MaintenanceCli._backfill).parameters
        assert params["limit"].default == 0


class TestBackfillCaptureRedaction:
    """The backfill producer writes a PII-clean capture file (bug class 3)."""

    def _write(self, tmp_path: Path, text: str) -> Path:
        transcript = tmp_path / "sess1234abcd.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        # Pin the mtime: the capture frontmatter timestamp falls back to the
        # transcript's mtime, so rewriting the file on a rerun must not let a
        # straddled clock tick change the bytes this test asserts are identical.
        os.utime(transcript, (1_700_000_000, 1_700_000_000))
        BackfillSession(transcript, str(tmp_path), text).write_capture_file()
        return tmp_path / ".punt-labs" / "quarry" / "captures" / "session-sess1234.md"

    def test_capture_file_has_zero_pii(self, tmp_path: Path) -> None:
        capture = self._write(
            tmp_path, "ran /Users/jdoe/x and emailed jdoe@example.com"
        )
        content = capture.read_text(encoding="utf-8")
        assert "/Users/" not in content
        assert "@" not in content
        assert "~/x" in content

    def test_rerun_produces_identical_capture(self, tmp_path: Path) -> None:
        """A second backfill of the same transcript yields the same bytes."""
        capture = self._write(tmp_path, "path /Users/jane/proj here")
        first = capture.read_bytes()
        capture = self._write(tmp_path, "path /Users/jane/proj here")
        assert capture.read_bytes() == first

    def test_missing_transcript_degrades_instead_of_aborting(
        self, tmp_path: Path
    ) -> None:
        """A transcript deleted mid-run must not abort the backfill.

        ``stat()`` on a vanished file raises ``OSError``; the writer falls back
        to ``now()`` and still produces the capture rather than propagating.
        """
        missing = tmp_path / "sess1234abcd.jsonl"  # never created — stat() will raise

        BackfillSession(
            missing, str(tmp_path), "ran /Users/jdoe/x here"
        ).write_capture_file()

        captures = tmp_path / ".punt-labs" / "quarry" / "captures"
        content = (captures / "session-sess1234.md").read_text(encoding="utf-8")
        assert "~/x" in content
        assert "/Users/" not in content


class TestBackfillSessionBody:
    def test_the_body_is_the_text_when_there_are_no_artifacts(
        self, tmp_path: Path
    ) -> None:
        session = BackfillSession(tmp_path / "abcd1234.jsonl", "", "plain talk")
        assert session.body == "plain talk"

    def test_the_artifact_header_precedes_the_conversation(
        self, tmp_path: Path
    ) -> None:
        text = "opened PR #123 for the fix"
        session = BackfillSession(tmp_path / "abcd1234.jsonl", "", text)
        assert session.body.endswith(f"\n\n{text}")
        assert "123" in session.body.split("\n\n")[0]
