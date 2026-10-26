"""The error for a daemon that answered neither over TLS nor over plain HTTP."""

from __future__ import annotations


class DaemonUnreadyError(Exception):
    """The plain-HTTP retry after a failed TLS attempt failed too.

    The message names the TLS failure that came first, so the degraded report
    does not lose the cause the retry hid.
    """
