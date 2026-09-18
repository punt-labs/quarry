"""Behaviour of the Loop 2 value types."""

from __future__ import annotations

from quarry.mission_sync_types import MissionSyncOutcome, SyncOptions, SyncTally


class TestSyncOptions:
    def test_defaults_are_a_full_live_sync(self) -> None:
        options = SyncOptions()
        assert options.mission_id == ""
        assert options.dry_run is False
        assert options.force is False


class TestMissionSyncOutcome:
    def _outcome(self, *, dry_run: bool = False) -> MissionSyncOutcome:
        return MissionSyncOutcome(
            filed=("mission-quarry-m-1-r1",),
            skipped=("mission-quarry-m-1-r2",),
            errors=("m-2: contract.yaml is not a mapping",),
            dry_run=dry_run,
        )

    def test_to_dict_is_the_json_shape(self) -> None:
        assert self._outcome().to_dict() == {
            "filed": ["mission-quarry-m-1-r1"],
            "skipped": ["mission-quarry-m-1-r2"],
            "errors": ["m-2: contract.yaml is not a mapping"],
            "dry_run": False,
        }

    def test_render_lists_every_document_and_error(self) -> None:
        text = self._outcome().render()
        assert text.splitlines()[0] == (
            "▶  Mission memories: filed 1, skipped 1, errors 1"
        )
        assert "  filed: mission-quarry-m-1-r1" in text
        assert "  skipped (already filed): mission-quarry-m-1-r2" in text
        assert "  error: m-2: contract.yaml is not a mapping" in text

    def test_dry_run_render_says_would_file(self) -> None:
        text = self._outcome(dry_run=True).render()
        assert "would file 1" in text
        assert "  would file: mission-quarry-m-1-r1" in text

    def test_has_errors(self) -> None:
        assert self._outcome().has_errors is True
        assert MissionSyncOutcome((), (), (), dry_run=False).has_errors is False


class TestSyncTally:
    def test_empty_tally_is_an_empty_outcome(self) -> None:
        assert SyncTally().outcome(dry_run=True) == MissionSyncOutcome(
            (), (), (), dry_run=True
        )

    def test_scan_errors_lead_and_dispositions_keep_arrival_order(self) -> None:
        tally = SyncTally(("m-2: contract.yaml is not a mapping",))
        tally.filed("r1")
        tally.error("r2: daemon returned HTTP 503: queue full")
        tally.skipped("r3")
        tally.filed("r4")
        assert tally.outcome(dry_run=False) == MissionSyncOutcome(
            filed=("r1", "r4"),
            skipped=("r3",),
            errors=(
                "m-2: contract.yaml is not a mapping",
                "r2: daemon returned HTTP 503: queue full",
            ),
            dry_run=False,
        )
