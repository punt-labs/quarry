"""The finalize ledger has one wire shape on the HTTP and local doctor paths."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.api.health import FinalizeDatabaseHealth
from quarry.doctor_daemon import DaemonDiagnostics
from quarry.doctor_health_probe import HealthProbe
from quarry.finalize_ledger import FinalizeLedger

from .test_doctor_daemon import _run_dir_at

if TYPE_CHECKING:
    from pathlib import Path


def test_local_snapshot_and_http_body_share_field_names() -> None:
    ledger = FinalizeLedger("/d/lancedb")
    ledger.record_failure("Offset overflow error")
    wire = ledger.health().model_dump()
    assert set(wire) == set(FinalizeDatabaseHealth.model_fields)


def test_doctor_consumes_the_body_the_route_emits(tmp_path: Path) -> None:
    ledger = FinalizeLedger("/d/lancedb")
    ledger.record_failure("Offset overflow error")
    body = {"state": "ready", "finalize": {"databases": [ledger.health().model_dump()]}}
    (tmp_path / "serve.port").write_text("8420")
    with (
        _run_dir_at(tmp_path) as settings,
        patch.object(HealthProbe, "fetch", return_value=body),
    ):
        result = DaemonDiagnostics.finalize(settings)
    assert "1 consecutive failures" in result.message
