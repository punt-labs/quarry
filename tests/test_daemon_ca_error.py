"""DaemonCaError: an unusable CA is an error of its own, not a bare Exception."""

from __future__ import annotations

import pytest

from quarry.daemon_ca_error import DaemonCaError


def test_it_carries_the_reason_and_its_cause() -> None:
    cause = OSError("denied")
    with pytest.raises(DaemonCaError, match=r"ca\.crt cannot be read") as raised:
        msg = "ca.crt cannot be read (PermissionError)"
        raise DaemonCaError(msg) from cause

    assert raised.value.__cause__ is cause


def test_it_is_not_a_failure_of_the_daemon_itself() -> None:
    assert not issubclass(DaemonCaError, OSError)
