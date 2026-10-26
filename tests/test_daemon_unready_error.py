"""DaemonUnreadyError: the retry's failure keeps the TLS failure that came first."""

from __future__ import annotations

import pytest

from quarry.daemon_unready_error import DaemonUnreadyError


def test_it_carries_the_reason_and_its_cause() -> None:
    cause = ConnectionResetError("reset")
    with pytest.raises(DaemonUnreadyError, match="TLS attempt failed first") as raised:
        msg = "the TLS attempt failed first (ConnectionResetError)"
        raise DaemonUnreadyError(msg) from cause

    assert raised.value.__cause__ is cause
