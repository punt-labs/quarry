"""Reproduce quarry-uq95: repeated capture of an UNCHANGED transcript re-embeds
every chunk and grows the LanceDB table without bound.

PROVEN, not assumed (bead quarry-uq95 is explicit that the mechanism was only
observed, never proven): the capture ingest path (``session_transcript.py`` ->
``api/capture_ingest.py`` -> ``daemon/routes/captures.py`` ->
``ScrubbedIngestJob.scrub_and_ingest`` -> ``ingest_content`` ->
``ChunkStoreFunnel``) has no content-hash or chunk-identity gate before the
delete+re-add: ``overwrite=True`` (the capture default, ``captures.py``
``_require_options``) makes ``_chunk_embed_store`` run
``LegacyUrlNames.replace_prior`` -> ``ChunkStore.delete_document`` followed by
a full embed-and-insert, EVERY time, whether or not the transcript text
changed since the last capture. The only existing "unchanged content" gate in
this codebase, :class:`quarry.ingestion.settled_file.SettledFile`, guards the
directory-*sync* watch path (``sync_change.py`` / ``sync_planner.py`` /
``file_indexer.py``) and is never consulted by the capture-ingest path at all
(``grep -rn "SettledFile\\|StoredSessions" session_transcript.py
pre_compact_hook.py hooks_agent.py daemon/content_jobs.py
daemon/routes/captures.py`` returns nothing). ``StoredSessions`` /
``TranscriptSnapshot`` (``stored_sessions.py``) is the ONE place a
"skip unchanged" decision already exists for transcripts -- but it is wired
only into ``backfill_filing.py`` / ``backfill_project.py``, never into the
live-session hook path (``session_transcript.py``'s
``SessionTranscriptCapture.capture()`` calls ``_archive`` /
``_write_local_capture`` / ``_send`` unconditionally; no snapshot-vs-stored
comparison anywhere in that file, ``pre_compact_hook.py``, or
``hooks_agent.py``).

Each pass of this re-ingest ALSO leaves the LanceDB table one more commit
version and one more data fragment behind it (DES-034's single-writer
contract runs delete-then-add as two separate commits), which is the second,
independently proven axis: the same defect that re-embeds every chunk is
ALSO what drives #622's disk-growth report (quarry-b9c4) -- see
``TestVersionChurnGrowsWithRepeatedReingest`` below, which measures table
version count, fragment count, and on-disk byte size growing linearly with N
re-ingests of byte-IDENTICAL content, with no change to the document at all.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import lancedb
import pytest

from quarry.config import Settings
from quarry.db import Database
from quarry.db.schema import TABLE_NAME
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.ingestion.web_ingest import InlineIngest, ingest_content
from quarry.results import IngestResult
from tests.fakes import FakeEmbeddingBackend

_REINGEST_PASSES = 6
_TRANSCRIPT_TEXT = (
    "role: user\nhello\n\nrole: assistant\n" + "lorem ipsum dolor sit amet. " * 80
)
_DOCUMENT_NAME = "session-aba3b7a9"
_COLLECTION = "repo-captures"


def _ingest_capture_once(db: Database, settings: Settings) -> IngestResult:
    """Ingest *_TRANSCRIPT_TEXT* exactly as the capture route does: overwrite=True.

    Mirrors ``daemon/content_jobs.py: ScrubbedIngestJob.scrub_and_ingest`` ->
    ``ingest_content`` for a transcript capture (``session_transcript.py``'s
    ``_send`` -> the daemon's ``/capture`` route -> ``captures.py:
    _require_options`` default ``overwrite=True``).
    """
    return ingest_content(
        InlineIngest(_TRANSCRIPT_TEXT, format_hint="plain"),
        _DOCUMENT_NAME,
        Progress(None),
        IngestContext(db, settings, overwrite=True, collection=_COLLECTION),
    )


class TestUnchangedTranscriptReembedsEveryChunk:
    """Deliverable 1: the embed count must stay flat across N unchanged re-ingests.

    FAILS on current main: nothing short-circuits an identical capture, so
    ``embed_texts`` is called with the SAME three chunks on every pass and the
    cumulative embedded-text count scales linearly with N.
    """

    def test_embed_call_count_stays_flat_across_repeated_unchanged_captures(
        self, tmp_path: Path
    ) -> None:
        db = Database.connect(tmp_path / "lancedb")
        backend = FakeEmbeddingBackend()
        settings = Settings()

        with patch(
            "quarry.ingestion.streaming.get_embedding_backend", lambda _s: backend
        ):
            for _ in range(_REINGEST_PASSES):
                _ingest_capture_once(db, settings)

        chunks_per_pass = 3
        # This is the defect: embedded grows by chunks_per_pass EVERY pass
        # because nothing detects the content is unchanged. A fixed
        # implementation embeds once (chunks_per_pass total) and skips the
        # other (_REINGEST_PASSES - 1) passes as no-ops.
        assert len(backend.embedded) == chunks_per_pass, (
            f"expected a flat embed count of {chunks_per_pass} across "
            f"{_REINGEST_PASSES} re-ingests of UNCHANGED content; got "
            f"{len(backend.embedded)} -- every pass re-embedded the whole "
            "transcript instead of skipping a no-op capture"
        )

    def test_stored_chunk_count_never_exceeds_one_documents_worth(
        self, tmp_path: Path
    ) -> None:
        """The table must hold one session's chunks, not N accumulated copies.

        Overwrite deletes the prior copy before adding
        (``ChunkStoreFunnel._embed_and_store`` -> ``LegacyUrlNames.replace_prior``
        -> ``ChunkStore.delete_document``), so row COUNT does stay bounded today
        -- this assertion passes even on unfixed code. It is here so a future
        regression that drops the delete (e.g. switching to append-only) is
        caught immediately, distinct from the version/fragment churn below.
        """
        db = Database.connect(tmp_path / "lancedb")
        backend = FakeEmbeddingBackend()
        settings = Settings()

        with patch(
            "quarry.ingestion.streaming.get_embedding_backend", lambda _s: backend
        ):
            for _ in range(_REINGEST_PASSES):
                _ingest_capture_once(db, settings)

        table = lancedb.connect(tmp_path / "lancedb").open_table(TABLE_NAME)
        assert table.count_rows() == 3


class TestIsolateWhichStepHoldsTheWork:
    """Deliverable 2: isolate (a) re-ingest, (b) optimize/FTS rebuild, (c) ONNX.

    Each sub-test holds two of the three steps fixed and varies the third, so
    the measured growth (or its absence) is attributable to exactly one step.
    """

    def test_a_capture_reingest_path_is_the_source_of_repeated_embedding(
        self, tmp_path: Path
    ) -> None:
        """(a) PROVEN: delete-then-reindex of an unchanged transcript re-embeds.

        No optimize() call in this test -- step (b) is absent entirely -- yet
        the embed count still scales with N. This isolates the re-ingest path
        itself (not the optimizer, not the embedding session) as the step that
        holds/duplicates the work.
        """
        db = Database.connect(tmp_path / "lancedb")
        backend = FakeEmbeddingBackend()
        settings = Settings()

        with patch(
            "quarry.ingestion.streaming.get_embedding_backend", lambda _s: backend
        ):
            for _ in range(_REINGEST_PASSES):
                _ingest_capture_once(db, settings)
                # Deliberately NOT calling db.optimizer.optimize() here.

        assert len(backend.embedded) == 3 * _REINGEST_PASSES, (
            "re-embedding scaled with N even with the optimizer never invoked -- "
            "(a) the capture re-ingest path is the proven cause, independent of (b)"
        )

    def test_b_optimize_is_not_the_cause_of_repeated_embedding(
        self, tmp_path: Path
    ) -> None:
        """(b) RULED OUT as the cause of re-EMBEDDING: optimize() never embeds.

        ``TableOptimizer.optimize`` (``db/optimizer.py``) only calls
        ``table.optimize()`` and ``table.create_index(..., replace=True)`` --
        neither touches the embedding backend. Calling it after every pass
        changes zero embed calls versus the no-optimize variant above,
        confirming the embed-count defect lives entirely in the ingest path,
        not in post-sync finalization.
        """
        db = Database.connect(tmp_path / "lancedb")
        backend = FakeEmbeddingBackend()
        settings = Settings()

        with patch(
            "quarry.ingestion.streaming.get_embedding_backend", lambda _s: backend
        ):
            for _ in range(_REINGEST_PASSES):
                _ingest_capture_once(db, settings)
                db.optimizer.optimize()

        assert len(backend.embedded) == 3 * _REINGEST_PASSES
        # Identical embed count to the no-optimize test above: optimize() adds
        # zero additional embed calls, so it is ruled out as a cause of (a)'s
        # re-embedding. (It is NOT ruled out as a contributor to disk growth --
        # see TestVersionChurnGrowsWithRepeatedReingest, which measures that
        # optimize() does not even reclaim the churn the re-ingest produces.)

    def test_c_the_onnx_embedding_session_only_does_what_it_is_asked(
        self, tmp_path: Path
    ) -> None:
        """(c) RULED OUT as an independent duplication source.

        The fake embedding backend (standing in 1:1 for
        ``quarry.embeddings.OnnxEmbeddingBackend`` per
        ``DocumentStreamer.stream_batches``, ``streaming.py:87-91``) records
        every text passed to ``embed_texts``. The duplication count observed
        above exactly equals (chunks per pass) * (number of
        ``ingest_content`` calls) -- the embedding backend embeds precisely
        what it is handed, once per call, with no internal retry or
        amplification. The session itself does not re-request embeddings; the
        CALLER (the ingest path) is what re-requests them on every pass.
        """
        db = Database.connect(tmp_path / "lancedb")
        backend = FakeEmbeddingBackend()
        settings = Settings()

        with patch(
            "quarry.ingestion.streaming.get_embedding_backend", lambda _s: backend
        ):
            _ingest_capture_once(db, settings)

        # One ingest call -> exactly one batch of embed_texts, chunks_per_pass
        # texts, no amplification inside the backend itself.
        assert len(backend.embedded) == 3


class TestVersionChurnGrowsWithRepeatedReingest:
    """Deliverable (operator extension, bead quarry-b9c4 / GitHub #622):
    does the capture re-ingest defect drive #622's disk-growth report?

    PROVEN: re-ingesting byte-IDENTICAL content N times grows the LanceDB
    table's commit-version count, on-disk data-fragment count, and total byte
    size LINEARLY with N -- not because of real data growth (the document
    never changes), but because ``ChunkStoreFunnel._embed_and_store`` issues
    one ``delete()`` commit plus one ``add()`` commit per pass (DES-034's
    single-writer contract), and neither commit is ever superseded without an
    explicit ``optimize(cleanup_older_than=...)`` call with a short-enough
    window. #622 observed 10.8k table versions / 8.2k data files for only
    33.5k rows -- consistent with exactly this churn, independent of the
    oversized-binary-row / optimize-overflow issue b9c4 tracks separately.
    """

    def test_version_and_fragment_count_grow_linearly_with_repeated_reingest(
        self, tmp_path: Path
    ) -> None:
        lancedb_path = tmp_path / "lancedb"
        db = Database.connect(lancedb_path)
        backend = FakeEmbeddingBackend()
        settings = Settings()

        versions: list[int] = []
        fragments: list[int] = []
        with patch(
            "quarry.ingestion.streaming.get_embedding_backend", lambda _s: backend
        ):
            for _ in range(_REINGEST_PASSES):
                _ingest_capture_once(db, settings)
                raw_table = lancedb.connect(lancedb_path).open_table(TABLE_NAME)
                versions.append(raw_table.version)
                fragments.append(db.optimizer.count_fragments())

        # Each pass issues delete() then add() -- two LanceDB commits, so the
        # version count climbs by 2 per pass (measured). The first pass's
        # delete is a no-op against a not-yet-existing table (no commit), and
        # its add is folded into create_table's own commit -- so pass 1 lands
        # at version 2, and each subsequent pass's delete+add lands 2 versions
        # later.
        assert versions == sorted(versions), "version must never decrease"
        assert versions[-1] > versions[0], (
            f"table version did not grow across {_REINGEST_PASSES} re-ingests "
            f"of unchanged content: {versions}"
        )
        growth_per_pass = (versions[-1] - versions[0]) / (len(versions) - 1)
        assert growth_per_pass == pytest.approx(2.0, abs=0.01), (
            f"expected version growth of 2 per re-ingest pass (one delete commit "
            f"+ one add commit), got {growth_per_pass:.2f} across {versions}"
        )

        assert fragments[-1] > fragments[0], (
            f"fragment count did not grow across {_REINGEST_PASSES} re-ingests "
            f"of unchanged content: {fragments}"
        )
        assert fragments == list(range(1, _REINGEST_PASSES + 1)), (
            f"expected exactly one new fragment per re-ingest pass (the add() "
            f"half of delete-then-reindex), got {fragments}"
        )

    def test_disk_bytes_grow_linearly_even_though_the_document_never_changed(
        self, tmp_path: Path
    ) -> None:
        lancedb_path = tmp_path / "lancedb"
        db = Database.connect(lancedb_path)
        backend = FakeEmbeddingBackend()
        settings = Settings()

        sizes: list[int] = []
        with patch(
            "quarry.ingestion.streaming.get_embedding_backend", lambda _s: backend
        ):
            for _ in range(_REINGEST_PASSES):
                _ingest_capture_once(db, settings)
                sizes.append(
                    sum(
                        f.stat().st_size for f in lancedb_path.rglob("*") if f.is_file()
                    )
                )

        assert sizes == sorted(sizes), "on-disk size must never shrink mid-run"
        assert sizes[-1] > sizes[0] * 2, (
            f"on-disk byte size should grow substantially (roughly linearly) "
            f"across {_REINGEST_PASSES} re-ingests of the SAME unchanged "
            f"transcript, since each pass leaves its superseded fragment on "
            f"disk; got {sizes}"
        )

    def test_default_optimize_does_not_reclaim_the_reingest_churn(
        self, tmp_path: Path
    ) -> None:
        """Confirms b9c4's optimize-overflow is additive to this defect, not it.

        ``SyncFinalizer._optimize`` (``sync_finalize.py``) calls
        ``TableOptimizer.optimize()`` with its DEFAULT ``cleanup_older_than``
        (1 hour, ``db/optimizer.py: 108``). A churn produced and reclaimed
        within the same test run (seconds, not hours) is NOT pruned by that
        default -- so running the production finalize path after every pass
        does not mask the version/fragment growth proven above; the growth is
        intrinsic to the re-ingest, not merely unreclaimed because nobody
        called optimize.
        """
        lancedb_path = tmp_path / "lancedb"
        db = Database.connect(lancedb_path)
        backend = FakeEmbeddingBackend()
        settings = Settings()

        fragments: list[int] = []
        with patch(
            "quarry.ingestion.streaming.get_embedding_backend", lambda _s: backend
        ):
            for _ in range(_REINGEST_PASSES):
                _ingest_capture_once(db, settings)
                db.optimizer.optimize()  # production default: cleanup_older_than=1h
                fragments.append(db.optimizer.count_fragments())

        assert fragments[-1] > fragments[0], (
            f"optimize() with its production default window did not reclaim "
            f"fragments across {_REINGEST_PASSES} re-ingests of unchanged "
            f"content -- the churn survives the real post-sync finalize path: "
            f"{fragments}"
        )
