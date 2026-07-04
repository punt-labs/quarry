from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from quarry.config import Settings
from quarry.db import Database
from quarry.db.storage import get_db
from quarry.types import LanceDB

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Environment variables that .envrc / shell may set and that pydantic-settings
# would otherwise silently inject into Settings instances created in tests.
_QUARRY_ENV_VARS = (
    "EMBEDDING_DIMENSION",
    "EMBEDDING_MODEL",
    "LANCEDB_PATH",
    "LOG_PATH",
    "QUARRY_API_KEY",
    "QUARRY_PROVIDER",
    "QUARRY_ROOT",
    "REGISTRY_PATH",
)


@pytest.fixture(autouse=True)
def _no_remote_config() -> Generator[None]:
    """Prevent tests from reading the real mcp-proxy config on disk.

    Existing CLI tests assume local DB access. This fixture returns an empty
    config so ``read_proxy_config()`` never triggers remote routing unless a
    test explicitly patches it.
    """
    with patch("quarry.__main__.read_proxy_config", return_value={}):
        yield


@pytest.fixture(autouse=True)
def _isolate_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip quarry-related env vars so tests get deterministic Settings defaults.

    Without this, .envrc exports leak into every Settings() call, causing
    spurious failures when the test assumes the code-level default.
    """
    for var in _QUARRY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _force_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force quarry's rich consoles to emit plain, ANSI-free CLI output.

    ``CliRunner`` captures output through a pipe, so rich would normally
    disable color on its own. But each ``Console(stderr=True)`` in
    ``src/quarry`` is built with ``force_terminal=None`` and holds a live
    ``os.environ`` reference: rich re-reads ``FORCE_COLOR`` at *render* time,
    not at construction. So a color-forcing ambient env makes these consoles
    emit ANSI escapes into the captured output at print time, breaking the
    plain substring assertions. Deleting the env var at runtime does not help
    — rich re-reads it on the next render. Replacing each console with one
    pinned to ``force_terminal=False, no_color=True`` defeats that render-time
    re-read, making the gate color-deterministic regardless of the shell.

    Every production ``Console(`` in ``src/quarry`` must be patched here:
    ``__main__.err_console`` (local CLI errors) and
    ``remote_client._err_console`` (remote-path errors). Add any new one.
    Typer is not patched: every ``typer.Typer`` app sets
    ``rich_markup_mode=None``, so typer renders errors/help through click's
    plain formatter, never through ``rich_utils`` — it emits no color.
    """
    for target in ("quarry.__main__.err_console", "quarry.remote_client._err_console"):
        monkeypatch.setattr(
            target, Console(stderr=True, no_color=True, force_terminal=False)
        )


@pytest.fixture(scope="session")
def embedding_model_name() -> str:
    return "Snowflake/snowflake-arctic-embed-m-v1.5"


@pytest.fixture(scope="session")
def _warm_embedding_model(embedding_model_name: str) -> None:
    """Load ONNX embedding model once per session."""
    from quarry.ingestion.backends import get_embedding_backend

    settings = Settings(embedding_model=embedding_model_name)
    get_embedding_backend(settings).embed_texts(["warm up"])


@pytest.fixture()
def integration_settings(
    embedding_model_name: str, _warm_embedding_model: None
) -> Settings:
    """Settings with real embedding model."""
    return Settings(embedding_model=embedding_model_name)


@pytest.fixture()
def lance_db(tmp_path: Path) -> LanceDB:
    return get_db(tmp_path / "db")


@pytest.fixture()
def database(lance_db: LanceDB) -> Database:
    """Database facade wrapping the test ``lance_db`` connection."""
    return Database(lance_db)


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
