"""The suite writes nothing under the operator's real ``~/.punt-labs/quarry``.

Two mechanisms, tested separately: the ``HOME`` redirect that makes production
unreachable, and the three-file guard that proves the redirect is in force.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.config import Settings
from quarry.db_pointer import SELECTION
from quarry.logging_config import LoggingConfig
from quarry.url_safety import UrlRejectedError, UrlSafetyCheck
from tests.hermetic_env import ENV, ProductionTreeGuard


class TestHomeRedirect:
    """All three home-resolution routes land inside the session temp home."""

    def test_path_home(self) -> None:
        assert Path.home().is_relative_to(ENV.home)

    def test_path_expanduser_method(self) -> None:
        assert Path("~").expanduser().is_relative_to(ENV.home)

    def test_pathlib_expanduser_delegates_to_os_path_expanduser(self) -> None:
        """The two expanduser routes are one implementation, so one check covers both.

        ``os.path.expanduser`` is the route the redirect exists for, and
        ``Path.expanduser`` calls it rather than reimplementing it. Proving the
        delegation here means the assertions above cover both spellings.
        """
        sentinel = str(ENV.home / "sentinel")
        with patch.object(os.path, "expanduser", return_value=sentinel) as delegate:
            assert Path("~").expanduser() == Path(sentinel)
        delegate.assert_called_once_with("~")

    def test_expanduser_ignores_a_patched_path_home(self) -> None:
        """Why the redirect must be the env var and not a ``Path.home`` patch.

        ``expanduser`` resolves through ``$HOME`` and never consults
        ``Path.home``, so patching the classmethod would leave this route — and
        the ``os.path.expanduser`` one behind it — pointed at production.
        """
        elsewhere = Path("/nonexistent-patched-home")
        with patch.object(Path, "home", classmethod(lambda _cls: elsewhere)):
            assert Path.home() == elsewhere, "the patch must actually be in force"
            assert Path("~").expanduser().is_relative_to(ENV.home)

    def test_quarry_root_is_redirected(self) -> None:
        assert Settings.load().quarry_root.is_relative_to(ENV.home)

    def test_config_path_is_redirected(self) -> None:
        assert SELECTION.path.is_relative_to(ENV.home)

    def test_log_dir_is_redirected(self) -> None:
        assert LoggingConfig.log_dir().is_relative_to(ENV.home)

    def test_configure_writes_inside_the_session_home(self) -> None:
        """The breach this whole design exists to close: CLI logging is contained."""
        LoggingConfig.configure()
        log = LoggingConfig.log_dir() / "quarry.log"
        assert log.parent.is_dir()
        assert log.is_relative_to(ENV.home)


class TestProductionTreeGuard:
    """The guard fires on a change to any watched file, and stays quiet otherwise."""

    def test_quiet_when_nothing_moves(self, tmp_path: Path) -> None:
        watched = tmp_path / "quarry.log"
        watched.write_text("x")
        assert ProductionTreeGuard((watched,)).changed() == []

    def test_quiet_for_a_file_that_never_existed(self, tmp_path: Path) -> None:
        assert ProductionTreeGuard((tmp_path / "absent",)).changed() == []

    def test_fires_on_append(self, tmp_path: Path) -> None:
        """The log breach: an appended line moves both size and mtime."""
        watched = tmp_path / "quarry.log"
        watched.write_text("first\n")
        guard = ProductionTreeGuard((watched,))
        with watched.open("a") as handle:
            handle.write("second\n")
        assert len(guard.changed()) == 1

    def test_fires_on_creation(self, tmp_path: Path) -> None:
        """A config.toml written where none existed breaches in the other direction."""
        watched = tmp_path / "config.toml"
        guard = ProductionTreeGuard((watched,))
        watched.write_text('[default]\ndatabase = "work"\n')
        assert len(guard.changed()) == 1

    def test_fires_on_deletion(self, tmp_path: Path) -> None:
        watched = tmp_path / "registry.db"
        watched.write_text("db")
        guard = ProductionTreeGuard((watched,))
        watched.unlink()
        assert len(guard.changed()) == 1

    def test_names_every_breached_path(self, tmp_path: Path) -> None:
        """The message identifies which file moved, not merely that one did."""
        first, second = tmp_path / "a.log", tmp_path / "b.toml"
        guard = ProductionTreeGuard((first, second))
        first.write_text("x")
        second.write_text("y")
        breaches = guard.changed()
        assert len(breaches) == 2
        assert str(first) in breaches[0]
        assert str(second) in breaches[1]

    def test_watches_exactly_the_one_quiescent_production_file(self) -> None:
        """The guard watches exactly one file — the one nothing else writes.

        ``quarry.log`` and ``registry.db`` are excluded on purpose: two
        redirects already put both out of a test's reach, while the daemon,
        every live MCP client, and the watch loop write them continuously — so
        watching them reported other processes rather than breaches.
        """
        names = [p.name for p in ENV.real_tree]
        assert names == ["config.toml"]
        assert all(not p.is_dir() for p in ENV.real_tree)


class TestAmbientGitConfig:
    """The shell's git-signing injection does not follow the suite in."""

    def test_signing_config_is_dropped(self) -> None:
        """The redirected home has no keyring, so a sandbox repo must not sign."""
        assert "GIT_CONFIG_COUNT" not in os.environ

    def test_signing_key_and_program_are_dropped_with_it(self) -> None:
        """The count is the index; leaving the pairs behind would be half a job."""
        leftovers = [k for k in os.environ if k.startswith("GIT_CONFIG_")]
        assert leftovers == []


class TestAmbientCodexHome:
    """The operator's ``CODEX_HOME`` does not follow the suite in.

    Left ambient, ``Harness._codex_home`` reads ``$CODEX_HOME`` directly and
    ignores whatever stubbed ``home`` a skills-install test constructs a
    ``SkillsInstaller`` with — silently depositing a skill into the
    operator's REAL codex directory regardless of the sandbox the test
    believes it is writing to.
    """

    def test_codex_home_is_dropped(self) -> None:
        assert "CODEX_HOME" not in os.environ


class TestThreadPins:
    """The per-run thread budget is pinned before lance builds its runtime."""

    def test_pool_sizes_are_bounded(self) -> None:
        """A ceiling, not an exact value.

        ``ThreadConfig._cap_env`` may lower any of these mid-session (GPU caps
        OMP at 1), so the invariant the pin establishes is the upper bound --
        asserting equality would make this test depend on which other test
        constructed a ``ThreadConfig`` first.
        """
        for name in ("OMP_NUM_THREADS", "LANCE_CPU_THREADS", "LANCE_IO_THREADS"):
            assert int(os.environ[name]) <= 2, name


class TestRealModelGuard:
    """The guard that turns a silent 410 MB model load into a named failure.

    Like the thread invariant, this one only fires on a suite that has already
    gone wrong, so its failure path needs driving directly.
    """

    def test_constructing_a_session_is_refused(self) -> None:
        import onnxruntime

        with pytest.raises(AssertionError) as caught:
            onnxruntime.InferenceSession("/nonexistent/model.onnx")
        assert "loaded a real ONNX model" in str(caught.value)

    def test_the_failure_names_the_offending_test(self) -> None:
        """A bare 'a model was loaded' would not say which test to go fix."""
        import onnxruntime

        with pytest.raises(AssertionError) as caught:
            onnxruntime.InferenceSession("/nonexistent/model.onnx")
        message = str(caught.value)
        assert "test_the_failure_names_the_offending_test" in message
        assert "pytest.mark.embedding" in message, "the message must say the way out"


class TestNoRealDns:
    """The fetch gate resolves from a fake, so the suite makes no DNS query.

    quarry resolves every candidate URL before allowing a fetch. Left real, that
    is an outbound lookup per check and a suite that fails whenever the network
    moves -- which it did, intermittently, before this fixture existed.

    Proven by behaviour rather than by inspecting the patch: only a fake can
    answer for a hostname that cannot exist, and the other two show the fake is
    faithful where faithfulness is what keeps the SSRF policy honest.
    """

    def test_a_hostname_that_cannot_exist_still_resolves(self) -> None:
        """Proof rather than inspection: real DNS could never answer this."""
        resolved = UrlSafetyCheck.validated_addresses("no-such-host.invalid")
        assert [str(a) for a in resolved] == ["93.184.216.34"]

    def test_an_address_literal_resolves_to_itself(self) -> None:
        """The fake must not tell the SSRF policy a private address is public."""
        with pytest.raises(UrlRejectedError):
            UrlSafetyCheck.validated_addresses("10.0.0.1")

    def test_the_overlong_label_boundary_still_raises(self) -> None:
        """The fake reproduces the real resolver's UnicodeError, not just success."""
        assert UrlSafetyCheck.reject_reason(f"https://{'a' * 64}.example.com/")


class TestSubprocessNetworkIsRefused:
    """A subprocess resolves and connects on its own, past the in-process fake.

    The DNS fake patches ``socket.getaddrinfo`` in THIS interpreter, which does
    nothing for a ``git`` the suite shells out to.  Restricting git to local
    transports is what actually closes that, so these prove the pin holds by
    running the real binary rather than by reading the environment back.
    """

    def test_git_refuses_a_remote_protocol(self, tmp_path: Path) -> None:
        """An ssh remote fails in git itself — no resolver, no socket, no wait."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@h:o/r.git"], cwd=repo, check=True
        )
        proc = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert proc.returncode != 0
        assert "not allowed" in proc.stderr

    def test_a_local_path_remote_still_works(self, tmp_path: Path) -> None:
        """The pin must refuse the network without disabling git's local use."""
        origin = tmp_path / "origin"
        origin.mkdir()
        subprocess.run(["git", "init", "-q", "--bare"], cwd=origin, check=True)
        clone = tmp_path / "clone"
        proc = subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        assert (clone / ".git").is_dir()
