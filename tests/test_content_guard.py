"""Tests for quarry.content_guard: binary classification at document level."""

from __future__ import annotations

import pytest

from quarry.content_guard import (
    LARGE_PAGE_CHARS,
    BinaryContent,
    UnindexableContentError,
)

_NONTEXT = "\ufffd"


def _with_ratio(length: int, ratio: float) -> str:
    """Return *length* characters of which ``ratio`` are U+FFFD."""
    bad = round(length * ratio)
    return "a" * (length - bad) + _NONTEXT * bad


class TestMagic:
    @pytest.mark.parametrize(
        ("prefix", "label"),
        [
            ("%PDF-1.5\n", "pdf header"),
            ("PK\x03\x04rest", "zip archive header"),
            ("\x89PNG\r\n", "png header"),
            ("\ufffdPNG\r\n", "png header"),
            ("GIF89a", "gif header"),
            ("\xff\xd8\xff\xe0", "jpeg header"),
            ("\ufffd\ufffd\ufffd\ufffd", "jpeg header"),
            ("\x7fELF\x02", "elf header"),
        ],
    )
    def test_known_magic_is_rejected(self, prefix: str, label: str) -> None:
        with pytest.raises(UnindexableContentError, match=label):
            BinaryContent().require_document([prefix + "x" * 100])

    def test_magic_after_media_type_marker_is_rejected(self) -> None:
        content = "<!-- media_type: application/pdf -->\n%PDF-1.5\n" + "x" * 50
        with pytest.raises(UnindexableContentError, match="pdf header"):
            BinaryContent().require_document([content])

    def test_prose_that_mentions_a_header_passes(self) -> None:
        BinaryContent().require_document(["The %PDF- header opens every file."])

    def test_magic_only_counts_on_the_first_page(self) -> None:
        BinaryContent().require_document(["clean opening page", "%PDF-1.5 quoted"])

    def test_message_carries_the_stable_prefix(self) -> None:
        with pytest.raises(UnindexableContentError) as info:
            BinaryContent().require_document(["%PDF-1.5"])
        assert str(info.value) == "binary content rejected: pdf header"

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(UnindexableContentError, ValueError)


class TestRatio:
    def test_exactly_the_threshold_passes(self) -> None:
        BinaryContent().require_document([_with_ratio(1000, 0.25)])

    def test_just_over_the_threshold_is_rejected(self) -> None:
        with pytest.raises(UnindexableContentError, match="non-text ratio"):
            BinaryContent().require_document([_with_ratio(1000, 0.251)])

    def test_nul_bytes_alone_pass(self) -> None:
        BinaryContent().require_document(["clean text " * 40 + "\x00" * 34])

    def test_c0_controls_count_but_whitespace_controls_do_not(self) -> None:
        guard = BinaryContent()
        guard.require_document(["\t\n\r\f" * 100])
        with pytest.raises(UnindexableContentError):
            guard.require_document(["\x01\x02\x0b\x1f" * 100])

    def test_empty_document_passes(self) -> None:
        BinaryContent().require_document([])
        BinaryContent().require_document([""])

    def test_message_reports_ratio_and_threshold(self) -> None:
        with pytest.raises(UnindexableContentError) as info:
            BinaryContent().require_document([_with_ratio(1000, 0.5)])
        assert str(info.value) == (
            "binary content rejected: non-text ratio 0.50 exceeds 0.25"
        )

    def test_threshold_is_configurable(self) -> None:
        with pytest.raises(UnindexableContentError):
            BinaryContent(max_nontext_ratio=0.1).require_document(
                [_with_ratio(1000, 0.2)]
            )


class TestDocumentLevel:
    def test_small_dense_page_inside_a_clean_document_is_accepted(self) -> None:
        """The z-manual shape: one 677-character page at 0.34 among 40 clean ones."""
        clean = ["ordinary prose. " * 300] * 40
        dense = _with_ratio(677, 0.34)
        BinaryContent().require_document([*clean, dense])

    def test_single_large_binary_page_is_rejected(self) -> None:
        with pytest.raises(UnindexableContentError):
            BinaryContent().require_document([_with_ratio(2_000_000, 0.52)])

    def test_large_page_at_the_limit_with_density_is_rejected(self) -> None:
        clean = ["ordinary prose. " * 600] * 80
        page = _with_ratio(LARGE_PAGE_CHARS, 0.30)
        with pytest.raises(UnindexableContentError, match="page 81"):
            BinaryContent().require_document([*clean, page])

    def test_page_just_under_the_limit_is_accepted_in_a_clean_document(self) -> None:
        clean = ["ordinary prose. " * 600] * 80
        page = _with_ratio(LARGE_PAGE_CHARS - 1, 0.30)
        BinaryContent().require_document([*clean, page])

    def test_aggregate_ratio_rejects_many_small_dense_pages(self) -> None:
        pages = [_with_ratio(1000, 0.4)] * 30
        with pytest.raises(UnindexableContentError, match="non-text ratio"):
            BinaryContent().require_document(pages)

    def test_large_page_constant_is_64_kib(self) -> None:
        assert LARGE_PAGE_CHARS == 64 * 1024


class TestSniff:
    def test_clean_body_passes(self) -> None:
        BinaryContent().sniff("a plain note\n" * 1000)

    def test_magic_is_rejected(self) -> None:
        with pytest.raises(UnindexableContentError, match="pdf header"):
            BinaryContent().sniff("%PDF-1.7\n" + "x" * 500)

    def test_dense_small_body_is_rejected(self) -> None:
        with pytest.raises(UnindexableContentError, match="non-text ratio"):
            BinaryContent().sniff(_with_ratio(2000, 0.6))

    def test_binary_tail_of_a_large_body_is_sampled(self) -> None:
        window = 32 * 1024
        body = "a" * (window * 4) + _NONTEXT * (window * 2)
        with pytest.raises(UnindexableContentError, match="non-text ratio"):
            BinaryContent().sniff(body)

    def test_binary_middle_of_a_large_body_is_sampled(self) -> None:
        window = 32 * 1024
        body = "a" * (window * 3) + _NONTEXT * (window * 3) + "a" * (window * 3)
        with pytest.raises(UnindexableContentError, match="non-text ratio"):
            BinaryContent().sniff(body)

    def test_empty_body_passes(self) -> None:
        BinaryContent().sniff("")

    def test_sniff_is_bounded_for_huge_bodies(self) -> None:
        BinaryContent().sniff("a" * (50 * 1024 * 1024))
