"""URL ingestion: fetch, parse HTML, chunk, embed, store."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Self
from urllib.parse import urlparse

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.ingestion.url_fetcher import UrlFetcher
    from quarry.sitemap import SitemapEntry
    from quarry.types import LanceDB

from quarry.db import ChunkCatalog, ChunkStore
from quarry.extractors.html_extractor import HtmlExtractor
from quarry.models import PageContent
from quarry.results import IngestResult, SitemapResult
from quarry.sitemap import SitemapDiscovery

logger = logging.getLogger(__name__)


class UrlIngester:
    """Ingest web content: single URLs, sitemaps, and auto-discovery."""

    _db: LanceDB
    _settings: Settings
    _fetcher: UrlFetcher

    def __new__(
        cls,
        db: LanceDB,
        settings: Settings,
        fetcher: UrlFetcher,
    ) -> Self:
        self = super().__new__(cls)
        self._db = db
        self._settings = settings
        self._fetcher = fetcher
        return self

    def ingest_url(
        self,
        url: str,
        *,
        overwrite: bool = False,
        collection: str = "default",
        document_name: str | None = None,
        timeout: int = 30,
        progress_callback: Callable[[str], None] | None = None,
        agent_handle: str = "",
        memory_type: str = "",
        summary: str = "",
    ) -> IngestResult:
        """Fetch a URL, extract text from HTML, chunk, embed, store."""
        progress = _make_progress(progress_callback)
        document_name = document_name or url

        progress("Fetching: %s", url)
        html = self._fetcher.fetch(url, timeout=timeout)
        progress("Fetched %d characters", len(html))

        if overwrite:
            ChunkStore(self._db).delete_document(
                document_name, collection=collection, count=False
            )

        pages = HtmlExtractor().extract_from_html(html, document_name, url)
        progress("Sections: %d", len(pages))

        return _chunk_embed_store(
            pages,
            document_name,
            self._db,
            self._settings,
            progress,
            collection=collection,
            source_format=".html",
            sections=len(pages),
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )

    def _ingest_with_delay(
        self,
        page_url: str,
        *,
        overwrite: bool,
        collection: str,
        document_name: str | None,
        timeout: int,
        delay: float,
        agent_handle: str = "",
        memory_type: str = "",
        summary: str = "",
    ) -> IngestResult:
        """Ingest a single URL with a pre-fetch delay to avoid rate limiting."""
        import random  # noqa: PLC0415
        import time  # noqa: PLC0415

        jitter = random.uniform(0, 1.0)  # noqa: S311
        time.sleep(delay + jitter)

        return self.ingest_url(
            page_url,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            timeout=timeout,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )

    def _bulk_ingest(
        self,
        entries: list[SitemapEntry],
        *,
        source_url: str,
        collection: str,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        limit: int = 0,
        overwrite: bool = False,
        workers: int = 4,
        delay: float = 0.5,
        timeout: int = 30,
        progress: Callable[..., None],
        agent_handle: str = "",
        memory_type: str = "",
        summary: str = "",
    ) -> SitemapResult:
        """Filter, dedup, and parallel-ingest a list of sitemap entries."""
        from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

        workers = max(1, workers)
        total_discovered = len(entries)

        filtered = SitemapDiscovery.filter_entries(
            entries,
            include=include,
            exclude=exclude,
            limit=limit,
        )
        after_filter = len(filtered)
        progress("After filtering: %d URLs", after_filter)

        existing_docs = ChunkCatalog(self._db).list_documents(
            collection_filter=collection
        )
        existing_timestamps: dict[str, str] = {
            doc["document_name"]: doc["ingestion_timestamp"] for doc in existing_docs
        }

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
                    pass  # Can't parse -- re-ingest to be safe
            to_ingest.append((entry.loc, None))

        progress("%d to ingest, %d up-to-date", len(to_ingest), skipped)

        ingested = 0
        failed = 0
        errors: list[str] = []

        if to_ingest:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        self._ingest_with_delay,
                        page_url,
                        overwrite=True,
                        collection=collection,
                        document_name=doc_name,
                        timeout=timeout,
                        delay=delay,
                        agent_handle=agent_handle,
                        memory_type=memory_type,
                        summary=summary,
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
            sitemap_url=source_url,
            collection=collection,
            total_discovered=total_discovered,
            after_filter=after_filter,
            ingested=ingested,
            skipped=skipped,
            failed=failed,
            errors=errors,
        )

    def ingest_sitemap(
        self,
        url: str,
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
        agent_handle: str = "",
        memory_type: str = "",
        summary: str = "",
    ) -> SitemapResult:
        """Crawl a sitemap and ingest all discovered URLs."""
        progress = _make_progress(progress_callback)

        if not collection:
            collection = urlparse(url).hostname or "default"

        progress("Fetching sitemap: %s", url)
        entries = SitemapDiscovery.discover_urls(url)
        progress("Discovered %d URLs", len(entries))

        return self._bulk_ingest(
            entries,
            source_url=url,
            collection=collection,
            include=include,
            exclude=exclude,
            limit=limit,
            overwrite=overwrite,
            workers=workers,
            delay=delay,
            timeout=timeout,
            progress=progress,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )

    def ingest_auto(
        self,
        url: str,
        *,
        overwrite: bool = False,
        collection: str = "",
        workers: int = 4,
        delay: float = 0.5,
        timeout: int = 30,
        progress_callback: Callable[[str], None] | None = None,
        agent_handle: str = "",
        memory_type: str = "",
        summary: str = "",
    ) -> IngestResult | SitemapResult:
        """Smart URL ingestion: discover sitemap, crawl if found, else single page."""
        progress = _make_progress(progress_callback)
        parsed = urlparse(url)

        if not collection:
            collection = parsed.hostname or "default"

        path_lower = parsed.path.lower()
        last_segment = path_lower.rsplit("/", 1)[-1]
        is_sitemap = last_segment.startswith("sitemap") and (
            last_segment.endswith((".xml", ".xml.gz", ".txt"))
            or last_segment == "sitemap"
        )
        if is_sitemap:
            progress("URL is a sitemap, crawling directly")
            return self.ingest_sitemap(
                url,
                collection=collection,
                overwrite=overwrite,
                workers=workers,
                delay=delay,
                timeout=timeout,
                progress_callback=progress_callback,
                agent_handle=agent_handle,
                memory_type=memory_type,
                summary=summary,
            )

        progress("Discovering sitemaps for %s://%s", parsed.scheme, parsed.netloc)
        try:
            entries = SitemapDiscovery.discover_pages(url)
        except Exception:
            logger.exception("Sitemap discovery failed for %s", url)
            entries = []

        if not entries:
            progress("No sitemap found, ingesting single page")
            return self.ingest_url(
                url,
                overwrite=overwrite,
                collection=collection,
                timeout=timeout,
                progress_callback=progress_callback,
                agent_handle=agent_handle,
                memory_type=memory_type,
                summary=summary,
            )

        progress("Discovered %d pages via sitemap", len(entries))

        path = parsed.path.rstrip("/")
        include: list[str] | None = None
        if path:
            include = [path, f"{path}/*"]

        if include:
            entries = SitemapDiscovery.filter_entries(entries, include=include)

        if not entries:
            progress("Sitemap has no pages matching %s, ingesting single page", path)
            return self.ingest_url(
                url,
                overwrite=overwrite,
                collection=collection,
                timeout=timeout,
                progress_callback=progress_callback,
                agent_handle=agent_handle,
                memory_type=memory_type,
                summary=summary,
            )

        return self._bulk_ingest(
            entries,
            source_url=url,
            collection=collection,
            overwrite=overwrite,
            workers=workers,
            delay=delay,
            timeout=timeout,
            progress=progress,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
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
    sections: int | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Shared pipeline: chunk pages, embed, store in LanceDB."""
    import time  # noqa: PLC0415

    from quarry.ingestion.backends import get_embedding_backend  # noqa: PLC0415
    from quarry.ingestion.chunker import chunk_pages  # noqa: PLC0415

    progress("Chunking")
    t0 = time.perf_counter()
    chunks = chunk_pages(
        pages,
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
        collection=collection,
        source_format=source_format,
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )
    t_chunk = time.perf_counter() - t0
    logger.info(
        "url_ingester: chunked %d pages -> %d chunks in %.2fs",
        len(pages),
        len(chunks),
        t_chunk,
    )
    progress("Created %d chunks", len(chunks))

    inserted = 0
    if chunks:
        embedder = get_embedding_backend(settings)
        progress("Generating embeddings (%s)", embedder.model_name)
        texts = [c.text for c in chunks]
        t0 = time.perf_counter()
        vectors = embedder.embed_texts(texts)
        t_embed = time.perf_counter() - t0
        logger.info(
            "url_ingester: embedded %d chunks in %.2fs (%.1f chunks/s)",
            len(chunks),
            t_embed,
            len(chunks) / t_embed if t_embed > 0 else float("inf"),
        )

        progress("Storing in LanceDB")
        t0 = time.perf_counter()
        inserted = ChunkStore(db).insert(chunks, vectors)
        t_store = time.perf_counter() - t0
        logger.info("url_ingester: stored %d chunks in %.2fs", inserted, t_store)
        progress("Done: %d chunks indexed from %s", inserted, document_name)
    else:
        progress("No text found -- nothing to index")

    result: IngestResult = {
        "document_name": document_name,
        "collection": collection,
        "chunks": inserted,
    }
    if sections is not None:
        result["sections"] = sections
    return result
