"""Tests for the 5 sync concurrency fixes.

Fix 1: Server-side sync lock (409 on concurrent POST /sync)
Fix 2: Registration subsumption (parent deregisters children, child rejected)
Fix 3: Batch LanceDB writes (single table.add per sync_collection)
Fix 4: Guard optimize_table on fragment count
Fix 5: Async sync endpoint (202 Accepted, task_id, GET /sync/<id>)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from starlette.testclient import TestClient

from quarry.database import (
    FRAGMENT_THRESHOLD,
    TABLE_NAME,
    batch_insert_chunks,
    count_fragments,
    optimize_table,
)
from quarry.http_server import SyncTaskState, _QuarryContext, build_app
from quarry.models import Chunk
from quarry.sync_registry import (
    list_registrations,
    open_registry,
    register_directory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.lancedb_path.mkdir(parents=True)
    s.registry_path = tmp_path / "registry.db"
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    s.chunk_max_chars = 1800
    s.chunk_overlap_chars = 200
    return s


def _mock_db() -> MagicMock:
    return MagicMock()


def _mock_embedder() -> MagicMock:
    embedder = MagicMock()
    embedder.embed_query.return_value = np.zeros(768, dtype=np.float32)
    embedder.dimension = 768
    embedder.model_name = "test-model"
    return embedder


_SHARED_DB = _mock_db()
_SHARED_EMBEDDER = _mock_embedder()


def _make_client(tmp_path: Path) -> TestClient:
    settings = _mock_settings(tmp_path)
    ctx = _QuarryContext(settings)
    ctx.__dict__["db"] = _SHARED_DB
    ctx.__dict__["embedder"] = _SHARED_EMBEDDER
    return TestClient(build_app(ctx), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fix 1: Server-side sync lock
# ---------------------------------------------------------------------------


class TestSyncLock:
    """Fix 1: concurrent POST /sync returns 409."""

    def test_second_post_while_running_returns_409(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER

        # Simulate a running sync task.
        ctx.sync_task = SyncTaskState(task_id="sync-existing", status="running")

        app = build_app(ctx)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post("/sync", json={})
        assert resp.status_code == 409
        data = resp.json()
        assert data["task_id"] == "sync-existing"
        assert "already in progress" in data["error"].lower()

    def test_post_allowed_after_previous_completed(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER

        # Previous sync completed.
        ctx.sync_task = SyncTaskState(task_id="sync-old", status="completed")

        app = build_app(ctx)
        client = TestClient(app, raise_server_exceptions=False)

        with patch("quarry.sync.sync_all", return_value={}):
            resp = client.post("/sync", json={})
        assert resp.status_code == 202

    def test_post_allowed_after_previous_failed(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER

        # Previous sync failed.
        ctx.sync_task = SyncTaskState(
            task_id="sync-bad", status="failed", error="disk full"
        )

        app = build_app(ctx)
        client = TestClient(app, raise_server_exceptions=False)

        with patch("quarry.sync.sync_all", return_value={}):
            resp = client.post("/sync", json={})
        assert resp.status_code == 202

    def test_lock_released_after_sync_completes(self, tmp_path: Path) -> None:
        """After background task completes, the state is no longer 'running'."""
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER

        ctx.sync_task = SyncTaskState(task_id="sync-done", status="completed")

        # A new POST should succeed, not 409.
        app = build_app(ctx)
        client = TestClient(app, raise_server_exceptions=False)
        with patch("quarry.sync.sync_all", return_value={}):
            resp = client.post("/sync", json={})
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Fix 2: Registration subsumption
# ---------------------------------------------------------------------------


class TestRegistrationSubsumption:
    """Fix 2: parent subsumes children, child rejected by parent."""

    def test_parent_deregisters_children(self, tmp_path: Path) -> None:
        conn = open_registry(tmp_path / "r.db")
        child = tmp_path / "parent" / "child"
        child.mkdir(parents=True)
        parent = tmp_path / "parent"

        register_directory(conn, child, "child-col")
        assert len(list_registrations(conn)) == 1

        # Registering the parent should deregister the child.
        register_directory(conn, parent, "parent-col")
        regs = list_registrations(conn)
        assert len(regs) == 1
        assert regs[0].collection == "parent-col"
        assert regs[0].directory == str(parent.resolve())
        conn.close()

    def test_child_rejected_when_parent_exists(self, tmp_path: Path) -> None:
        conn = open_registry(tmp_path / "r.db")
        parent = tmp_path / "parent"
        parent.mkdir()
        child = parent / "child"
        child.mkdir()

        register_directory(conn, parent, "parent-col")

        with pytest.raises(ValueError, match="already covered by parent"):
            register_directory(conn, child, "child-col")
        conn.close()

    def test_non_overlapping_registrations_unaffected(self, tmp_path: Path) -> None:
        conn = open_registry(tmp_path / "r.db")
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()

        register_directory(conn, a, "alpha")
        register_directory(conn, b, "beta")

        regs = list_registrations(conn)
        assert len(regs) == 2
        conn.close()

    def test_same_directory_still_rejected(self, tmp_path: Path) -> None:
        """Re-registering the same dir is not subsumption, it's a duplicate."""
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "docs"
        d.mkdir()

        register_directory(conn, d, "first")

        with pytest.raises(ValueError, match="already registered"):
            register_directory(conn, d, "second")
        conn.close()

    def test_parent_subsumes_multiple_children(self, tmp_path: Path) -> None:
        conn = open_registry(tmp_path / "r.db")
        parent = tmp_path / "root"
        parent.mkdir()
        c1 = parent / "a"
        c1.mkdir()
        c2 = parent / "b"
        c2.mkdir()

        register_directory(conn, c1, "c1")
        register_directory(conn, c2, "c2")
        assert len(list_registrations(conn)) == 2

        register_directory(conn, parent, "root")
        regs = list_registrations(conn)
        assert len(regs) == 1
        assert regs[0].collection == "root"
        conn.close()


# ---------------------------------------------------------------------------
# Fix 3: Batch LanceDB writes
# ---------------------------------------------------------------------------


class TestBatchInsertChunks:
    """Fix 3: batch_insert_chunks writes all chunks in one table.add() call."""

    def _make_chunk(self, name: str, collection: str = "col") -> Chunk:
        from datetime import UTC, datetime

        return Chunk(
            document_name=name,
            document_path=f"/fake/{name}",
            collection=collection,
            page_number=1,
            total_pages=1,
            chunk_index=0,
            text="content",
            page_raw_text="content",
            page_type="text",
            source_format=".txt",
            ingestion_timestamp=datetime.now(UTC),
        )

    def test_empty_batch_returns_zero(self) -> None:
        db = MagicMock()
        assert batch_insert_chunks(db, []) == 0

    def test_single_document_batch(self) -> None:
        db = MagicMock()
        db.list_tables.return_value.tables = [TABLE_NAME]
        table = MagicMock()
        db.open_table.return_value = table
        table.schema = MagicMock()

        chunks = [self._make_chunk("a.txt")]
        vectors = np.zeros((1, 768), dtype=np.float32)

        count = batch_insert_chunks(db, [(chunks, vectors)])
        assert count == 1
        table.add.assert_called_once()

    def test_multiple_documents_single_add(self) -> None:
        """N documents should produce exactly 1 table.add() call."""
        db = MagicMock()
        db.list_tables.return_value.tables = [TABLE_NAME]
        table = MagicMock()
        db.open_table.return_value = table
        table.schema = MagicMock()

        batch = []
        for name in ("a.txt", "b.txt", "c.txt"):
            chunks = [self._make_chunk(name)]
            vectors = np.zeros((1, 768), dtype=np.float32)
            batch.append((chunks, vectors))

        count = batch_insert_chunks(db, batch)
        assert count == 3
        # Only 1 add call, not 3.
        assert table.add.call_count == 1
        # The single add call should have 3 records.
        records = table.add.call_args[0][0]
        assert len(records) == 3


# ---------------------------------------------------------------------------
# Fix 4: Guard optimize_table on fragment count
# ---------------------------------------------------------------------------


class TestOptimizeGuard:
    """Fix 4: optimize_table skips when fragment count > threshold."""

    def test_optimize_skips_above_threshold(self) -> None:
        db = MagicMock()
        db.list_tables.return_value.tables = [TABLE_NAME]
        table = MagicMock()
        db.open_table.return_value = table

        with patch(
            "quarry.database.count_fragments",
            return_value=FRAGMENT_THRESHOLD + 1,
        ):
            optimize_table(db)

        table.optimize.assert_not_called()

    def test_optimize_runs_below_threshold(self) -> None:
        db = MagicMock()
        db.list_tables.return_value.tables = [TABLE_NAME]
        table = MagicMock()
        db.open_table.return_value = table

        with patch("quarry.database.count_fragments", return_value=100):
            optimize_table(db)

        table.optimize.assert_called_once()

    def test_optimize_force_bypasses_threshold(self) -> None:
        db = MagicMock()
        db.list_tables.return_value.tables = [TABLE_NAME]
        table = MagicMock()
        db.open_table.return_value = table

        with patch(
            "quarry.database.count_fragments",
            return_value=FRAGMENT_THRESHOLD + 1,
        ):
            optimize_table(db, force=True)

        table.optimize.assert_called_once()

    def test_optimize_logs_warning_above_threshold(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        db = MagicMock()
        db.list_tables.return_value.tables = [TABLE_NAME]
        table = MagicMock()
        db.open_table.return_value = table

        with (
            patch(
                "quarry.database.count_fragments",
                return_value=FRAGMENT_THRESHOLD + 1,
            ),
            caplog.at_level(logging.WARNING),
        ):
            optimize_table(db)

        assert any("Skipping optimization" in msg for msg in caplog.messages)
        assert any("quarry optimize --force" in msg for msg in caplog.messages)

    def test_count_fragments_no_table(self) -> None:
        db = MagicMock()
        db.list_tables.return_value.tables = []
        assert count_fragments(db) == 0

    def test_count_fragments_counts_data_dir(self, tmp_path: Path) -> None:
        """count_fragments counts entries in the data/ directory."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        for i in range(5):
            (data_dir / f"fragment-{i}").mkdir()

        db = MagicMock()
        db.list_tables.return_value.tables = [TABLE_NAME]
        table = MagicMock()
        table.uri = str(tmp_path)
        db.open_table.return_value = table

        assert count_fragments(db) == 5


# ---------------------------------------------------------------------------
# Fix 5: Async sync endpoint
# ---------------------------------------------------------------------------


class TestAsyncSyncEndpoint:
    """Fix 5: POST /sync returns 202 + task_id; GET /sync/<id> returns status."""

    def test_post_returns_202_with_task_id(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        with patch("quarry.sync.sync_all", return_value={}):
            resp = client.post("/sync", json={})
        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == "accepted"
        assert data["task_id"].startswith("sync-")

    def test_get_unknown_task_returns_404(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        resp = client.get("/sync/nonexistent")
        assert resp.status_code == 404

    def test_get_running_task(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        ctx.sync_task = SyncTaskState(task_id="sync-abc", status="running")

        client = TestClient(build_app(ctx), raise_server_exceptions=False)
        resp = client.get("/sync/sync-abc")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["task_id"] == "sync-abc"

    def test_get_completed_task_includes_results(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        ctx.sync_task = SyncTaskState(
            task_id="sync-done",
            status="completed",
            results={"math": {"ingested": 5}},
        )

        client = TestClient(build_app(ctx), raise_server_exceptions=False)
        resp = client.get("/sync/sync-done")
        data = resp.json()
        assert data["status"] == "completed"
        assert data["results"]["math"]["ingested"] == 5

    def test_get_failed_task_includes_error(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        ctx.sync_task = SyncTaskState(
            task_id="sync-bad",
            status="failed",
            error="disk full",
        )

        client = TestClient(build_app(ctx), raise_server_exceptions=False)
        resp = client.get("/sync/sync-bad")
        data = resp.json()
        assert data["status"] == "failed"
        assert data["error"] == "disk full"

    def test_409_includes_existing_task_id(self, tmp_path: Path) -> None:
        """409 response includes the task_id of the running sync."""
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        ctx.sync_task = SyncTaskState(task_id="sync-running", status="running")

        client = TestClient(build_app(ctx), raise_server_exceptions=False)
        resp = client.post("/sync", json={})
        assert resp.status_code == 409
        data = resp.json()
        assert data["task_id"] == "sync-running"
