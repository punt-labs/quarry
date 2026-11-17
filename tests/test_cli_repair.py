"""The `quarry repair` command: consent, the daemon guard, and the rebuild."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from quarry.cli_captures import CliPlumbing
from quarry.cli_repair import RepairCli
from quarry.config import Settings
from quarry.db.schema import TABLE_NAME, SchemaManager
from quarry.db.storage import get_db

runner = CliRunner()
_DIM = 768


def _row(doc: str, index: int, text: str) -> dict[str, object]:
    return {
        "text": text,
        "vector": [0.5] * _DIM,
        "document_name": doc,
        "document_path": f"/d/{doc}",
        "collection": "col",
        "page_number": 1,
        "total_pages": 1,
        "chunk_index": index,
        "page_type": "text",
        "source_format": ".txt",
        "ingestion_timestamp": datetime.now(UTC),
        "agent_handle": "",
        "memory_type": "",
        "summary": "",
        "source_version": "",
    }


def _db_path(tmp_path: Path) -> Path:
    path = tmp_path / "default" / "lancedb"
    table = get_db(path).create_table(
        TABLE_NAME, data=[], schema=SchemaManager.schema()
    )
    table.add([_row("ok", 0, "fine text"), _row("pdf", 0, "�" * 80)])
    return path


def _digest(path: Path) -> dict[str, str]:
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def _app(
    db_path: Path,
    out: list[object],
    *,
    json_mode: bool = False,
    err: MagicMock | None = None,
) -> typer.Typer:
    def emit(data: object, text: str = "") -> None:
        out.append(data if json_mode else text)

    plumbing = CliPlumbing(
        emit=emit,
        cli_errors=lambda fn: fn,
        client=MagicMock(),
        err_console=err or MagicMock(),
        is_quiet=lambda: False,
    )
    app = typer.Typer()
    settings = Settings(lancedb_path=db_path, quarry_root=db_path.parent.parent)
    with patch("quarry.cli_repair.Settings.load", return_value=settings):
        RepairCli(plumbing).register(app)
    app.command(name="noop")(
        lambda: None
    )  # a second command keeps `repair` a subcommand
    return app


def _run(
    db_path: Path, args: list[str], *, input: str = ""
) -> tuple[int, list[object]]:
    out: list[object] = []
    app = _app(db_path, out)
    settings = Settings(lancedb_path=db_path, quarry_root=db_path.parent.parent)
    with (
        patch("quarry.cli_repair.Settings.load", return_value=settings),
        patch("quarry.daemon_guard.HealthProbe.fetch", return_value=None),
    ):
        result = runner.invoke(app, ["repair", *args], input=input)
    return result.exit_code, out


class TestConsent:
    def test_yes_rebuilds_without_prompting(self, tmp_path: Path) -> None:
        db = _db_path(tmp_path)
        code, _ = _run(db, ["--yes"])
        rows = get_db(db).open_table(TABLE_NAME).search().limit(10).to_list()
        assert (code, [r["document_name"] for r in rows]) == (0, ["ok"])

    def test_the_prompt_names_what_will_be_dropped(self, tmp_path: Path) -> None:
        _, out = _run(_db_path(tmp_path), [], input="n\n")
        text = "\n".join(str(o) for o in out)
        assert "1 binary" in text
        assert "0 over-budget" in text

    @pytest.mark.parametrize("answer", ["n\n", "\n", "", "no\n", "maybe\n"])
    def test_anything_but_yes_changes_nothing_and_exits_zero(
        self, tmp_path: Path, answer: str
    ) -> None:
        db = _db_path(tmp_path)
        before = _digest(db)
        code, _ = _run(db, [], input=answer)
        assert (code, _digest(db)) == (0, before)

    @pytest.mark.parametrize("answer", ["y\n", "yes\n", "Y\n"])
    def test_an_affirmative_answer_rebuilds(self, tmp_path: Path, answer: str) -> None:
        db = _db_path(tmp_path)
        _run(db, [], input=answer)
        assert get_db(db).open_table(TABLE_NAME).count_rows() == 1

    def test_nothing_to_drop_needs_no_consent_and_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "default" / "lancedb"
        get_db(path).create_table(
            TABLE_NAME, data=[], schema=SchemaManager.schema()
        ).add([_row("ok", 0, "fine")])
        before = _digest(path)
        code, _ = _run(path, [], input="")
        assert (code, _digest(path)) == (0, before)

    def test_dry_run_reports_and_changes_nothing(self, tmp_path: Path) -> None:
        db = _db_path(tmp_path)
        before = _digest(db)
        code, out = _run(db, ["--dry-run", "--yes"])
        assert (code, _digest(db)) == (0, before)
        assert "1 binary" in "\n".join(str(o) for o in out)


class TestDaemonRefusal:
    def test_a_running_daemon_refuses_and_touches_nothing(self, tmp_path: Path) -> None:
        db = _db_path(tmp_path)
        (tmp_path / "default" / "serve.port").write_text("65000")
        before = _digest(db)
        out: list[object] = []
        err = MagicMock()
        app = _app(db, out, err=err)
        settings = Settings(lancedb_path=db, quarry_root=db.parent.parent)
        with (
            patch("quarry.cli_repair.Settings.load", return_value=settings),
            patch(
                "quarry.daemon_guard.HealthProbe.fetch",
                return_value={"state": "ready"},
            ),
        ):
            result = runner.invoke(app, ["repair", "--yes"])
        assert result.exit_code != 0
        assert "stop it first" in str(err.print.call_args)
        assert _digest(db) == before

    def test_a_stale_port_file_does_not_block(self, tmp_path: Path) -> None:
        db = _db_path(tmp_path)
        (tmp_path / "default" / "serve.port").write_text("65000")
        code, _ = _run(db, ["--yes"])
        assert code == 0
        assert get_db(db).open_table(TABLE_NAME).count_rows() == 1


class TestReclaimWithoutDroppedRows:
    """A table of valid rows bloated by leaked files is reclaimed, with consent."""

    def _bloated(self, tmp_path: Path) -> Path:
        path = tmp_path / "default" / "lancedb"
        get_db(path).create_table(
            TABLE_NAME, data=[], schema=SchemaManager.schema()
        ).add([_row("ok", 0, "fine")])
        data = path / f"{TABLE_NAME}.lance" / "data"
        data.mkdir(exist_ok=True)
        (data / "leaked.lance").write_bytes(b"\0" * 4 * 2**20)
        return path

    def _size(self, path: Path) -> int:
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())

    def test_consent_rebuilds_and_shrinks_the_table(self, tmp_path: Path) -> None:
        path = self._bloated(tmp_path)
        before = self._size(path)
        code, _ = _run(path, [], input="y\n")
        assert code == 0
        assert self._size(path) < before / 2
        assert get_db(path).open_table(TABLE_NAME).count_rows() == 1

    def test_declining_leaves_the_bloated_table_untouched(self, tmp_path: Path) -> None:
        path = self._bloated(tmp_path)
        before = _digest(path)
        code, _ = _run(path, [], input="n\n")
        assert (code, _digest(path)) == (0, before)

    def test_the_prompt_names_reclaiming_disk_not_deleting_rows(
        self, tmp_path: Path
    ) -> None:
        path = self._bloated(tmp_path)
        _, out = _run(path, [], input="n\n")
        text = "\n".join(str(o) for o in out)
        assert "reclaim" in text.lower()
        assert "0 binary" in text

    def test_dry_run_stays_read_only(self, tmp_path: Path) -> None:
        path = self._bloated(tmp_path)
        before = _digest(path)
        code, _ = _run(path, ["--dry-run"])
        assert (code, _digest(path)) == (0, before)

    def test_a_clean_compact_table_is_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "default" / "lancedb"
        get_db(path).create_table(
            TABLE_NAME, data=[], schema=SchemaManager.schema()
        ).add([_row("ok", 0, "fine")])
        before = _digest(path)
        code, _ = _run(path, [], input="y\n")
        assert (code, _digest(path)) == (0, before)


class TestSiblingDaemon:
    def test_a_daemon_on_the_default_database_refuses_a_repair_of_another(
        self, tmp_path: Path
    ) -> None:
        import fcntl
        import os

        root = tmp_path / "data"
        for name in ("default", "work"):
            (root / name).mkdir(parents=True)
            (root / name / "registry.db").write_bytes(b"")
        work = root / "work" / "lancedb"
        table = get_db(work).create_table(
            TABLE_NAME, data=[], schema=SchemaManager.schema()
        )
        table.add([_row("ok", 0, "fine text"), _row("pdf", 0, "\ufffd" * 80)])
        before = _digest(work)
        fd = os.open(str(root / "default" / "serve.lock"), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            code, _ = _run(work, ["--yes"])
        finally:
            os.close(fd)
        assert code != 0
        assert _digest(work) == before


def test_json_mode_emits_the_plan(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    out: list[object] = []
    app = _app(db, out, json_mode=True)
    settings = Settings(lancedb_path=db, quarry_root=db.parent.parent)
    with (
        patch("quarry.cli_repair.Settings.load", return_value=settings),
        patch("quarry.daemon_guard.HealthProbe.fetch", return_value=None),
    ):
        runner.invoke(app, ["repair", "--dry-run"])
    assert json.loads(json.dumps(out[0]))["binary_rows"] == 1
