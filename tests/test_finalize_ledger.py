"""Tests for the per-database finalize failure ledger and its backoff."""

from __future__ import annotations

from quarry.finalize_ledger import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    FinalizeLedger,
    FinalizeLedgers,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_fresh_ledger_is_not_backed_off() -> None:
    assert not FinalizeLedger("db", _Clock()).is_backed_off()


def test_failure_backs_off_until_window_elapses() -> None:
    clock = _Clock()
    ledger = FinalizeLedger("db", clock)
    ledger.record_failure("boom")
    assert ledger.is_backed_off()
    clock.now += BACKOFF_BASE_SECONDS + 1
    assert not ledger.is_backed_off()


def test_consecutive_failures_double_the_window() -> None:
    clock = _Clock()
    ledger = FinalizeLedger("db", clock)
    ledger.record_failure("a")
    clock.now += BACKOFF_BASE_SECONDS + 1
    ledger.record_failure("b")
    clock.now += BACKOFF_BASE_SECONDS + 1
    assert ledger.is_backed_off()
    clock.now += BACKOFF_BASE_SECONDS
    assert not ledger.is_backed_off()


def test_window_is_capped() -> None:
    ledger = FinalizeLedger("db", _Clock())
    for _ in range(40):
        ledger.record_failure("x")
    assert ledger.health().retry_in_seconds <= BACKOFF_CAP_SECONDS


def test_success_resets_the_ledger() -> None:
    ledger = FinalizeLedger("db", _Clock())
    ledger.record_failure("boom")
    ledger.record_success()
    health = ledger.health()
    assert not ledger.is_backed_off()
    assert (health.consecutive_failures, health.last_error) == (0, "")


def test_registry_returns_one_ledger_per_database() -> None:
    assert FinalizeLedgers.for_database("/x/one") is FinalizeLedgers.for_database(
        "/x/one"
    )
