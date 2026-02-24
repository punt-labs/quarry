"""Document ingestion pipeline: dispatch by format, chunk, embed, store."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

from quarry.backends import get_embedding_backend, get_ocr_backend
from quarry.chunker import chunk_pages
from quarry.code_processor import SUPPORTED_CODE_EXTENSIONS, process_code_file
from quarry.config import Settings
from quarry.database import delete_document, insert_chunks, list_documents
from quarry.html_processor import (
    SUPPORTED_HTML_EXTENSIONS,
    process_html_file,
    process_html_text,
)
from quarry.image_analyzer import (
    SUPPORTED_IMAGE_EXTENSIONS,
    analyze_image,
)
from quarry.models import PageContent, PageType
from quarry.pdf_analyzer import analyze_pdf
from quarry.presentation_processor import (
    SUPPORTED_PRESENTATION_EXTENSIONS,
    process_presentation_file,
)
from quarry.results import IngestResult, SitemapResult
from quarry.spreadsheet_processor import (
    SUPPORTED_SPREADSHEET_EXTENSIONS,
    process_spreadsheet_file,
)
from quarry.text_extractor import extract_text_pages
from quarry.text_processor import (
    SUPPORTED_TEXT_EXTENSIONS,
    process_raw_text,
    process_text_file,
)
from quarry.types import LanceDB

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = (
    frozenset({".pdf"})
    | SUPPORTED_TEXT_EXTENSIONS
    | SUPPORTED_IMAGE_EXTENSIONS
    | SUPPORTED_CODE_EXTENSIONS
    | SUPPORTED_SPREADSHEET_EXTENSIONS
    | SUPPORTED_HTML_EXTENSIONS
    | SUPPORTED_PRESENTATION_EXTENSIONS
)


def ingest_document(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Ingest a document: dispatch to format-specific handler.

    Supported formats: PDF, TXT, MD, TEX, DOCX, HTML, PPTX, PNG, JPEG, TIFF,
    BMP, WebP, XLSX, CSV.

    Args:
        file_path: Path to the document.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name. Defaults to
            ``file_path.name``.  Pass a relative path (e.g. from
            ``file_path.relative_to(root)``) to avoid collisions when
            syncing directories with identically-named files in
            subdirectories.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.

    Raises:
        FileNotFoundError: If file does not exist.
        ValueError: If file format is not supported.
    """
    if not file_path.exists():
        msg = f"File not found: {file_path}"
        raise FileNotFoundError(msg)

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return ingest_pdf(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_CODE_EXTENSIONS:
        return ingest_code_file(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_TEXT_EXTENSIONS:
        return ingest_text_file(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return ingest_image(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_SPREADSHEET_EXTENSIONS:
        return ingest_spreadsheet(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_HTML_EXTENSIONS:
        return ingest_html_file(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    if suffix in SUPPORTED_PRESENTATION_EXTENSIONS:
        return ingest_presentation(
            file_path,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
        )

    msg = f"Unsupported file format: {suffix}"
    raise ValueError(msg)


def ingest_pdf(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Ingest a PDF document: analyze, extract/OCR, chunk, embed, store.

    Args:
        file_path: Path to the PDF file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results (pages, chunks, etc).
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("Analyzing: %s", document_name)

    if overwrite:
        delete_document(db, document_name, collection=collection)

    analyses = analyze_pdf(file_path)
    total_pages = len(analyses)

    text_pages = [a.page_number for a in analyses if a.page_type == PageType.TEXT]
    image_pages = [a.page_number for a in analyses if a.page_type == PageType.IMAGE]

    progress(
        "Pages: %d total, %d text, %d image",
        total_pages,
        len(text_pages),
        len(image_pages),
    )

    all_pages: list[PageContent] = []

    if text_pages:
        progress("Extracting text from %d pages", len(text_pages))
        extracted = extract_text_pages(
            file_path, text_pages, total_pages, document_name=document_name
        )
        all_pages.extend(extracted)

    if image_pages:
        progress("Running OCR on %d pages", len(image_pages))
        ocr = get_ocr_backend(settings)
        ocr_results = ocr.ocr_document(
            file_path, image_pages, total_pages, document_name=document_name
        )
        all_pages.extend(ocr_results)

    all_pages.sort(key=lambda p: p.page_number)

    return _chunk_embed_store(
        all_pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format=".pdf",
        total_pages=total_pages,
        text_pages=len(text_pages),
        image_pages=len(image_pages),
    )


def ingest_text_file(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Ingest a text document: read, split into sections, chunk, embed, store.

    Supported: .txt, .md, .tex, .docx.

    Args:
        file_path: Path to the text file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("Reading: %s", document_name)

    if overwrite:
        delete_document(db, document_name, collection=collection)

    pages = process_text_file(file_path)
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        sections=len(pages),
    )


def ingest_code_file(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Ingest source code: parse into definitions, chunk, embed, store.

    Uses tree-sitter for language-aware splitting when available.

    Args:
        file_path: Path to the source code file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("Parsing: %s", document_name)

    if overwrite:
        delete_document(db, document_name, collection=collection)

    pages = process_code_file(file_path)
    progress("Definitions: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        definitions=len(pages),
    )


def ingest_spreadsheet(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Ingest a spreadsheet: read sheets, serialize to LaTeX, chunk, embed, store.

    Supported: .xlsx, .csv.

    Args:
        file_path: Path to the spreadsheet file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("Reading: %s", document_name)

    if overwrite:
        delete_document(db, document_name, collection=collection)

    pages, sheet_count = process_spreadsheet_file(
        file_path,
        max_chars=settings.chunk_max_chars,
        document_name=document_name,
    )
    progress("Sheets: %d, sections: %d", sheet_count, len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        sheets=sheet_count,
    )


def ingest_html_file(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Ingest an HTML document: parse, clean, convert to Markdown, chunk, embed, store.

    Supported: .html, .htm.

    Args:
        file_path: Path to the HTML file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("Reading: %s", document_name)

    if overwrite:
        delete_document(db, document_name, collection=collection)

    pages = process_html_file(file_path)
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        sections=len(pages),
    )


def ingest_presentation(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Ingest a presentation: extract slides, chunk, embed, store.

    Supported: .pptx.

    Args:
        file_path: Path to the presentation file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("Reading: %s", document_name)

    if overwrite:
        delete_document(db, document_name, collection=collection)

    pages = process_presentation_file(file_path, document_name=document_name)
    progress("Slides: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        slides=len(pages),
    )


def ingest_image(
    file_path: Path,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Ingest a standalone image: OCR, chunk, embed, store.

    Supported: PNG, JPEG, TIFF (multi-page), BMP, WebP.
    BMP and WebP are converted to PNG before OCR (required by most engines).

    Single-page images use the OCR backend's sync API. Multi-page TIFFs use
    the async API (S3 for cloud backends, local for on-device backends).

    Args:
        file_path: Path to image file.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("Analyzing image: %s", document_name)

    if overwrite:
        delete_document(db, document_name, collection=collection)

    analysis = analyze_image(file_path)
    progress(
        "Image: %s, %d pages, conversion=%s",
        analysis.format,
        analysis.page_count,
        analysis.needs_conversion,
    )

    if analysis.page_count > 1:
        return _ingest_multipage_image(
            file_path,
            analysis.page_count,
            db,
            settings,
            progress,
            document_name=document_name,
            collection=collection,
        )

    image_bytes = _prepare_image_bytes(
        file_path,
        needs_conversion=analysis.needs_conversion,
        max_bytes=settings.textract_max_image_bytes,
    )
    ocr = get_ocr_backend(settings)
    page = ocr.ocr_image_bytes(
        image_bytes,
        document_name=document_name,
        document_path=file_path.resolve(),
    )

    return _chunk_embed_store(
        [page],
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        file_format=analysis.format,
        image_pages=1,
    )


def _prepare_image_bytes(
    image_path: Path,
    *,
    needs_conversion: bool,
    max_bytes: int = 0,
) -> bytes:
    """Read image bytes, converting format and/or downscaling as needed.

    Format conversion: MPO → JPEG, BMP/WebP → PNG, with EXIF transpose.
    Size reduction: when *max_bytes* > 0 and the encoded image exceeds it,
    lossless formats are re-encoded as JPEG (quality 95); if still too large,
    dimensions are halved up to 5 times.
    """
    if not needs_conversion and max_bytes <= 0:
        return image_path.read_bytes()

    if not needs_conversion:
        raw = image_path.read_bytes()
        if len(raw) <= max_bytes:
            return raw

    from PIL import Image, ImageOps  # noqa: PLC0415

    with Image.open(image_path) as im:
        img = ImageOps.exif_transpose(im)

        out_fmt: str
        save_kw: dict[str, int]
        if im.format == "MPO":
            out_fmt, save_kw = "JPEG", {"quality": 95}
        elif im.format in ("BMP", "WEBP"):
            out_fmt, save_kw = "PNG", {}
        else:
            out_fmt, save_kw = im.format or "PNG", {}

        return _encode_image_to_fit(img, out_fmt, save_kw, max_bytes, image_path.name)


def _encode_image_to_fit(
    img: PILImage,
    out_fmt: str,
    save_kw: dict[str, int],
    max_bytes: int,
    name: str,
) -> bytes:
    """Encode image, re-encoding as JPEG and/or downscaling if oversized.

    Strategy: save as-is; if over limit, re-encode as JPEG (much smaller for
    photos); then downscale by halves until under limit. Keeps quality while
    meeting OCR engine byte limits.
    """
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    buf = io.BytesIO()
    img.save(buf, format=out_fmt, **save_kw)
    data = buf.getvalue()

    if max_bytes <= 0 or len(data) <= max_bytes:
        return data

    # Re-encode as JPEG if not already (much smaller for photos)
    if out_fmt != "JPEG":
        out_fmt, save_kw = "JPEG", {"quality": 95}
        rgb = img.convert("RGB") if img.mode != "RGB" else img
        buf = io.BytesIO()
        rgb.save(buf, format=out_fmt, **save_kw)
        data = buf.getvalue()
        logger.info("Re-encoded %s as JPEG (%d bytes)", name, len(data))
        if len(data) <= max_bytes:
            return data
        img = rgb

    # Downscale until under limit
    current = img
    for _ in range(5):
        w, h = current.size
        new_w, new_h = max(1, w // 2), max(1, h // 2)
        if (new_w, new_h) == (w, h):
            break
        current = current.resize((new_w, new_h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        current.save(buf, format=out_fmt, **save_kw)
        data = buf.getvalue()
        logger.info(
            "Downscaled %s to %dx%d (%d bytes)",
            name,
            new_w,
            new_h,
            len(data),
        )
        if len(data) <= max_bytes:
            return data

    if max_bytes > 0 and len(data) > max_bytes:
        logger.warning(
            "%s still %d bytes after downscaling (limit %d)",
            name,
            len(data),
            max_bytes,
        )

    return data


def _ingest_multipage_image(
    file_path: Path,
    page_count: int,
    db: LanceDB,
    settings: Settings,
    progress: Callable[..., None],
    *,
    document_name: str | None = None,
    collection: str = "default",
) -> IngestResult:
    """Ingest a multi-page image (TIFF) via the OCR backend's async path."""
    document_name = document_name or file_path.name
    progress("Running OCR on %d pages (async)", page_count)
    all_page_numbers = list(range(1, page_count + 1))
    ocr = get_ocr_backend(settings)
    pages = ocr.ocr_document(
        file_path, all_page_numbers, page_count, document_name=document_name
    )

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        file_format="TIFF",
        image_pages=page_count,
    )


def ingest_content(
    content: str,
    document_name: str,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    format_hint: str = "auto",
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Ingest inline content: split into sections, chunk, embed, store.

    Args:
        content: The text content to ingest.
        document_name: Name for the document.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        format_hint: One of 'auto', 'plain', 'markdown', 'latex'.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)

    progress("Processing: %s", document_name)

    if overwrite:
        delete_document(db, document_name, collection=collection)

    pages = process_raw_text(content, document_name, format_hint=format_hint)
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format="inline",
        sections=len(pages),
    )


def _fetch_url(url: str, *, timeout: int = 30) -> str:
    """Fetch a URL and return the response body as text.

    Raises:
        ValueError: If the URL is not HTTP(S) or the response is not HTML.
        OSError: On network errors.
    """
    import urllib.request  # noqa: PLC0415
    from urllib.error import HTTPError, URLError  # noqa: PLC0415

    if not url.startswith(("http://", "https://")):
        msg = f"Only HTTP(S) URLs are supported: {url}"
        raise ValueError(msg)

    request = urllib.request.Request(  # noqa: S310
        url,
        headers={"User-Agent": "quarry/1.0 (+https://github.com/punt-labs/quarry)"},
    )
    _allowed_media_types = {"text/html", "application/xhtml+xml"}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            final_url: str = resp.geturl()
            if not final_url.startswith(("http://", "https://")):
                msg = f"Redirect left HTTP(S): {final_url}"
                raise ValueError(msg)
            content_type: str = resp.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type and media_type not in _allowed_media_types:
                msg = f"URL returned non-HTML content: {content_type}"
                raise ValueError(msg)
            charset = resp.headers.get_content_charset() or "utf-8"
            body: bytes = resp.read()
            return body.decode(charset, errors="replace")
    except HTTPError as exc:
        msg = f"HTTP {exc.code} fetching {url}"
        raise ValueError(msg) from exc
    except URLError as exc:
        msg = f"Cannot reach {url}: {exc.reason}"
        raise OSError(msg) from exc


def ingest_url(
    url: str,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    timeout: int = 30,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult:
    """Fetch a URL, extract text from HTML, chunk, embed, store.

    Args:
        url: HTTP(S) URL to fetch.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: If True, delete existing data for this URL first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name. Defaults to URL.
        timeout: HTTP request timeout in seconds.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.

    Raises:
        ValueError: If URL is invalid, unreachable, or returns non-HTML.
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or url

    progress("Fetching: %s", url)
    html = _fetch_url(url, timeout=timeout)
    progress("Fetched %d characters", len(html))

    if overwrite:
        delete_document(db, document_name, collection=collection)

    pages = process_html_text(html, document_name, url)
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        db,
        settings,
        progress,
        collection=collection,
        source_format=".html",
        sections=len(pages),
    )


def _ingest_url_with_delay(
    page_url: str,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool,
    collection: str,
    document_name: str | None,
    timeout: int,
    delay: float,
) -> IngestResult:
    """Ingest a single URL with a pre-fetch delay to avoid rate limiting.

    The delay includes random jitter (0-1.0s) to prevent synchronized
    bursts when multiple workers start at the same time.
    """
    import random  # noqa: PLC0415
    import time  # noqa: PLC0415

    jitter = random.uniform(0, 1.0)  # noqa: S311
    time.sleep(delay + jitter)

    return ingest_url(
        page_url,
        db,
        settings,
        overwrite=overwrite,
        collection=collection,
        document_name=document_name,
        timeout=timeout,
    )


def ingest_sitemap(
    url: str,
    db: LanceDB,
    settings: Settings,
    *,
    collection: str = "",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    limit: int = 0,
    overwrite: bool = False,
    workers: int = 4,
    delay: float = 0.5,
    timeout: int = 30,
    progress_callback: Callable[[str], None] | None = None,
) -> SitemapResult:
    """Crawl a sitemap and ingest all discovered URLs.

    Fetches the sitemap, discovers all URLs (following sitemap indexes),
    applies include/exclude filters, deduplicates against existing documents
    via <lastmod>, and ingests new/changed URLs in parallel.

    Args:
        url: Sitemap URL.
        db: LanceDB connection.
        settings: Application settings.
        collection: Collection name. Defaults to sitemap URL domain.
        include: URL path glob patterns to include (repeatable).
        exclude: URL path glob patterns to exclude (repeatable).
        limit: Max URLs to ingest (0 = no limit).
        overwrite: Force re-ingest regardless of <lastmod>.
        workers: Parallel fetch workers (minimum 1).
        delay: Base delay in seconds between fetches per worker
            (adds 0-1.0s random jitter). Default 0.5s.
        timeout: HTTP timeout in seconds.
        progress_callback: Optional callable for progress messages.

    Returns:
        SitemapResult with counts and error details.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415

    from quarry.sitemap import discover_urls, filter_entries  # noqa: PLC0415

    progress = _make_progress(progress_callback)
    workers = max(1, workers)

    if not collection:
        collection = urlparse(url).hostname or "default"

    progress("Discovering URLs from %s", url)
    entries = discover_urls(url, timeout=timeout)
    total_discovered = len(entries)
    progress("Discovered %d URLs", total_discovered)

    filtered = filter_entries(entries, include=include, exclude=exclude, limit=limit)
    after_filter = len(filtered)
    progress("After filtering: %d URLs", after_filter)

    # Build lookup of existing documents for lastmod dedup
    existing_docs = list_documents(db, collection_filter=collection)
    existing_timestamps: dict[str, str] = {
        doc["document_name"]: doc["ingestion_timestamp"] for doc in existing_docs
    }

    # Determine which URLs to ingest vs skip
    to_ingest: list[tuple[str, str | None]] = []
    skipped = 0

    for entry in filtered:
        existing_ts = existing_timestamps.get(entry.loc)
        if existing_ts and not overwrite and entry.lastmod is not None:
            from datetime import UTC, datetime  # noqa: PLC0415

            try:
                existing_dt = datetime.fromisoformat(
                    str(existing_ts).replace("Z", "+00:00")
                )
                if existing_dt.tzinfo is None:
                    existing_dt = existing_dt.replace(tzinfo=UTC)
                if entry.lastmod <= existing_dt:
                    skipped += 1
                    continue
            except (ValueError, TypeError):
                pass  # Can't parse — re-ingest to be safe
        to_ingest.append((entry.loc, None))

    progress("%d to ingest, %d up-to-date", len(to_ingest), skipped)

    ingested = 0
    failed = 0
    errors: list[str] = []

    if to_ingest:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _ingest_url_with_delay,
                    page_url,
                    db,
                    settings,
                    overwrite=True,
                    collection=collection,
                    document_name=doc_name,
                    timeout=timeout,
                    delay=delay,
                ): page_url
                for page_url, doc_name in to_ingest
            }
            for future in as_completed(futures):
                page_url = futures[future]
                try:
                    future.result()
                    ingested += 1
                    progress(
                        "Ingested %s (%d/%d)",
                        page_url,
                        ingested,
                        len(to_ingest),
                    )
                except Exception as exc:
                    failed += 1
                    errors.append(f"{page_url}: {exc}")
                    logger.exception("Failed to ingest %s", page_url)
                    progress("Failed %s: %s", page_url, exc)

    progress(
        "Done: %d ingested, %d skipped, %d failed",
        ingested,
        skipped,
        failed,
    )

    return SitemapResult(
        sitemap_url=url,
        collection=collection,
        total_discovered=total_discovered,
        after_filter=after_filter,
        ingested=ingested,
        skipped=skipped,
        failed=failed,
        errors=errors,
    )


def ingest_auto(
    url: str,
    db: LanceDB,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "",
    workers: int = 4,
    delay: float = 0.5,
    timeout: int = 30,
    progress_callback: Callable[[str], None] | None = None,
) -> IngestResult | SitemapResult:
    """Smart URL ingestion: discover sitemap, crawl if found, else single page.

    1. Probe the site for a sitemap (robots.txt, then /sitemap.xml).
    2. If found, derive a path prefix filter from the input URL and bulk-crawl.
    3. If not found, fall back to single-page ingestion.

    Args:
        url: Any HTTP(S) URL on the target site.
        db: LanceDB connection.
        settings: Application settings.
        overwrite: Force re-ingest regardless of existing data.
        collection: Collection name. Defaults to the URL hostname.
        workers: Parallel fetch workers for sitemap crawl (default 4).
        delay: Base delay between fetches in seconds (default 0.5).
        timeout: HTTP timeout in seconds.
        progress_callback: Optional callable for progress messages.

    Returns:
        SitemapResult if a sitemap was discovered, IngestResult otherwise.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    from quarry.sitemap import discover_sitemap  # noqa: PLC0415

    progress = _make_progress(progress_callback)
    parsed = urlparse(url)

    if not collection:
        collection = parsed.hostname or "default"

    # If the URL itself is a sitemap, skip discovery and crawl directly.
    path_lower = parsed.path.lower()
    is_sitemap = (
        path_lower.endswith(("sitemap.xml", "sitemap.xml.gz"))
        or "/sitemap" in path_lower
    )
    if is_sitemap:
        progress("URL is a sitemap, crawling directly")
        return ingest_sitemap(
            url,
            db,
            settings,
            collection=collection,
            overwrite=overwrite,
            workers=workers,
            delay=delay,
            timeout=timeout,
            progress_callback=progress_callback,
        )

    progress("Probing %s://%s for sitemap", parsed.scheme, parsed.netloc)
    sitemap_urls = discover_sitemap(url, timeout=timeout)

    if not sitemap_urls:
        progress("No sitemap found, ingesting single page")
        return ingest_url(
            url,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            timeout=timeout,
            progress_callback=progress_callback,
        )

    # Derive include filter from input URL path
    path = parsed.path.rstrip("/")
    include: list[str] | None = None
    if path:
        include = [path, f"{path}/*"]

    sitemap_url = sitemap_urls[0]
    progress("Discovered sitemap: %s", sitemap_url)

    return ingest_sitemap(
        sitemap_url,
        db,
        settings,
        collection=collection,
        include=include,
        overwrite=overwrite,
        workers=workers,
        delay=delay,
        timeout=timeout,
        progress_callback=progress_callback,
    )


def _make_progress(
    callback: Callable[[str], None] | None,
) -> Callable[..., None]:
    """Create a progress reporter that logs and optionally calls a callback."""

    def _progress(fmt: str, *args: object) -> None:
        logger.info(fmt, *args)
        if callback is not None:
            callback(fmt % args if args else fmt)

    return _progress


def _chunk_embed_store(
    pages: list[PageContent],
    document_name: str,
    db: LanceDB,
    settings: Settings,
    progress: Callable[..., None],
    *,
    collection: str = "default",
    source_format: str = "",
    total_pages: int | None = None,
    text_pages: int | None = None,
    image_pages: int | None = None,
    sections: int | None = None,
    definitions: int | None = None,
    sheets: int | None = None,
    slides: int | None = None,
    file_format: str | None = None,
) -> IngestResult:
    """Shared pipeline: chunk pages, embed, store in LanceDB."""
    progress("Chunking")
    chunks = chunk_pages(
        pages,
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
        collection=collection,
        source_format=source_format,
    )
    progress("Created %d chunks", len(chunks))

    inserted = 0
    if chunks:
        embedder = get_embedding_backend(settings)
        progress("Generating embeddings (%s)", embedder.model_name)
        texts = [c.text for c in chunks]
        vectors = embedder.embed_texts(texts)

        progress("Storing in LanceDB")
        inserted = insert_chunks(db, chunks, vectors)
        progress("Done: %d chunks indexed from %s", inserted, document_name)
    else:
        progress("No text found — nothing to index")

    result: IngestResult = {
        "document_name": document_name,
        "collection": collection,
        "chunks": inserted,
    }
    if total_pages is not None:
        result["total_pages"] = total_pages
    if text_pages is not None:
        result["text_pages"] = text_pages
    if image_pages is not None:
        result["image_pages"] = image_pages
    if sections is not None:
        result["sections"] = sections
    if definitions is not None:
        result["definitions"] = definitions
    if sheets is not None:
        result["sheets"] = sheets
    if slides is not None:
        result["slides"] = slides
    if file_format is not None:
        result["format"] = file_format
    return result
