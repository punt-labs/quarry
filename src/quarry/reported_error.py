"""An exception that names the class of a bug caught elsewhere, for a task's error."""

from __future__ import annotations

import re
from typing import ClassVar


class ReportedError(Exception):
    """A fault whose report is ``internal error: <reported_as>``.

    Raised with the class name of a bug a lower layer caught and recorded, because
    the exception that surfaces is only a carrier.  The name is checked before it
    is reported, so nothing but an identifier can reach a task's error text.
    """

    _NAME: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

    @property
    def reported_as(self) -> str:
        """Return the class name to report, or ``sitemap`` if none is usable."""
        name = str(self.args[0]) if self.args else ""
        return name if self._NAME.fullmatch(name) else "sitemap"
