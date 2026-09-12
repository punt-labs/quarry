"""Tests for :class:`~quarry.cli_formatters.ResultFormatter`.

Each method mirrors the ``--json`` payload shape the CLI already emits, so
these tests build the same dict shapes the real HTTP/local responses produce.
"""

from __future__ import annotations

from quarry.cli_formatters import ResultFormatter


class TestRegistrations:
    def test_empty_returns_placeholder(self) -> None:
        assert ResultFormatter.registrations([]) == "No registered directories."

    def test_formats_collection_and_directory(self) -> None:
        regs: list[dict[str, object]] = [
            {
                "collection": "docs",
                "directory": "/home/u/docs",
                "watch_state": "watched",
            }
        ]
        assert ResultFormatter.registrations(regs) == "docs: /home/u/docs (watched)"

    def test_shows_degraded_watch_state(self) -> None:
        regs: list[dict[str, object]] = [
            {"collection": "docs", "directory": "/x", "watch_state": "degraded"}
        ]
        assert ResultFormatter.registrations(regs) == "docs: /x (degraded)"

    def test_omits_suffix_when_watch_state_absent(self) -> None:
        """A response from a daemon predating DES-045e still renders cleanly."""
        regs: list[dict[str, object]] = [{"collection": "docs", "directory": "/x"}]
        assert ResultFormatter.registrations(regs) == "docs: /x"

    def test_multiple_registrations_one_per_line(self) -> None:
        regs: list[dict[str, object]] = [
            {"collection": "a", "directory": "/a", "watch_state": "watched"},
            {"collection": "b", "directory": "/b", "watch_state": "scan-only"},
        ]
        assert ResultFormatter.registrations(regs) == (
            "a: /a (watched)\nb: /b (scan-only)"
        )


class TestDatabases:
    def test_empty_returns_placeholder(self) -> None:
        assert ResultFormatter.databases([]) == "No databases found."

    def test_formats_name_and_document_count(self) -> None:
        dbs: list[dict[str, object]] = [{"name": "main", "document_count": 12}]
        assert ResultFormatter.databases(dbs) == "main: 12 documents"

    def test_missing_document_count_defaults_to_zero(self) -> None:
        assert ResultFormatter.databases([{"name": "main"}]) == "main: 0 documents"


class TestCoerceResults:
    def test_non_dict_input_returns_empty(self) -> None:
        assert ResultFormatter.coerce_results(["not", "a", "dict"]) == {}
        assert ResultFormatter.coerce_results(None) == {}

    def test_keys_stringified_and_non_dict_values_dropped_to_empty(self) -> None:
        raw = {1: {"pushed": True}, "b": "not-a-dict"}
        assert ResultFormatter.coerce_results(raw) == {"1": {"pushed": True}, "b": {}}


class TestHasFailures:
    def test_true_when_any_result_not_pushed(self) -> None:
        data: dict[str, dict[str, object]] = {
            "a": {"pushed": True},
            "b": {"pushed": False},
        }
        assert ResultFormatter.has_failures(data) is True

    def test_false_when_all_pushed(self) -> None:
        data: dict[str, dict[str, object]] = {
            "a": {"pushed": True},
            "b": {"pushed": True},
        }
        assert ResultFormatter.has_failures(data) is False

    def test_false_for_empty_data(self) -> None:
        assert ResultFormatter.has_failures({}) is False


class TestCapturesPush:
    def test_empty_returns_placeholder(self) -> None:
        assert (
            ResultFormatter.captures_push({}) == "No shadow-enabled projects to push."
        )

    def test_pushed_project(self) -> None:
        data: dict[str, dict[str, object]] = {"docs": {"pushed": True, "rescrubbed": 2}}
        assert ResultFormatter.captures_push(data) == "docs: pushed; rescrubbed 2"

    def test_aborted_project_reports_reason(self) -> None:
        data: dict[str, dict[str, object]] = {
            "docs": {"pushed": False, "aborted_reason": "no remote", "rescrubbed": 0}
        }
        assert ResultFormatter.captures_push(data) == (
            "docs: not pushed (no remote); rescrubbed 0"
        )

    def test_committed_but_push_failed(self) -> None:
        data: dict[str, dict[str, object]] = {
            "docs": {"pushed": False, "rescrubbed": 1}
        }
        assert ResultFormatter.captures_push(data) == (
            "docs: committed, push failed; rescrubbed 1"
        )
