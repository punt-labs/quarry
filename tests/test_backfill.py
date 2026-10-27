"""Tests for the backfill-sessions command and supporting functions."""

from __future__ import annotations

import asyncio
import builtins
import errno
import inspect
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.api import BackfillRequest
from quarry.atomic_file import AtomicFile
from quarry.backfill import Backfill, BackfillConfig, BackfillRuntime
from quarry.backfill_mapping import ProjectMapping, ProjectMappingResolver
from quarry.backfill_run import BackfillRun
from quarry.backfill_session import BackfillSession
from quarry.backfill_stats import BackfillStats
from quarry.backfill_unindexable import UnindexableTranscripts
from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.capture import CaptureRequest, CaptureWriter
from quarry.capture_directory import CaptureDirectory
from quarry.capture_outcome import CaptureOutcome, CaptureResult
from quarry.capture_reaper import CaptureReaper
from quarry.cli_maintenance import MaintenanceCli
from quarry.config import Settings
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.queued_ingest import QueuedIngest, QueuePolicy
from quarry.daemon.run_record import RunRecord
from quarry.daemon.tasks import TaskState
from quarry.db.chunk_catalog import ChunkCatalog
from quarry.db.facade import Database
from quarry.file_stamp import FileStamp
from quarry.ingestion.streaming import DocumentStreamer
from quarry.loaded_transcript import LoadedTranscript
from quarry.results import DocumentSummary
from quarry.run_halt import RunHalt
from quarry.shared_project_dirs import SharedProjectDirs
from quarry.stop_signal import StopSignal
from quarry.sync_registry import DirectoryRegistration, SyncRegistry
from quarry.transcript_loader import TranscriptLoader
from quarry.transcript_read import TranscriptRead
from quarry.transcript_reader import TranscriptReader
from quarry.transcript_records import TranscriptRecords
from quarry.transcript_snapshot import TranscriptSnapshot
from tests.queue_fixtures import ctx_with_queue, writer_for
from tests.transcript_writers import DirectWriter, RecordingWriter, runtime_over

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from quarry.models import PageContent

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
        field = "dry_run"
        with pytest.raises(AttributeError):
            setattr(cfg, field, True)


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

        with patch(
            "quarry.home_paths.QuarryHome.claude_projects", return_value=projects_dir
        ):
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

        with patch(
            "quarry.home_paths.QuarryHome.claude_projects", return_value=projects_dir
        ):
            mappings = ProjectMappingResolver.resolve_all([reg])

        assert len(mappings) == 0

    def test_no_projects_dir(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "does-not-exist"

        with patch(
            "quarry.home_paths.QuarryHome.claude_projects", return_value=projects_dir
        ):
            mappings = ProjectMappingResolver.resolve_all([])

        assert mappings == []


class TestSharedProjectDir:
    """Two registrations that Claude Code encodes to one projects dir (#557)."""

    @staticmethod
    def _shared_env(tmp_path: Path) -> tuple[Path, list[DirectoryRegistration]]:
        projects_dir = tmp_path / ".claude" / "projects"
        shared = projects_dir / "-w-my-notes"
        shared.mkdir(parents=True)
        for name, cwd in (("dot", "/w/my.notes"), ("under", "/w/my_notes")):
            record = {"type": "user", "cwd": cwd}
            (shared / f"{name}.jsonl").write_text(json.dumps(record) + "\n")
        (shared / "nocwd.jsonl").write_text(json.dumps({"type": "user"}) + "\n")
        regs = [
            DirectoryRegistration(
                directory=directory,
                collection=collection,
                registered_at="2025-01-01T00:00:00",
            )
            for directory, collection in (("/w/my.notes", "dot"), ("/w/my_notes", "u"))
        ]
        return projects_dir, regs

    def test_each_registration_gets_only_the_transcripts_run_in_it(
        self, tmp_path: Path
    ) -> None:
        projects_dir, regs = self._shared_env(tmp_path)
        with patch(
            "quarry.home_paths.QuarryHome.claude_projects", return_value=projects_dir
        ):
            mappings = ProjectMappingResolver.resolve_all(regs)
            files = {m.collection: m.transcript_files() for m in mappings}

        assert all(m.shared for m in mappings)
        assert [f.name for f in files["dot"]] == ["dot.jsonl"]
        assert [f.name for f in files["u"]] == ["under.jsonl"]

    def test_a_sole_registration_keeps_every_transcript(self, tmp_path: Path) -> None:
        projects_dir, regs = self._shared_env(tmp_path)
        with patch(
            "quarry.home_paths.QuarryHome.claude_projects", return_value=projects_dir
        ):
            (mapping,) = ProjectMappingResolver.resolve_all(regs[:1])
            files = mapping.transcript_files()

        assert not mapping.shared
        assert len(files) == 3

    def test_the_collision_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        projects_dir, regs = self._shared_env(tmp_path)
        with patch(
            "quarry.home_paths.QuarryHome.claude_projects", return_value=projects_dir
        ):
            ProjectMappingResolver.resolve_all(regs)

        assert "-w-my-notes is shared by 2 registrations" in caplog.text

    def test_an_unreadable_transcript_is_not_attributed(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.jsonl"
        assert SharedProjectDirs.recorded_cwd(missing) is None

    def test_a_transcript_that_cannot_be_opened_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        directory = tmp_path / "-w-my-notes" / "dir.jsonl"
        directory.mkdir(parents=True)

        assert SharedProjectDirs.recorded_cwd(directory) is None
        assert "transcript in shared dir -w-my-notes could not be read" in caplog.text

    @pytest.mark.parametrize(
        "bad",
        ["{not json", "[" * 100_000, json.dumps({"cwd": 7}), json.dumps(["/w/x"])],
        ids=["malformed", "too-deep", "non-string-cwd", "not-an-object"],
    )
    def test_a_bad_record_is_passed_over(self, tmp_path: Path, bad: str) -> None:
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(bad + "\n" + json.dumps({"cwd": "/w/my.notes"}) + "\n")
        assert SharedProjectDirs.recorded_cwd(transcript) == "/w/my.notes"

    def test_only_bad_records_attribute_to_no_project(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("{not json\n" + json.dumps({"cwd": None}) + "\n")
        assert SharedProjectDirs.recorded_cwd(transcript) is None

    def test_a_cwd_beyond_the_scanned_head_is_not_read(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.jsonl"
        filler = json.dumps({"text": "x" * (2 << 20)})
        transcript.write_text(filler + "\n" + json.dumps({"cwd": "/w/my.notes"}) + "\n")
        assert SharedProjectDirs.recorded_cwd(transcript) is None

    def test_sharing_registrations_scan_each_transcript_once(
        self, tmp_path: Path
    ) -> None:
        projects_dir, regs = self._shared_env(tmp_path)
        opened: list[str] = []
        real_open = Path.open

        def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            opened.append(self.name)
            return real_open(self, *args, **kwargs)

        with (
            patch(
                "quarry.home_paths.QuarryHome.claude_projects",
                return_value=projects_dir,
            ),
            patch.object(Path, "open", counting_open),
        ):
            for mapping in ProjectMappingResolver.resolve_all(regs):
                mapping.transcript_files()

        assert sorted(opened) == ["dot.jsonl", "nocwd.jsonl", "under.jsonl"]


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

        with patch(
            "quarry.home_paths.QuarryHome.claude_projects", return_value=projects_dir
        ):
            mapping = ProjectMapping("my-project", "", "")
            files = mapping.transcript_files()

        assert len(files) == 2
        assert all(f.suffix == ".jsonl" for f in files)

    def test_empty_directory(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        project = projects_dir / "empty"
        project.mkdir(parents=True)

        with patch(
            "quarry.home_paths.QuarryHome.claude_projects", return_value=projects_dir
        ):
            mapping = ProjectMapping("empty", "", "")
            files = mapping.transcript_files()

        assert files == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)

        with patch(
            "quarry.home_paths.QuarryHome.claude_projects", return_value=projects_dir
        ):
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


# A transcript's mtime outside the racy window of any read the tests make.
_LONG_AGO = 1_700_000_000


def _make_transcript(path: Path, messages: list[dict[str, object]]) -> None:
    """Write a minimal JSONL transcript file, last modified long enough ago to trust."""
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")
    os.utime(path, (_LONG_AGO, _LONG_AGO))


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
    with patch(
        "quarry.home_paths.QuarryHome.claude_projects", return_value=env["projects_dir"]
    ):
        return Backfill.sessions(config or BackfillConfig(), runtime)


def _registered_env(tmp_path: Path) -> dict[str, Path]:
    env = _make_env(tmp_path)
    _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")
    return env


def _stored_doc(version: str) -> dict[str, object]:
    return {
        "document_name": "session-abcd1234",
        "document_path": "",
        "collection": "myproject-captures",
        "total_pages": 1,
        "chunk_count": 5,
        "indexed_pages": 1,
        "ingestion_timestamp": "2000-01-01T00:00:00",
        "source_version": version,
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

        env = _make_env(tmp_path)
        # Initialize empty registry (no registrations)
        SyncRegistry(env["registry_path"]).close()

        stats = _run(env, RecordingWriter())

        assert stats.ingested == 0
        assert stats.skipped_unregistered == 1

    def test_the_registry_is_read_once_and_closed(self, tmp_path: Path) -> None:
        env = _registered_env(tmp_path)

        with patch.object(SyncRegistry, "close") as close:
            _run(env, RecordingWriter())

        close.assert_called_once()

    def test_dedup_skips_existing(self, tmp_path: Path) -> None:
        writer = RecordingWriter()
        env = _registered_env(tmp_path)
        current = TranscriptSnapshot.taken(env["transcript"]).version
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            return_value=[_stored_doc(current)],
        ):
            stats = _run(env, writer)

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
        env = _registered_env(tmp_path)
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            return_value=[_stored_doc("1:1")],
        ):
            stats = _run(env, writer)

        [call] = writer.calls
        assert call.name == "session-abcd1234"
        assert call.collection == "myproject-captures"
        assert call.version == TranscriptSnapshot.taken(env["transcript"]).version
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
        _blank(env["transcript"])
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

        fake_pat = "ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"
        email = "leak.user@evil-example.com"
        env = _registered_env(tmp_path)
        _make_transcript(
            env["transcript"],
            [_user_message(f"my token is {fake_pat} and my email is {email}")],
        )
        settings = _make_settings(env["db_path"], env["registry_path"])
        writer = DirectWriter(Database.connect(env["db_path"]), settings)

        stored: list[PageContent] = []

        def _capture_build_chunks(
            _self: DocumentStreamer, pages: list[PageContent], _labels: object
        ) -> list[object]:
            stored.extend(pages)
            return []

        with (
            patch.object(DocumentStreamer, "build_chunks", _capture_build_chunks),
            patch("quarry.db.chunk_store.ChunkStore.delete_document"),
        ):
            _run(env, writer)

        assert stored, "the job never reached the chunker"
        joined = " ".join(page.text for page in stored)
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
        _blank(env["transcript"])

        stats = _run(env, RecordingWriter(), BackfillConfig(dry_run=True))

        assert stats.skipped_empty == 1
        assert not env["registry_path"].with_name("backfill-unindexable.json").exists()


class TestEmptyAndUnreadable:
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_they_are_counted_apart_in_a_real_run_and_a_dry_run(
        self, tmp_path: Path, *, dry_run: bool
    ) -> None:
        env = _registered_env(tmp_path)
        _blank(env["project_dir"] / "dddd0001-0000.jsonl")
        (env["project_dir"] / "eeee0001-0000.jsonl").mkdir()  # not a transcript

        stats = _run(env, RecordingWriter(), BackfillConfig(dry_run=dry_run))

        assert stats.skipped_empty == 1
        assert stats.skipped_unreadable == 0  # a directory is not listed at all
        assert stats.would_ingest + stats.ingested == 1

    def test_a_transcript_that_vanished_after_it_was_listed_is_a_skip(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        gone = env["project_dir"] / "ffff0001-0000.jsonl"
        listed = ProjectMapping.transcript_files

        def listing(self: ProjectMapping) -> list[Path]:
            return [*listed(self), gone]

        with patch.object(ProjectMapping, "transcript_files", listing):
            stats = _run(env, RecordingWriter())

        assert stats.skipped_unreadable == 1
        assert stats.failed == 0
        assert stats.errors == []


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

    def test_a_writer_whose_job_had_started_when_it_gave_up_counts_it_failed(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002")
        writer = RecordingWriter(
            BackfillWriteError("task cancelled").as_outcome(
                WriteOutcome.ABORTED_RUNNING
            )
        )

        stats = _run(env, writer)

        assert len(writer.calls) == 1
        assert stats.failed == 1  # it started: it may still be stored
        assert stats.not_attempted == 2  # the two it never reached
        assert stats.errors[0] == "backfill stopped: task cancelled"
        assert any("task cancelled" in e for e in stats.errors[1:])

    def test_a_job_past_its_commit_when_it_gave_up_is_unconfirmed_not_stored(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001")
        writer = RecordingWriter(
            BackfillWriteError("task cancelled").as_outcome(
                WriteOutcome.ABORTED_COMMITTING
            )
        )

        with caplog.at_level(logging.INFO):
            stats = _run(env, writer)
        again = RecordingWriter()
        _run(env, again)

        assert stats.stored_unconfirmed == 1
        assert (stats.ingested, stats.failed) == (0, 0)  # neither stored nor lost
        assert stats.errors[0] == "backfill stopped: task cancelled"
        assert "1 stored unconfirmed" in stats.summary()
        [line] = [
            r
            for r in _library_records(caplog)
            if env["transcript"].name in r.getMessage()
        ]
        assert line.levelno == logging.WARNING
        # no stamp was recorded, so the next run files it again
        assert len(again.calls) == 2

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

        assert len(writer.calls) == RunHalt.MAX_FULL
        assert stats.failed == RunHalt.MAX_FULL
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

        with patch(
            "quarry.capture.CaptureWriter.outcome",
            return_value=CaptureResult(CaptureOutcome.FAILED, "ENOSPC"),
        ):
            stats = _run(env, writer)

        assert writer.calls == []  # not handed on as if it had been written
        assert stats.failed == 1
        assert stats.errors == [
            f"{env['transcript'].name}: capture file could not be written (ENOSPC)"
        ]


class TestARefusedTranscriptIsCountedRefusedEveryRunAndNeverConsumesTheLimit:
    @staticmethod
    def _big(path: Path) -> None:
        path.write_text("x" * (1024 * 1024 + 1))
        os.utime(path, (_LONG_AGO, _LONG_AGO))

    def test_an_oversized_transcript_is_refused_on_every_run_never_as_empty(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        self._big(env["transcript"])
        loads: list[str] = []
        real_load = TranscriptLoader.load

        def counting(path: Path) -> LoadedTranscript:
            loads.append(path.name)
            return real_load(path)

        with (
            patch.object(TranscriptLoader, "MAX_BYTES", 1024 * 1024),
            patch.object(TranscriptLoader, "load", counting),
        ):
            first = _run(env, RecordingWriter())
            second = _run(env, RecordingWriter())

        note = f"{env['transcript'].name}: transcript is over the 1 MiB backfill limit"
        for stats in (first, second):
            assert (stats.skipped_refused, stats.failed, stats.skipped_empty) == (
                1,
                0,
                0,
            )
            assert stats.errors == [note]  # one line per run
            assert "1 refused" in stats.summary()
        # re-detected from st_size each run, never cached: the refusal never
        # reads a byte, so it costs a stat, not a read
        assert loads == [env["transcript"].name] * 2

    def test_a_transcript_that_changed_since_it_was_refused_is_looked_at_again(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        self._big(env["transcript"])
        with patch.object(TranscriptLoader, "MAX_BYTES", 1024 * 1024):
            _run(env, RecordingWriter())
            _make_transcript(env["transcript"], [_user_message("small now")])
            again = RecordingWriter()
            stats = _run(env, again)

        assert [call.name for call in again.calls] == ["session-abcd1234"]
        assert (stats.failed, stats.skipped_refused) == (0, 0)

    def test_a_pipe_in_the_project_is_refused_without_holding_the_scan(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        fifo = env["project_dir"] / "ffff0001-0000-0000-0000-000000000000.jsonl"
        os.mkfifo(fifo)
        os.utime(fifo, (_LONG_AGO, _LONG_AGO))
        started = time.monotonic()

        stats = _run(env, RecordingWriter())

        assert time.monotonic() - started < 10
        assert (stats.skipped_refused, stats.failed) == (1, 0)
        assert stats.errors == [f"{fifo.name}: transcript is not a regular file"]
        assert stats.ingested == 1  # the real transcript beside it was filed

    def test_a_read_fault_does_not_use_up_the_limit(self, tmp_path: Path) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001")
        real_open = builtins.open

        def fails_for_the_first(path: Path, *args: Any, **kwargs: Any) -> object:
            if Path(path).name.startswith("abcd1234"):
                raise OSError(errno.EMFILE, "Too many open files")
            return real_open(path, *args, **kwargs)

        writer = RecordingWriter()
        with patch("quarry.transcript_loader.open", fails_for_the_first, create=True):
            stats = _run(env, writer, BackfillConfig(limit=1))

        assert stats.failed == 1  # the unreadable one, counted ...
        assert [call.name for call in writer.calls] == ["session-bbbb0001"]  # ... free
        assert stats.ingested == 1


class TestATranscriptPastTheRecordBound:
    @staticmethod
    def _many(env: dict[str, Path], count: int) -> None:
        _make_transcript(
            env["transcript"], [_user_message(f"turn {n}") for n in range(count)]
        )

    def test_it_is_stored_from_its_newest_records_and_counted_truncated(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)
        self._many(env, 8)
        writer = RecordingWriter()

        with (
            patch.object(TranscriptRecords, "MAX", 5),
            patch.object(TranscriptRecords, "TRIM", 2),
            caplog.at_level(logging.WARNING),
        ):
            stats = _run(env, writer)

        assert (stats.truncated, stats.ingested) == (1, 1)
        assert "1 truncated" in stats.summary()
        assert "turn 7" in writer.calls[0].body
        assert "turn 0" not in writer.calls[0].body
        [line] = [
            r
            for r in caplog.records
            if "oldest records past the record bound" in r.getMessage()
        ]
        assert env["transcript"].name in line.getMessage()
        assert "3 oldest" in line.getMessage()

    def test_a_truncated_read_with_no_text_is_refused_and_never_recorded(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        self._many(env, 3)
        real_load = TranscriptLoader.load

        def loses_its_text(path: Path) -> LoadedTranscript:
            loaded = real_load(path)
            return LoadedTranscript(loaded.read, "", truncated=2)

        with patch.object(TranscriptLoader, "load", loses_its_text):
            first = _run(env, RecordingWriter())
            second = _run(env, RecordingWriter())

        reason = "transcript tail holds no text past the record bound"
        for stats in (first, second):  # not remembered, so seen again every run
            assert (stats.skipped_refused, stats.skipped_empty) == (1, 0)
            assert stats.errors == [f"{env['transcript'].name}: {reason}"]


class TestACaptureThatWasNotWrittenSaysWhy:
    def test_a_symlink_where_the_capture_goes_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        target = env["real_project"] / ".punt-labs" / "quarry" / "captures"
        target.mkdir(parents=True)
        (target / "session-abcd1234.md").symlink_to(tmp_path / "elsewhere.md")
        writer = RecordingWriter()

        stats = _run(env, writer)

        assert writer.calls == []
        assert stats.errors == [
            (
                f"{env['transcript'].name}: "
                "capture refused: symlink or non-directory in path"
            )
        ]
        assert not (tmp_path / "elsewhere.md").exists()

    def test_nothing_to_capture_is_a_skip_not_a_failure(self, tmp_path: Path) -> None:
        env = _registered_env(tmp_path)
        writer = RecordingWriter()

        with patch(
            "quarry.capture.CaptureWriter.outcome",
            return_value=CaptureResult(CaptureOutcome.EMPTY),
        ):
            stats = _run(env, writer)

        assert writer.calls == []
        assert stats.skipped_empty == 1
        assert stats.failed == 0
        assert stats.errors == []

    def test_nothing_to_capture_is_not_remembered_so_a_later_run_looks_again(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        with patch(
            "quarry.capture.CaptureWriter.outcome",
            return_value=CaptureResult(CaptureOutcome.EMPTY),
        ):
            _run(env, RecordingWriter())
        later = RecordingWriter()

        stats = _run(env, later)

        assert [call.name for call in later.calls] == ["session-abcd1234"]
        assert stats.skipped_empty == 0


class TestACrashedCaptureWritesLeftoversAreReapedOncePerProject:
    def test_the_stale_temp_file_of_a_crashed_write_is_removed_by_the_run(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        captures = env["real_project"].joinpath(*CaptureDirectory.RELATIVE)
        captures.mkdir(parents=True)
        stale = captures / ".session-feed0001.md.x1y2z3.tmp"
        stale.write_text("half")
        os.utime(stale, (_LONG_AGO, _LONG_AGO))

        _run(env, RecordingWriter())

        assert not stale.exists()

    def test_it_happens_once_for_a_project_not_once_per_transcript(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002", "bbbb0003")

        with patch.object(CaptureReaper, "reap", return_value=0) as reap:
            _run(env, RecordingWriter())

        assert reap.call_count == 1

    def test_unlocked_capture_writes_are_counted_in_the_summary(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001")

        with patch("fcntl.flock", side_effect=OSError(errno.ENOLCK, "no locks")):
            stats = _run(env, RecordingWriter())

        assert stats.unlocked_captures == 2
        assert "2 captures written unlocked" in stats.summary()

    def test_a_dry_run_reaps_nothing_and_never_begins_the_project(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        captures = env["real_project"].joinpath(*CaptureDirectory.RELATIVE)
        captures.mkdir(parents=True)
        stale = captures / ".session-feed0001.md.x1y2z3.tmp"
        stale.write_text("half")
        os.utime(stale, (_LONG_AGO, _LONG_AGO))

        with patch.object(CaptureReaper, "reap") as reap:
            _run(env, RecordingWriter(), BackfillConfig(dry_run=True))

        assert reap.call_count == 0
        assert stale.exists()  # a dry run deletes nothing

    def test_a_captures_directory_that_cannot_be_swept_does_not_stop_the_run(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        captures = env["real_project"].joinpath(*CaptureDirectory.RELATIVE)
        captures.mkdir(parents=True)
        captures.chmod(0)
        writer = RecordingWriter()
        try:
            stats = _run(env, writer)
        finally:
            captures.chmod(0o700)

        # the sweep failed (EACCES), and so did the capture write: the one
        # transcript fails by name, the run reports it instead of crashing
        assert stats.failed == 1
        assert stats.errors == [
            f"{env['transcript'].name}: capture file could not be written (EACCES)"
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


def _blank(path: Path) -> None:
    """Empty *path*, leaving its mtime where it was long enough ago to trust."""
    path.write_text("")
    os.utime(path, (_LONG_AGO, _LONG_AGO))


def _bump_mtime(path: Path) -> None:
    """Make *path* look newer, as if it had been appended to."""
    later = path.stat().st_mtime + 60
    os.utime(path, (later, later))


def _recorded(env: dict[str, Path]) -> dict[str, int]:
    state = _state_file(env)
    return json.loads(state.read_text()) if state.exists() else {}


_EMFILE_LINE = "abcd1234-0000-0000-0000-000000000000.jsonl: unreadable (EMFILE)"


class TestAReadFaultIsAFailureNeverAnEmptyTranscript:
    @staticmethod
    def _failing_open(errno_code: int) -> AbstractContextManager[object]:
        real_open = builtins.open

        def fails(path: Path, *args: Any, **kwargs: Any) -> object:
            if Path(path).suffix == ".jsonl":
                raise OSError(errno_code, os.strerror(errno_code))
            return real_open(path, *args, **kwargs)

        return patch("quarry.transcript_loader.open", fails, create=True)

    def test_a_read_error_is_a_failure_never_recorded_and_retried_next_run(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = _registered_env(tmp_path)

        with self._failing_open(errno.EMFILE), caplog.at_level(logging.WARNING):
            stats = _run(env, RecordingWriter())

        assert stats.failed == 1
        assert stats.skipped_unreadable == 0
        assert stats.skipped_empty == 0
        assert stats.errors == [
            "abcd1234-0000-0000-0000-000000000000.jsonl: unreadable (EMFILE)"
        ]
        assert _recorded(env) == {}
        assert "EMFILE" in caplog.text
        again = RecordingWriter()
        _run(env, again)
        assert [call.name for call in again.calls] == ["session-abcd1234"]

    def test_a_run_whose_every_read_fails_ends_failed_with_the_reason(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002")

        with self._failing_open(errno.EMFILE):
            stats = _run(env, RecordingWriter())
        state = TaskState(task_id="t", kind="backfill")
        RunRecord(state, StopSignal(), dry_run=False).record(stats)

        assert stats.failed == 3
        assert stats.stored_nothing
        assert state.status == "failed"
        assert state.error == f"backfill stored nothing: {_EMFILE_LINE}"

    def test_a_transcript_that_is_not_utf8_is_a_failure(self, tmp_path: Path) -> None:
        env = _registered_env(tmp_path)
        env["transcript"].write_bytes(b"\xff\xfe not utf-8")
        os.utime(env["transcript"], (_LONG_AGO, _LONG_AGO))

        stats = _run(env, RecordingWriter())

        assert stats.failed == 1
        name = "abcd1234-0000-0000-0000-000000000000.jsonl"
        assert stats.errors == [f"{name}: unreadable (UnicodeDecodeError)"]


class TestATranscriptThatChangedIsNotRecorded:
    def test_text_appended_between_the_read_and_the_record_is_not_skipped_next_time(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _blank(env["transcript"])
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
        _blank(env["transcript"])  # something to remember, so a save happens

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
            patch.object(BackfillRun, "_process", side_effect=RuntimeError("the run")),
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
        real = CaptureWriter.outcome

        def capture_fails_for_the_second(
            self: CaptureWriter, request: CaptureRequest
        ) -> CaptureResult:
            if request.session_id.startswith("bbbb0001"):
                return CaptureResult(CaptureOutcome.FAILED, "EIO")
            return real(self, request)

        with patch.object(CaptureWriter, "outcome", capture_fails_for_the_second):
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
            patch(
                "quarry.home_paths.QuarryHome.claude_projects",
                return_value=env["projects_dir"],
            ),
            patch.object(ScrubbedIngestJob, "run", hangs),
        ):
            stats = await asyncio.to_thread(
                Backfill.sessions, BackfillConfig(), runtime
            )

        assert ran == ["session-abcd1234"]  # not one orphan per transcript
        assert stats.failed == 1  # the job that hung had started: counted, not lost
        assert stats.not_attempted == 2
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

        async def cancelled(
            _self: QueuedIngest, _job: object, _state: TaskState
        ) -> TaskState:
            raise asyncio.CancelledError

        with (
            patch(
                "quarry.home_paths.QuarryHome.claude_projects",
                return_value=env["projects_dir"],
            ),
            patch.object(QueuedIngest, "run_tracked", cancelled),
        ):
            stats = await asyncio.to_thread(
                Backfill.sessions, BackfillConfig(), runtime
            )

        assert stats.errors == ["backfill stopped: daemon shutting down"]
        assert "Cancelled" not in " ".join(stats.errors)


class TestTheStopIsCheckedBeforeEveryBlockingStep:
    def test_a_stop_before_a_project_is_read_never_lists_its_store(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        stop = StopSignal()
        stop.set("task cancelled")

        with patch.object(ChunkCatalog, "list_documents") as listing:
            stats = _run(env, RecordingWriter(), stop=stop)

        listing.assert_not_called()
        assert stats.not_examined == 1

    def test_a_stop_while_a_transcript_is_read_writes_no_capture_file(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        stop = StopSignal()
        real_load = TranscriptLoader.load

        def stops_during_the_read(path: Path) -> LoadedTranscript:
            loaded = real_load(path)
            stop.set("task cancelled")
            return loaded

        with (
            patch.object(TranscriptLoader, "load", stops_during_the_read),
            patch.object(CaptureWriter, "outcome") as capture,
        ):
            stats = _run(env, RecordingWriter(), stop=stop)

        capture.assert_not_called()
        assert stats.not_attempted == 1
        assert stats.failed == 0

    def test_a_stop_while_the_capture_file_is_written_never_reaches_the_writer(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        stop = StopSignal()
        writer = RecordingWriter()

        def stops_during_the_capture(
            _self: CaptureWriter, _request: CaptureRequest
        ) -> CaptureResult:
            stop.set("daemon shutting down")
            return CaptureResult(CaptureOutcome.WRITTEN)

        with patch.object(CaptureWriter, "outcome", stops_during_the_capture):
            stats = _run(env, writer, stop=stop)

        assert writer.calls == []  # nothing was handed to the scrub and the queue
        assert stats.not_attempted == 1
        assert stats.errors == ["backfill stopped: daemon shutting down"]


class TestARecentTranscriptIsNeverRememberedAsEmpty:
    def test_one_modified_a_moment_before_the_read_is_read_again_next_run(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        env["transcript"].write_text("")  # modified just now: inside the racy window

        stats = _run(env, RecordingWriter())

        assert stats.skipped_empty == 1
        assert _recorded(env) == {}

    def test_one_that_has_been_still_for_a_while_is_remembered(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _blank(env["transcript"])

        _run(env, RecordingWriter())

        assert list(_recorded(env)) == [str(env["transcript"])]


class TestNotAttemptedCountsOnlyWhatStillNeededWork:
    def test_current_and_known_unindexable_transcripts_are_not_counted(
        self, tmp_path: Path
    ) -> None:
        env = _registered_env(tmp_path)
        _write_sessions(env, "bbbb0001", "bbbb0002", "bbbb0003")
        quiet = env["project_dir"] / "bbbb0002-0000-0000-0000-000000000000.jsonl"
        cache = UnindexableTranscripts(_state_file(env))
        cache.record(TranscriptRead(quiet, FileStamp.of(quiet.stat()), _NOW))
        cache.save()
        current = {
            "document_name": "session-bbbb0001",
            "document_path": "",
            "collection": "myproject-captures",
            "total_pages": 1,
            "chunk_count": 1,
            "indexed_pages": 1,
            "ingestion_timestamp": "2000-01-01T00:00:00",
            "source_version": TranscriptSnapshot.taken(
                env["project_dir"] / "bbbb0001-0000-0000-0000-000000000000.jsonl"
            ).version,
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
        assert "2 not examined" in stats.summary()


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

        assert BackfillRequest().limit == 0

    def test_cli_option_default(self) -> None:

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
        session = BackfillSession(transcript, str(tmp_path), text)
        CaptureWriter().outcome(session.capture_request())
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

        session = BackfillSession(missing, str(tmp_path), "ran /Users/jdoe/x here")
        CaptureWriter().outcome(session.capture_request())

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
