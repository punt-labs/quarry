"""The error for a daemon CA that exists but cannot be used."""

from __future__ import annotations


class DaemonCaError(Exception):
    """The daemon's installed CA exists but cannot be used to verify it."""
