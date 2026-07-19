"""Enable and disable quarry knowledge capture for project directories."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from yaml import YAMLError

if TYPE_CHECKING:
    from quarry.api import (
        DeleteCollectionRequest,
        DeregisterAccepted,
        DeregisterRequest,
        RegisterRequest,
        RegistrationList,
        TaskAccepted,
    )
    from quarry.registrations import Registrations

logger = logging.getLogger(__name__)


class RegistryClient(Protocol):
    """The daemon-registry surface enable/disable need — the client is the adapter.

    Depending on this port (not the concrete ``QuarryClient``) keeps enable/disable
    off the client package's import graph and lets a test supply an in-memory
    stand-in.
    """

    def list_registrations(self) -> RegistrationList: ...
    def register(self, req: RegisterRequest) -> TaskAccepted: ...
    def deregister(self, req: DeregisterRequest) -> DeregisterAccepted: ...
    def delete_collection(self, req: DeleteCollectionRequest) -> TaskAccepted: ...


_CLAUDEMD_BEGIN = "<!-- quarry:begin -->"
_CLAUDEMD_END = "<!-- quarry:end -->"

_CLAUDEMD_BLOCK = """\
<!-- quarry:begin -->
## Quarry

Local semantic search is available via quarry. Use it to search indexed
documents by meaning, ingest new content, and recall knowledge across sessions.

- Before using WebSearch or WebFetch for research, run `/find` with the query
  first. Quarry indexes this codebase, design docs, prior session transcripts,
  and web pages from previous research. If quarry returns relevant results,
  use them — do not re-research what has already been found.
- Use grep for symbol lookups and value lookups; use quarry for "why", "how",
  and "what did we decide about X" questions.
- **Slash commands**: `/find`, `/ingest`, `/remember`, `/explain`, `/source`,
  `/quarry`
- **Research agent**: `researcher` — combines quarry local search with web
  research. Use for deep investigation across local docs and the web.
- **Auto-behaviors**: working directory is auto-indexed at session start;
  URLs fetched via WebFetch are auto-ingested; transcripts are captured before
  context compaction.
- **Search tip**: natural language queries work best ("What were Q3 margins?"
  outperforms "Q3 margins").
<!-- quarry:end -->
"""


@dataclass(frozen=True, slots=True)
class EnableResult:
    """Result of enabling quarry for a project directory."""

    directory: str
    collection: str
    captures_collection: str
    memory_collections: list[str] = field(default_factory=list)
    config_path: str = ""
    created_registration: bool = False
    claudemd_appended: bool = False
    ethos_skipped: bool = False
    ethos_updated: list[str] = field(default_factory=list)
    ethos_already_set: list[str] = field(default_factory=list)
    ethos_created: list[str] = field(default_factory=list)
    ethos_failed: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DisableResult:
    """Result of disabling quarry.  ``removed`` is the registry file count the
    daemon reported synchronously; the chunk purge runs as a background task."""

    directory: str
    collection: str
    captures_collection: str
    removed: int = 0
    config_removed: bool = False
    claudemd_removed: bool = False


_GLOBAL_IDENTITIES = Path.home() / ".punt-labs" / "ethos" / "identities"

_CONFIG_TEMPLATE = """\
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
# shadow:                          # push redacted captures to a PRIVATE shadow repo
#   enabled: false                 # opt-in network+security action, off by default
#   remote: ""                     # empty -> derive <origin>-quarry from origin
#   acknowledge_unverified: false  # push even when gh cannot confirm private
---

# Quarry Project Configuration

Controls quarry's passive knowledge capture. Set any field to `false` to disable
that capture type; uncomment `shadow` to move redacted captures off the public
repo into a per-project private shadow (`<repo>` -> `<repo>-quarry`).

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
- `shadow`: pre-create the private repo, then set `enabled: true`
"""


def enable_project(
    directory: Path,
    client: RegistryClient,
    collection_override: str = "",
) -> EnableResult:
    """Enable quarry knowledge capture for a project directory.

    The registry is the daemon's (DES-031 I2): coverage is computed from its
    ``RegistrationList`` and a new registration is dispatched via ``client``, never
    a local ``SyncRegistry``.  The project files (config.md, CLAUDE.md, ethos ext)
    are the client's and are written locally.
    """
    from quarry.registrations import Registrations  # noqa: PLC0415

    # expanduser BEFORE resolve: a bare "~/proj" otherwise resolves against cwd
    # ("./~/proj"), targeting the wrong directory.
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        msg = f"directory not found: {directory}"
        raise ValueError(msg)

    view = Registrations(client.list_registrations().registrations)
    collection, created = _resolve_or_register(
        view, client, directory, collection_override
    )

    captures_collection = f"{collection}-captures"

    (
        created_handles,
        updated_handles,
        already_set_handles,
        failed_handles,
        ethos_skipped,
    ) = _bootstrap_ethos_memory()

    memory_collections = [f"memory-{h}" for h in created_handles]

    config_path = _write_project_config(directory)
    claudemd_appended = _append_claudemd_block(directory)
    if claudemd_appended:
        logger.info("Appended quarry instructions to CLAUDE.md")

    return EnableResult(
        directory=str(directory),
        collection=collection,
        captures_collection=captures_collection,
        memory_collections=memory_collections,
        config_path=config_path,
        created_registration=created,
        claudemd_appended=claudemd_appended,
        ethos_skipped=ethos_skipped,
        ethos_updated=updated_handles,
        ethos_already_set=already_set_handles,
        ethos_created=created_handles,
        ethos_failed=failed_handles,
    )


def disable_project(
    directory: Path,
    client: RegistryClient,
    *,
    keep_data: bool = False,
) -> DisableResult:
    """Disable quarry knowledge capture for a project directory.

    Idempotent and retry-safe.  Deregisters the covering collection via the daemon
    (dropping the registry row and purging its chunks server-side) and, unless
    ``keep_data``, dispatches a purge of the ``-captures`` sibling — both
    fire-and-forget; the registry is never mutated through a local ``SyncRegistry``.

    A directory with no covering registration is NOT an error: it was never
    enabled, or a prior partial disable already removed it.  The local project
    files are still cleaned and the call succeeds, so a retry after a mid-teardown
    failure always converges to fully-disabled.  Local file cleanup runs BEFORE
    the best-effort captures purge, so a rejected purge can never leave config.md
    or CLAUDE.md claiming enabled.
    """
    from quarry.api import DeleteCollectionRequest, DeregisterRequest  # noqa: PLC0415
    from quarry.client.errors import QuarryError  # noqa: PLC0415
    from quarry.registrations import Registrations  # noqa: PLC0415

    # expanduser BEFORE resolve: a bare "~/proj" otherwise resolves against cwd,
    # targeting (and deregistering) the wrong path.
    directory = directory.expanduser().resolve()
    view = Registrations(client.list_registrations().registrations)
    covering = view.covering(directory)

    # Disabling a CHILD of a registered parent is a real error — the parent covers
    # it; never silently deregister the parent. This guard alone stays fatal.
    if covering is not None and covering.directory != str(directory):
        msg = (
            f"no registration for {directory}; "
            f"it is covered by parent registration at {covering.directory}"
        )
        raise ValueError(msg)

    collection = covering.collection if covering is not None else ""
    removed = 0
    if covering is not None:
        removed = client.deregister(
            DeregisterRequest(collection=collection, keep_data=keep_data)
        ).removed

    # Clean local files whether or not a registration was present, and BEFORE the
    # best-effort captures purge below — a retry always reaches here.
    config_path = directory / ".punt-labs" / "quarry" / "config.md"
    config_removed = False
    if config_path.exists():
        config_path.unlink()
        config_removed = True

    quarry_dir = directory / ".punt-labs" / "quarry"
    if quarry_dir.is_dir() and not any(quarry_dir.iterdir()):
        quarry_dir.rmdir()

    claudemd_removed = _remove_claudemd_block(directory)
    if claudemd_removed:
        logger.info("Removed quarry instructions from CLAUDE.md")

    # Best-effort captures purge, dispatched last. A rejection is caught and
    # warned, never propagated: the primary teardown (deregister + local file
    # cleanup) already succeeded and disable is idempotent, so a stranded
    # secondary purge must not fail the whole command. Once the registration is
    # gone a retry cannot re-derive the captures name, so this is the one attempt.
    captures_collection = f"{collection}-captures" if collection else ""
    if covering is not None and not keep_data:
        try:
            client.delete_collection(DeleteCollectionRequest(name=captures_collection))
        except QuarryError:
            logger.warning(
                "captures purge for %s was rejected; its chunks may remain, but "
                "the project is fully disabled (deregistered + local files removed)",
                captures_collection,
            )

    return DisableResult(
        directory=str(directory),
        collection=collection,
        captures_collection=captures_collection,
        removed=removed,
        config_removed=config_removed,
        claudemd_removed=claudemd_removed,
    )


def _resolve_or_register(
    view: Registrations,
    client: RegistryClient,
    directory: Path,
    collection_override: str,
) -> tuple[str, bool]:
    """Reuse the covering registration, or dispatch a new one to the daemon.

    Returns (collection_name, created).  Raises ValueError when *directory* is a
    child of an existing registration (sessions there use the parent's collection
    automatically).
    """
    from quarry.api import RegisterRequest  # noqa: PLC0415

    covering = view.covering(directory)
    if covering is not None:
        if covering.directory == str(directory):
            return covering.collection, False
        msg = (
            f"This directory is already covered by the registration at "
            f"{covering.directory} (collection: {covering.collection}). "
            f"Sessions here use that collection automatically. No action needed."
        )
        raise ValueError(msg)

    name = collection_override or view.unique_collection_name(directory)
    # Fire-and-forget: the daemon re-guards the path on its own filesystem and
    # writes the registry row as a background task.
    client.register(RegisterRequest(directory=str(directory), collection=name))
    return name, True


def _bootstrap_ethos_memory() -> tuple[
    list[str], list[str], list[str], list[str], bool
]:
    """Create quarry.yaml ext files and write session_context.

    Reads only the global identities directory (repo-level identities are
    read-only). Returns (created, updated, already_set, failed, skipped);
    skipped is True when the global identities directory does not exist.

    A handle appears in ``failed`` when its session_context write raised an
    I/O or YAML error — the useful part never landed, so the caller must not
    report unqualified success for it.  Non-OSError/YAMLError exceptions are
    real bugs and propagate.
    """
    from quarry.doctor import (  # noqa: PLC0415
        _write_ethos_ext_session_context,  # pyright: ignore[reportPrivateUsage]
    )

    if not _GLOBAL_IDENTITIES.is_dir():
        return [], [], [], [], True

    created: list[str] = []
    updated: list[str] = []
    already_set: list[str] = []
    failed: list[str] = []
    for identity_file in sorted(_GLOBAL_IDENTITIES.glob("*.yaml")):
        handle = identity_file.stem
        ext_dir = _GLOBAL_IDENTITIES / f"{handle}.ext"
        ext_dir.mkdir(exist_ok=True)
        quarry_yaml = ext_dir / "quarry.yaml"
        if not quarry_yaml.exists():
            quarry_yaml.write_text(
                f"memory_collection: memory-{handle}\n",
                encoding="utf-8",
            )
            created.append(handle)

        try:
            result = _write_ethos_ext_session_context(quarry_yaml, handle)
        except (OSError, YAMLError, UnicodeDecodeError):
            # UnicodeDecodeError (a ValueError, not an OSError) fires on a
            # non-UTF8/corrupt identity file — record the handle and continue
            # rather than crash enable; a real bug still propagates.
            logger.warning("failed to write session context for %s", handle)
            failed.append(handle)
            continue
        if result == "updated":
            updated.append(handle)
        elif result == "already_set":
            already_set.append(handle)

    return created, updated, already_set, failed, False


def _write_project_config(directory: Path) -> str:
    """Write config.md atomically (O_CREAT|O_EXCL, no overwrite); return its path."""
    config_dir = directory / ".punt-labs" / "quarry"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.md"
    try:
        fd = os.open(str(config_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            with os.fdopen(fd, "w") as f:
                fd = -1  # fdopen took ownership before write
                f.write(_CONFIG_TEMPLATE)
        finally:
            if fd >= 0:
                os.close(fd)
    except FileExistsError:
        pass
    return str(config_path)


def _append_claudemd_block(directory: Path) -> bool:
    """Append quarry instruction block to CLAUDE.md. Idempotent.

    Creates the file if it does not exist. Returns True if the block
    was appended, False if it was already present.
    """
    claudemd = directory / "CLAUDE.md"
    if claudemd.exists():
        content = claudemd.read_text(encoding="utf-8")
        if _CLAUDEMD_BEGIN in content:
            return False
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n" + _CLAUDEMD_BLOCK
    else:
        content = _CLAUDEMD_BLOCK
    claudemd.write_text(content, encoding="utf-8")
    return True


def _remove_claudemd_block(directory: Path) -> bool:
    """Remove quarry instruction block from CLAUDE.md.

    Removes everything from ``<!-- quarry:begin -->`` through
    ``<!-- quarry:end -->`` inclusive. Both markers must be present.
    Cleans up extra trailing blank lines left by removal. Returns
    True if a block was removed, False otherwise.
    """
    claudemd = directory / "CLAUDE.md"
    if not claudemd.exists():
        return False
    content = claudemd.read_text(encoding="utf-8")
    if _CLAUDEMD_BEGIN not in content or _CLAUDEMD_END not in content:
        return False
    pattern = (
        r"\n?" + re.escape(_CLAUDEMD_BEGIN) + r".*?" + re.escape(_CLAUDEMD_END) + r"\n?"
    )
    cleaned = re.sub(pattern, "", content, flags=re.DOTALL)
    cleaned = cleaned.rstrip() + "\n"
    claudemd.write_text(cleaned, encoding="utf-8")
    return True
