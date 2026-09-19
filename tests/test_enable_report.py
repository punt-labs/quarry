"""Tests for the enable/disable CLI summary presenters."""

from __future__ import annotations

from quarry.enable import DisableResult, EnableResult
from quarry.enable_report import DisableReport, EnableReport
from quarry.ethos_memory import EthosMemoryResult


def _enable(ethos: EthosMemoryResult) -> EnableResult:
    """Return a minimal EnableResult carrying *ethos* as its bootstrap outcome."""
    return EnableResult(
        directory="/p", collection="p", captures_collection="p-captures", ethos=ethos
    )


def test_enable_report_lists_claudemd_steps() -> None:
    result = EnableResult(
        directory="/p",
        collection="p",
        captures_collection="p-captures",
        config_path="/p/.punt-labs/quarry/config.md",
        guide_deposited=True,
        enabled_marker_written=True,
        import_registered=True,
        gitignore_ensured=True,
    )
    lines = EnableReport(result).lines()
    assert lines[0] == "Enabled quarry for /p"
    joined = "\n".join(lines)
    assert "Deposited quarry guide" in joined
    assert "Registered @.punt-labs/quarry/CLAUDE.md" in joined
    # Symmetry with DisableReport's marker line: the write is surfaced.
    assert "Wrote enabled marker" in joined
    assert ".gitignore excludes captures and lock files" in joined


def test_enable_report_omits_absent_steps() -> None:
    result = EnableResult(
        directory="/p", collection="p", captures_collection="p-captures"
    )
    joined = "\n".join(EnableReport(result).lines())
    assert "Registered @" not in joined
    # An idempotent re-enable's marker no-op is visible by its absence, like
    # DisableReport omits the marker line when nothing was removed.
    assert "Wrote enabled marker" not in joined
    assert ".gitignore excludes captures and lock files" not in joined
    assert "Ethos" not in joined


def test_enable_report_gitignore_message_states_postcondition_not_delta() -> None:
    """The .gitignore line must not name one entry when only the other was added.

    Regression for a Bugbot LOW finding: gitignore_ensured is a single bool
    covering two possible entries (captures path, FileLock's lock-artifact
    glob). The message states the guaranteed postcondition (both entries are
    now present) rather than which specific entry this run backfilled, so it
    stays accurate whether this run added the captures line, the lock glob,
    or both.
    """
    result = EnableResult(
        directory="/p",
        collection="p",
        captures_collection="p-captures",
        gitignore_ensured=True,
    )
    joined = "\n".join(EnableReport(result).lines())
    assert ".gitignore excludes captures and lock files" in joined
    # The old wording claimed one specific entry -- must not reappear.
    assert "Added captures/ exclusion" not in joined


def test_enable_report_reports_ethos_skipped() -> None:
    report = EnableReport(_enable(EthosMemoryResult(skipped=True)))
    assert "  Ethos: not installed (agent memory skipped)" in report.lines()


def test_enable_report_reports_created_and_memory_collections() -> None:
    joined = "\n".join(
        EnableReport(_enable(EthosMemoryResult(created=["rmh"]))).lines()
    )
    assert "Ethos created: rmh" in joined
    assert "Memory collections: memory-rmh" in joined


def test_enable_report_announces_vendored_refresh_as_a_diff_to_commit() -> None:
    result = _enable(EthosMemoryResult(vendored_updated=["claude", "rmh"]))
    joined = "\n".join(EnableReport(result).lines())
    assert "Ethos guide refreshed (vendored — commit via PR): claude, rmh" in joined


def test_enable_report_vendored_line_accompanies_not_installed() -> None:
    """A repo can carry a vendored tree while the operator has no global ethos."""
    result = _enable(EthosMemoryResult(skipped=True, vendored_updated=["rmh"]))
    joined = "\n".join(EnableReport(result).lines())
    assert "Ethos: not installed (agent memory skipped)" in joined
    assert "Ethos guide refreshed (vendored — commit via PR): rmh" in joined


def test_enable_report_omits_vendored_line_when_nothing_refreshed() -> None:
    joined = "\n".join(EnableReport(_enable(EthosMemoryResult())).lines())
    assert "vendored" not in joined


def test_enable_report_reports_ethos_failed_last() -> None:
    result = _enable(EthosMemoryResult(created=["rmh"], failed=["rmh"]))
    assert EnableReport(result).lines()[-1] == "  Ethos FAILED: rmh"


def test_disable_report_purge_queued_when_not_keep_data() -> None:
    result = DisableResult(
        directory="/p",
        collection="p",
        captures_collection="p-captures",
        removed=3,
        import_pruned=True,
        enabled_marker_removed=True,
    )
    joined = "\n".join(DisableReport(result, keep_data=False).lines())
    assert "Deregistered p (3 files); chunk purge queued" in joined
    assert "Removed @.punt-labs/quarry/CLAUDE.md" in joined
    assert "Removed enabled marker (guide left dormant)" in joined


def test_disable_report_keeps_data_when_flag_set() -> None:
    result = DisableResult(
        directory="/p", collection="p", captures_collection="p-captures", removed=3
    )
    joined = "\n".join(DisableReport(result, keep_data=True).lines())
    assert "kept indexed data" in joined


def test_disable_report_idempotent_noop() -> None:
    result = DisableResult(directory="/p", collection="", captures_collection="")
    joined = "\n".join(DisableReport(result, keep_data=False).lines())
    assert "Already disabled (no registration)" in joined
