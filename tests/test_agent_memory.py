"""Tests for agent memory: schema, FTS, hybrid search, RRF, decay, ethos."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

from quarry.database import (
    _RRF_K,
    TABLE_NAME,
    _ensure_fts_index,
    _migrate_schema,
    _schema,
    _temporal_weight,
    ensure_schema,
    get_db,
    hybrid_search,
    insert_chunks,
    search,
)
from quarry.models import Chunk


def _make_chunk(
    text: str = "test chunk text",
    document_name: str = "test.pdf",
    chunk_index: int = 0,
    collection: str = "default",
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> Chunk:
    return Chunk(
        document_name=document_name,
        document_path=".tmp/test.pdf",
        collection=collection,
        page_number=1,
        total_pages=1,
        chunk_index=chunk_index,
        text=text,
        page_raw_text=f"raw text for {text}",
        page_type="text",
        source_format=".pdf",
        ingestion_timestamp=datetime.now(tz=UTC),
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


def _random_vectors(n: int, dim: int = 768) -> NDArray[np.float32]:
    rng = np.random.default_rng(42)
    vecs = rng.standard_normal((n, dim)).astype(np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / norms  # type: ignore[no-any-return]


def _create_legacy_table(db_path: Path) -> None:
    """Create a table with the old schema (no agent memory columns)."""
    import lancedb
    import pyarrow as pa

    legacy_schema = pa.schema(
        [
            pa.field("text", pa.utf8()),
            pa.field("vector", pa.list_(pa.float32(), 768)),
            pa.field("document_name", pa.utf8()),
            pa.field("document_path", pa.utf8()),
            pa.field("collection", pa.utf8()),
            pa.field("page_number", pa.int32()),
            pa.field("total_pages", pa.int32()),
            pa.field("chunk_index", pa.int32()),
            pa.field("page_raw_text", pa.utf8()),
            pa.field("page_type", pa.utf8()),
            pa.field("source_format", pa.utf8()),
            pa.field("ingestion_timestamp", pa.timestamp("us", tz="UTC")),
        ]
    )
    db = lancedb.connect(str(db_path))
    vec = _random_vectors(1)[0].tolist()
    record = {
        "text": "legacy data",
        "vector": vec,
        "document_name": "old.pdf",
        "document_path": ".tmp/old.pdf",
        "collection": "default",
        "page_number": 1,
        "total_pages": 1,
        "chunk_index": 0,
        "page_raw_text": "legacy raw",
        "page_type": "text",
        "source_format": ".pdf",
        "ingestion_timestamp": datetime.now(tz=UTC),
    }
    db.create_table(TABLE_NAME, data=[record], schema=legacy_schema)


class TestSchemaMigration:
    def test_migration_adds_columns(self, tmp_path: Path) -> None:
        """Migration adds agent_handle, memory_type, summary to legacy table."""
        db_path = tmp_path / "db"
        db_path.mkdir()
        _create_legacy_table(db_path)

        db = get_db(db_path)
        table = db.open_table(TABLE_NAME)

        # Verify columns are missing before migration
        field_names = {f.name for f in table.schema}
        assert "agent_handle" not in field_names
        assert "memory_type" not in field_names
        assert "summary" not in field_names

        _migrate_schema(table)

        # Verify columns exist after migration
        field_names = {f.name for f in table.schema}
        assert "agent_handle" in field_names
        assert "memory_type" in field_names
        assert "summary" in field_names

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """Running migration twice does not raise or duplicate columns."""
        db_path = tmp_path / "db"
        db_path.mkdir()
        _create_legacy_table(db_path)

        db = get_db(db_path)
        table = db.open_table(TABLE_NAME)

        _migrate_schema(table)
        _migrate_schema(table)  # second call should be a no-op

        field_names = [f.name for f in table.schema]
        assert field_names.count("agent_handle") == 1
        assert field_names.count("memory_type") == 1
        assert field_names.count("summary") == 1

    def test_migration_defaults_to_empty_strings(self, tmp_path: Path) -> None:
        """Migrated columns default to empty strings for existing rows."""
        db_path = tmp_path / "db"
        db_path.mkdir()
        _create_legacy_table(db_path)

        db = get_db(db_path)
        table = db.open_table(TABLE_NAME)
        _migrate_schema(table)

        rows = table.search().limit(10).to_list()
        assert len(rows) == 1
        assert rows[0]["agent_handle"] == ""
        assert rows[0]["memory_type"] == ""
        assert rows[0]["summary"] == ""

    def test_ensure_schema_public_api(self, tmp_path: Path) -> None:
        """ensure_schema() migrates and creates FTS index on existing table."""
        db_path = tmp_path / "db"
        db_path.mkdir()
        _create_legacy_table(db_path)

        db = get_db(db_path)
        ensure_schema(db)

        table = db.open_table(TABLE_NAME)
        field_names = {f.name for f in table.schema}
        assert "agent_handle" in field_names

    def test_ensure_schema_noop_on_empty_db(self, tmp_path: Path) -> None:
        """ensure_schema() is a no-op when the table does not exist."""
        db = get_db(tmp_path / "db")
        ensure_schema(db)  # should not raise


class TestFTSIndex:
    def test_fts_index_creation(self, tmp_path: Path) -> None:
        """FTS index can be created on a table with data."""
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(text="LanceDB vector database for search")]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        # insert_chunks triggers _get_or_create_table which creates FTS index
        # Verify by doing an FTS search
        table = db.open_table(TABLE_NAME)
        results = table.search("LanceDB", query_type="fts").limit(5).to_list()
        assert len(results) >= 1
        assert "LanceDB" in str(results[0]["text"])

    def test_fts_search_returns_keyword_matches(self, tmp_path: Path) -> None:
        """FTS search finds exact keyword matches that vector search might miss."""
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(
                text="The API rate limit is 100 requests per second",
                chunk_index=0,
            ),
            _make_chunk(
                text="Machine learning models need training data",
                chunk_index=1,
            ),
            _make_chunk(
                text="API authentication uses OAuth2 tokens",
                chunk_index=2,
            ),
        ]
        vectors = _random_vectors(3)
        insert_chunks(db, chunks, vectors)

        table = db.open_table(TABLE_NAME)
        results = table.search("API", query_type="fts").limit(10).to_list()

        texts = [str(r["text"]) for r in results]
        assert any("API rate limit" in t for t in texts)
        assert any("API authentication" in t for t in texts)

    def test_fts_index_replace_is_idempotent(self, tmp_path: Path) -> None:
        """Creating FTS index twice with replace=True does not raise."""
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(text="test data")]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        table = db.open_table(TABLE_NAME)
        _ensure_fts_index(table)  # already created during insert
        _ensure_fts_index(table)  # should not raise


class TestNewColumnsInPipeline:
    def test_chunk_defaults_to_empty_strings(self) -> None:
        """Chunk dataclass defaults new fields to empty strings."""
        chunk = _make_chunk()
        assert chunk.agent_handle == ""
        assert chunk.memory_type == ""
        assert chunk.summary == ""

    def test_chunk_accepts_new_fields(self) -> None:
        """Chunk dataclass accepts agent_handle, memory_type, summary."""
        chunk = _make_chunk(
            agent_handle="rmh",
            memory_type="fact",
            summary="Test summary",
        )
        assert chunk.agent_handle == "rmh"
        assert chunk.memory_type == "fact"
        assert chunk.summary == "Test summary"

    def test_insert_stores_new_fields(self, tmp_path: Path) -> None:
        """New fields are stored and retrievable from LanceDB."""
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(
                text="The API rate limit is 100 req/s",
                agent_handle="rmh",
                memory_type="fact",
                summary="API rate limit documentation",
            )
        ]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        results = search(db, vectors[0], limit=1)
        assert len(results) == 1
        assert results[0]["agent_handle"] == "rmh"
        assert results[0]["memory_type"] == "fact"
        assert results[0]["summary"] == "API rate limit documentation"

    def test_insert_without_new_fields(self, tmp_path: Path) -> None:
        """Chunks without new fields store empty strings (backwards compatible)."""
        db = get_db(tmp_path / "db")
        chunks = [_make_chunk(text="plain document chunk")]
        vectors = _random_vectors(1)
        insert_chunks(db, chunks, vectors)

        results = search(db, vectors[0], limit=1)
        assert len(results) == 1
        assert results[0]["agent_handle"] == ""
        assert results[0]["memory_type"] == ""
        assert results[0]["summary"] == ""

    def test_schema_includes_new_columns(self) -> None:
        """The canonical schema includes agent_handle, memory_type, summary."""
        schema = _schema()
        field_names = {f.name for f in schema}
        assert "agent_handle" in field_names
        assert "memory_type" in field_names
        assert "summary" in field_names


class TestChunkerThreading:
    def test_chunk_pages_passes_new_fields(self) -> None:
        """chunk_pages threads agent_handle, memory_type, summary to Chunks."""
        from quarry.chunker import chunk_pages
        from quarry.models import PageContent, PageType

        pages = [
            PageContent(
                document_name="test.md",
                document_path=".tmp/test.md",
                page_number=1,
                total_pages=1,
                text="Some test content for chunking.",
                page_type=PageType.TEXT,
            )
        ]
        chunks = chunk_pages(
            pages,
            agent_handle="kpz",
            memory_type="observation",
            summary="Test observation",
        )
        assert len(chunks) >= 1
        assert chunks[0].agent_handle == "kpz"
        assert chunks[0].memory_type == "observation"
        assert chunks[0].summary == "Test observation"

    def test_chunk_pages_defaults_new_fields(self) -> None:
        """chunk_pages defaults new fields to empty strings."""
        from quarry.chunker import chunk_pages
        from quarry.models import PageContent, PageType

        pages = [
            PageContent(
                document_name="test.md",
                document_path=".tmp/test.md",
                page_number=1,
                total_pages=1,
                text="Some test content.",
                page_type=PageType.TEXT,
            )
        ]
        chunks = chunk_pages(pages)
        assert len(chunks) >= 1
        assert chunks[0].agent_handle == ""
        assert chunks[0].memory_type == ""
        assert chunks[0].summary == ""


class TestHybridSearch:
    def test_hybrid_returns_results(self, tmp_path: Path) -> None:
        """Hybrid search returns results combining vector and FTS."""
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(
                text="LanceDB is a vector database",
                chunk_index=0,
            ),
            _make_chunk(
                text="PostgreSQL is a relational database",
                chunk_index=1,
            ),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        results = hybrid_search(db, "LanceDB vector", vectors[0], limit=5)
        assert len(results) >= 1
        # The LanceDB chunk should rank highly due to both
        # vector similarity and keyword match
        texts = [str(r["text"]) for r in results]
        assert any("LanceDB" in t for t in texts)

    def test_hybrid_empty_table(self, tmp_path: Path) -> None:
        """Hybrid search returns empty list when table doesn't exist."""
        db = get_db(tmp_path / "db")
        vec = _random_vectors(1)[0]
        results = hybrid_search(db, "test", vec)
        assert results == []

    def test_hybrid_fts_boosts_keyword_matches(self, tmp_path: Path) -> None:
        """FTS channel boosts results with exact keyword matches."""
        db = get_db(tmp_path / "db")
        # Use very different vectors so vector channel alone
        # wouldn't rank the keyword match first
        rng = np.random.default_rng(99)
        vecs = rng.standard_normal((3, 768)).astype(np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        vecs = vecs / norms

        chunks = [
            _make_chunk(
                text="The authentication service uses OAuth2",
                chunk_index=0,
            ),
            _make_chunk(
                text="Memory management in operating systems",
                chunk_index=1,
            ),
            _make_chunk(
                text="OAuth2 token refresh flow documentation",
                chunk_index=2,
            ),
        ]
        insert_chunks(db, chunks, vecs)

        # Query with "OAuth2" — FTS should boost chunks 0 and 2
        results = hybrid_search(db, "OAuth2", vecs[1], limit=10)
        texts = [str(r["text"]) for r in results]
        assert any("OAuth2" in t for t in texts)

    def test_hybrid_with_agent_handle_filter(self, tmp_path: Path) -> None:
        """Hybrid search filters by agent_handle."""
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(
                text="API rate limit is 100 req/s",
                chunk_index=0,
                agent_handle="rmh",
            ),
            _make_chunk(
                text="API rate limit is 200 req/s",
                chunk_index=1,
                agent_handle="kpz",
            ),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        results = hybrid_search(
            db,
            "API rate limit",
            vectors[0],
            agent_handle_filter="rmh",
        )
        assert len(results) >= 1
        assert all(str(r["agent_handle"]) == "rmh" for r in results)

    def test_hybrid_with_memory_type_filter(self, tmp_path: Path) -> None:
        """Hybrid search filters by memory_type."""
        db = get_db(tmp_path / "db")
        chunks = [
            _make_chunk(
                text="Always run migrations before deploy",
                chunk_index=0,
                memory_type="procedure",
            ),
            _make_chunk(
                text="The deploy process is reliable",
                chunk_index=1,
                memory_type="opinion",
            ),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        results = hybrid_search(
            db,
            "deploy",
            vectors[0],
            memory_type_filter="procedure",
        )
        assert len(results) >= 1
        assert all(str(r["memory_type"]) == "procedure" for r in results)


class TestRRFFusion:
    def test_rrf_score_calculation(self) -> None:
        """RRF score for rank 0 is 1/(K+0) = 1/60."""
        expected = 1.0 / (_RRF_K + 0)
        assert abs(expected - 1.0 / 60) < 1e-9

    def test_rrf_dual_channel_boost(self, tmp_path: Path) -> None:
        """A result appearing in both channels scores higher than one."""
        db = get_db(tmp_path / "db")
        # Insert one chunk that should match both vector and FTS
        chunks = [
            _make_chunk(
                text="LanceDB vector database search",
                chunk_index=0,
            ),
            _make_chunk(
                text="Unrelated content about cooking recipes",
                chunk_index=1,
            ),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        # Use the first chunk's vector as query — it should match
        # on vector AND on FTS for "LanceDB"
        results = hybrid_search(db, "LanceDB", vectors[0], limit=2)
        assert len(results) >= 1
        # The dual-channel match should be ranked first
        assert "LanceDB" in str(results[0]["text"])


class TestTemporalDecay:
    def test_no_decay_returns_one(self) -> None:
        """With decay_rate=0, temporal weight is always 1.0."""
        ts = datetime.now(tz=UTC)
        assert _temporal_weight(ts, ts.timestamp(), 0.0) == 1.0

    def test_recent_memory_weighted_higher(self) -> None:
        """Recent timestamps get higher weight than old ones."""
        now = datetime.now(tz=UTC)
        recent = now - timedelta(hours=1)
        old = now - timedelta(hours=100)

        w_recent = _temporal_weight(recent, now.timestamp(), 0.01)
        w_old = _temporal_weight(old, now.timestamp(), 0.01)
        assert w_recent > w_old

    def test_decay_with_string_timestamp(self) -> None:
        """Temporal weight works with ISO format string timestamps."""
        now = datetime.now(tz=UTC)
        ts_str = (now - timedelta(hours=24)).isoformat()
        weight = _temporal_weight(ts_str, now.timestamp(), 0.01)
        # exp(-0.01 * 24) ≈ 0.787
        assert 0.75 < weight < 0.80

    def test_hybrid_search_with_decay(self, tmp_path: Path) -> None:
        """Hybrid search applies temporal decay when decay_rate > 0."""
        db = get_db(tmp_path / "db")
        now = datetime.now(tz=UTC)
        old_ts = now - timedelta(days=30)

        chunks = [
            Chunk(
                document_name="test.pdf",
                document_path=".tmp/test.pdf",
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="API rate limit documentation",
                page_raw_text="API rate limit documentation",
                page_type="text",
                source_format=".pdf",
                ingestion_timestamp=old_ts,
                agent_handle="rmh",
                memory_type="fact",
            ),
            Chunk(
                document_name="test.pdf",
                document_path=".tmp/test.pdf",
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=1,
                text="API rate limit updated info",
                page_raw_text="API rate limit updated info",
                page_type="text",
                source_format=".pdf",
                ingestion_timestamp=now,
                agent_handle="rmh",
                memory_type="fact",
            ),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        # With high decay, the recent chunk should rank first
        results = hybrid_search(
            db,
            "API rate limit",
            vectors[0],
            limit=2,
            decay_rate=0.05,
        )
        assert len(results) == 2
        # Recent chunk (index 1) should outrank old chunk (index 0)
        assert int(str(results[0]["chunk_index"])) == 1

    def test_decay_exempts_empty_memory_type(self, tmp_path: Path) -> None:
        """Rows with empty memory_type (documents, expertise) are exempt from decay."""
        db = get_db(tmp_path / "db")
        now = datetime.now(tz=UTC)
        old_ts = now - timedelta(days=30)

        # Two chunks with empty agent_handle — decay should NOT apply
        chunks = [
            Chunk(
                document_name="doc.txt",
                document_path=".tmp/doc.txt",
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="unscoped old content",
                page_raw_text="unscoped old content",
                page_type="text",
                source_format=".txt",
                ingestion_timestamp=old_ts,
                agent_handle="",
                memory_type="",
            ),
            Chunk(
                document_name="doc.txt",
                document_path=".tmp/doc.txt",
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=1,
                text="unscoped new content",
                page_raw_text="unscoped new content",
                page_type="text",
                source_format=".txt",
                ingestion_timestamp=now,
                agent_handle="",
                memory_type="",
            ),
        ]
        vectors = _random_vectors(2)
        insert_chunks(db, chunks, vectors)

        results = hybrid_search(
            db,
            "unscoped content",
            vectors[0],
            limit=2,
            decay_rate=0.05,
        )
        assert len(results) == 2
        # With no decay applied, vector similarity determines order:
        # vectors[0] is the query, so chunk_index=0 should rank first
        assert int(str(results[0]["chunk_index"])) == 0


# ── Ethos identity tagging tests ──────────────────────────────────


def _make_ethos_config(project_dir: Path, agent: str) -> None:
    """Create a .punt-labs/ethos/config.yaml with given agent handle."""
    ethos_dir = project_dir / ".punt-labs" / "ethos"
    ethos_dir.mkdir(parents=True, exist_ok=True)
    (ethos_dir / "config.yaml").write_text(f"agent: {agent}\n")


def _make_transcript(tmp_path: Path, text: str = "test message") -> Path:
    """Create a minimal JSONL transcript file."""
    transcript = tmp_path / "transcript.jsonl"
    record = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
    transcript.write_text(json.dumps(record) + "\n")
    return transcript


def _mock_settings() -> object:
    """Create a mock Settings with fake lancedb_path."""
    from unittest.mock import MagicMock

    settings = MagicMock()
    settings.lancedb_path = Path("/fake/lancedb")
    settings.registry_path = Path("/fake/registry.db")
    return settings


class TestReadEthosAgentHandle:
    def test_reads_agent_from_config(self, tmp_path: Path) -> None:
        """Reads agent handle from .punt-labs/ethos/config.yaml."""
        from quarry.hooks import _read_ethos_agent_handle

        _make_ethos_config(tmp_path, "claude")
        assert _read_ethos_agent_handle(str(tmp_path)) == "claude"

    def test_returns_empty_when_no_config(self, tmp_path: Path) -> None:
        """Returns empty string when no ethos config exists."""
        from quarry.hooks import _read_ethos_agent_handle

        assert _read_ethos_agent_handle(str(tmp_path)) == ""

    def test_walks_up_to_find_config(self, tmp_path: Path) -> None:
        """Walks up directory tree to find ethos config."""
        from quarry.hooks import _read_ethos_agent_handle

        _make_ethos_config(tmp_path, "rmh")
        subdir = tmp_path / "src" / "quarry"
        subdir.mkdir(parents=True)
        assert _read_ethos_agent_handle(str(subdir)) == "rmh"

    def test_handles_malformed_yaml(self, tmp_path: Path) -> None:
        """Returns empty string for malformed YAML."""
        from quarry.hooks import _read_ethos_agent_handle

        ethos_dir = tmp_path / ".punt-labs" / "ethos"
        ethos_dir.mkdir(parents=True)
        (ethos_dir / "config.yaml").write_text(": invalid: yaml: [")
        assert _read_ethos_agent_handle(str(tmp_path)) == ""

    def test_handles_missing_agent_field(self, tmp_path: Path) -> None:
        """Returns empty string when config has no agent field."""
        from quarry.hooks import _read_ethos_agent_handle

        ethos_dir = tmp_path / ".punt-labs" / "ethos"
        ethos_dir.mkdir(parents=True)
        (ethos_dir / "config.yaml").write_text("some_other_key: value\n")
        assert _read_ethos_agent_handle(str(tmp_path)) == ""


class TestPreCompactEthosTagging:
    def test_passes_agent_handle_to_popen(self, tmp_path: Path) -> None:
        """PreCompact passes ethos agent handle to background process."""
        from quarry.hooks import handle_pre_compact

        project = tmp_path / "myproject"
        project.mkdir()
        _make_ethos_config(project, "claude")
        transcript = _make_transcript(tmp_path)

        with (
            patch(
                "quarry.hooks.Path.home",
                return_value=tmp_path / "home",
            ),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "quarry.hooks._collection_for_cwd",
                return_value="myproject",
            ),
            patch("quarry.hooks.subprocess.Popen") as mock_popen,
        ):
            handle_pre_compact(
                {
                    "cwd": str(project),
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        args = mock_popen.call_args[0][0]
        # Last three args: agent_handle, memory_type, summary.
        assert args[-3] == "claude"
        assert args[-2] == ""
        assert args[-1] == ""

    def test_empty_handle_when_no_ethos(self, tmp_path: Path) -> None:
        """PreCompact passes empty agent_handle when no ethos config."""
        from quarry.hooks import handle_pre_compact

        transcript = _make_transcript(tmp_path)

        with (
            patch(
                "quarry.hooks.Path.home",
                return_value=tmp_path / "home",
            ),
            patch(
                "quarry.hooks._resolve_settings",
                return_value=_mock_settings(),
            ),
            patch(
                "quarry.hooks._collection_for_cwd",
                return_value=None,
            ),
            patch("quarry.hooks.subprocess.Popen") as mock_popen,
        ):
            handle_pre_compact(
                {
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                }
            )

        args = mock_popen.call_args[0][0]
        # Last three args: agent_handle, memory_type, summary — all empty.
        assert args[-3:] == ["", "", ""]


# ── New regression tests ─────────────────────────────────────────────


class TestIngestUrlThreadsAgentHandle:
    def test_ingest_url_passes_agent_handle(self) -> None:
        """ingest_url threads agent_handle/memory_type/summary to _chunk_embed_store."""
        from unittest.mock import MagicMock

        from quarry.pipeline import ingest_url

        html = "<html><body>hi</body></html>"
        result = {
            "document_name": "x",
            "collection": "c",
            "chunks": 0,
        }
        with (
            patch("quarry.pipeline._fetch_url", return_value=html),
            patch("quarry.pipeline.process_html_text", return_value=[]),
            patch("quarry.pipeline._chunk_embed_store") as mock_ces,
            patch("quarry.pipeline.delete_document"),
        ):
            mock_ces.return_value = result
            ingest_url(
                "https://example.com",
                MagicMock(),
                MagicMock(),
                agent_handle="rmh",
                memory_type="fact",
                summary="test summary",
            )
            _, kwargs = mock_ces.call_args
            assert kwargs["agent_handle"] == "rmh"
            assert kwargs["memory_type"] == "fact"
            assert kwargs["summary"] == "test summary"


class TestTemporalWeightEdgeCases:
    def test_empty_timestamp_returns_one(self) -> None:
        """_temporal_weight returns 1.0 for empty/unparseable timestamps."""
        from datetime import UTC, datetime

        now = datetime.now(tz=UTC).timestamp()
        assert _temporal_weight("", now, 0.01) == 1.0
        assert _temporal_weight(None, now, 0.01) == 1.0
        assert _temporal_weight("not-a-date", now, 0.01) == 1.0

    def test_naive_datetime_treated_as_utc(self) -> None:
        """_temporal_weight treats naive datetimes as UTC (no crash)."""
        from datetime import datetime, timedelta

        now_utc = datetime(2026, 1, 1, 12, 0, 0)
        naive_ts = now_utc - timedelta(hours=10)
        # Should not crash and should produce a valid weight < 1.0.
        from datetime import UTC

        now_aware = now_utc.replace(tzinfo=UTC)
        weight = _temporal_weight(naive_ts, now_aware.timestamp(), 0.01)
        assert 0 < weight < 1.0


class TestRowKeyMissingFields:
    def test_missing_fields_no_crash(self) -> None:
        """_row_key returns defaults when dict keys are missing."""
        from quarry.database import _row_key

        key = _row_key({})
        assert key == ("", 0, 0)

    def test_partial_fields(self) -> None:
        """_row_key handles partial dict without crashing."""
        from quarry.database import _row_key

        key = _row_key({"document_name": "doc.pdf"})
        assert key == ("doc.pdf", 0, 0)
