"""The verified visibility of a shadow remote, parsed from ``gh`` output."""

from __future__ import annotations

import json
from enum import Enum


class Visibility(Enum):
    """The verified visibility of the shadow remote."""

    PRIVATE = "private"
    PUBLIC = "public"
    UNKNOWN = "unknown"

    @classmethod
    def from_gh(cls, value: str) -> Visibility:
        """Map a ``gh repo view`` visibility string to an enum member."""
        normalized = value.strip().lower()
        if normalized == "public":
            return cls.PUBLIC
        if normalized == "private":
            return cls.PRIVATE
        return cls.UNKNOWN

    @classmethod
    def from_json(cls, gh_json: str) -> Visibility:
        """Map a ``gh repo view --json visibility`` payload to a member.

        A payload git/gh could not produce as valid JSON is unverifiable, not
        "public/private": fall back to UNKNOWN so the visibility gate refuses
        or requires acknowledgement rather than guessing a member.
        """
        try:
            data = json.loads(gh_json)
        except json.JSONDecodeError:
            return cls.UNKNOWN
        return cls.from_gh(str(data.get("visibility", "")))
