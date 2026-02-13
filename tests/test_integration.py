from __future__ import annotations

from pathlib import Path

import pytest

from quarry.backends import get_embedding_backend
from quarry.config import Settings
from quarry.database import (
    count_chunks,
    get_page_text,
    list_collections,
    list_documents,
    search,
)
from quarry.pipeline import ingest_document, ingest_text
from quarry.results import SearchResult
from quarry.types import LanceDB

from .conftest import FIXTURES_DIR

pytestmark = pytest.mark.slow


# ── helpers ──────────────────────────────────────────────────────────


def _search(
    db: LanceDB,
    query: str,
    settings: Settings,
    *,
    limit: int = 5,
    document_filter: str | None = None,
    collection_filter: str | None = None,
) -> list[SearchResult]:
    """Embed a query and search the database."""
    vector = get_embedding_backend(settings).embed_query(query)
    return search(
        db,
        vector,
        limit=limit,
        document_filter=document_filter,
        collection_filter=collection_filter,
    )


# ── text file tests ─────────────────────────────────────────────────


class TestTextFileIngestAndSearch:
    def test_ingest_txt_and_search(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        path = FIXTURES_DIR / "photosynthesis.txt"
        ingest_document(path, lance_db, integration_settings)

        results = _search(lance_db, "how plants convert sunlight", integration_settings)
        assert len(results) > 0
        top_text = str(results[0]["text"]).lower()
        assert "photosynthesis" in top_text or "chloroplast" in top_text

    def test_search_ranks_relevant_higher(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        for name in (
            "photosynthesis.txt",
            "french-revolution.txt",
            "quantum-computing.txt",
        ):
            ingest_document(FIXTURES_DIR / name, lance_db, integration_settings)

        results = _search(
            lance_db, "quantum superposition qubits", integration_settings
        )
        assert len(results) > 0
        assert str(results[0]["document_name"]) == "quantum-computing.txt"

    def test_ingest_reports_correct_metadata(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        path = FIXTURES_DIR / "photosynthesis.txt"
        result = ingest_document(path, lance_db, integration_settings)

        assert result["document_name"] == "photosynthesis.txt"
        assert int(str(result["sections"])) >= 3
        assert int(str(result["chunks"])) >= 3


# ── markdown tests ───────────────────────────────────────────────────


class TestMarkdownIngestAndSearch:
    def test_markdown_splits_on_headings(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        result = ingest_document(
            FIXTURES_DIR / "guide.md", lance_db, integration_settings
        )
        assert int(str(result["sections"])) == 3

        results = _search(lance_db, "SQL relational databases", integration_settings)
        assert len(results) > 0
        top_text = str(results[0]["text"]).lower()
        assert "database" in top_text

    def test_markdown_search_across_sections(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        ingest_document(FIXTURES_DIR / "guide.md", lance_db, integration_settings)

        results = _search(
            lance_db, "neural networks machine learning", integration_settings
        )
        assert len(results) > 0
        top_text = str(results[0]["text"]).lower()
        assert "learning" in top_text or "neural" in top_text


# ── LaTeX tests ──────────────────────────────────────────────────────


class TestLatexIngestion:
    def test_latex_splits_on_sections(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        result = ingest_document(
            FIXTURES_DIR / "calculus.tex", lance_db, integration_settings
        )
        assert int(str(result["sections"])) >= 2


# ── PDF tests ────────────────────────────────────────────────────────


class TestPdfIngestion:
    def test_pdf_text_extraction(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
        pdf_fixture: Path,
    ) -> None:
        result = ingest_document(pdf_fixture, lance_db, integration_settings)
        assert int(str(result["total_pages"])) == 2
        assert int(str(result["text_pages"])) == 2

        results = _search(
            lance_db, "software engineering testing", integration_settings
        )
        assert len(results) > 0
        top_text = str(results[0]["text"]).lower()
        assert "software" in top_text

    def test_pdf_page_retrieval(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
        pdf_fixture: Path,
    ) -> None:
        ingest_document(pdf_fixture, lance_db, integration_settings)

        raw = get_page_text(lance_db, pdf_fixture.name, 1)
        assert raw is not None
        assert "software" in raw.lower() or "engineering" in raw.lower()


# ── DOCX tests ───────────────────────────────────────────────────────


class TestDocxIngestion:
    def test_docx_splits_on_heading_styles(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
        docx_fixture: Path,
    ) -> None:
        result = ingest_document(docx_fixture, lance_db, integration_settings)
        assert int(str(result["sections"])) == 2

        results = _search(
            lance_db, "virtual memory process scheduling", integration_settings
        )
        assert len(results) > 0
        top_text = str(results[0]["text"]).lower()
        assert "operating" in top_text or "memory" in top_text


# ── image OCR tests ──────────────────────────────────────────────────


class TestImageOcr:
    def test_png_ocr_and_search(
        self,
        lance_db: LanceDB,
        aws_settings: Settings,
        png_fixture: Path,
    ) -> None:
        result = ingest_document(png_fixture, lance_db, aws_settings)
        assert int(str(result["chunks"])) >= 1

        results = _search(lance_db, "Hello OCR", aws_settings)
        assert len(results) > 0


# ── collection tests ─────────────────────────────────────────────────


class TestCollectionIsolation:
    def test_search_within_collection(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        ingest_document(
            FIXTURES_DIR / "photosynthesis.txt",
            lance_db,
            integration_settings,
            collection="biology",
        )
        ingest_document(
            FIXTURES_DIR / "french-revolution.txt",
            lance_db,
            integration_settings,
            collection="history",
        )

        results = _search(
            lance_db,
            "chloroplast photosynthesis",
            integration_settings,
            collection_filter="biology",
        )
        assert len(results) > 0
        for r in results:
            assert str(r["collection"]) == "biology"

    def test_list_documents_by_collection(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        ingest_document(
            FIXTURES_DIR / "photosynthesis.txt",
            lance_db,
            integration_settings,
            collection="biology",
        )
        ingest_document(
            FIXTURES_DIR / "french-revolution.txt",
            lance_db,
            integration_settings,
            collection="history",
        )

        docs = list_documents(lance_db, collection_filter="biology")
        assert len(docs) == 1
        assert str(docs[0]["document_name"]) == "photosynthesis.txt"

    def test_list_collections(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        ingest_document(
            FIXTURES_DIR / "photosynthesis.txt",
            lance_db,
            integration_settings,
            collection="biology",
        )
        ingest_document(
            FIXTURES_DIR / "french-revolution.txt",
            lance_db,
            integration_settings,
            collection="history",
        )

        collections = list_collections(lance_db)
        assert len(collections) == 2
        names = {str(c["collection"]) for c in collections}
        assert names == {"biology", "history"}


# ── overwrite tests ──────────────────────────────────────────────────


class TestOverwriteBehavior:
    def test_overwrite_replaces_content(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        ingest_text(
            "The mitochondria is the powerhouse of the cell.",
            "bio.txt",
            lance_db,
            integration_settings,
        )
        ingest_text(
            "Tectonic plates shift and cause earthquakes along fault lines.",
            "bio.txt",
            lance_db,
            integration_settings,
            overwrite=True,
        )

        old_results = _search(lance_db, "mitochondria powerhouse", integration_settings)
        new_results = _search(
            lance_db, "tectonic plates earthquakes", integration_settings
        )

        old_texts = [str(r["text"]).lower() for r in old_results]
        assert not any("mitochondria" in t for t in old_texts)

        assert len(new_results) > 0
        assert "tectonic" in str(new_results[0]["text"]).lower()

        docs = list_documents(lance_db)
        assert len(docs) == 1

    def test_no_overwrite_duplicates(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        ingest_text(
            "Helium is a noble gas with atomic number two.",
            "chem.txt",
            lance_db,
            integration_settings,
        )
        count_before = count_chunks(lance_db)

        ingest_text(
            "Helium is a noble gas with atomic number two.",
            "chem.txt",
            lance_db,
            integration_settings,
            overwrite=False,
        )
        count_after = count_chunks(lance_db)

        assert count_after == count_before * 2


# ── multi-document search tests ──────────────────────────────────────


class TestMultiDocumentSearch:
    def test_search_returns_correct_document(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        for name in (
            "photosynthesis.txt",
            "french-revolution.txt",
            "quantum-computing.txt",
        ):
            ingest_document(FIXTURES_DIR / name, lance_db, integration_settings)

        queries = {
            "chloroplast Calvin cycle": "photosynthesis.txt",
            "Bastille guillotine revolution": "french-revolution.txt",
            "qubits entanglement Shor algorithm": "quantum-computing.txt",
        }
        for query, expected_doc in queries.items():
            results = _search(lance_db, query, integration_settings)
            assert len(results) > 0, f"No results for: {query}"
            assert str(results[0]["document_name"]) == expected_doc, (
                f"Expected {expected_doc} for '{query}', "
                f"got {results[0]['document_name']}"
            )

    def test_document_filter(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        for name in (
            "photosynthesis.txt",
            "french-revolution.txt",
            "quantum-computing.txt",
        ):
            ingest_document(FIXTURES_DIR / name, lance_db, integration_settings)

        results = _search(
            lance_db,
            "science",
            integration_settings,
            document_filter="photosynthesis.txt",
        )
        for r in results:
            assert str(r["document_name"]) == "photosynthesis.txt"


# ── raw text ingestion tests ─────────────────────────────────────────


class TestRawTextIngestion:
    def test_ingest_text_and_search(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        content = (
            "Plate tectonics describes the large-scale motion of Earth's "
            "lithosphere. The lithosphere is divided into several tectonic "
            "plates that float on the semi-fluid asthenosphere beneath them."
        )
        result = ingest_text(content, "geology.txt", lance_db, integration_settings)
        assert int(str(result["chunks"])) >= 1

        results = _search(lance_db, "tectonic plates lithosphere", integration_settings)
        assert len(results) > 0
        assert "tectonic" in str(results[0]["text"]).lower()

    def test_ingest_text_with_collection(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
    ) -> None:
        ingest_text(
            "RNA polymerase transcribes DNA into messenger RNA.",
            "bio-notes.txt",
            lance_db,
            integration_settings,
            collection="notes",
        )

        collections = list_collections(lance_db)
        names = {str(c["collection"]) for c in collections}
        assert "notes" in names


# ── collection derivation tests ──────────────────────────────────────


class TestCollectionDerivation:
    def test_auto_derive_from_parent_dir(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
        tmp_path: Path,
    ) -> None:
        topic_dir = tmp_path / "ml-101"
        topic_dir.mkdir()
        notes = topic_dir / "notes.txt"
        notes.write_text(
            "Gradient descent minimizes a loss function by iteratively "
            "adjusting model parameters in the direction of steepest descent."
        )

        from quarry.collections import derive_collection

        collection = derive_collection(notes)
        ingest_document(notes, lance_db, integration_settings, collection=collection)

        docs = list_documents(lance_db)
        assert len(docs) == 1
        assert str(docs[0]["collection"]) == "ml-101"

    def test_explicit_override(
        self,
        lance_db: LanceDB,
        integration_settings: Settings,
        tmp_path: Path,
    ) -> None:
        topic_dir = tmp_path / "ml-101"
        topic_dir.mkdir()
        notes = topic_dir / "notes.txt"
        notes.write_text(
            "Newton's laws of motion describe the relationship between "
            "a body and the forces acting upon it."
        )

        from quarry.collections import derive_collection

        collection = derive_collection(notes, explicit="physics")
        ingest_document(notes, lance_db, integration_settings, collection=collection)

        docs = list_documents(lance_db)
        assert len(docs) == 1
        assert str(docs[0]["collection"]) == "physics"
