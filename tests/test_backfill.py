"""Tests for the backfill-sessions command and supporting functions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.backfill import (
    build_project_mappings,
    document_name_for_transcript,
    encode_project_path,
    is_already_ingested,
    list_transcript_files,
)
from quarry.config import Settings
from quarry.sync_registry import DirectoryRegistration

runner = CliRunner()


# ---------------------------------------------------------------------------
# encode_project_path
# ---------------------------------------------------------------------------


class TestEncodeProjectPath:
    def test_simple_path(self) -> None:
        assert encode_project_path("/Users/jfreeman/code") == "-Users-jfreeman-code"

    def test_path_with_hyphens(self) -> None:
        result = encode_project_path("/Users/jfreeman/Coding/punt-labs/quarry")
        assert result == "-Users-jfreeman-Coding-punt-labs-quarry"

    def test_root_path(self) -> None:
        assert encode_project_path("/") == "-"

    def test_single_component(self) -> None:
        assert encode_project_path("/home") == "-home"


# ---------------------------------------------------------------------------
# build_project_mappings
# ---------------------------------------------------------------------------


class TestBuildProjectMappings:
    def test_matches_registration_to_directory(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        encoded = "-Users-jfreeman-Coding-punt-labs-quarry"
        (projects_dir / encoded).mkdir(parents=True)

        reg = DirectoryRegistration(
            directory="/Users/jfreeman/Coding/punt-labs/quarry",
            collection="quarry",
            registered_at="2025-01-01T00:00:00",
        )

        with patch("quarry.backfill.CLAUDE_PROJECTS_DIR", projects_dir):
            mappings = build_project_mappings([reg])

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

        with patch("quarry.backfill.CLAUDE_PROJECTS_DIR", projects_dir):
            mappings = build_project_mappings([reg])

        assert len(mappings) == 0

    def test_no_projects_dir(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "does-not-exist"

        with patch("quarry.backfill.CLAUDE_PROJECTS_DIR", projects_dir):
            mappings = build_project_mappings([])

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

        with patch("quarry.backfill.CLAUDE_PROJECTS_DIR", projects_dir):
            files = list_transcript_files("my-project")

        assert len(files) == 2
        assert all(f.suffix == ".jsonl" for f in files)

    def test_empty_directory(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        project = projects_dir / "empty"
        project.mkdir(parents=True)

        with patch("quarry.backfill.CLAUDE_PROJECTS_DIR", projects_dir):
            files = list_transcript_files("empty")

        assert files == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)

        with patch("quarry.backfill.CLAUDE_PROJECTS_DIR", projects_dir):
            files = list_transcript_files("nonexistent")

        assert files == []


# ---------------------------------------------------------------------------
# document_name_for_transcript
# ---------------------------------------------------------------------------


class TestDocumentNameForTranscript:
    def test_uses_session_prefix_and_mtime(self, tmp_path: Path) -> None:
        name = "1e7aa08d-c485-45d1-8228-54d1a375c812.jsonl"
        transcript = tmp_path / name
        transcript.write_text("{}")

        result = document_name_for_transcript(transcript)

        assert result.startswith("session-1e7aa08d-")
        parts = result.split("-", 2)
        assert len(parts) == 3
        assert parts[0] == "session"
        assert parts[1] == "1e7aa08d"


# ---------------------------------------------------------------------------
# is_already_ingested
# ---------------------------------------------------------------------------


class TestIsAlreadyIngested:
    def test_found(self) -> None:
        existing = {
            "session-1e7aa08d-20250101T000000",
            "session-abcd1234-20250102T000000",
        }
        assert is_already_ingested("1e7aa08d", existing) is True

    def test_not_found(self) -> None:
        existing = {"session-abcd1234-20250102T000000"}
        assert is_already_ingested("1e7aa08d", existing) is False

    def test_empty_set(self) -> None:
        assert is_already_ingested("1e7aa08d", set()) is False


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
    from quarry.sync_registry import (
        open_registry,
        register_directory,
    )

    Path(project_path).mkdir(parents=True, exist_ok=True)
    conn = open_registry(registry_path)
    try:
        register_directory(conn, Path(project_path), collection)
    finally:
        conn.close()


def _make_settings(db_path: Path, registry_path: Path) -> Settings:
    """Load settings with overridden paths."""
    from quarry.config import (
        load_settings,
        resolve_db_paths,
    )

    settings = resolve_db_paths(load_settings(), None)
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
    encoded = encode_project_path(str(real_project.resolve()))

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
                "quarry.backfill.CLAUDE_PROJECTS_DIR",
                env["projects_dir"],
            ),
            patch("quarry.backfill.ingest_content") as mock_ingest,
        ):
            stats = backfill_sessions(settings, dry_run=True)

        mock_ingest.assert_not_called()
        assert stats.ingested == 1
        assert stats.skipped_existing == 0

    def test_skip_unregistered_projects(self, tmp_path: Path) -> None:
        from quarry.backfill import backfill_sessions
        from quarry.sync_registry import open_registry

        env = _make_env(tmp_path)
        settings = _make_settings(env["db_path"], env["registry_path"])

        # Initialize empty registry (no registrations)
        conn = open_registry(env["registry_path"])
        conn.close()

        with patch(
            "quarry.backfill.CLAUDE_PROJECTS_DIR",
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
            "document_name": "session-abcd1234-20250101T000000",
            "document_path": "",
            "collection": "myproject-captures",
            "total_pages": 1,
            "chunk_count": 5,
            "indexed_pages": 1,
            "ingestion_timestamp": "2025-01-01T00:00:00",
        }
        with (
            patch(
                "quarry.backfill.CLAUDE_PROJECTS_DIR",
                env["projects_dir"],
            ),
            patch(
                "quarry.backfill.list_documents",
                return_value=[fake_doc],
            ),
            patch("quarry.backfill.ingest_content") as mock_ingest,
        ):
            stats = backfill_sessions(settings)

        mock_ingest.assert_not_called()
        assert stats.ingested == 0
        assert stats.skipped_existing == 1

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
            "quarry.backfill.CLAUDE_PROJECTS_DIR",
            env["projects_dir"],
        ):
            stats = backfill_sessions(settings, dry_run=True, limit=1)

        assert stats.ingested == 1

    def test_collection_override(self, tmp_path: Path) -> None:
        from quarry.backfill import backfill_sessions

        env = _make_env(tmp_path)
        settings = _make_settings(env["db_path"], env["registry_path"])
        _setup_registry(env["registry_path"], str(env["real_project"]), "myproject")

        with patch(
            "quarry.backfill.CLAUDE_PROJECTS_DIR",
            env["projects_dir"],
        ):
            stats = backfill_sessions(
                settings,
                collection_override="my-override",
                dry_run=True,
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
                "quarry.backfill.CLAUDE_PROJECTS_DIR",
                env["projects_dir"],
            ),
            patch("quarry.backfill.ingest_content") as mock_ingest,
        ):
            stats = backfill_sessions(settings)

        mock_ingest.assert_not_called()
        assert stats.ingested == 0
        assert stats.skipped_empty == 1


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
        assert "--provider" in result.output
