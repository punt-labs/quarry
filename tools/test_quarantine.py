"""Reject quarantines without an owner, issue and short expiry."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Reject unowned, expired, or overlong quarantines before deselection."""
    today = datetime.now(UTC).date()
    for item in items:
        for mark in item.iter_markers("quarantine"):
            issue = mark.kwargs.get("issue", "")
            owner = mark.kwargs.get("owner", "")
            expires = mark.kwargs.get("expires", "")
            if (
                not mark.args
                and isinstance(issue, str)
                and re.fullmatch(r"PL-[1-9][0-9]*", issue)
                and isinstance(owner, str)
                and owner.strip()
                and isinstance(expires, str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", expires)
            ):
                try:
                    expiry = datetime.strptime(expires, "%Y-%m-%d").replace(tzinfo=UTC)
                except ValueError:
                    pass
                else:
                    if today <= expiry.date() <= today + timedelta(days=14):
                        continue
            msg = (
                f"{item.nodeid}: quarantine requires a Linear issue, named owner, "
                "and an unexpired YYYY-MM-DD deadline within 14 days"
            )
            raise pytest.UsageError(msg)
