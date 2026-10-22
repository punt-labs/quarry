"""Tests for quarry.ingestion.media_type_policy: the media-type allow-list."""

from __future__ import annotations

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.ingestion.media_type_policy import MediaTypePolicy, UnsupportedContentError


class TestMediaTypePolicy:
    @pytest.mark.parametrize(
        "media_type",
        [
            "",
            "text/html",
            "text/plain",
            "text/markdown",
            "application/json",
            "application/xml",
            "application/xhtml+xml",
            "application/yaml",
            "application/x-yaml",
            "application/javascript",
            "application/vnd.api+json",
            "application/atom+xml",
        ],
    )
    def test_allowed(self, media_type: str) -> None:
        policy = MediaTypePolicy()
        assert policy.allows(media_type)
        policy.require(media_type)

    @pytest.mark.parametrize(
        "media_type",
        [
            "application/pdf",
            "application/octet-stream",
            "application/zip",
            "image/png",
            "audio/mpeg",
            "video/mp4",
            "font/woff2",
        ],
    )
    def test_rejected(self, media_type: str) -> None:
        with pytest.raises(UnsupportedContentError):
            MediaTypePolicy().require(media_type)

    def test_message_has_the_stable_prefix_and_no_url(self) -> None:
        with pytest.raises(UnsupportedContentError) as info:
            MediaTypePolicy().require("application/pdf")
        assert (
            str(info.value)
            == "content rejected: unsupported media type application/pdf"
        )

    def test_hostile_media_type_is_not_echoed_in_the_message(self) -> None:
        with pytest.raises(UnsupportedContentError) as info:
            MediaTypePolicy().require("image/x\n<!-- injected -->")
        assert str(info.value) == "content rejected: unsupported media type (invalid)"

    def test_unsupported_content_is_an_unindexable_value_error(self) -> None:
        assert issubclass(UnsupportedContentError, UnindexableContentError)
        assert issubclass(UnsupportedContentError, ValueError)

    def test_policy_is_configurable(self) -> None:
        policy = MediaTypePolicy(exact=frozenset(), prefixes=("text/",), suffixes=())
        assert not policy.allows("application/json")
        assert policy.allows("text/plain")


class TestNormalization:
    @pytest.mark.parametrize(
        "header",
        ["Application/JSON", "application/json; charset=utf-8", " TEXT/Plain "],
    )
    def test_allows_normalizes_its_input(self, header: str) -> None:
        assert MediaTypePolicy().allows(header)

    def test_a_refused_header_is_still_refused_after_normalizing(self) -> None:
        assert not MediaTypePolicy().allows("Application/PDF; x=y")

    def test_exact_entries_must_be_lowercase(self) -> None:
        with pytest.raises(ValueError, match="lowercase"):
            MediaTypePolicy(exact=frozenset({"Application/JSON"}))

    def test_prefixes_must_be_lowercase(self) -> None:
        with pytest.raises(ValueError, match="lowercase"):
            MediaTypePolicy(prefixes=("TEXT/",))

    def test_suffixes_must_be_lowercase(self) -> None:
        with pytest.raises(ValueError, match="lowercase"):
            MediaTypePolicy(suffixes=("+JSON",))


class TestLabel:
    def test_a_well_formed_type_is_echoed(self) -> None:
        assert MediaTypePolicy.label("Application/PDF; x=1") == "application/pdf"

    @pytest.mark.parametrize(
        "raw", ["", "pdf", "a/b\n<!-- x -->", "x" * 500 + "/y", "a b/c"]
    )
    def test_anything_else_is_not_echoed(self, raw: str) -> None:
        assert MediaTypePolicy.label(raw) == "(invalid)"
