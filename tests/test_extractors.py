"""Protocol conformance tests for FormatExtractor."""

from __future__ import annotations

from pathlib import Path

from quarry.extractors.protocol import FormatExtractor
from quarry.models import PageContent


class _StubExtractor:
    """Minimal stub satisfying FormatExtractor."""

    def extract_pages(
        self,
        path: Path,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        return []


def test_stub_satisfies_format_extractor_protocol() -> None:
    stub = _StubExtractor()
    assert isinstance(stub, FormatExtractor)


def test_non_conforming_object_rejected() -> None:
    assert not isinstance(object(), FormatExtractor)
