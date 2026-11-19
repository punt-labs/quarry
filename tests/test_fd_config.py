"""Unit tests for the daemon's file-descriptor envelope and its config target.

Cross-platform: the ``resource`` module is replaced with a fake, so the raise,
clamp, never-lower, and every fail-safe degrade path are exercised without
touching the real process ``RLIMIT_NOFILE`` on any host.
"""

from __future__ import annotations

import logging
from typing import ClassVar, Self, final

import pytest

from quarry import fd_config
from quarry.config import DEFAULT_FD_LIMIT, Settings
from quarry.fd_config import _SERVICE_FD_HARD, FdEnvelope, FdServiceLimits


@final
class _FakeResource:
    """A ``resource`` stand-in that records setrlimit and can be told to raise."""

    RLIMIT_NOFILE: ClassVar[int] = 7
    RLIM_INFINITY: ClassVar[int] = 2**63 - 1

    __slots__ = ("_get_exc", "_limits", "_set_args", "_set_exc")

    _limits: tuple[int, int]
    _get_exc: OSError | None
    _set_exc: OSError | ValueError | None
    _set_args: tuple[int, tuple[int, int]] | None

    def __new__(
        cls,
        *,
        soft: int,
        hard: int,
        get_exc: OSError | None = None,
        set_exc: OSError | ValueError | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._limits = (soft, hard)
        self._get_exc = get_exc
        self._set_exc = set_exc
        self._set_args = None
        return self

    def getrlimit(self, _which: int) -> tuple[int, int]:
        """Return the current (soft, hard), or raise the configured error."""
        if self._get_exc is not None:
            raise self._get_exc
        return self._limits

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        """Record and apply (soft, hard), or raise the configured error."""
        if self._set_exc is not None:
            raise self._set_exc
        self._set_args = (which, limits)
        self._limits = limits

    @property
    def set_args(self) -> tuple[int, tuple[int, int]] | None:
        """The (which, (soft, hard)) of the last setrlimit call, or ``None``."""
        return self._set_args


def test_raise_below_target(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeResource(soft=256, hard=1_000_000)
    monkeypatch.setattr(fd_config, "resource", fake)
    state = FdEnvelope(target=8192).apply()
    assert fake.set_args == (_FakeResource.RLIMIT_NOFILE, (8192, 1_000_000))
    assert "raised" in state


def test_clamp_to_hard(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeResource(soft=256, hard=4096)
    monkeypatch.setattr(fd_config, "resource", fake)
    FdEnvelope(target=8192).apply()
    # Effective clamps to the hard limit — never a value above it.
    assert fake.set_args == (_FakeResource.RLIMIT_NOFILE, (4096, 4096))


def test_never_lower_inherited_higher_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeResource(soft=65536, hard=1_000_000)
    monkeypatch.setattr(fd_config, "resource", fake)
    state = FdEnvelope(target=8192).apply()
    assert fake.set_args is None  # a higher inherited soft (systemd) is honored
    assert "unchanged" in state


def test_rlim_infinity_hard_sets_target(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeResource(soft=256, hard=_FakeResource.RLIM_INFINITY)
    monkeypatch.setattr(fd_config, "resource", fake)
    FdEnvelope(target=8192).apply()
    # Unbounded hard: the target is set as-is, no clamp.
    infinity = _FakeResource.RLIM_INFINITY
    assert fake.set_args == (_FakeResource.RLIMIT_NOFILE, (8192, infinity))


@pytest.mark.parametrize("exc", [OSError("boom"), ValueError("bad limit")])
def test_degrade_when_setrlimit_raises(
    monkeypatch: pytest.MonkeyPatch, exc: OSError | ValueError
) -> None:
    fake = _FakeResource(soft=256, hard=1_000_000, set_exc=exc)
    monkeypatch.setattr(fd_config, "resource", fake)
    state = FdEnvelope(target=8192).apply()  # must not propagate (Bug-class-2)
    assert "unchanged" in state
    assert "failed" in state


def test_degrade_when_getrlimit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeResource(soft=256, hard=1_000_000, get_exc=OSError("nope"))
    monkeypatch.setattr(fd_config, "resource", fake)
    state = FdEnvelope(target=8192).apply()  # must not propagate
    assert fake.set_args is None
    assert "getrlimit failed" in state


def test_degrade_when_resource_module_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fd_config, "resource", None)
    state = FdEnvelope(target=8192).apply()  # must not call setrlimit, must not raise
    assert "unavailable" in state


def test_fd_limit_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUARRY_FD_LIMIT", raising=False)
    assert Settings().fd_limit == 8192


def test_fd_limit_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARRY_FD_LIMIT", "65536")
    assert Settings().fd_limit == 65536


def test_fd_limit_malformed_degrades(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("QUARRY_FD_LIMIT", "not-a-number")
    with caplog.at_level(logging.WARNING):
        settings = Settings()  # must not raise a ValidationError (Bug-class-2)
    assert settings.fd_limit == 8192
    assert "QUARRY_FD_LIMIT" in caplog.text


def test_fd_limit_nonpositive_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARRY_FD_LIMIT", "0")
    assert Settings().fd_limit == 8192


class TestFdServiceLimits:
    """Unit tests for the FdServiceLimits value class (RLIMIT_NOFILE ceiling).

    The service manager bakes these limits BEFORE spawning quarryd because it can
    raise the hard limit above the 256 a fresh launchd bootstrap inherits — the
    root cause of ``FD headroom: 9/256``.  FdEnvelope (DES-046) then
    lifts the soft limit off this ceiling at runtime instead of clamping to 256.
    """

    def test_from_settings_default_soft_and_hard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default: soft = 8192 (Settings.fd_limit), hard = _SERVICE_FD_HARD (65536)."""
        monkeypatch.delenv("QUARRY_FD_LIMIT", raising=False)
        limits = FdServiceLimits.from_settings()
        assert limits.soft == 8192
        assert limits.hard == _SERVICE_FD_HARD == 65536

    def test_soft_follows_fd_limit_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QUARRY_FD_LIMIT raises the baked soft floor; the hard ceiling is fixed."""
        monkeypatch.setenv("QUARRY_FD_LIMIT", "16384")
        limits = FdServiceLimits.from_settings()
        assert limits.soft == 16384
        assert limits.hard == 65536

    def test_soft_clamped_to_hard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A QUARRY_FD_LIMIT above the hard ceiling clamps — soft may not exceed hard.

        A plist/unit whose soft limit exceeds its hard limit is rejected by the
        service manager, so the class clamps rather than emit an invalid file.
        """
        monkeypatch.setenv("QUARRY_FD_LIMIT", "999999")
        limits = FdServiceLimits.from_settings()
        assert limits.soft == 65536
        assert limits.hard == 65536

    def test_soft_floored_to_default_when_below(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QUARRY_FD_LIMIT below the safe default is FLOORED — it may only RAISE.

        config._coerce_fd_limit accepts any positive int, so QUARRY_FD_LIMIT=100
        would otherwise bake SoftResourceLimits=100 and silently reintroduce the
        exact EMFILE condition this ceiling fixes.  The floor is DEFAULT_FD_LIMIT.
        """
        monkeypatch.setenv("QUARRY_FD_LIMIT", "100")
        limits = FdServiceLimits.from_settings()
        assert limits.soft == 8192
        assert limits.hard == 65536

    def test_soft_raised_above_default_not_floored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A QUARRY_FD_LIMIT above the default raises the soft floor, still <= hard."""
        monkeypatch.setenv("QUARRY_FD_LIMIT", "30000")
        limits = FdServiceLimits.from_settings()
        assert limits.soft == 30000
        assert limits.hard == 65536

    def test_hard_below_default_raises(self) -> None:
        """A hard ceiling under DEFAULT_FD_LIMIT is rejected at construction.

        The keyword constructor is public.  With hard < DEFAULT_FD_LIMIT the
        clamp ``min(max(soft, DEFAULT_FD_LIMIT), hard)`` collapses to hard,
        silently re-admitting a soft below the EMFILE-safe floor.  Reject up
        front (PY-CC-2) so the floor is genuinely unconditional, not just an
        accident of the production hard=65536.
        """
        with pytest.raises(ValueError, match="below the safe default"):
            FdServiceLimits(soft=100, hard=DEFAULT_FD_LIMIT - 1)

    def test_launchd_fragment_carries_both_numberoffiles(self) -> None:
        """The plist fragment names Soft/Hard ResourceLimits with NumberOfFiles."""
        fragment = FdServiceLimits(soft=8192, hard=65536).launchd_fragment()
        assert "<key>SoftResourceLimits</key>" in fragment
        assert "<key>HardResourceLimits</key>" in fragment
        assert fragment.count("<key>NumberOfFiles</key>") == 2
        assert "<integer>8192</integer>" in fragment
        assert "<integer>65536</integer>" in fragment

    def test_systemd_directive_format(self) -> None:
        """The systemd directive is ``LimitNOFILE=<soft>:<hard>``."""
        assert (
            FdServiceLimits(soft=8192, hard=65536).systemd_directive()
            == "LimitNOFILE=8192:65536"
        )
