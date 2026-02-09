from __future__ import annotations

from pathlib import Path

import pytest

from quarry.config import Settings
from quarry.database import get_db
from quarry.types import LanceDB

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def embedding_model_name() -> str:
    return "Snowflake/snowflake-arctic-embed-m-v1.5"


@pytest.fixture(scope="session")
def _warm_embedding_model(embedding_model_name: str) -> None:
    """Load embedding model once per session."""
    from quarry.embeddings import embed_texts

    embed_texts(["warm up"], model_name=embedding_model_name)


@pytest.fixture()
def integration_settings(
    embedding_model_name: str, _warm_embedding_model: None
) -> Settings:
    """Settings with real embedding model, dummy AWS creds."""
    return Settings(
        aws_access_key_id="test-not-used",
        aws_secret_access_key="test-not-used",
        embedding_model=embedding_model_name,
        textract_poll_initial=0,
    )


@pytest.fixture()
def aws_settings(embedding_model_name: str, _warm_embedding_model: None) -> Settings:
    """Settings with real AWS creds. Skip if no credentials available."""
    import botocore.session

    session = botocore.session.get_session()
    creds = session.get_credentials()
    if creds is None:
        pytest.skip("No AWS credentials available")
    resolved = creds.get_frozen_credentials()
    if not resolved.access_key or not resolved.secret_key:
        pytest.skip("AWS credentials incomplete")
    return Settings(
        aws_access_key_id=resolved.access_key,
        aws_secret_access_key=resolved.secret_key,
        embedding_model=embedding_model_name,
        textract_poll_initial=1,
    )


@pytest.fixture()
def lance_db(tmp_path: Path) -> LanceDB:
    return get_db(tmp_path / "db")


@pytest.fixture(scope="session")
def pdf_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate 2-page PDF with embedded text via PyMuPDF."""
    import fitz

    tmp = tmp_path_factory.mktemp("fixtures")
    path = tmp / "test-document.pdf"

    doc = fitz.open()

    page1 = doc.new_page(width=612, height=792)
    page1.insert_text(
        (72, 72),
        (
            "Software Engineering Principles\n\n"
            "Software engineering applies systematic, disciplined approaches\n"
            "to the development, operation, and maintenance of software.\n"
            "Key practices include version control, automated testing,\n"
            "continuous integration, and code review."
        ),
        fontsize=12,
    )

    page2 = doc.new_page(width=612, height=792)
    page2.insert_text(
        (72, 72),
        (
            "Marine Biology Overview\n\n"
            "Marine biology studies organisms in the ocean and other\n"
            "saltwater environments. Coral reefs support approximately\n"
            "25 percent of all marine species despite covering less than\n"
            "one percent of the ocean floor."
        ),
        fontsize=12,
    )

    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture(scope="session")
def docx_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate DOCX with heading styles via python-docx."""
    from docx import Document

    tmp = tmp_path_factory.mktemp("fixtures")
    path = tmp / "test-document.docx"

    doc = Document()
    doc.add_heading("Distributed Systems", level=1)
    doc.add_paragraph(
        "Distributed systems coordinate multiple computers to achieve"
        " a common goal. Key challenges include consensus, fault tolerance,"
        " and network partitioning. The CAP theorem states that a distributed"
        " system cannot simultaneously guarantee consistency, availability,"
        " and partition tolerance."
    )

    doc.add_heading("Operating Systems", level=1)
    doc.add_paragraph(
        "Operating systems manage hardware resources and provide services"
        " for application software. Process scheduling, memory management,"
        " and file systems are core responsibilities. Modern operating"
        " systems use virtual memory to give each process an isolated"
        " address space."
    )

    doc.save(str(path))
    return path


@pytest.fixture(scope="session")
def png_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate PNG with text via PIL."""
    from PIL import Image, ImageDraw

    tmp = tmp_path_factory.mktemp("fixtures")
    path = tmp / "test-image.png"

    img = Image.new("RGB", (400, 100), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 40), "Hello OCR", fill="black")
    img.save(str(path))
    return path
