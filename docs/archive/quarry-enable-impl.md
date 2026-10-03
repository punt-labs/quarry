# quarry enable -- Task-Level Implementation Design

**Status:** Historical — snapshot from May 2026. Superseded by implementation.

## Problem

Quarry captures three kinds of knowledge per project: file sync,
passive captures (web fetches, session transcripts), and agent
memories. Today, these are set up through three disconnected
mechanisms:

1. **File sync**: SessionStart hook auto-registers the cwd. Crashes
   with `ValueError` when opened in a child directory of an existing
   registration.
2. **Passive captures**: Written to hardcoded fallback collections
   (`web-captures`, `session-notes`) that mix content across all
   projects.
3. **Agent memory**: Requires manual creation of `quarry.yaml`
   extension files in ethos identity directories. Nobody does this.

There is no single command that sets up all three for a project.

## CEO Feedback (Binding Constraints)

1. **No data migration.** Old stale collections are already cleaned
   up. No migration logic.
2. **Agent memory is agent-wide, not project-wide.** `memory-<handle>`
   is global across all repos.
3. **Auto-registration stays.** If a session starts in a directory
   with no covering registration, auto-register it. All three knowledge
   types should work without the user thinking about it. `quarry enable`
   is for explicit configuration, not for basic functionality.
4. **Knowledge vs memories.** Document chunks (files, web captures,
   session transcripts) are permanent -- no temporal decay. Agent
   memories (`/remember`) decay via RRF score adjustment. DES-017
   already implements this distinction via `memory_type`.

## Approach

### Collection Naming Scheme

| Type | Name Pattern | Example | Scope | Decay |
|------|-------------|---------|-------|-------|
| Project files | `<leaf>` | `quarry` | Per-directory registration | None |
| Captures | `<leaf>-captures` | `quarry-captures` | Per-directory, no file sync | None |
| Agent memory | `memory-<handle>` | `memory-claude` | Global (agent-wide) | Yes (DES-017) |

The project files collection uses the existing `_unique_collection_name`
logic from `hooks.py`. The captures collection appends `-captures` to
that name. Agent memory collections use the `memory-` prefix per
DES-018.

Knowledge (files, captures) is permanent reference material. Memories
are agent-learned information that decays via temporal weighting in
`hybrid_search`. This distinction is already implemented: `_fuse_rrf`
applies decay only to chunks with `memory_type` in `_DECAYABLE_TYPES`.

### What Changes

**`quarry enable` command** -- explicit per-project setup.

**SessionStart hook** -- auto-register stays, child-directory crash
fixed, captures routed to `<name>-captures`.

**`quarry disable` command** -- removes project registration and
captures collection, preserves agent memory.

**Ethos bootstrapping** -- `quarry enable` creates `quarry.yaml` ext
files with `memory_collection` and `session_context`.

---

## 1. `quarry enable` Command

### CLI Interface

```text
quarry enable [DIRECTORY] [--collection NAME]
```

- `DIRECTORY`: defaults to `.` (cwd). Must be an existing directory.
- `--collection`: override the auto-derived collection name.

### Behavior

1. Resolve `DIRECTORY` to an absolute path.
2. Check for existing registration covering this directory:
   a. Exact match: reuse the existing collection name.
   b. Parent match: raise `ValueError` with the message: "This
      directory is already covered by the registration at `<parent>`
      (collection: `<collection>`). Sessions here use that collection
      automatically. No action needed."
   c. No match: register the directory with a new collection name
      using `_unique_collection_name` (or `--collection` override).
3. Create the captures collection name: `<collection>-captures`.
   No LanceDB action needed -- the collection is created lazily when
   the first chunk is inserted (LanceDB collections are just values
   in the `collection` column). This applies to both local and remote
   servers: remote collections are also created lazily on first
   ingestion, not during sync.
4. Discover ethos identities and bootstrap agent memory:
   a. Call `_bootstrap_ethos_memory()` (no parameters -- it
      unconditionally reads `Path.home() / ".punt-labs" / "ethos" /
      "identities"`).
   b. For each identity `<handle>.yaml`, ensure `<handle>.ext/`
      exists and contains `quarry.yaml` with `memory_collection:
      memory-<handle>`.
   c. Run `_write_ethos_ext_session_context` on each `quarry.yaml`
      to add `session_context` if missing.
   d. If ethos is not installed (global identities dir missing),
      skip with a warning.
5. Write `.punt-labs/quarry/config.md` in the project directory with
   default frontmatter (all capture types enabled).
6. Print summary: registered directory, captures collection, agent
   memory collections created/updated, config file path.

### Exit Codes

- 0: success (including idempotent re-run)
- 1: directory not found, or directory is child of existing
  registration

### Exact Code Changes

**File: `src/quarry/__main__.py`**

Add `enable` and `disable` to `_COMMAND_ORDER` list, after `"sync"` and
before `"optimize"`.

New function `enable_cmd`:

```python
@app.command(name="enable")
@_cli_errors
def enable_cmd(
    directory: Annotated[
        Path,
        typer.Argument(help="Project directory to enable (default: cwd)"),
    ] = Path("."),
    collection: Annotated[
        str,
        typer.Option("--collection", "-c", help="Override collection name"),
    ] = "",
) -> None:
    """Enable quarry knowledge capture for a project directory."""
```

Implementation calls `quarry.enable.enable_project()`, which raises
`ValueError` for error cases (child of registered parent, directory not
found). The CLI layer catches `ValueError` and prints the message with
exit code 1:

```python
try:
    result = enable_project(directory.resolve(), collection_override=collection)
except ValueError as exc:
    _emit(str(exc), is_err=True)
    raise typer.Exit(code=1) from None
```

JSON output uses `dataclasses.asdict(result)` passed to `_emit`, which
calls `json.dumps` (consistent with the existing `--json` pattern in
`__main__.py`).

**File: `src/quarry/enable.py`** (new module)

```python
"""Enable and disable quarry knowledge capture for project directories."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnableResult:
    """Result of quarry enable for a single project directory."""
    directory: str
    collection: str
    captures_collection: str
    memory_collections: list[str]
    config_path: str
    created_registration: bool
    ethos_skipped: bool
    ethos_updated: list[str]
    ethos_already_set: list[str]
    ethos_created: list[str]


def enable_project(
    directory: Path,
    collection_override: str = "",
) -> EnableResult:
    """Enable quarry knowledge capture for a project directory."""
    ...


def disable_project(
    directory: Path,
) -> DisableResult:
    """Disable quarry knowledge capture for a project directory."""
    ...
```

Key internal functions:

- `_resolve_or_register(conn, directory, collection_override)` --
  finds existing registration or creates one. Returns `(collection,
  created_bool)`. Raises `ValueError` for parent-covered-child case.
- `_bootstrap_ethos_memory()` -- unconditionally reads
  `Path.home() / ".punt-labs" / "ethos" / "identities"`. No parameter
  for the identities directory -- this prevents accidental writes to
  repo-level identities (which are read-only, managed by ethos
  submodule/bundle). Returns `(created, updated, already_set,
  skipped_bool)`. Sets `skipped_bool=True` if the global identities
  directory does not exist.
- `_write_project_config(directory)` -- writes
  `.punt-labs/quarry/config.md` with default frontmatter. Idempotent:
  does not overwrite existing file.

### JSON Output

When `--json` is set, `enable_cmd` calls `dataclasses.asdict(result)`
and passes the resulting dict to the existing `_emit` helper, which
calls `json.dumps`. This matches the `--json` pattern used by other
commands in `__main__.py`.

### Idempotency

Running `quarry enable` twice in the same directory:

- Registration: already exists, reused.
- Config file: already exists, not overwritten.
- Ethos ext files: `_write_ethos_ext_session_context` returns
  `"already_set"`.
- Output: reports what was already configured.

---

## 2. `quarry disable` Command

### CLI Interface

```text
quarry disable [DIRECTORY] [--keep-data]
```

- `DIRECTORY`: defaults to `.` (cwd).
- `--keep-data`: keep indexed chunks in LanceDB (default: delete
  project files and captures collections).

### Behavior

1. Resolve `DIRECTORY` to absolute path.
2. Find the registration covering this directory (exact or parent).
3. If no registration found, print error and exit 1.
4. Deregister the directory (removes file records from registry).
5. Unless `--keep-data`:
   a. Delete all chunks in the project files collection.
   b. Delete all chunks in the `<collection>-captures` collection.
6. Agent memory collections (`memory-<handle>`) are **not** touched.
   They belong to the agent, not the project.
7. Remove `.punt-labs/quarry/config.md` from the project directory.
8. Remove `.punt-labs/quarry/` directory if empty after config file
   deletion. Leave `.punt-labs/` untouched (shared with ethos).
9. Print summary.

### Exact Code Changes

**File: `src/quarry/__main__.py`**

```python
@app.command(name="disable")
@_cli_errors
def disable_cmd(
    directory: Annotated[
        Path,
        typer.Argument(help="Project directory to disable (default: cwd)"),
    ] = Path("."),
    keep_data: Annotated[
        bool,
        typer.Option("--keep-data", help="Keep indexed data in LanceDB"),
    ] = False,
) -> None:
    """Disable quarry knowledge capture for a project directory."""
```

Implementation calls `quarry.enable.disable_project()`, which raises
`ValueError` when no registration covers the directory. The CLI layer
catches `ValueError` and prints the message with exit code 1.

### `disable_project` Pseudocode

```python
def disable_project(directory: Path, *, keep_data: bool = False) -> DisableResult:
    conn = open_registry(settings.registry_path)
    try:
        # 1. Find the covering registration.
        collection = _collection_for_cwd_conn(conn, str(directory))
        if collection is None:
            msg = f"no registration covers {directory}"
            raise ValueError(msg)

        # 2. Derive captures collection name.
        captures_collection = f"{collection}-captures"

        # 3. Deregister by collection name (not directory path).
        deregister_directory(conn, collection)

        # 4. Delete chunks unless keep_data.
        deleted_chunks = 0
        if not keep_data:
            deleted_chunks += _delete_collection_chunks(collection)
            deleted_chunks += _delete_collection_chunks(captures_collection)

        # 5. Remove config file.
        config_path = directory / ".punt-labs" / "quarry" / "config.md"
        config_removed = False
        if config_path.exists():
            config_path.unlink()
            config_removed = True

        # 6. Clean up empty directory.
        quarry_dir = directory / ".punt-labs" / "quarry"
        if quarry_dir.is_dir() and not any(quarry_dir.iterdir()):
            quarry_dir.rmdir()
        # Leave .punt-labs/ untouched (shared with ethos).

        return DisableResult(
            directory=str(directory),
            collection=collection,
            captures_collection=captures_collection,
            deleted_chunks=deleted_chunks,
            config_removed=config_removed,
        )
    finally:
        conn.close()
```

**File: `src/quarry/enable.py`**

```python
@dataclass(frozen=True)
class DisableResult:
    """Result of quarry disable for a single project directory."""
    directory: str
    collection: str
    captures_collection: str
    deleted_chunks: int
    config_removed: bool
```

---

## 3. SessionStart Hook Changes

### Current Behavior (broken)

`handle_session_start` in `hooks.py`:

1. Reads cwd from payload.
2. Checks if cwd has an existing registration (exact match only).
3. If no match: derives a collection name and calls
   `register_directory()`.
4. If cwd is a child of an existing registration,
   `register_directory()` raises `ValueError` ("directory already
   covered by parent registration"). The exception propagates and
   the hook returns `{}` (fail-open), but the user gets no quarry
   context.

### New Behavior

1. Reads cwd from payload.
2. Check hook config (`load_hook_config`). If `session_sync` is
   disabled, return early.
3. Look for an existing registration covering cwd:
   a. **Exact match**: use that collection.
   b. **Parent match** (walk up): use the parent's collection.
      This is the child-directory fix.
   c. **No match**: check whether any existing registration is a
      *descendant* of the candidate directory. If so, skip
      auto-registration and log a warning: "Existing child
      registrations found; skipping auto-register to prevent
      subsumption. Run `quarry enable <parent>` to explicitly register
      the parent." If no descendants exist, auto-register cwd with
      `_unique_collection_name`. This preserves auto-registration per
      CEO feedback while preventing subsumption data loss.
4. Determine the captures collection: `<collection>-captures`.
5. Fire background sync (unchanged).
6. Return `additionalContext` with collection name, captures
   collection, and sync status.

### Exact Code Changes

**File: `src/quarry/hooks.py`**

Replace the body of `handle_session_start` with logic that uses
`_collection_for_cwd` first, falling back to auto-register only when
no coverage exists:

```python
def handle_session_start(payload: dict[str, object]) -> dict[str, object]:
    from quarry.sync_registry import (
        list_registrations,
        open_registry,
        register_directory,
    )

    cwd_obj = payload.get("cwd")
    cwd = cwd_obj if isinstance(cwd_obj, str) else ""
    if not cwd:
        return {}

    config = load_hook_config(cwd)
    if not config.session_sync:
        return {}

    directory = Path(cwd).resolve()
    if not directory.is_dir():
        return {}

    settings = _resolve_settings()
    conn = open_registry(settings.registry_path)
    try:
        # Step 1: Walk up from cwd to find covering registration.
        collection = _collection_for_cwd_conn(conn, str(directory))

        if collection is None:
            # Step 2: No coverage -- check for descendant registrations
            # before auto-registering. A parent registration would
            # subsume existing child registrations, causing data loss.
            registrations = list_registrations(conn)
            has_children = any(
                _is_ancestor_of(directory, Path(r.directory))
                for r in registrations
            )
            if has_children:
                logger.warning(
                    "session-start: existing child registrations found "
                    "under %s; skipping auto-register to prevent "
                    "subsumption. Run 'quarry enable %s' to explicitly "
                    "register the parent.",
                    directory, directory,
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": (
                            f"Quarry: child registrations exist under {directory}. "
                            "Auto-register skipped to prevent subsumption. "
                            f"Run 'quarry enable {directory}' to register the parent."
                        ),
                    },
                }
            else:
                collection = _unique_collection_name(conn, directory)
                register_directory(conn, directory, collection)
                logger.info(
                    "session-start: auto-registered %s as '%s'",
                    directory, collection,
                )

        captures_collection = f"{collection}-captures"
        sync_status = _sync_in_background()
        # ... build and return context (unchanged structure)
    finally:
        conn.close()
```

The key change: `_collection_for_cwd` is called first. If cwd is a
child of a registered parent, it returns the parent's collection.
Auto-register only fires when no coverage exists at all.

**New helper: `_collection_for_cwd_conn`**

Extracted from `_collection_for_cwd` to accept an open `sqlite3.Connection`
instead of opening its own. Avoids a second connection open:

```python
def _collection_for_cwd_conn(
    conn: sqlite3.Connection,
    cwd: str,
) -> str | None:
    """Resolve the registered collection for cwd using an open connection.

    Walks up from cwd to find a registered parent or exact match.
    """
    from quarry.sync_registry import list_registrations

    registrations = list_registrations(conn)
    if not registrations:
        return None

    reg_map = {r.directory: r.collection for r in registrations}
    current = Path(cwd).resolve()
    while True:
        key = str(current)
        if key in reg_map:
            return reg_map[key]
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None
```

Refactor `_collection_for_cwd` to use `_collection_for_cwd_conn`:

```python
def _collection_for_cwd(cwd: str) -> str | None:
    if not cwd:
        return None
    settings = _resolve_settings()
    conn = open_registry(settings.registry_path)
    try:
        return _collection_for_cwd_conn(conn, cwd)
    finally:
        conn.close()
```

### Hook Routing: Which Collection Gets What

| Hook | Collection | Content |
|------|-----------|---------|
| SessionStart | `<name>` (file sync) | Triggers background sync of project files |
| PostToolUse (WebFetch) | `<name>-captures` | Fetched web pages |
| PreCompact | `<name>-captures` | Session transcripts |

**Note on transcript `memory_type`:** Transcripts are stored with
`memory_type=""` because they are permanent knowledge, not agent
memories. Do not add `memory_type` to transcript ingestion -- it would
trigger temporal decay on session history, causing old transcripts to
rank lower over time despite being equally valuable reference material.

**File: `src/quarry/hooks.py`**

Change `handle_post_web_fetch`:

```python
# Before:
collection = _collection_for_cwd(cwd) or _WEB_CAPTURES_FALLBACK

# After:
base_collection = _collection_for_cwd(cwd)
collection = f"{base_collection}-captures" if base_collection else _WEB_CAPTURES_FALLBACK
```

Change `handle_pre_compact`:

```python
# Before:
collection = _collection_for_cwd(cwd) or _SESSION_NOTES_FALLBACK

# After:
base_collection = _collection_for_cwd(cwd)
collection = f"{base_collection}-captures" if base_collection else _SESSION_NOTES_FALLBACK
```

The fallback constants `_WEB_CAPTURES_FALLBACK` and
`_SESSION_NOTES_FALLBACK` remain for sessions with no covering
registration. This preserves backward compatibility for users who
have not run `quarry enable` and whose sessions start outside any
registered directory.

---

## 4. Ethos Bootstrapping

### When

`quarry enable` calls `_bootstrap_ethos_memory()`. This is the only
entry point -- `quarry install` does NOT bootstrap agent memory
collections (it only writes `session_context` into existing
`quarry.yaml` files, per DES-019).

### Where

Unconditionally scans `Path.home() / ".punt-labs" / "ethos" / "identities"`
(global identities only). The function accepts no path parameter to
prevent accidental writes to repo-level identities, which are read-only
(managed by ethos submodule/bundle) and do not have `.ext/` directories.

**Note:** If `quarry.yaml` already exists, its `memory_collection`
value is not validated or updated. If the existing value is incorrect,
it must be manually edited. The function only adds missing files and
appends `session_context` to existing ones.

### Logic

For each `<handle>.yaml` in the identities directory:

1. Check if `<handle>.ext/` exists. Create it if not.
2. Check if `<handle>.ext/quarry.yaml` exists.
3. If not, create it with:

   ```yaml
   memory_collection: memory-<handle>
   ```

4. Run `_write_ethos_ext_session_context(quarry_yaml, handle)` to
   append `session_context` if missing. This reuses the existing
   function from `doctor.py`.

### Exact Code Changes

**File: `src/quarry/enable.py`**

```python
_GLOBAL_IDENTITIES = Path.home() / ".punt-labs" / "ethos" / "identities"


def _bootstrap_ethos_memory() -> tuple[list[str], list[str], list[str], bool]:
    """Create quarry.yaml ext files and write session_context.

    Unconditionally reads the global identities directory. Repo-level
    identities are read-only and must not be modified.

    Returns (created, updated, already_set, skipped) where skipped is
    True when the global identities directory does not exist.
    """
    from quarry.doctor import _write_ethos_ext_session_context

    if not _GLOBAL_IDENTITIES.is_dir():
        return [], [], [], True

    created: list[str] = []
    updated: list[str] = []
    already_set: list[str] = []

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

        result = _write_ethos_ext_session_context(quarry_yaml, handle)
        if result == "updated":
            updated.append(handle)
        elif result == "already_set":
            already_set.append(handle)

    return created, updated, already_set, False
```

---

## 5. Config File Template

### Path

`<project-root>/.punt-labs/quarry/config.md`

### Content

```markdown
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
---

# Quarry Project Configuration

This file controls quarry's passive knowledge capture for this project.
Set any field to `false` to disable that capture type.

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
```

### Creation Logic

`_write_project_config` creates the parent directories and writes the
file. If the file already exists, it is not overwritten (preserving
user customizations).

---

## 6. Interaction with `quarry register` / `quarry deregister`

### `quarry register`

Unchanged. Users can still manually register directories. If a
directory was registered via `quarry register` before `quarry enable`
was available, `quarry enable` detects the existing registration and
reuses it (step 2a).

### `quarry deregister`

Unchanged. Removes the registration and optionally deletes indexed
data. Does NOT remove the captures collection or config file -- those
are `quarry disable` responsibilities.

### Relationship

`quarry enable` is a superset of `quarry register`. It calls
`register_directory` internally for the file sync collection, then
additionally sets up captures routing and ethos bootstrapping.

`quarry disable` is a superset of `quarry deregister`. It calls
`deregister_directory` internally, then additionally cleans up the
captures collection and config file.

Users who only use `quarry register` / `quarry deregister` continue to
work exactly as before. Their web captures go to the fallback
collections; they don't get per-project captures separation or ethos
bootstrapping.

---

## 7. `_collection_for_cwd` Fix Detail

### Current Bug

`handle_session_start` calls `_find_registration` which does exact
match only on `reg.directory == directory`. If the user opens a
session in `/home/user/quarry/src/` but quarry is registered at
`/home/user/quarry/`, the exact match fails, and the hook calls
`register_directory` which raises because the child is covered by the
parent.

### Fix

Replace the `_find_registration` + `register_directory` pattern with
`_collection_for_cwd_conn` which walks up from cwd to find any
covering registration. Auto-register only when no coverage exists.

The walk-up logic is already implemented in `_collection_for_cwd` at
line 286 of `hooks.py`. The fix extracts the logic to accept a
connection parameter and uses it in `handle_session_start`.

---

## 8. `quarry doctor` Changes

### `check_environment` Output

Add a new check: `_check_enable_status` that reports whether the cwd
has quarry enabled. Gates on two conditions only: (a) a covering
registration exists, and (b) the config file is present. Does **not**
check whether the captures collection exists in LanceDB -- captures
collections are created lazily on first ingestion and may not exist yet
for a freshly enabled project.

```python
def _check_enable_status(registry_path: Path, cwd: str) -> CheckResult:
    """Check if the cwd has quarry enable'd."""
```

Returns:

- Pass: "enabled (collection: quarry, captures: quarry-captures)"
- Fail (not required): "not enabled -- run 'quarry enable'"

This check is informational (`required=False`).

### `_check_orphaned_captures`

Add a second doctor check that scans LanceDB collections matching
`*-captures` and verifies the base collection has a covering
registration:

```python
def _check_orphaned_captures(registry_path: Path, db_path: Path) -> CheckResult:
    """Report captures collections whose base collection has no registration."""
```

For each `<name>-captures` collection found in the chunks table, check
whether any registration maps to `<name>`. If not, report it as an
orphan. This catches cases where `quarry deregister` removed the
file-sync registration but left the captures collection behind (since
`deregister` does not clean up captures -- only `quarry disable` does).

Returns:

- Pass: "no orphaned captures collections"
- Warning: "orphaned captures: foo-captures, bar-captures (no registration for foo, bar)"

This check is informational (`required=False`).

---

## 9. Rejected Alternatives

### Remove auto-register entirely (superseded by CEO feedback)

Early drafts of this design (and the original PR/FAQ Q9) proposed
removing auto-register from SessionStart and requiring explicit `quarry
enable`. CEO feedback explicitly overruled this: "All three knowledge
types should be captured without the user thinking about it."
Auto-register stays; `quarry enable` adds captures routing and ethos
bootstrapping on top. The hook fix addresses the child-directory crash
by walking up to the parent registration, not by removing auto-register.

### Per-project memory collections

Agent memory scoped to `memory-<handle>-<project>` instead of global
`memory-<handle>`. Rejected: CEO feedback says "agent memory is
agent-wide, not project-wide." An agent's knowledge about deployment
procedures learned in repo A should be available when working in
repo B.

### Write captures to the project files collection

Instead of a separate `<name>-captures` collection. Rejected: the
original problem statement says "searches for code in this repo don't
return last week's Hacker News article." Separation is the purpose.

### Migrate data from `web-captures` / `session-notes`

CEO feedback: "Old stale collections are already cleaned up. Don't
design migration logic."

---

## 10. Test Cases

### Unit Tests: `tests/test_enable.py` (new file)

**T1: enable registers a new directory.**
Setup: empty registry, tmp directory.
Call `enable_project(tmp_dir)`.
Assert: registration exists, collection name matches dir leaf,
`EnableResult.created_registration` is True.

**T2: enable is idempotent on already-registered directory.**
Setup: register tmp_dir as "foo".
Call `enable_project(tmp_dir)`.
Assert: collection is "foo", `created_registration` is False.

**T3: enable on child of registered parent raises ValueError.**
Setup: register `/home/user/project` as collection "project".
Call `enable_project(Path("/home/user/project/src"))`.
Assert: raises `ValueError` with message containing "already covered by
the registration at" and the parent path and collection name.

**T3b: CLI enable on child of registered parent exits 1.**
Setup: register `/home/user/project`.
Use typer's `CliRunner`.
Call `["enable", "/home/user/project/src"]`.
Assert: exit code 1, stderr/output contains "already covered".

**T4: enable with --collection override.**
Setup: empty registry.
Call `enable_project(tmp_dir, collection_override="custom")`.
Assert: registration uses "custom" as collection name.

**T5: enable creates config file.**
Setup: tmp_dir with no `.punt-labs/quarry/config.md`.
Call `enable_project(tmp_dir)`.
Assert: config file exists, contains `auto_capture:` block.

**T6: enable does not overwrite existing config file.**
Setup: tmp_dir with custom config.md content.
Call `enable_project(tmp_dir)`.
Assert: config file content unchanged.

**T7: enable creates ethos ext quarry.yaml files.**
Setup: mock identities dir with `claude.yaml`, `rmh.yaml`, no .ext dirs.
Call `_bootstrap_ethos_memory()` (with monkeypatched `_GLOBAL_IDENTITIES`).
Assert: `claude.ext/quarry.yaml` and `rmh.ext/quarry.yaml` created
with `memory_collection: memory-claude` and `memory_collection: memory-rmh`.

**T7b: existing quarry.yaml with wrong memory_collection is not modified.**
Setup: mock identities dir with `claude.yaml`, pre-existing
`claude.ext/quarry.yaml` containing `memory_collection: wrong-name`.
Call `_bootstrap_ethos_memory()`.
Assert: `claude.ext/quarry.yaml` still contains `memory_collection: wrong-name`
(the value is not validated or updated).

**T8: enable skips ethos when identities dir missing.**
Call `enable_project(tmp_dir)` with no ethos installed.
Assert: `EnableResult.ethos_skipped` is True, no crash.

**T9: enable derives captures collection name correctly.**
Call `enable_project(tmp_dir)`.
Assert: `EnableResult.captures_collection == f"{collection}-captures"`.

**T10: disable removes registration.**
Setup: enable a directory, then disable it.
Assert: registration gone, `DisableResult.collection` correct.

**T11: disable removes config file.**
Setup: enable a directory (creates config), then disable.
Assert: `.punt-labs/quarry/config.md` removed.

**T12: disable with --keep-data preserves LanceDB data.**
Setup: enable, ingest a file, then disable with keep_data=True.
Assert: chunks still exist in LanceDB.

**T13: disable preserves agent memory collections.**
Setup: enable, remember something in `memory-claude`, disable.
Assert: `memory-claude` collection still has data.

**T14: disable on unregistered directory returns error.**
Call `disable_project(tmp_dir)` with no registration.
Assert: raises ValueError.

### Unit Tests: `tests/test_hooks.py` (existing, extend)

**T15: session-start on child directory uses parent collection.**
Setup: register `/parent` as "proj".
Payload: `{"cwd": "/parent/child"}`.
Call `handle_session_start(payload)`.
Assert: context mentions collection "proj", no ValueError raised.

**T16: session-start on unregistered directory auto-registers.**
Setup: empty registry.
Payload: `{"cwd": "/new/project"}`.
Call `handle_session_start(payload)`.
Assert: registration created, context returned.

**T16b: session-start on parent of existing child registrations skips auto-register.**
Setup: register `/parent/child-a` and `/parent/child-b`.
Payload: `{"cwd": "/parent"}`.
Call `handle_session_start(payload)`.
Assert: no new registration created for `/parent`, warning logged
containing "existing child registrations found" and "skipping
auto-register to prevent subsumption".

**T17: web-fetch routes to captures collection.**
Setup: register `/project` as "proj".
Payload: `{"cwd": "/project", ...}` with valid URL.
Mock `ingest_content` to capture the collection argument.
Call `handle_post_web_fetch(payload)`.
Assert: collection argument is "proj-captures".

**T18: pre-compact routes to captures collection.**
Setup: register `/project` as "proj".
Payload with valid transcript.
Mock `_spawn_background_ingest` to capture the collection argument.
Call `handle_pre_compact(payload)`.
Assert: collection argument is "proj-captures".

**T19: web-fetch with no registration uses fallback.**
Setup: empty registry.
Payload with valid URL.
Call `handle_post_web_fetch(payload)`.
Assert: collection is `_WEB_CAPTURES_FALLBACK`.

**T20: pre-compact with no registration uses fallback.**
Setup: empty registry.
Payload with valid transcript.
Call `handle_pre_compact(payload)`.
Assert: collection is `_SESSION_NOTES_FALLBACK`.

### Unit Tests: `tests/test_enable_cli.py` (new file)

**T21: `quarry enable` CLI happy path.**
Use typer's `CliRunner`.
Call `["enable", str(tmp_dir)]`.
Assert: exit code 0, output mentions collection name.

**T22: `quarry enable --collection custom` CLI.**
Call `["enable", str(tmp_dir), "--collection", "custom"]`.
Assert: exit code 0, output mentions "custom".

**T23: `quarry disable` CLI happy path.**
Enable first, then call `["disable", str(tmp_dir)]`.
Assert: exit code 0.

**T24: `quarry disable` on unregistered directory.**
Call `["disable", str(tmp_dir)]`.
Assert: exit code 1, error message printed.

**T25: `quarry enable --json` outputs structured data.**
Call `["--json", "enable", str(tmp_dir)]`.
Assert: stdout is valid JSON with expected fields.

---

## 11. Implementation Order

1. **`src/quarry/enable.py`** -- new module with `enable_project`,
   `disable_project`, supporting dataclasses and helpers.
2. **`src/quarry/hooks.py`** -- extract `_collection_for_cwd_conn`,
   fix `handle_session_start`, update web-fetch and pre-compact
   routing.
3. **`src/quarry/__main__.py`** -- add `enable` and `disable`
   commands, import from `enable.py`.
4. **`src/quarry/doctor.py`** -- add `_check_enable_status` and
   `_check_orphaned_captures`.
5. **`tests/test_enable.py`** -- T1-T14.
6. **`tests/test_hooks.py`** -- T15-T20.
7. **`tests/test_enable_cli.py`** -- T21-T25.

---

## 12. Write Set

| File | Action |
|------|--------|
| `src/quarry/enable.py` | New: enable/disable logic |
| `src/quarry/hooks.py` | Modify: fix session-start, route captures |
| `src/quarry/__main__.py` | Modify: add enable/disable commands |
| `src/quarry/doctor.py` | Modify: add enable status check, orphaned captures check |
| `tests/test_enable.py` | New: enable/disable unit tests |
| `tests/test_hooks.py` | Modify: add hook routing tests |
| `tests/test_enable_cli.py` | New: CLI integration tests |

---

## Implementation Spec

This section refines the design above into a mechanical implementation
plan. Every function signature, import path, and test fixture is
specified exactly. Implementation should require no architectural
judgment.

### S1. Module: `src/quarry/enable.py` (new)

All enable/disable logic lives here. Heavy imports (lancedb, sync
registry) are top-level -- this is not a hook entry point, so startup
latency does not matter.

```python
"""Enable and disable quarry knowledge capture for project directories."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)
```

#### S1.1 Result dataclasses

```python
@dataclass(frozen=True)
class EnableResult:
    """Result of enabling quarry for a project directory."""

    directory: str
    collection: str
    captures_collection: str
    memory_collections: list[str] = field(default_factory=list)
    config_path: str = ""
    created_registration: bool = False
    ethos_skipped: bool = False
    ethos_updated: list[str] = field(default_factory=list)
    ethos_already_set: list[str] = field(default_factory=list)
    ethos_created: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DisableResult:
    """Result of disabling quarry for a project directory."""

    directory: str
    collection: str
    captures_collection: str
    deleted_chunks: int = 0
    config_removed: bool = False
```

Note: `EnableResult.memory_collections` uses `field(default_factory=list)`
because mutable default arguments are forbidden on frozen dataclasses.

#### S1.2 `enable_project`

```python
def enable_project(
    directory: Path,
    collection_override: str = "",
) -> EnableResult:
```

Implementation steps (refer to design section 1 for rationale):

1. Validate `directory.is_dir()` -- raise `ValueError` if not.
2. Call `_resolve_or_register(conn, directory, collection_override)`.
3. Derive `captures_collection = f"{collection}-captures"`.
4. Call `_bootstrap_ethos_memory()`.
5. Call `_write_project_config(directory)`.
6. Build and return `EnableResult`.

Registry interaction uses:

- `from quarry.config import load_settings, resolve_db_paths`
- `from quarry.sync_registry import open_registry, list_registrations,
  register_directory, get_registration`
- `from quarry.hooks import _collection_for_cwd_conn` (the new helper)

#### S1.3 `_resolve_or_register`

```python
def _resolve_or_register(
    conn: sqlite3.Connection,
    directory: Path,
    collection_override: str,
) -> tuple[str, bool]:
    """Find existing registration or create one.

    Returns (collection_name, created_bool).
    Raises ValueError for parent-covered-child case.
    """
```

Implementation:

1. Import `_collection_for_cwd_conn` from `quarry.hooks`.
2. Call `_collection_for_cwd_conn(conn, str(directory))`.
3. If found and `directory` exactly matches the registration's directory,
   return `(collection, False)` -- exact match, reuse.
4. If found but `directory` is a child of the registration's directory,
   raise `ValueError` with the message from design section 1 step 2b.
   To distinguish child-match from exact-match: compare
   `str(directory)` against the registration's directory by iterating
   `list_registrations(conn)` and checking which one matched.
5. If not found, derive name via `_unique_collection_name(conn, directory)`
   from `quarry.hooks` (or `collection_override` if non-empty), call
   `register_directory(conn, directory, name)`, return `(name, True)`.

**Important**: `_unique_collection_name` is currently in `hooks.py`.
It should stay there (no module move in this change) -- `enable.py`
imports it. The function is a pure helper with no hook-specific logic.

#### S1.4 `_bootstrap_ethos_memory`

Signature and implementation exactly as specified in design section 4.
The function imports `_write_ethos_ext_session_context` from
`quarry.doctor` -- this is an existing function (line 778 of
`doctor.py`).

```python
_GLOBAL_IDENTITIES = Path.home() / ".punt-labs" / "ethos" / "identities"


def _bootstrap_ethos_memory() -> tuple[list[str], list[str], list[str], bool]:
    """Create quarry.yaml ext files and write session_context.

    Returns (created, updated, already_set, skipped).
    """
```

The monkeypatch target for testing is `quarry.enable._GLOBAL_IDENTITIES`.

#### S1.5 `_write_project_config`

```python
_CONFIG_TEMPLATE = """\
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
---

# Quarry Project Configuration

This file controls quarry's passive knowledge capture for this project.
Set any field to `false` to disable that capture type.

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
"""


def _write_project_config(directory: Path) -> str:
    """Write .punt-labs/quarry/config.md. Idempotent: no overwrite.

    Returns the config file path as a string.
    """
```

Creates `directory / ".punt-labs" / "quarry"` with `mkdir(parents=True,
exist_ok=True)`. Writes `_CONFIG_TEMPLATE` only if the file does not
exist.

#### S1.6 `disable_project`

```python
def disable_project(
    directory: Path,
    *,
    keep_data: bool = False,
) -> DisableResult:
```

Implementation uses:

- `from quarry.hooks import _collection_for_cwd_conn`
- `from quarry.sync_registry import open_registry, deregister_directory`
- `from quarry.database import get_db, delete_collection as
  db_delete_collection`

Steps:

1. Open registry, call `_collection_for_cwd_conn(conn, str(directory))`.
2. If `None`, raise `ValueError(f"no registration covers {directory}")`.
3. Derive `captures_collection = f"{collection}-captures"`.
4. Call `deregister_directory(conn, collection)` -- this removes the
   registration row and returns document names.
5. If not `keep_data`: call `db_delete_collection(db, collection)` and
   `db_delete_collection(db, captures_collection)`. Sum the deleted
   counts.
6. Remove `directory / ".punt-labs" / "quarry" / "config.md"` if it
   exists.
7. Remove `directory / ".punt-labs" / "quarry"` if empty after config
   removal. Leave `.punt-labs/` untouched.
8. Return `DisableResult`.

### S2. Module: `src/quarry/hooks.py` (modify)

#### S2.1 Extract `_collection_for_cwd_conn`

New function, placed immediately before `_collection_for_cwd` (before
line 286):

```python
def _collection_for_cwd_conn(
    conn: sqlite3.Connection,
    cwd: str,
) -> str | None:
    """Resolve the registered collection for cwd using an open connection.

    Walk up from cwd to find a registered parent or exact match.
    """
    from quarry.sync_registry import list_registrations  # noqa: PLC0415

    registrations = list_registrations(conn)
    if not registrations:
        return None

    reg_map = {r.directory: r.collection for r in registrations}
    current = Path(cwd).resolve()
    while True:
        key = str(current)
        if key in reg_map:
            return reg_map[key]
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None
```

Refactor `_collection_for_cwd` to delegate:

```python
def _collection_for_cwd(cwd: str) -> str | None:
    if not cwd:
        return None
    settings = _resolve_settings()
    conn = open_registry(settings.registry_path)
    try:
        return _collection_for_cwd_conn(conn, cwd)
    finally:
        conn.close()
```

The `open_registry` import is already at the top of
`_collection_for_cwd`; move it to the module-level deferred import
block or keep it inline -- either works since this is not a hot path.

#### S2.2 Fix `handle_session_start`

Replace the body of `handle_session_start` (lines 208-279) with the
logic from design section 3. Key changes:

1. Import `_is_ancestor_of` from `quarry.sync_registry` (add to the
   existing deferred import block at line 216).
2. Replace `_find_registration` call with `_collection_for_cwd_conn`.
3. Add the descendant-check guard before auto-registration.
4. Add captures collection to the `additionalContext`.

The exact code is in design section 3. The `_find_registration` function
stays in the module (existing tests use it, and it costs nothing to
keep). No behavioral change for callers other than `handle_session_start`.

#### S2.3 Route captures to `<name>-captures`

In `handle_post_web_fetch` (line 395):

```python
# Before:
collection = _collection_for_cwd(cwd) or _WEB_CAPTURES_FALLBACK

# After:
base_collection = _collection_for_cwd(cwd)
collection = f"{base_collection}-captures" if base_collection else _WEB_CAPTURES_FALLBACK
```

In `handle_pre_compact` (line 725):

```python
# Before:
collection = _collection_for_cwd(cwd) or _SESSION_NOTES_FALLBACK

# After:
base_collection = _collection_for_cwd(cwd)
collection = f"{base_collection}-captures" if base_collection else _SESSION_NOTES_FALLBACK
```

### S3. Module: `src/quarry/__main__.py` (modify)

#### S3.1 Update `_COMMAND_ORDER`

Add `"enable"` and `"disable"` after `"sync"` and before `"optimize"`:

```python
_COMMAND_ORDER: list[str] = [
    # Product commands
    "find",
    "ingest",
    "show",
    "remember",
    "status",
    "use",
    "delete",
    "register",
    "deregister",
    "sync",
    "enable",
    "disable",
    "optimize",
    ...
]
```

#### S3.2 Add `enable_cmd`

Place after `sync_cmd` and before `optimize_cmd`:

```python
@app.command(name="enable")
@_cli_errors
def enable_cmd(
    directory: Annotated[
        Path,
        typer.Argument(help="Project directory to enable (default: cwd)"),
    ] = Path("."),
    collection: Annotated[
        str,
        typer.Option("--collection", "-c", help="Override collection name"),
    ] = "",
) -> None:
    """Enable quarry knowledge capture for a project directory."""
    from quarry.enable import enable_project  # noqa: PLC0415

    resolved = directory.resolve()
    try:
        result = enable_project(resolved, collection_override=collection)
    except ValueError as exc:
        _emit({"error": str(exc)}, "")
        err_console.print(f"Error: {exc}", style="red")
        raise typer.Exit(code=1) from None

    import dataclasses  # noqa: PLC0415

    lines: list[str] = [
        f"Enabled quarry for {result.directory}",
        f"  Collection: {result.collection}",
        f"  Captures: {result.captures_collection}",
    ]
    if result.config_path:
        lines.append(f"  Config: {result.config_path}")
    if result.ethos_skipped:
        lines.append("  Ethos: not installed (agent memory skipped)")
    else:
        if result.ethos_created:
            lines.append(
                f"  Ethos created: {', '.join(result.ethos_created)}"
            )
        if result.ethos_updated:
            lines.append(
                f"  Ethos updated: {', '.join(result.ethos_updated)}"
            )
        if result.memory_collections:
            lines.append(
                f"  Memory collections: {', '.join(result.memory_collections)}"
            )

    _emit(dataclasses.asdict(result), "\n".join(lines))
```

The `ValueError` catch uses `raise typer.Exit(code=1) from None`
(not `from exc`) to suppress the traceback in non-verbose mode,
matching the existing pattern in `__main__.py`.

#### S3.3 Add `disable_cmd`

Place immediately after `enable_cmd`:

```python
@app.command(name="disable")
@_cli_errors
def disable_cmd(
    directory: Annotated[
        Path,
        typer.Argument(help="Project directory to disable (default: cwd)"),
    ] = Path("."),
    keep_data: Annotated[
        bool,
        typer.Option("--keep-data", help="Keep indexed data in LanceDB"),
    ] = False,
) -> None:
    """Disable quarry knowledge capture for a project directory."""
    from quarry.enable import disable_project  # noqa: PLC0415

    resolved = directory.resolve()
    try:
        result = disable_project(resolved, keep_data=keep_data)
    except ValueError as exc:
        _emit({"error": str(exc)}, "")
        err_console.print(f"Error: {exc}", style="red")
        raise typer.Exit(code=1) from None

    import dataclasses  # noqa: PLC0415

    lines: list[str] = [f"Disabled quarry for {result.directory}"]
    if result.deleted_chunks > 0:
        lines.append(f"  Deleted {result.deleted_chunks} chunks")
    if result.config_removed:
        lines.append("  Config file removed")

    _emit(dataclasses.asdict(result), "\n".join(lines))
```

### S4. Module: `src/quarry/doctor.py` (modify)

#### S4.1 `_check_enable_status`

Add after `_check_sync_directories` (line 407):

```python
def _check_enable_status(registry_path: Path, cwd: str) -> CheckResult:
    """Check if the cwd has quarry enabled."""
    from quarry.hooks import _collection_for_cwd  # noqa: PLC0415

    collection = _collection_for_cwd(cwd)
    if collection is None:
        return CheckResult(
            name="Enable status",
            passed=False,
            message="not enabled -- run 'quarry enable'",
            required=False,
        )
    captures = f"{collection}-captures"
    config_path = Path(cwd) / ".punt-labs" / "quarry" / "config.md"
    config_exists = config_path.is_file()
    parts = [f"collection: {collection}, captures: {captures}"]
    if not config_exists:
        parts.append("config.md missing (run 'quarry enable')")
    return CheckResult(
        name="Enable status",
        passed=True,
        message=", ".join(parts),
        required=False,
    )
```

#### S4.2 `_check_orphaned_captures`

```python
def _check_orphaned_captures(
    registry_path: Path,
    db_path: Path,
) -> CheckResult:
    """Report captures collections whose base has no registration."""
    from quarry.database import get_db, list_collections as db_list_collections  # noqa: PLC0415
    from quarry.sync_registry import list_registrations, open_registry  # noqa: PLC0415

    if not db_path.exists() or not registry_path.exists():
        return CheckResult(
            name="Orphaned captures",
            passed=True,
            message="no data yet",
            required=False,
        )

    db = get_db(db_path)
    cols = db_list_collections(db)
    col_names = {
        c["collection"] for c in cols if isinstance(c.get("collection"), str)
    }

    conn = open_registry(registry_path)
    try:
        regs = list_registrations(conn)
    finally:
        conn.close()

    registered = {r.collection for r in regs}
    orphans: list[str] = []
    for name in sorted(col_names):
        if name.endswith("-captures"):
            base = name.removesuffix("-captures")
            if base not in registered:
                orphans.append(name)

    if orphans:
        return CheckResult(
            name="Orphaned captures",
            passed=True,
            message=f"orphaned: {', '.join(orphans)}",
            required=False,
        )
    return CheckResult(
        name="Orphaned captures",
        passed=True,
        message="no orphaned captures collections",
        required=False,
    )
```

#### S4.3 Add to `check_environment`

Add these two checks to the `all_results` list in `check_environment`
(line 1034), after `_check_sync_directories`:

```python
_check_enable_status(settings.registry_path, os.getcwd()),
_check_orphaned_captures(settings.registry_path, settings.lancedb_path),
```

Import `os` is already at the top of `doctor.py`.

### S5. MCP Tool Changes

No MCP tool changes are needed. The MCP server
(`src/quarry/mcp_server.py`) operates on collections by name. The
enable/disable commands modify the registry and create collections,
but the MCP tools (find, ingest, remember, delete, register,
deregister, sync) are collection-agnostic -- they accept collection
names as parameters and operate on them. The captures routing change
in hooks affects which collection name is passed to ingestion, but
the MCP tools themselves do not need modification.

### S6. Test Plan

All tests use pytest. Fixtures use `tmp_path` for isolated filesystem
state. Registry fixtures open a fresh SQLite database per test.
Monkeypatch `quarry.hooks._resolve_settings` and
`quarry.enable._GLOBAL_IDENTITIES` for isolation.

#### S6.1 `tests/test_enable.py` (new file)

Fixture:

```python
@pytest.fixture()
def registry(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    """Open a fresh registry and return (conn, registry_path)."""
    db_path = tmp_path / "registry.db"
    conn = open_registry(db_path)
    return conn, db_path
```

Tests T1-T14 as specified in design section 10, with these
clarifications:

- **T3**: The parent path and collection name must appear in the
  `ValueError` message. Use `pytest.raises(ValueError, match=...)`.
- **T7**: Monkeypatch `quarry.enable._GLOBAL_IDENTITIES` to a
  `tmp_path` subdirectory. Create `claude.yaml` and `rmh.yaml` as
  minimal YAML files (`agent: claude\n`). Assert `.ext/quarry.yaml`
  files contain exact `memory_collection: memory-<handle>` text.
- **T7b**: Pre-create `claude.ext/quarry.yaml` with
  `memory_collection: wrong-name`. Assert file content is unchanged
  after `_bootstrap_ethos_memory()`.
- **T8**: Monkeypatch `_GLOBAL_IDENTITIES` to a nonexistent path.
- **T10-T13**: These need a `_resolved_settings` mock that returns
  a settings object with valid `registry_path` and `lancedb_path`
  pointing to `tmp_path` subdirectories.

#### S6.2 `tests/test_hooks.py` (extend existing)

Tests T15-T20 as specified in design section 10.

For T15 (child directory uses parent collection): the key assertion
is that `handle_session_start` returns a context dict with the parent's
collection name and does NOT raise `ValueError`. This test proves the
child-directory crash is fixed.

For T16b (parent of children skips auto-register): register two child
directories, then call `handle_session_start` with the parent as cwd.
Assert no new registration is created and the returned context contains
the subsumption warning text.

For T17-T20 (captures routing): mock `ingest_content` / `ingest_url`
or `_spawn_background_ingest` and capture the `collection` argument
passed to them.

#### S6.3 `tests/test_enable_cli.py` (new file)

Tests T21-T25. Use `typer.testing.CliRunner` and the `app` from
`quarry.__main__`. Each test needs `_resolve_settings` monkeypatched
to use `tmp_path`-based paths.

For T25 (`--json` output): pass `["--json", "enable", str(tmp_dir)]`
and parse stdout as JSON. Assert the JSON contains keys: `directory`,
`collection`, `captures_collection`, `created_registration`.

### S7. Dependency Order

The implementation must proceed in this order because each step depends
on the previous:

1. **`src/quarry/hooks.py`** -- extract `_collection_for_cwd_conn`.
   This is a pure refactor with no behavioral change. All existing
   tests must continue passing. Run `make check`.

2. **`src/quarry/enable.py`** -- new module. Imports
   `_collection_for_cwd_conn` from hooks and
   `_write_ethos_ext_session_context` from doctor. Can be written
   immediately after step 1 since it depends on the new helper.

3. **`src/quarry/__main__.py`** -- add `enable` and `disable` commands.
   Imports from `enable.py`. Depends on step 2.

4. **`src/quarry/hooks.py`** -- fix `handle_session_start` and update
   captures routing. Depends on step 1 (`_collection_for_cwd_conn`
   exists). Import `_is_ancestor_of` from `sync_registry`.

5. **`src/quarry/doctor.py`** -- add `_check_enable_status` and
   `_check_orphaned_captures`. No dependencies on steps 1-4 beyond
   `_collection_for_cwd` (already exists).

6. **`tests/test_enable.py`** -- test enable/disable logic. Depends
   on steps 1-2.

7. **`tests/test_hooks.py`** -- extend with T15-T20. Depends on
   steps 1, 4.

8. **`tests/test_enable_cli.py`** -- CLI integration tests. Depends
   on steps 1-3.

After all steps: `make check` must pass with zero violations.

### S8. Codebase-Specific Notes

These observations come from reading the actual source. They prevent
implementation surprises.

1. **`register_directory` raises on child-of-parent**: The existing
   `register_directory` in `sync_registry.py` (line 50) already raises
   `ValueError("directory already covered by parent registration
   ...")`. The fix in `handle_session_start` prevents this path from
   being reached by checking `_collection_for_cwd_conn` first.

2. **`_find_registration` does exact match only**: The existing helper
   at hooks.py line 39 checks `reg.directory == directory`. This is
   why child directories crash -- the exact match fails, the code falls
   through to `register_directory`, which raises. The new
   `_collection_for_cwd_conn` does walk-up matching.

3. **`deregister_directory` takes collection name, not directory path**:
   The `sync_registry.deregister_directory(conn, collection)` signature
   takes a collection name as string. The `disable_project` function
   must find the collection name first via `_collection_for_cwd_conn`,
   then pass that to `deregister_directory`.

4. **`_is_ancestor_of` is private in `sync_registry.py`**: It must be
   imported with the private name. This is acceptable -- both hooks.py
   and sync_registry.py are internal modules.

5. **`_write_ethos_ext_session_context` signature**: Takes
   `(quarry_yaml: Path, handle: str)` and returns a string literal:
   `"updated"`, `"already_set"`, or `"no_collection"`. The
   `_bootstrap_ethos_memory` function handles all three return values.

6. **`load_hook_config` is stdlib-only**: It lives in `_stdlib.py`
   and uses no third-party imports. The `enable.py` module does not
   need to call it -- hook config is only relevant during hook
   execution, not during `quarry enable`.

7. **`_emit` handles both JSON and text**: In `__main__.py`, `_emit`
   checks the global `_json_output` flag. Commands must pass both a
   structured data dict and a text string. The `enable_cmd` and
   `disable_cmd` implementations follow this pattern exactly.

8. **No remote path for enable/disable**: These commands are local-only.
   There is no remote quarry server equivalent. The `proxy_config`
   check that other commands do is not needed here.
