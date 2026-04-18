"""Tests for CLI logging UX: flag combos, pipe safety, progress, env overrides."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from typer.testing import CliRunner

import quarry.__main__ as cli_mod
from quarry.__main__ import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    return s


def _reset_globals() -> None:
    """Reset CLI globals between tests."""
    cli_mod._json_output = False
    cli_mod._verbose = False
    cli_mod._quiet = False
    cli_mod._global_db = ""


def _mock_status_settings() -> MagicMock:
    """Return mock settings suitable for the status command."""
    s = _mock_settings()
    s.registry_path.exists.return_value = False
    s.lancedb_path.exists.return_value = False
    return s


@contextlib.contextmanager
def _status_context() -> Generator[None]:
    """Patch everything the status command needs."""
    s = _mock_status_settings()
    with (
        patch("quarry.__main__._resolved_settings", return_value=s),
        patch("quarry.__main__.get_db"),
        patch("quarry.__main__.list_documents", return_value=[]),
        patch("quarry.__main__.count_chunks", return_value=0),
        patch("quarry.__main__.db_list_collections", return_value=[]),
    ):
        yield


@contextlib.contextmanager
def _find_context() -> Generator[None]:
    """Patch everything the find command needs (one result)."""
    mock_backend = MagicMock()
    mock_backend.embed_query.return_value = np.zeros(768, dtype=np.float32)
    mock_results = [
        {
            "document_name": "report.pdf",
            "page_number": 3,
            "chunk_index": 0,
            "text": "quarterly revenue grew 15%",
            "page_type": "text",
            "source_format": ".pdf",
            "_distance": 0.15,
            "collection": "default",
            "summary": "",
        },
    ]
    with (
        patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
        patch("quarry.__main__.get_db"),
        patch("quarry.__main__.get_embedding_backend", return_value=mock_backend),
        patch("quarry.__main__.hybrid_search", return_value=mock_results),
    ):
        yield


# ---------------------------------------------------------------------------
# 1. Flag combination matrix (architecture doc section 4.5)
#
# All 6 valid combinations verified against stdout/stderr content for
# two representative commands: `find` and `status`.
# ---------------------------------------------------------------------------


class TestFlagCombinationMatrix:
    """Verify stdout/stderr contracts for all 6 valid flag combinations."""

    # -- Default (no flags) --

    def test_default_find_human_text_on_stdout(self) -> None:
        """Default find: human-readable text on stdout, no progress on stdout."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["find", "revenue"])
        assert result.exit_code == 0
        assert "report.pdf" in result.stdout
        assert "p.3" in result.stdout

    def test_default_status_human_text_on_stdout(self) -> None:
        """Default status: human-readable summary on stdout."""
        _reset_globals()
        with _status_context():
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "Documents" in result.stdout

    def test_default_configures_warning_level(self) -> None:
        """Default (no flags) calls configure_logging with WARNING."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.configure_logging") as mock_cfg,
        ):
            result = runner.invoke(app, ["list", "documents"])
        assert result.exit_code == 0
        mock_cfg.assert_called_once_with(stderr_level="WARNING")

    # -- --verbose --

    def test_verbose_find_stdout_unchanged(self) -> None:
        """--verbose does not change stdout content for find."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["--verbose", "find", "revenue"])
        assert result.exit_code == 0
        assert "report.pdf" in result.stdout

    def test_verbose_configures_info_level(self) -> None:
        """--verbose calls configure_logging with INFO."""
        _reset_globals()
        s = _mock_status_settings()
        with (
            patch("quarry.__main__._resolved_settings", return_value=s),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
            patch("quarry.__main__.configure_logging") as mock_cfg,
        ):
            result = runner.invoke(app, ["--verbose", "status"])
        assert result.exit_code == 0
        mock_cfg.assert_called_once_with(stderr_level="INFO")

    # -- --quiet --

    def test_quiet_find_stdout_has_results(self) -> None:
        """--quiet still emits results on stdout."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["--quiet", "find", "revenue"])
        assert result.exit_code == 0
        assert "report.pdf" in result.stdout

    def test_quiet_status_stdout_has_results(self) -> None:
        """--quiet still emits results on stdout for status."""
        _reset_globals()
        with _status_context():
            result = runner.invoke(app, ["--quiet", "status"])
        assert result.exit_code == 0
        assert "Documents" in result.stdout

    def test_quiet_configures_critical_level(self) -> None:
        """--quiet calls configure_logging with CRITICAL."""
        _reset_globals()
        s = _mock_status_settings()
        with (
            patch("quarry.__main__._resolved_settings", return_value=s),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
            patch("quarry.__main__.configure_logging") as mock_cfg,
        ):
            result = runner.invoke(app, ["--quiet", "status"])
        assert result.exit_code == 0
        mock_cfg.assert_called_once_with(stderr_level="CRITICAL")

    # -- --json --

    def test_json_find_valid_json_stdout(self) -> None:
        """--json find: stdout is valid JSON array."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["--json", "find", "revenue"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert data[0]["document_name"] == "report.pdf"

    def test_json_status_valid_json_stdout(self) -> None:
        """--json status: stdout is valid JSON object."""
        _reset_globals()
        with _status_context():
            result = runner.invoke(app, ["--json", "status"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "document_count" in data

    def test_json_configures_default_warning_level(self) -> None:
        """--json alone uses WARNING level (default)."""
        _reset_globals()
        s = _mock_status_settings()
        with (
            patch("quarry.__main__._resolved_settings", return_value=s),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
            patch("quarry.__main__.configure_logging") as mock_cfg,
        ):
            result = runner.invoke(app, ["--json", "status"])
        assert result.exit_code == 0
        mock_cfg.assert_called_once_with(stderr_level="WARNING")

    # -- --json --verbose --

    def test_json_verbose_find_valid_json_stdout(self) -> None:
        """--json --verbose: JSON on stdout, INFO on stderr."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["--json", "--verbose", "find", "revenue"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert data[0]["document_name"] == "report.pdf"

    def test_json_verbose_configures_info_level(self) -> None:
        """--json --verbose calls configure_logging with INFO."""
        _reset_globals()
        s = _mock_status_settings()
        with (
            patch("quarry.__main__._resolved_settings", return_value=s),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
            patch("quarry.__main__.configure_logging") as mock_cfg,
        ):
            result = runner.invoke(app, ["--json", "--verbose", "status"])
        assert result.exit_code == 0
        mock_cfg.assert_called_once_with(stderr_level="INFO")

    # -- --json --quiet --

    def test_json_quiet_find_valid_json_stdout(self) -> None:
        """--json --quiet: JSON on stdout, minimal stderr."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["--json", "--quiet", "find", "revenue"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert data[0]["document_name"] == "report.pdf"

    def test_json_quiet_configures_critical_level(self) -> None:
        """--json --quiet calls configure_logging with CRITICAL."""
        _reset_globals()
        s = _mock_status_settings()
        with (
            patch("quarry.__main__._resolved_settings", return_value=s),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
            patch("quarry.__main__.configure_logging") as mock_cfg,
        ):
            result = runner.invoke(app, ["--json", "--quiet", "status"])
        assert result.exit_code == 0
        mock_cfg.assert_called_once_with(stderr_level="CRITICAL")


# ---------------------------------------------------------------------------
# 2. --verbose --quiet mutual exclusion
# ---------------------------------------------------------------------------


class TestVerboseQuietMutualExclusion:
    """Verify --verbose and --quiet cannot be combined."""

    def test_exit_code_is_1(self) -> None:
        _reset_globals()
        result = runner.invoke(app, ["--verbose", "--quiet", "status"])
        assert result.exit_code == 1

    def test_error_message_mentions_mutually_exclusive(self) -> None:
        _reset_globals()
        result = runner.invoke(app, ["--verbose", "--quiet", "status"])
        assert "mutually exclusive" in result.output.lower()

    def test_reverse_order_also_rejected(self) -> None:
        """--quiet --verbose (reversed order) is also rejected."""
        _reset_globals()
        result = runner.invoke(app, ["--quiet", "--verbose", "status"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output.lower()

    def test_json_verbose_quiet_rejected(self) -> None:
        """--json --verbose --quiet is rejected (triple flag)."""
        _reset_globals()
        result = runner.invoke(app, ["--json", "--verbose", "--quiet", "status"])
        assert result.exit_code == 1

    def test_mutual_exclusion_error_on_stderr(self) -> None:
        """The mutually-exclusive error goes to stderr, not stdout."""
        _reset_globals()
        result = runner.invoke(app, ["--verbose", "--quiet", "status"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.stderr.lower()


# ---------------------------------------------------------------------------
# 3. Pipe safety: --json find produces valid JSON, zero noise on stderr
# ---------------------------------------------------------------------------


class TestPipeSafety:
    """Stdout must be pipeable -- only machine-parseable content, no noise."""

    def test_json_find_stdout_is_valid_json(self) -> None:
        """quarry --json find produces valid JSON on stdout."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["--json", "find", "revenue"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_json_find_stderr_empty_on_success(self) -> None:
        """quarry --json find: zero bytes on stderr for a successful command."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["--json", "find", "revenue"])
        assert result.exit_code == 0
        assert result.stderr.strip() == ""

    def test_json_status_stdout_is_parseable(self) -> None:
        """quarry --json status produces parseable JSON on stdout."""
        _reset_globals()
        with _status_context():
            result = runner.invoke(app, ["--json", "status"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, dict)

    def test_json_status_stderr_empty_on_success(self) -> None:
        """quarry --json status: zero bytes on stderr for a successful command."""
        _reset_globals()
        with _status_context():
            result = runner.invoke(app, ["--json", "status"])
        assert result.exit_code == 0
        assert result.stderr.strip() == ""

    def test_default_find_stdout_has_no_json(self) -> None:
        """Default mode find produces human text, not JSON, on stdout."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["find", "revenue"])
        assert result.exit_code == 0
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)

    def test_json_find_single_json_line(self) -> None:
        """Stdout in --json mode is a single valid JSON document."""
        _reset_globals()
        with _find_context():
            result = runner.invoke(app, ["--json", "find", "revenue"])
        assert result.exit_code == 0
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1
        json.loads(lines[0])

    def test_json_list_documents_stdout_is_array(self) -> None:
        """quarry --json list documents produces a JSON array on stdout."""
        _reset_globals()
        mock_docs = [
            {
                "document_name": "a.pdf",
                "collection": "default",
                "indexed_pages": 5,
                "total_pages": 5,
                "chunk_count": 10,
            },
        ]
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=mock_docs),
        ):
            result = runner.invoke(app, ["--json", "list", "documents"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert data[0]["document_name"] == "a.pdf"
        # stderr should be empty
        assert result.stderr.strip() == ""


# ---------------------------------------------------------------------------
# 4. Progress bar on stderr, not stdout
# ---------------------------------------------------------------------------


class TestProgressStderr:
    """Progress bar must render on stderr, suppressed by --quiet and --json."""

    def test_progress_context_manager_uses_err_console(self) -> None:
        """_progress constructs Progress with err_console (stderr)."""
        _reset_globals()
        cli_mod._json_output = False
        cli_mod._quiet = False
        with patch("quarry.__main__.Progress") as mock_progress_cls:
            mock_instance = MagicMock()
            mock_progress_cls.return_value = mock_instance
            mock_instance.add_task.return_value = 0
            with cli_mod._progress("Testing") as cb:
                assert cb is not None
        mock_progress_cls.assert_called_once_with(console=cli_mod.err_console)

    def test_ingest_file_progress_on_stderr_not_stdout(self) -> None:
        """quarry ingest <file> uses _progress which renders on stderr."""
        _reset_globals()
        mock_result = {
            "document_name": "test.txt",
            "chunks_created": 5,
            "status": "success",
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.derive_collection", return_value="default"),
            patch(
                "quarry.__main__.ingest_document",
                return_value=mock_result,
            ) as mock_ingest,
            patch("quarry.__main__.Progress") as mock_progress_cls,
        ):
            mock_instance = MagicMock()
            mock_progress_cls.return_value = mock_instance
            mock_instance.add_task.return_value = 0
            result = runner.invoke(app, ["ingest", "/tmp/test.txt"])
        assert result.exit_code == 0
        mock_progress_cls.assert_called_once_with(console=cli_mod.err_console)
        assert mock_ingest.call_args[1]["progress_callback"] is not None

    def test_ingest_quiet_suppresses_progress(self) -> None:
        """quarry --quiet ingest <file> passes None callback (no progress)."""
        _reset_globals()
        mock_result = {
            "document_name": "test.txt",
            "chunks_created": 5,
            "status": "success",
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.derive_collection", return_value="default"),
            patch(
                "quarry.__main__.ingest_document",
                return_value=mock_result,
            ) as mock_ingest,
        ):
            result = runner.invoke(app, ["--quiet", "ingest", "/tmp/test.txt"])
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["progress_callback"] is None

    def test_ingest_json_suppresses_progress(self) -> None:
        """quarry --json ingest <file> passes None callback (no progress)."""
        _reset_globals()
        mock_result = {
            "document_name": "test.txt",
            "chunks_created": 5,
            "status": "success",
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.derive_collection", return_value="default"),
            patch(
                "quarry.__main__.ingest_document",
                return_value=mock_result,
            ) as mock_ingest,
        ):
            result = runner.invoke(app, ["--json", "ingest", "/tmp/test.txt"])
        _reset_globals()
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["progress_callback"] is None

    def test_sync_progress_on_stderr(self) -> None:
        """quarry sync wraps sync_all in _progress on err_console."""
        _reset_globals()
        mock_sync_result = MagicMock()
        mock_sync_result.ingested = 1
        mock_sync_result.refreshed = 0
        mock_sync_result.deleted = 0
        mock_sync_result.skipped = 0
        mock_sync_result.failed = 0
        mock_sync_result.errors = []
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.sync_all",
                return_value={"default": mock_sync_result},
            ),
            patch("quarry.__main__.Progress") as mock_progress_cls,
        ):
            mock_instance = MagicMock()
            mock_progress_cls.return_value = mock_instance
            mock_instance.add_task.return_value = 0
            result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        mock_progress_cls.assert_called_once_with(console=cli_mod.err_console)

    def test_sync_quiet_suppresses_progress(self) -> None:
        """quarry --quiet sync passes None progress callback."""
        _reset_globals()
        mock_sync_result = MagicMock()
        mock_sync_result.ingested = 0
        mock_sync_result.refreshed = 0
        mock_sync_result.deleted = 0
        mock_sync_result.skipped = 0
        mock_sync_result.failed = 0
        mock_sync_result.errors = []
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.sync_all",
                return_value={"default": mock_sync_result},
            ) as mock_sync,
        ):
            result = runner.invoke(app, ["--quiet", "sync"])
        assert result.exit_code == 0
        assert mock_sync.call_args[1]["progress_callback"] is None


# ---------------------------------------------------------------------------
# 5. QUARRY_LOG_LEVEL override
# ---------------------------------------------------------------------------


class TestQuarryLogLevelOverride:
    """QUARRY_LOG_LEVEL env var overrides flag-derived log level."""

    def test_env_debug_overrides_default_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QUARRY_LOG_LEVEL=DEBUG produces DEBUG-level stderr handler."""
        monkeypatch.setenv("QUARRY_LOG_LEVEL", "DEBUG")
        with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
            from quarry.logging_config import configure_logging

            configure_logging(stderr_level="WARNING")
        config = mock_dc.call_args[0][0]
        assert config["handlers"]["stderr"]["level"] == "DEBUG"

    def test_env_debug_overrides_even_without_verbose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QUARRY_LOG_LEVEL=DEBUG works without --verbose flag on the CLI."""
        monkeypatch.setenv("QUARRY_LOG_LEVEL", "DEBUG")
        _reset_globals()
        s = _mock_status_settings()
        with (
            patch("quarry.__main__._resolved_settings", return_value=s),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
            patch("quarry.logging_config.logging.config.dictConfig") as mock_dc,
        ):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        config = mock_dc.call_args[0][0]
        assert config["handlers"]["stderr"]["level"] == "DEBUG"

    def test_env_info_overrides_quiet_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QUARRY_LOG_LEVEL=INFO overrides --quiet's CRITICAL level."""
        monkeypatch.setenv("QUARRY_LOG_LEVEL", "INFO")
        _reset_globals()
        s = _mock_status_settings()
        with (
            patch("quarry.__main__._resolved_settings", return_value=s),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
            patch("quarry.logging_config.logging.config.dictConfig") as mock_dc,
        ):
            result = runner.invoke(app, ["--quiet", "status"])
        assert result.exit_code == 0
        config = mock_dc.call_args[0][0]
        assert config["handlers"]["stderr"]["level"] == "INFO"

    def test_empty_env_var_uses_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty QUARRY_LOG_LEVEL falls back to flag-derived level."""
        monkeypatch.setenv("QUARRY_LOG_LEVEL", "")
        with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
            from quarry.logging_config import configure_logging

            configure_logging(stderr_level="INFO")
        config = mock_dc.call_args[0][0]
        assert config["handlers"]["stderr"]["level"] == "INFO"

    def test_case_insensitive_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """QUARRY_LOG_LEVEL is case-insensitive (e.g., 'debug' works)."""
        monkeypatch.setenv("QUARRY_LOG_LEVEL", "debug")
        with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
            from quarry.logging_config import configure_logging

            configure_logging(stderr_level="WARNING")
        config = mock_dc.call_args[0][0]
        assert config["handlers"]["stderr"]["level"] == "DEBUG"

    def test_third_party_suppressed_even_with_debug_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Third-party loggers stay at WARNING even with QUARRY_LOG_LEVEL=DEBUG."""
        monkeypatch.setenv("QUARRY_LOG_LEVEL", "DEBUG")
        with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
            from quarry.logging_config import configure_logging

            configure_logging(stderr_level="WARNING")
        config = mock_dc.call_args[0][0]
        for name in ("lancedb", "onnxruntime", "httpx"):
            assert config["loggers"][name]["level"] == "WARNING"


# ---------------------------------------------------------------------------
# 6. Fatal errors under --quiet
# ---------------------------------------------------------------------------


class TestFatalErrorsUnderQuiet:
    """--quiet must still show fatal errors on stderr when exit code is 1."""

    def test_quiet_cli_error_shows_on_stderr(self) -> None:
        """quarry --quiet <cmd> still shows Error: on stderr for exceptions."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.list_documents",
                side_effect=RuntimeError("database corrupt"),
            ),
        ):
            result = runner.invoke(app, ["--quiet", "list", "documents"])
        assert result.exit_code == 1
        assert "database corrupt" in result.stderr

    def test_quiet_error_exit_code_1(self) -> None:
        """Fatal errors produce exit code 1 even with --quiet."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.count_chunks",
                side_effect=RuntimeError("lance failure"),
            ),
        ):
            result = runner.invoke(app, ["--quiet", "status"])
        assert result.exit_code == 1

    def test_quiet_json_error_still_on_stderr(self) -> None:
        """quarry --json --quiet <cmd> still shows errors on stderr."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.list_documents",
                side_effect=RuntimeError("broken table"),
            ),
        ):
            result = runner.invoke(app, ["--json", "--quiet", "list", "documents"])
        assert result.exit_code == 1
        assert "broken table" in result.stderr

    def test_quiet_json_error_stdout_empty(self) -> None:
        """Under --json --quiet, a fatal error emits nothing on stdout."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.list_documents",
                side_effect=RuntimeError("fatal error"),
            ),
        ):
            result = runner.invoke(app, ["--json", "--quiet", "list", "documents"])
        assert result.exit_code == 1
        assert result.stdout.strip() == ""

    def test_quiet_ingest_missing_file_error_on_stderr(self) -> None:
        """quarry --quiet ingest <nonexistent> shows error on stderr."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.derive_collection", return_value="default"),
            patch(
                "quarry.__main__.ingest_document",
                side_effect=FileNotFoundError("no such file"),
            ),
        ):
            result = runner.invoke(app, ["--quiet", "ingest", "/nonexistent/file.pdf"])
        assert result.exit_code == 1
        assert "no such file" in result.stderr

    def test_quiet_find_error_on_stderr(self) -> None:
        """quarry --quiet find produces error on stderr when search fails."""
        _reset_globals()
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch(
                "quarry.__main__.hybrid_search",
                side_effect=RuntimeError("search index corrupt"),
            ),
        ):
            result = runner.invoke(app, ["--quiet", "find", "test query"])
        assert result.exit_code == 1
        assert "search index corrupt" in result.stderr

    def test_quiet_optimize_threshold_error_on_stderr(self) -> None:
        """quarry --quiet optimize shows threshold error on stderr."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.database.count_fragments", return_value=20000),
            patch("quarry.database.FRAGMENT_THRESHOLD", 10000),
        ):
            result = runner.invoke(app, ["--quiet", "optimize"])
        assert result.exit_code == 1
        # The threshold error is always shown because it precedes a non-zero exit.
        assert "exceed" in result.stderr.lower() or "threshold" in result.stderr.lower()


# ---------------------------------------------------------------------------
# 7. remember_cmd progress wrapper
# ---------------------------------------------------------------------------


class TestRememberProgress:
    """Verify remember wraps ingest_content in _progress on stderr."""

    def test_remember_default_has_progress_callback(self) -> None:
        """remember in default mode passes a non-None progress_callback."""
        _reset_globals()
        cli_mod._json_output = False
        cli_mod._quiet = False
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"document_name": "notes.md", "chunks": 1},
            ) as mock_ingest,
            patch("quarry.__main__.Progress") as mock_progress_cls,
        ):
            mock_instance = MagicMock()
            mock_progress_cls.return_value = mock_instance
            mock_instance.add_task.return_value = 0
            result = runner.invoke(
                app,
                ["remember", "--name", "notes.md"],
                input="some meeting notes",
            )
        _reset_globals()
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["progress_callback"] is not None

    def test_remember_progress_uses_err_console(self) -> None:
        """remember's progress bar renders on stderr via err_console."""
        _reset_globals()
        cli_mod._json_output = False
        cli_mod._quiet = False
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"document_name": "notes.md", "chunks": 1},
            ),
            patch("quarry.__main__.Progress") as mock_progress_cls,
        ):
            mock_instance = MagicMock()
            mock_progress_cls.return_value = mock_instance
            mock_instance.add_task.return_value = 0
            result = runner.invoke(
                app,
                ["remember", "--name", "notes.md"],
                input="content here",
            )
        _reset_globals()
        assert result.exit_code == 0
        mock_progress_cls.assert_called_once_with(console=cli_mod.err_console)

    def test_remember_quiet_no_progress(self) -> None:
        """remember --quiet passes None callback (no spinner)."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"document_name": "notes.md", "chunks": 1},
            ) as mock_ingest,
        ):
            result = runner.invoke(
                app,
                ["--quiet", "remember", "--name", "notes.md"],
                input="content here",
            )
        _reset_globals()
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["progress_callback"] is None

    def test_remember_json_no_progress(self) -> None:
        """remember --json passes None callback (no spinner)."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"document_name": "notes.md", "chunks": 1},
            ) as mock_ingest,
        ):
            result = runner.invoke(
                app,
                ["--json", "remember", "--name", "notes.md"],
                input="content here",
            )
        _reset_globals()
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["progress_callback"] is None

    def test_remember_json_quiet_no_progress(self) -> None:
        """remember --json --quiet passes None callback."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"document_name": "notes.md", "chunks": 1},
            ) as mock_ingest,
        ):
            result = runner.invoke(
                app,
                ["--json", "--quiet", "remember", "--name", "notes.md"],
                input="content here",
            )
        _reset_globals()
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["progress_callback"] is None

    def test_remember_result_on_stdout(self) -> None:
        """remember emits result on stdout regardless of progress state."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"document_name": "notes.md", "chunks": 3},
            ),
            patch("quarry.__main__.Progress") as mock_progress_cls,
        ):
            mock_instance = MagicMock()
            mock_progress_cls.return_value = mock_instance
            mock_instance.add_task.return_value = 0
            result = runner.invoke(
                app,
                ["remember", "--name", "notes.md"],
                input="content here",
            )
        _reset_globals()
        assert result.exit_code == 0
        assert "notes.md" in result.stdout


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


class TestOptimizeQuietGuard:
    """Verify optimize respects --quiet for its stderr messages."""

    def test_optimize_quiet_no_fragment_count(self) -> None:
        """quarry --quiet optimize suppresses 'Fragment count:' on stderr."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.database.count_fragments", return_value=10),
            patch("quarry.database.optimize_table"),
        ):
            result = runner.invoke(app, ["--quiet", "optimize"])
        assert result.exit_code == 0
        assert "Fragment count" not in result.stderr

    def test_optimize_default_shows_fragment_count(self) -> None:
        """quarry optimize (default) shows 'Fragment count:' on stderr."""
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.database.count_fragments", return_value=42),
            patch("quarry.database.optimize_table"),
        ):
            result = runner.invoke(app, ["optimize"])
        assert result.exit_code == 0
        assert "Fragment count" in result.stderr


class TestProgressContextEdgeCases:
    """Edge cases for the _progress context manager."""

    def test_progress_stops_on_exception(self) -> None:
        """_progress stops the Rich progress bar even when an exception is raised."""
        _reset_globals()
        cli_mod._json_output = False
        cli_mod._quiet = False
        with patch("quarry.__main__.Progress") as mock_progress_cls:
            mock_instance = MagicMock()
            mock_progress_cls.return_value = mock_instance
            mock_instance.add_task.return_value = 0
            with (
                pytest.raises(ValueError, match="boom"),
                cli_mod._progress("Testing"),
            ):
                raise ValueError("boom")
        mock_instance.stop.assert_called_once()

    def test_progress_callback_updates_task(self) -> None:
        """The callback returned by _progress calls p.update on the task."""
        _reset_globals()
        cli_mod._json_output = False
        cli_mod._quiet = False
        with patch("quarry.__main__.Progress") as mock_progress_cls:
            mock_instance = MagicMock()
            mock_progress_cls.return_value = mock_instance
            mock_instance.add_task.return_value = 42
            with cli_mod._progress("Starting") as cb:
                assert cb is not None
                cb("Step 1 complete")
        mock_instance.update.assert_called_once_with(42, description="Step 1 complete")

    def test_progress_quiet_and_json_combined(self) -> None:
        """_progress yields None when both _quiet and _json_output are set."""
        _reset_globals()
        cli_mod._json_output = True
        cli_mod._quiet = True
        with cli_mod._progress("test") as cb:
            pass
        assert cb is None
