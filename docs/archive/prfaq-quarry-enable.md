# PR/FAQ: quarry enable

**Status:** Historical — snapshot from May 2026. Superseded by implementation.

## CEO Override

Q9 and Q13 in the Internal FAQ originally proposed removing auto-registration
from the SessionStart hook and eliminating fallback capture collections.
Both are superseded by CEO feedback (see "CEO Feedback (Binding Constraints)"
in the design doc): **auto-registration is preserved.** Sessions in
unregistered directories continue to auto-register and capture knowledge
without requiring explicit `quarry enable`. The hook fix addresses the
child-directory crash by walking up to the covering parent registration,
not by removing auto-register.

## Press Release

**Quarry adds `enable` / `disable` commands to separate software installation from project activation**

Quarry, the local semantic search engine for AI agents, today ships
`quarry enable` and `quarry disable` -- two commands that give users
explicit control over which projects get passive knowledge capture and
how that capture is scoped.

Previously, `quarry install` installed the software *and* the
SessionStart hook auto-registered every working directory as a
collection. This created collisions (opening a session in a subdirectory
of an already-registered parent raised ValueError), mixed web captures
and session transcripts into file-sync collections, and left agent
memory collections uncreated because nobody ran the manual ethos
extension setup.

`quarry enable` solves this in one command. Run it inside a project
directory and quarry creates three scoped collections: one for
directory-synced project files, one for passive captures (web fetches
and session transcripts), and one per-agent memory collection
bootstrapped from the ethos identity registry. Run `quarry disable` to
remove the project's registrations and stop all passive capture.

"Before `quarry enable`, I had to manually create ethos extension files,
debug subsumption crashes when I opened a terminal in the wrong
subdirectory, and live with web research dumped into my code index.
Now I run one command per repo and everything is scoped correctly."
-- An engineer running 14 quarry-enabled repos across 3 machines.

`quarry enable` ships in quarry v1.16.0. Existing registrations
continue to work; `enable` is additive, not destructive.

## External FAQ

### Q1: What does `quarry enable` do that `quarry install` does not?

`quarry install` downloads the embedding model, creates the data
directory, configures MCP clients, and registers the system daemon. It
is machine-scoped: you run it once per machine.

`quarry enable` is project-scoped: you run it inside a repo directory.
It creates the directory registration for file sync, creates a
project-scoped captures collection for web fetches and session
transcripts, bootstraps agent memory collections from ethos identities,
and writes the per-project config at `.punt-labs/quarry/config.md`. It
also fixes the SessionStart hook so it never crashes on child
directories -- it detects the parent registration and reuses it.

### Q2: What happens if I already have registrations from the SessionStart hook?

Nothing breaks. `quarry enable` checks for existing registrations. If
your directory is already registered, it reuses that registration for
the file-sync collection and creates only the missing captures and
memory collections. The command is idempotent.

### Q3: What are the three collection types?

1. **Project files** (`<name>`): Directory-synced collection. Contains
   chunked and embedded copies of your source files. Updated by
   `quarry sync`. This is what the SessionStart hook currently creates.

2. **Captures** (`<name>-captures`): Web pages fetched during research
   and session transcripts captured at compaction. Not directory-synced
   -- content arrives via hooks only. Separated from project files so
   searches for "code in this repo" don't return last week's Hacker News
   article.

3. **Agent memory** (`memory-<handle>`): Per-agent collection scoped by
   ethos identity. Contains facts, observations, procedures, and
   opinions remembered by that agent across sessions. Created once per
   agent handle found in the ethos identity registry.

### Q4: Do I need ethos installed?

No. Without ethos, `quarry enable` creates the project files and
captures collections but skips agent memory. Agent memory is an
ethos-dependent feature. A warning is printed so you know what was
skipped.

### Q5: Can I disable specific capture types?

Yes. `quarry enable` writes `.punt-labs/quarry/config.md` with all
capture types enabled. Edit the frontmatter to disable individual hooks:

```yaml
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
---
```

Set any field to `false` to disable that capture type. This file already
works today -- `quarry enable` just creates it with explicit defaults
instead of relying on implicit all-enabled behavior.

### Q6: What does `quarry disable` do?

`quarry disable` removes the directory registration, deletes the
captures collection data, and removes the config file. Agent memory
collections are preserved -- they belong to the agent, not the project.
After disabling, the SessionStart hook sees no registration and does
not crash.

### Q7: How does this fix the child-directory crash?

The SessionStart hook calls `register_directory()`, which raises
`ValueError` when the directory is a child of an existing registration.
The fix changes the hook to call `_collection_for_cwd_conn` first, which
walks up the directory tree to find the covering parent registration and
uses that collection. Auto-register fires only when no coverage exists
at all -- not when a parent already covers the directory. No
`register_directory` call on child directories, no crash.

### Q8: Does this work with remote quarry servers?

Yes. `quarry enable` operates on the local registry. When a remote
server is configured via `quarry login`, the captures and memory
collections are created on the remote server lazily when the first chunk
is ingested (LanceDB collections are just values in the `collection`
column -- no explicit create step). Collection names are the same
regardless of local or remote mode.

## Internal FAQ

### Q9: What changes in the SessionStart hook?

**Superseded by CEO feedback -- auto-registration is preserved.** The
hook retains its auto-register behavior for sessions with no covering
registration. The change is how it handles child directories:

1. The hook calls `_collection_for_cwd_conn` to walk up from cwd and
   find a covering registration (exact or parent match).
2. If a parent registration covers the cwd, the hook uses the parent's
   collection. This is the child-directory crash fix -- previously the
   hook attempted `register_directory` which raised `ValueError`.
3. If no coverage exists at all, the hook auto-registers the cwd with
   `_unique_collection_name` + `register_directory`, exactly as before.
4. Before auto-registering, the hook checks whether any existing
   registration is a *descendant* of the candidate directory. If so, it
   skips auto-registration and logs a warning to prevent subsumption of
   child registrations. See F7 in the design doc.

This is not a behavioral change for the normal case. Users who open
sessions in registered directories or their children see the same
behavior. The only new case: auto-register is blocked when the cwd is a
parent of existing child registrations.

### Q10: What is the collection naming scheme?

| Type | Name pattern | Example |
|------|-------------|---------|
| Project files | `<dir-leaf>` | `quarry` |
| Captures | `<dir-leaf>-captures` | `quarry-captures` |
| Agent memory | `memory-<handle>` | `memory-claude` |

The project files collection uses the existing `_unique_collection_name`
logic (leaf name, disambiguated with parent if collision). Captures
appends `-captures`. Agent memory uses `memory-` prefix, matching the
convention in DES-018.

### Q11: How does ethos bootstrapping work?

`quarry enable` reads the global ethos identity registry at
`~/.punt-labs/ethos/identities/`. Per-repo identities at
`<repo>/.punt-labs/ethos/identities/` are read-only (managed by ethos
submodule/bundle) and are not modified.

For each identity that does not already have a `quarry.yaml` extension
file, `quarry enable` creates `<handle>.ext/quarry.yaml` containing:

```yaml
memory_collection: memory-<handle>
```

Then runs the existing `_configure_ethos_ext` logic to append
`session_context` to each file.

This closes the bootstrapping gap: today, `quarry install` step 8/8
writes `session_context` into *existing* `quarry.yaml` files, but
nobody creates those files in the first place.

### Q12: What are the dependencies on ethos?

`quarry enable` reads ethos config at two points:

1. **Identity discovery**: iterates `.ext/` directories under
   `identities/`. This is a filesystem read -- no ethos library needed.
2. **Extension file creation**: writes `quarry.yaml` into `<handle>.ext/`
   directories. Again, filesystem only.

Quarry does not import ethos. The dependency is structural (file layout
convention) not library. This is consistent with DES-008: quarry depends
on ethos for identity data, ethos has zero knowledge of quarry.

### Q13: What happens to the hardcoded fallback collections?

**Superseded by CEO feedback -- fallback constants remain.** The
`_WEB_CAPTURES_FALLBACK = "web-captures"` and
`_SESSION_NOTES_FALLBACK = "session-notes"` constants in `hooks.py`
continue to serve sessions with no covering registration. This preserves
backward compatibility: sessions started outside any registered directory
still capture web fetches and session transcripts into the global
fallback collections.

When a session starts in a registered directory (via `quarry enable`,
`quarry register`, or auto-register), captures route to the
project-scoped `<name>-captures` collection instead. The fallback
constants are only reached when `_collection_for_cwd` returns None.

Existing data in `web-captures` and `session-notes` collections remains
accessible. No migration is needed -- these collections continue to work
as before for unregistered sessions.

### Q14: What are the risks?

1. **Child subsumption on auto-register.** If a user opens a session in
   a parent directory that has child registrations, auto-registering the
   parent would subsume those children. Mitigation: the hook checks for
   descendant registrations before auto-registering and skips with a
   warning if any exist (F7).

2. **Ethos layout changes.** If ethos changes the identity extension
   file layout, quarry's bootstrapping breaks. Mitigation: the layout
   has been stable since ethos v2.0. Pin to the documented convention
   and add a version check if ethos introduces a layout migration.

3. **Collection proliferation.** Each project now gets 2+ collections
   (files + captures + N memory collections). A user with 14 repos and
   6 agent identities could have 14 * 2 + 6 = 34 collections.
   Mitigation: collections in LanceDB are just subdirectories of the
   table's namespace. The storage overhead is the metadata only --
   vectors are shared across collections in a single `chunks` table.

4. **Agent memory collection ownership ambiguity.** Memory collections
   are global (per-agent, not per-project) but bootstrapped per-project.
   Two repos with the same agent both write to `memory-claude`.
   This is intentional -- agent memory is the agent's, not the
   project's -- but could surprise users who expect project isolation.
   Document this clearly.

### Q15: What is the implementation scope?

| Component | Change |
|-----------|--------|
| `src/quarry/__main__.py` | New `enable` and `disable` commands |
| `src/quarry/hooks.py` | Fix child-directory crash in `handle_session_start`; block parent-of-children auto-register; route captures to `<name>-captures` collection |
| `src/quarry/_stdlib.py` | No change (config loading already works) |
| `src/quarry/doctor.py` | Add `enable` status to `check_environment` output |
| `src/quarry/sync_registry.py` | No schema change; new helper to check if a collection exists by name pattern |
| `tests/` | New tests for enable/disable commands, hook behavior without auto-register, captures routing, ethos bootstrapping |
| `.punt-labs/quarry/config.md` | Template created by `quarry enable` |
| `DESIGN.md` | New ADR (DES-023 or next) documenting the enable/disable lifecycle |

Estimated at 4-6 files changed, 300-500 lines net new. Standard
pipeline: design, implement, test, review, document.

### Q16: Should `quarry enable` run automatically from `quarry install`?

No. Install is machine-scoped, enable is project-scoped. Running enable
from install would require knowing which project to enable, which
install does not know. The separation is the point: install once, enable
per-project.

The SessionStart hook auto-registers projects that have no covering
registration, so users get quarry functionality without running
`quarry enable`. The `enable` command adds captures routing, ethos
bootstrapping, and explicit configuration on top of auto-register.
