"""Tests for quarry.content_guard: binary classification at document level."""

from __future__ import annotations

import pytest

from quarry.content_guard import BinaryContent, UnindexableContentError

LARGE_PAGE_CHARS = BinaryContent.LARGE_PAGE_CHARS
SAMPLE_WINDOW = BinaryContent.SAMPLE_WINDOW_CHARS
_PDF_EVIDENCE = "%PDF-1.5\n%" + "\ufffd" * 4 + "\n"

_NONTEXT = "\ufffd"


def _with_ratio(length: int, ratio: float) -> str:
    """Return *length* characters of which ``ratio`` are U+FFFD."""
    bad = round(length * ratio)
    return "a" * (length - bad) + _NONTEXT * bad


class TestMagic:
    @pytest.mark.parametrize(
        ("prefix", "label"),
        [
            (_PDF_EVIDENCE, "pdf header"),
            ("PK\x03\x04rest", "zip archive header"),
            ("\x89PNG\r\n", "png header"),
            ("\ufffdPNG\r\n", "png header"),
            ("GIF89a\x00\x01\x02\x03\x04", "gif header"),
            ("\xff\xd8\xff\xe0", "jpeg header"),
            ("\ufffd\ufffd\ufffd\ufffd", "undecodable binary header"),
            ("\x7fELF\x02", "elf header"),
        ],
    )
    def test_known_magic_is_rejected(self, prefix: str, label: str) -> None:
        with pytest.raises(UnindexableContentError, match=label):
            BinaryContent().require_document([prefix + "x" * 100])

    def test_magic_after_media_type_marker_is_rejected(self) -> None:
        content = "<!-- media_type: application/pdf -->\n" + _PDF_EVIDENCE + "x" * 50
        with pytest.raises(UnindexableContentError, match="pdf header"):
            BinaryContent().require_document([content])

    def test_prose_that_mentions_a_header_passes(self) -> None:
        BinaryContent().require_document(["The %PDF- header opens every file."])

    def test_magic_only_counts_on_the_first_page(self) -> None:
        BinaryContent().require_document(["clean opening page", "%PDF-1.5 quoted"])

    def test_message_carries_the_stable_prefix(self) -> None:
        with pytest.raises(UnindexableContentError) as info:
            BinaryContent().require_document([_PDF_EVIDENCE])
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


class TestSniff:
    def test_clean_body_passes(self) -> None:
        BinaryContent().sniff("a plain note\n" * 1000)

    def test_magic_is_rejected(self) -> None:
        with pytest.raises(UnindexableContentError, match="pdf header"):
            BinaryContent().sniff(_PDF_EVIDENCE + "x" * 500)

    def test_dense_small_body_is_rejected(self) -> None:
        with pytest.raises(UnindexableContentError, match="non-text ratio"):
            BinaryContent().sniff(_with_ratio(2000, 0.6))

    def test_binary_tail_of_a_large_body_is_sampled(self) -> None:
        window = SAMPLE_WINDOW
        body = "a" * (window * 4) + _NONTEXT * (window * 2)
        with pytest.raises(UnindexableContentError, match="non-text ratio"):
            BinaryContent().sniff(body)

    def test_binary_middle_of_a_large_body_is_sampled(self) -> None:
        window = SAMPLE_WINDOW
        body = "a" * (window * 3) + _NONTEXT * (window * 3) + "a" * (window * 3)
        with pytest.raises(UnindexableContentError, match="non-text ratio"):
            BinaryContent().sniff(body)

    def test_empty_body_passes(self) -> None:
        BinaryContent().sniff("")

    def test_sniff_is_bounded_for_huge_bodies(self) -> None:
        BinaryContent().sniff("a" * (50 * 1024 * 1024))


class TestSoftSignatures:
    """Printable signatures reject only with non-text evidence."""

    @pytest.mark.parametrize(
        "text",
        [
            "GIF89a is an image format",
            "%PDF-1.7 notes on the spec",
            "  \n%PDF-1.4 is how a PDF begins",
        ],
    )
    def test_prose_that_starts_with_a_printable_signature_passes(
        self, text: str
    ) -> None:
        BinaryContent().sniff(text)
        BinaryContent().require_document([text])

    def test_a_printable_signature_with_binary_evidence_is_rejected(self) -> None:
        with pytest.raises(UnindexableContentError, match="pdf header"):
            BinaryContent().sniff(_PDF_EVIDENCE)

    def test_a_printable_signature_needs_exactly_the_soft_evidence(self) -> None:
        need = BinaryContent.SOFT_EVIDENCE
        BinaryContent().sniff("%PDF-1.5\n" + _NONTEXT * (need - 1) + "x" * 500)
        with pytest.raises(UnindexableContentError, match="pdf header"):
            BinaryContent().sniff("%PDF-1.5\n" + _NONTEXT * need + "x" * 500)

    def test_magic_after_leading_whitespace_is_still_found(self) -> None:
        with pytest.raises(UnindexableContentError, match="zip archive header"):
            BinaryContent().sniff("  \n\tPK\x03\x04rest")

    @pytest.mark.parametrize("prefix", ["PK\x03\x04", "\x89PNG", "\x7fELF"])
    def test_non_printable_signatures_need_no_evidence(self, prefix: str) -> None:
        with pytest.raises(UnindexableContentError):
            BinaryContent().sniff(prefix + "plain looking tail")


class TestRatioValidation:
    @pytest.mark.parametrize("ratio", [float("nan"), 0.0, 1.0, -1.0, 2.0])
    def test_a_ratio_outside_the_open_unit_interval_is_refused(
        self, ratio: float
    ) -> None:
        with pytest.raises(ValueError, match="max_nontext_ratio"):
            BinaryContent(max_nontext_ratio=ratio)


class TestSniffBoundaries:
    def test_a_ratio_of_exactly_the_threshold_passes_on_the_sniff_path(self) -> None:
        BinaryContent().sniff(_with_ratio(1000, 0.25))

    def test_a_body_just_over_the_threshold_is_rejected_on_the_sniff_path(
        self,
    ) -> None:
        with pytest.raises(UnindexableContentError):
            BinaryContent().sniff(_with_ratio(1000, 0.251))

    @pytest.mark.parametrize("extra", [0, 1])
    def test_binary_in_the_middle_window_is_caught_at_3w_and_3w_plus_1(
        self, extra: int
    ) -> None:
        w = SAMPLE_WINDOW
        body = "a" * w + _NONTEXT * w + "a" * (w + extra)
        with pytest.raises(UnindexableContentError):
            BinaryContent().sniff(body)

    def test_a_character_between_the_windows_is_not_judged(self) -> None:
        """Non-text exactly at the ratio passes; the skipped character goes unseen.

        At 3w + 1 characters the sample is the head, middle and tail windows, and
        the character at index 2w falls in none of them.  The head holds exactly
        the sample's share (a quarter of 3w); judging the whole body would add the
        skipped character and tip it over.
        """
        w = SAMPLE_WINDOW
        share = 3 * w // 4
        body = "a" * (w - share) + _NONTEXT * share + "a" * w + _NONTEXT + "a" * w
        assert len(body) == 3 * w + 1
        assert body[2 * w] == _NONTEXT
        BinaryContent().sniff(body)
        with pytest.raises(UnindexableContentError):
            BinaryContent().require_document([body])
