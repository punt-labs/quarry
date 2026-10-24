from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.logging_config import LoggingConfig
from tests.hermetic_env import ENV


def _fail(msg: str) -> None:
    raise RuntimeError(msg)


def test_env_var_overrides_stderr_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """QUARRY_LOG_LEVEL overrides the stderr_level parameter."""
    monkeypatch.setenv("QUARRY_LOG_LEVEL", "DEBUG")
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        LoggingConfig.configure(stderr_level="WARNING")
    config = mock_dc.call_args[0][0]
    assert config["handlers"]["stderr"]["level"] == "DEBUG"


def test_invalid_env_var_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid QUARRY_LOG_LEVEL is ignored; parameter value is used."""
    monkeypatch.setenv("QUARRY_LOG_LEVEL", "NONSENSE")
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        LoggingConfig.configure(stderr_level="WARNING")
    config = mock_dc.call_args[0][0]
    assert config["handlers"]["stderr"]["level"] == "WARNING"


def test_no_env_var_uses_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without QUARRY_LOG_LEVEL, the parameter controls stderr level."""
    monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        LoggingConfig.configure(stderr_level="INFO")
    config = mock_dc.call_args[0][0]
    assert config["handlers"]["stderr"]["level"] == "INFO"


def test_third_party_loggers_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Third-party loggers are pinned at WARNING to prevent DEBUG floods."""
    monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        LoggingConfig.configure()
    config = mock_dc.call_args[0][0]
    for name in ("lancedb", "onnxruntime", "httpx"):
        assert config["loggers"][name]["level"] == "WARNING"


class TestLogDirResolution:
    """The destination is resolved per call, so a caller can move it."""

    def test_env_var_selects_the_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path / "logs"))
        assert LoggingConfig.log_dir() == tmp_path / "logs"

    def test_absent_env_var_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QUARRY_LOG_DIR", raising=False)
        expected = Path.home() / ".punt-labs" / "quarry" / "logs"
        assert LoggingConfig.log_dir() == expected

    def test_empty_env_var_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exported-but-blank variable must not resolve the log to ``.``."""
        monkeypatch.setenv("QUARRY_LOG_DIR", "")
        expected = Path.home() / ".punt-labs" / "quarry" / "logs"
        assert LoggingConfig.log_dir() == expected

    def test_configure_writes_the_handler_under_the_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "redirected"
        monkeypatch.setenv("QUARRY_LOG_DIR", str(target))
        with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
            LoggingConfig.configure()
        config = mock_dc.call_args[0][0]
        assert config["handlers"]["file"]["filename"] == str(target / "quarry.log")
        assert target.is_dir(), "configure must create the directory it names"


class TestDaemonLogFile:
    """The daemon writes its own file beside the client's, not into it."""

    def test_daemon_and_client_write_different_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One long-lived writer and many short-lived ones stay separable.

        Interleaved, a line cannot be attributed to a process, which is what
        made the week's forensics hard; separate files make the daemon's own
        sequence readable on its own.
        """
        monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path))
        with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
            LoggingConfig.configure(log_file=LoggingConfig.DAEMON_LOG)
        daemon_file = mock_dc.call_args[0][0]["handlers"]["file"]["filename"]
        with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
            LoggingConfig.configure()
        client_file = mock_dc.call_args[0][0]["handlers"]["file"]["filename"]

        assert daemon_file == str(tmp_path / "quarryd.log")
        assert client_file == str(tmp_path / "quarry.log")
        assert daemon_file != client_file

    def test_both_files_share_one_rotation_policy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Splitting the file must not split the size cap or the backup count."""
        monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path))
        policies = []
        for name in (LoggingConfig.CLIENT_LOG, LoggingConfig.DAEMON_LOG):
            with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
                LoggingConfig.configure(log_file=name)
            handler = mock_dc.call_args[0][0]["handlers"]["file"]
            policies.append(
                (handler["class"], handler["maxBytes"], handler["backupCount"])
            )
        assert policies[0] == policies[1]
        assert policies[0] == (
            "quarry.log_redaction.RedactingFileHandler",
            5_242_880,
            5,
        )


class TestDaemonLoggingIsRealAndContained:
    """Configure for real (no mocks) and read the bytes back off disk."""

    def test_operational_lines_land_timestamped_in_the_daemon_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The defect was that these lines reached no file at all."""
        monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path))
        monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
        root = logging.getLogger()
        prior = (root.handlers[:], root.level)
        try:
            LoggingConfig.configure(log_file=LoggingConfig.DAEMON_LOG)
            logging.getLogger("quarry.db.optimizer").info("Optimized table chunks")
            for handler in logging.getLogger().handlers:
                handler.flush()
            written = (tmp_path / "quarryd.log").read_text()
        finally:
            for handler in logging.getLogger().handlers:
                handler.close()
            root.handlers[:], root.level = prior
        assert "Optimized table chunks" in written
        assert "[INFO] quarry.db.optimizer" in written
        # A timestamp is the whole point: an undated traceback in the
        # supervisor's stderr file is what made a dead error look live.
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", written)

    def test_the_written_file_has_no_url_secret_and_no_library_chatter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Configure for real: redaction and the first-party cap hold on disk."""
        monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path))
        monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
        root = logging.getLogger()
        prior = (root.handlers[:], root.level)
        try:
            LoggingConfig.configure(log_file=LoggingConfig.DAEMON_LOG)
            logging.getLogger("quarry.x").info(
                "fetch %s", "https://u:USEC@h.test/p?q=QSEC"
            )
            logging.getLogger("usp.helpers").info("noise https://h.test/s?token=QSEC")
            logging.getLogger("usp.fetch_parse").warning(
                "bad https://h.test/s?token=QSEC"
            )
            try:
                _fail("boom http://u:USEC@h.test/p?token=QSEC")
            except RuntimeError:
                logging.getLogger("quarry.x").exception("failed")
            for handler in logging.getLogger().handlers:
                handler.flush()
            written = (tmp_path / "quarryd.log").read_text()
        finally:
            for handler in logging.getLogger().handlers:
                handler.close()
            root.handlers[:], root.level = prior
        for marker in ("USEC", "QSEC"):
            assert marker not in written
        assert "fetch https://h.test/…(segments=1)" in written
        assert "noise" not in written
        assert "bad https://h.test/…(segments=1)" in written
        assert "RuntimeError" in written

    def test_the_suite_redirect_contains_daemon_logging(self) -> None:
        """Hermeticity: with no override set, the session HOME still binds it.

        The redirect is what keeps a daemon-path test out of the operator's
        real tree, and it must hold for the new file exactly as for the old.
        """
        resolved = LoggingConfig.log_dir()
        assert str(resolved).startswith(str(ENV.home)), resolved
        # Not ``Path("~").expanduser()`` for the negative: the redirect works by
        # moving $HOME, so expanduser resolves to the session home too and the
        # check would pass no matter what.  ENV.real_tree names the production
        # file directly, which is the only fixed reference to compare against.
        real_quarry = ENV.real_tree[0].parent
        assert not str(resolved).startswith(str(real_quarry)), resolved


class TestLogFileIsABareFilename:
    """The directory is chosen by QUARRY_LOG_DIR; the filename may not move it.

    Bug-class 2: a parameter joined into a path is a boundary, and the test
    that matters is the one that makes it raise rather than the one that shows
    the happy path working.
    """

    @pytest.mark.parametrize(
        "escape",
        [
            "sub/quarry.log",  # separator
            "/etc/quarry.log",  # absolute
            "../quarry.log",  # traversal
            "..",  # Path("..").name is ".." — the bare-name check misses it
            ".",
            "",  # would target the directory itself
        ],
    )
    def test_a_path_is_refused(self, escape: str) -> None:
        with pytest.raises(ValueError, match="bare filename"):
            LoggingConfig.configure(log_file=escape)

    def test_the_real_names_are_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The guard must not reject what the daemon and CLI actually pass."""
        monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path))
        for name in (LoggingConfig.CLIENT_LOG, LoggingConfig.DAEMON_LOG):
            with patch("quarry.logging_config.logging.config.dictConfig"):
                LoggingConfig.configure(log_file=name)


def test_a_parser_librarys_own_request_warnings_are_not_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetch layer logs each refusal once; USP's duplicate is capped at ERROR."""
    monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        LoggingConfig.configure()
    assert mock_dc.call_args[0][0]["loggers"]["usp.helpers"] == {"level": "ERROR"}
