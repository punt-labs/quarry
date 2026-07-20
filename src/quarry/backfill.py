"""Backfill historical Claude Code session transcripts into quarry."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self, final

from quarry.artifacts import (
    SessionArtifacts,
    extract_artifacts,
    format_artifacts_header,
)
from quarry.config import Settings
from quarry.db.facade import Database
from quarry.hooks import extract_transcript_text
from quarry.ingestion.pipeline import ingest_content
from quarry.scrub import scrub_and_log
from quarry.sync_registry import DirectoryRegistration, SyncRegistry
from quarry.transcript import Transcript

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


@dataclass(frozen=True, slots=True)
class BackfillConfig:
    """Configuration for a transcript backfill run."""

    dry_run: bool = False
    collection_override: str | None = None
    project_filter: str | None = None
    limit: int | None = None


@dataclass(frozen=True)
class BackfillStats:
    """Aggregate counts from a backfill run."""

    ingested: int = 0
    skipped_existing: int = 0
    skipped_unregistered: int = 0
    skipped_empty: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectMapping:
    """Maps an encoded Claude project directory to a quarry collection."""

    encoded_dir: str
    project_path: str
    collection: str
    captures_collection: str


@dataclass
class _Accumulator:
    """Mutable counters for the backfill loop."""

    ingested: int = 0
    skipped_existing: int = 0
    skipped_empty: int = 0
    processed: int = 0
    errors: list[str] = field(default_factory=list)


def encode_project_path(project_path: str) -> str:
    """Encode a project path the same way Claude Code does.

    Replace ``/`` with ``-``.  The leading ``-`` is preserved — Claude
    Code keeps it (e.g. ``/Users/jm`` → ``-Users-jm``).
    """
    return project_path.replace("/", "-")


def build_project_mappings(
    registrations: list[DirectoryRegistration],
) -> list[ProjectMapping]:
    """Build mappings from encoded Claude project dirs to quarry collections.

    For each registration, encode its directory path and check whether a
    matching subdirectory exists under ``~/.claude/projects/``. This avoids
    the ambiguous reverse-decode problem (hyphens in directory names).
    """
    mappings: list[ProjectMapping] = []
    if not CLAUDE_PROJECTS_DIR.is_dir():
        return mappings

    existing_dirs = {d.name for d in CLAUDE_PROJECTS_DIR.iterdir() if d.is_dir()}

    for reg in registrations:
        encoded = encode_project_path(reg.directory)
        if encoded in existing_dirs:
            mappings.append(
                ProjectMapping(
                    encoded_dir=encoded,
                    project_path=reg.directory,
                    collection=reg.collection,
                    captures_collection=f"{reg.collection}-captures",
                )
            )
    return mappings


def list_transcript_files(encoded_dir: str) -> list[Path]:
    """Return all JSONL transcript files for an encoded project directory."""
    project_dir = CLAUDE_PROJECTS_DIR / encoded_dir
    if not project_dir.is_dir():
        return []
    return sorted(project_dir.glob("*.jsonl"))


def document_name_for_transcript(transcript_path: Path) -> str:
    """Derive the ``session-<id[:8]>-<mtime>`` document name for a transcript."""
    return Transcript(transcript_path).document_name()


def is_already_ingested(
    session_id_prefix: str,
    existing_doc_names: set[str],
) -> bool:
    """Check if a transcript with the given session prefix is already ingested.

    Recognizes both the compaction hook's stable ``session-<id[:8]>`` name and
    backfill's own ``session-<id[:8]>-<mtime>`` name — so a session already
    captured by the hook is skipped instead of re-ingested as a duplicate.
    """
    stable = f"session-{session_id_prefix}"
    timestamped = f"{stable}-"
    return any(
        name == stable or name.startswith(timestamped) for name in existing_doc_names
    )


def _get_existing_doc_names(database: Database, collection: str) -> set[str]:
    """Return the set of document names in a collection."""
    docs = database.catalog.list_documents(collection_filter=collection)
    return {d["document_name"] for d in docs}


def _count_unregistered_dirs(mapped_dirs: set[str]) -> int:
    """Count Claude project directories that have no quarry registration."""
    if not CLAUDE_PROJECTS_DIR.is_dir():
        return 0
    all_dirs = {d.name for d in CLAUDE_PROJECTS_DIR.iterdir() if d.is_dir()}
    return len(all_dirs - mapped_dirs)


def _write_backfill_capture_file(
    project_path: str,
    session_id: str,
    transcript: Path,
    artifacts: SessionArtifacts,
    text: str,
) -> None:
    """Write a backfill session capture via the shared CaptureWriter.

    The writer scrubs secrets, PII, and profanity before any bytes reach the
    git-tracked capture file, and fails silently.
    """
    from quarry.capture import CaptureRequest, CaptureWriter  # noqa: PLC0415

    timestamp = Transcript(transcript).timestamp("%Y-%m-%dT%H:%M:%SZ")
    CaptureWriter().write(
        CaptureRequest(
            project_dir=Path(project_path),
            session_id=session_id,
            timestamp=timestamp,
            artifacts=artifacts,
            text=text,
            label="backfill",
        )
    )


@final
class _ProjectProcessor:
    """Ingest one project's not-yet-captured transcripts, tallying the run.

    Owns the per-project state the old free function threaded through eight
    parameters — the shared database, settings, accumulator, and the derived
    target collection and already-seen document names — so each step is a short
    method instead of one deeply nested loop.
    """

    _mapping: ProjectMapping
    _database: Database
    _settings: Settings
    _acc: _Accumulator
    _dry_run: bool
    _limit: int
    _collection: str
    _existing: set[str]
    _ingested: int
    _skipped: int

    def __new__(
        cls,
        mapping: ProjectMapping,
        database: Database,
        settings: Settings,
        acc: _Accumulator,
        *,
        dry_run: bool,
        collection_override: str,
        limit: int,
    ) -> Self:
        self = super().__new__(cls)
        self._mapping = mapping
        self._database = database
        self._settings = settings
        self._acc = acc
        self._dry_run = dry_run
        self._limit = limit
        self._collection = collection_override or mapping.captures_collection
        self._existing = set()
        self._ingested = 0
        self._skipped = 0
        return self

    def process(self) -> None:
        """Ingest every transcript for this project, then log the tally."""
        transcripts = list_transcript_files(self._mapping.encoded_dir)
        if not transcripts:
            return
        self._existing = _get_existing_doc_names(self._database, self._collection)
        for transcript in transcripts:
            if self._limit > 0 and self._acc.processed >= self._limit:
                break
            self._handle(transcript)
        logger.info(
            "backfill: %s -> %s (%d new, %d already ingested)",
            self._mapping.project_path,
            self._collection,
            self._ingested,
            self._skipped,
        )

    def _handle(self, transcript: Path) -> None:
        """Skip, dry-run, or ingest one transcript, updating the counters."""
        if is_already_ingested(transcript.stem[:8], self._existing):
            self._skipped += 1
            self._acc.skipped_existing += 1
            return
        if self._dry_run:
            self._ingested += 1
            self._acc.ingested += 1
            self._acc.processed += 1
            return
        text = extract_transcript_text(str(transcript))
        if not text.strip():
            self._acc.skipped_empty += 1
            self._acc.processed += 1
            return
        self._ingest(transcript, text)
        self._acc.processed += 1

    def _ingest(self, transcript: Path, text: str) -> None:
        """Write the capture file and ingest the transcript through the pipeline."""
        artifacts = extract_artifacts(text)
        _write_backfill_capture_file(
            project_path=self._mapping.project_path,
            session_id=transcript.stem,
            transcript=transcript,
            artifacts=artifacts,
            text=text,
        )
        header = format_artifacts_header(artifacts)
        body = f"{header}\n\n{text}" if header else text
        doc_name = document_name_for_transcript(transcript)
        try:
            # Scrub via the pipeline choke point: passing a content_scrubber makes
            # ingest_content redact the content AND the metadata, so the chunks
            # honour the DES-036 write-time scrub invariant through the one scrub
            # every scrubbed caller shares — backfill can never drift from it.
            # (The capture .md is scrubbed independently by CaptureWriter.)
            ingest_content(
                body,
                doc_name,
                self._database,
                self._settings,
                overwrite=True,
                collection=self._collection,
                format_hint="plain",
                content_scrubber=lambda t: scrub_and_log(t, "backfill"),
            )
        except Exception as exc:
            self._acc.errors.append(f"{transcript.name}: {exc}")
            logger.exception("backfill: failed to ingest %s", transcript.name)
            return
        self._existing.add(doc_name)
        self._ingested += 1
        self._acc.ingested += 1


def backfill_sessions(
    settings: Settings,
    *,
    dry_run: bool = False,
    collection_override: str = "",
    project_filter: str = "",
    limit: int = 0,
) -> BackfillStats:
    """Scan Claude Code project transcripts and ingest into quarry."""
    conn = SyncRegistry(settings.registry_path)
    try:
        registrations = conn.list_registrations()
    finally:
        conn.close()

    all_mappings = build_project_mappings(registrations)
    if project_filter:
        mappings = [m for m in all_mappings if m.project_path == project_filter]
    else:
        mappings = all_mappings

    database = Database.connect(settings.lancedb_path)
    acc = _Accumulator()
    skipped_unregistered = _count_unregistered_dirs(
        {m.encoded_dir for m in all_mappings}
    )

    for mapping in mappings:
        _ProjectProcessor(
            mapping,
            database,
            settings,
            acc,
            dry_run=dry_run,
            collection_override=collection_override,
            limit=limit,
        ).process()
        if limit > 0 and acc.processed >= limit:
            logger.info("backfill: reached limit of %d transcripts", limit)
            break

    return BackfillStats(
        ingested=acc.ingested,
        skipped_existing=acc.skipped_existing,
        skipped_unregistered=skipped_unregistered,
        skipped_empty=acc.skipped_empty,
        errors=acc.errors,
    )
