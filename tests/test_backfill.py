"""Tests for the backfill-sessions command and supporting functions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.backfill import BackfillConfig
from quarry.backfill_mapping import ProjectMapping, ProjectMappingResolver
from quarry.backfill_session import BackfillSession
from quarry.config import Settings
from quarry.sync_registry import DirectoryRegistration

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
# backfill_sessions integration
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
    from quarry.config import Settings

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


class TestBackfillSessions:
    def test_dry_run_no_writes(self, tmp_path: Path) -> None:
        from quarry.backfill import backfill_sessions

        env = _make_env(tmp_path)
        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")

        with (
            patch(
                "quarry.backfill_mapping.CLAUDE_PROJECTS_DIR",
                env["projects_dir"],
            ),
            patch("quarry.backfill.ingest_content") as mock_ingest,
        ):
            stats = backfill_sessions(settings, BackfillConfig(dry_run=True))

        mock_ingest.assert_not_called()
        assert stats.ingested == 1
        assert stats.skipped_existing == 0

    def test_skip_unregistered_projects(self, tmp_path: Path) -> None:
        from quarry.backfill import backfill_sessions
        from quarry.sync_registry import SyncRegistry

        env = _make_env(tmp_path)
        settings = _make_settings(env["db_path"], env["registry_path"])

        # Initialize empty registry (no registrations)
        conn = SyncRegistry(env["registry_path"])
        conn.close()

        with patch(
            "quarry.backfill_mapping.CLAUDE_PROJECTS_DIR",
            env["projects_dir"],
        ):
            stats = backfill_sessions(settings)

        assert stats.ingested == 0
        assert stats.skipped_unregistered == 1

    def test_dedup_skips_existing(self, tmp_path: Path) -> None:
        from quarry.backfill import backfill_sessions

        env = _make_env(tmp_path)
        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")

        fake_doc = {
            "document_name": "session-abcd1234",
            "document_path": "",
            "collection": "myproject-captures",
            "total_pages": 1,
            "chunk_count": 5,
            "indexed_pages": 1,
            "ingestion_timestamp": "2999-01-01T00:00:00",
        }
        with (
            patch(
                "quarry.backfill_mapping.CLAUDE_PROJECTS_DIR",
                env["projects_dir"],
            ),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
                return_value=[fake_doc],
            ),
            patch("quarry.backfill.ingest_content") as mock_ingest,
        ):
            stats = backfill_sessions(settings)

        mock_ingest.assert_not_called()
        assert stats.ingested == 0
        assert stats.skipped_existing == 1

    def test_a_session_that_grew_after_it_was_stored_is_ingested_again(
        self, tmp_path: Path
    ) -> None:
        """The compaction hook stores the session up to that moment; the rest of
        the conversation reaches the transcript afterwards, and backfill must file
        it over the partial document instead of skipping on its mere existence."""
        from quarry.backfill import backfill_sessions

        env = _make_env(tmp_path)
        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")

        partial = {
            "document_name": "session-abcd1234",
            "document_path": "",
            "collection": "myproject-captures",
            "total_pages": 1,
            "chunk_count": 5,
            "indexed_pages": 1,
            "ingestion_timestamp": "2000-01-01T00:00:00",
        }
        with (
            patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", env["projects_dir"]),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
                return_value=[partial],
            ),
            patch("quarry.backfill.ingest_content") as mock_ingest,
        ):
            stats = backfill_sessions(settings)

        mock_ingest.assert_called_once()
        assert mock_ingest.call_args.args[1] == "session-abcd1234"
        assert mock_ingest.call_args.args[3].overwrite is True
        assert stats.ingested == 1
        assert stats.skipped_existing == 0

    def test_the_snapshot_is_taken_before_the_transcript_is_read(
        self, tmp_path: Path
    ) -> None:
        """The stored time is when backfill read the transcript, not when it stored."""
        from datetime import UTC, datetime

        from quarry.backfill import backfill_sessions
        from quarry.transcript_reader import TranscriptReader

        env = _make_env(tmp_path)
        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")
        read_at: list[datetime] = []
        real_text = TranscriptReader.text

        def timed_text(self: TranscriptReader) -> str:
            read_at.append(datetime.now(UTC))
            return real_text(self)

        with (
            patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", env["projects_dir"]),
            patch.object(TranscriptReader, "text", timed_text),
            patch("quarry.backfill.ingest_content") as mock_ingest,
        ):
            backfill_sessions(settings)

        context = mock_ingest.call_args.args[3]
        assert context.ingested_at is not None
        assert context.ingested_at <= read_at[0]

    def test_an_ingest_failure_reports_the_class_not_the_message(
        self, tmp_path: Path
    ) -> None:
        from quarry.backfill import backfill_sessions

        env = _make_env(tmp_path)
        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")
        boom = OSError("/home/jane/secret-token-path")
        with (
            patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", env["projects_dir"]),
            patch("quarry.backfill.ingest_content", side_effect=boom),
        ):
            stats = backfill_sessions(settings)

        assert stats.errors == [f"{env['transcript'].name}: internal error: OSError"]

    def test_limit_flag(self, tmp_path: Path) -> None:
        from quarry.backfill import backfill_sessions

        env = _make_env(tmp_path)
        # Add a second transcript
        second = env["project_dir"] / "ef567890-0000-0000-0000-000000000000.jsonl"
        _make_transcript(
            second,
            [
                _user_message("second session"),
                _assistant_message("ok"),
            ],
        )

        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")

        with patch(
            "quarry.backfill_mapping.CLAUDE_PROJECTS_DIR",
            env["projects_dir"],
        ):
            stats = backfill_sessions(settings, BackfillConfig(dry_run=True, limit=1))

        assert stats.ingested == 1

    def test_collection_override(self, tmp_path: Path) -> None:
        from quarry.backfill import backfill_sessions

        env = _make_env(tmp_path)
        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")

        with patch(
            "quarry.backfill_mapping.CLAUDE_PROJECTS_DIR",
            env["projects_dir"],
        ):
            stats = backfill_sessions(
                settings,
                BackfillConfig(collection_override="my-override", dry_run=True),
            )

        assert stats.ingested == 1

    def test_empty_transcript_skipped(self, tmp_path: Path) -> None:
        from quarry.backfill import backfill_sessions

        env = _make_env(tmp_path)
        # Overwrite with empty content
        env["transcript"].write_text("")

        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")

        with (
            patch(
                "quarry.backfill_mapping.CLAUDE_PROJECTS_DIR",
                env["projects_dir"],
            ),
            patch("quarry.backfill.ingest_content") as mock_ingest,
        ):
            stats = backfill_sessions(settings)

        mock_ingest.assert_not_called()
        assert stats.ingested == 0
        assert stats.skipped_empty == 1

    def test_ingested_text_is_scrubbed(self, tmp_path: Path) -> None:
        """Transcript text is scrubbed before store — the DES-036 leak lock.

        Backfill routes through the pipeline choke point (it passes a
        content_scrubber, no manual pre-scrub), so ingest_content redacts the
        content before store.  A secret token and an email in a transcript must
        never reach the searchable LanceDB store (retrievable via /v1/search).
        Patching the store boundary asserts the REAL pipeline scrub ran.
        """
        from quarry.backfill import backfill_sessions
        from quarry.ingestion.streaming import DocumentStreamer

        fake_pat = "ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"
        email = "leak.user@evil-example.com"
        env = _make_env(tmp_path)
        _make_transcript(
            env["transcript"],
            [_user_message(f"my token is {fake_pat} and my email is {email}")],
        )
        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")

        stored: list[object] = []

        def _capture_build_chunks(
            _self: DocumentStreamer, pages: list[object], _labels: object
        ) -> list[object]:
            stored.extend(pages)
            return []

        with (
            patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", env["projects_dir"]),
            patch.object(DocumentStreamer, "build_chunks", _capture_build_chunks),
            patch("quarry.db.chunk_store.ChunkStore.delete_document"),
        ):
            backfill_sessions(settings)

        assert stored, "ingest_content never reached the chunker"
        joined = " ".join(page.text for page in stored)  # type: ignore[attr-defined]
        assert fake_pat not in joined
        assert email not in joined
        assert "[REDACTED:gh-pat]" in joined
        assert "[REDACTED:email]" in joined


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
