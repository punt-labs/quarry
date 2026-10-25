"""Tests for quarry.ingestion.run_context: one name listing for a bulk run's pages."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from quarry.config import Settings
from quarry.db import Database
from quarry.ingestion.ingest_context import IngestContext
from quarry.ingestion.run_context import RunContext
from tests.fakes import FakeEmbeddingBackend, patched_embedder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "lancedb"
    path.mkdir()
    with patched_embedder(FakeEmbeddingBackend(768)):
        yield Database.connect(path)


def _context(db: Database) -> IngestContext:
    return IngestContext(
        db,
        Settings(),
        collection="c",
        agent_handle="rmh",
        memory_type="fact",
        summary="a summary",
    )


class TestForRun:
    def test_it_keeps_every_knob_and_sets_overwrite(self, db: Database) -> None:
        run = RunContext.for_run(_context(db), overwrite=True)
        assert run.overwrite is True
        assert (run.collection, run.agent_handle, run.memory_type, run.summary) == (
            "c",
            "rmh",
            "fact",
            "a summary",
        )

    def test_it_is_an_ingest_context(self, db: Database) -> None:
        assert isinstance(
            RunContext.for_run(_context(db), overwrite=False), IngestContext
        )


class TestLegacyNamesOf:
    def test_the_pages_of_a_run_share_one_listing(self, db: Database) -> None:
        run = RunContext.for_run(_context(db), overwrite=True)
        assert RunContext.legacy_names_of(run) is RunContext.legacy_names_of(run)

    def test_a_single_ingest_gets_a_fresh_listing_each_time(self, db: Database) -> None:
        context = _context(db)
        assert RunContext.legacy_names_of(context) is not RunContext.legacy_names_of(
            context
        )

    def test_noting_a_stored_name_in_a_single_ingest_does_nothing(
        self, db: Database
    ) -> None:
        RunContext.note_stored(_context(db), "https://example.com/a")
