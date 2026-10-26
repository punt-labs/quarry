"""Settlement: how a store ended, in the words a job's status needs."""

from __future__ import annotations

from quarry.daemon.settlement import Settlement


def test_a_store_that_wrote_what_was_asked_carries_its_results() -> None:
    settlement = Settlement.done({"chunks": 3})

    assert settlement.is_stored
    assert settlement.results == {"chunks": 3}
    assert settlement.reason == ""


def test_a_results_that_is_empty_can_still_be_stored() -> None:
    assert Settlement.done({}).is_stored


def test_a_store_that_wrote_nothing_carries_its_reason() -> None:
    settlement = Settlement.failed("nothing was stored")

    assert not settlement.is_stored
    assert settlement.results == {}
    assert settlement.reason == "nothing was stored"
