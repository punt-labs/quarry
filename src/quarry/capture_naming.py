"""The document name a capture is filed under, and whether it names a page."""

from __future__ import annotations

import logging
from typing import final

from quarry.api.capture_ingest import SESSION_ID_PREFIX_LEN
from quarry.web_fetch_name import WebFetchName

logger = logging.getLogger(__name__)


@final
class CaptureNaming:
    """Name a capture, and decide whether the name makes it a fetched page."""

    __slots__ = ()

    @staticmethod
    def name(document_name: str, session_id: str) -> str | None:
        """Return the explicit name, else ``session-<id[:8]>``, else ``None``.

        Inputs are stripped and only accepted when non-empty, so a
        whitespace-only ``document_name``/``session_id`` names nothing rather
        than storing a blank-named document.
        """
        if name := document_name.strip():
            return name
        if session := session_id.strip():
            return f"session-{session[:SESSION_ID_PREFIX_LEN]}"
        return None

    @staticmethod
    def is_page(name: str, source_url: str) -> bool:
        """Return whether a capture named *name* is the page at *source_url*.

        A capture that names a source but is called something else is a label:
        it is stored as sent, never as that page, and says so once in the log.
        """
        is_page = name == WebFetchName.maybe(source_url)
        if source_url and not is_page:
            logger.info(
                "capture: source_url given but document_name is not its page name; "
                "stored as a label"
            )
        return is_page
