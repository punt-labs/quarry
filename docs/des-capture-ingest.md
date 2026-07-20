# Design: one scrubbing content path for remember and capture

**Status:** Proposed, ready for the operator to review.
**Date:** 2026-07-19
**Bead:** quarry-en68 — this fixes the capture starvation (quarry-lnog) and unblocks the client/engine boundary work (PR-6).
**Related work:** the daemon-first refactor (DES-031), the capture redaction rule (DES-036), the capture lifecycle (DES-030), the serialized indexing queue (quarry-lxrk), and the fallback-collection cleanup (quarry-czf3).

Every claim about how the code behaves today has been checked against the source, not assumed. Where it helps, the relevant file and function are named so a reviewer can confirm it.

---

## What's wrong today

Two separate problems turn out to be the same fix.

**First, we store secrets in cleartext.** Quarry is supposed to strip secrets, personal information, and profanity out of anything it persists. It does that for one surface and not the others, and the gap is an accident rather than a decision. When an agent runs `remember`, the text goes straight into the database with no scrubbing at all — the `remember` job simply calls the ingest pipeline, and the pipeline has no scrubbing step. So a remembered API key, file path, or email address lands in the `memory-<agent>` collection in the clear. Session-capture transcripts ride the exact same unscrubbed pipeline, so the capture collections hold cleartext too. (Only the human-readable `.md` copy of a capture gets scrubbed; the database copy never did.)

**Second, the capture hook is the last part of quarry that still runs its own engine.** The daemon-first refactor already turned the CLI, the MCP server, and the library into thin clients that talk to the one background daemon. The capture hook never got converted. Every time a session compacts, the hook spawns a brand-new detached process that loads the entire ~1.6 GB search engine from cold — the model, the database handles, everything — just to embed one transcript, and then exits.

Those two problems are one change: send the capture and remember content to the daemon that's already running, and have the daemon scrub it before it writes anything to the database.

### Why the machine melts (quarry-lnog)

Because each compaction spawns its own cold engine, and nothing limits how many run at once, they pile up. In a busy stretch about fourteen of these 1.6 GB processes were alive at the same time across concurrent sessions, and they oversubscribed an eight-core machine badly enough to drive the load average to 77–97. The cause isn't slow embedding — it's fourteen cold engines fighting for the CPU. There's no resident engine to reuse, so every compaction pays for a full cold start.

### Why this blocks the boundary work (PR-6)

The daemon-first design has a hard rule: no client process may import the engine — not the database layer, not the embedder, not the ingestion pipeline. The capture hook breaks that rule, because it reaches into all of them.

One correction to an earlier version of this document: the hook's engine imports are already written as *lazy* imports, tucked inside the handler functions behind early-return guards. That matters because the import-linter tool the boundary PR was going to rely on only sees imports at module load time — it can't see an import that lives inside a function body and only fires at runtime. So a naive import-linter rule would pass against today's code and prove nothing. The real test that proves the hook is clean is a runtime one: load the hook with the engine libraries deliberately poisoned so any attempt to import them blows up, and confirm the capture path still runs. Making *that* test pass is what this design is for.

### What DES-036 left uncovered

DES-036 set the rule that captures get scrubbed at write time, but it only ever covered the `.md` file that gets committed to git. The database copy of a session capture was never in scope, and the `remember` database copy was never considered at all. So both the per-agent memory collections and the session-capture collections contain cleartext today.

One thing to state accurately, because an earlier draft got it wrong: the *web-fetch* capture is not a leak. It already scrubs its database copy before writing. The reason web-fetch still needs converting is the boundary rule — it imports the engine — not a scrubbing gap. Only `remember` and the compaction transcript actually leak.

---

## The design, in one paragraph

There is one scrubbing routine for putting inline text into the database, and it is shared by two clearly different front doors. One front door is `remember`, which files a note into an agent's memory collection. The other is `capture`, which files a session transcript or a fetched web page into a project's captures collection. Both front doors scrub before they write; the only thing they do differently is decide which collection the text belongs in. A third, unrelated path — directory sync — fills a project's main collection from the actual files on disk, and that one is deliberately *not* scrubbed, because scrubbing real source code would corrupt it.

Here is how the two front doors line up:

| Front door | How you reach it | Where it files the text | Scrubbed? |
|------------|------------------|-------------------------|-----------|
| `remember` | `POST /v1/remember`, `client.remember()` | `memory-<agent>` | yes |
| `capture` | `POST /v1/capture`, `client.capture()` | `<repo>-captures` | yes |

An earlier draft argued for a separate capture endpoint on the grounds that "remember is deliberately left unscrubbed, so captures need their own scrubbing path." That was wrong. Nothing in the code or the design docs ever said remember should stay unscrubbed — the `remember` job just never had a scrubbing step. Remember *should* scrub. Once it does, remember and capture are the same operation with a different destination, so they run the same underlying job. They keep separate names and separate endpoints only so that a reader can tell a capture from a remember at a glance — the code is shared, the names are not.

---

## The principles this has to hold

These are the invariants the implementation must not violate. Each is stated plainly here and referred to by its short name later.

**Scrub everything inline, always.** Every inline-text write to the database — both remember and capture — is scrubbed on the daemon before a single chunk is stored. Scrubbing is safe to run twice (the redaction markers never match again), so the fact that the `.md` file is also scrubbed on the client side is not a problem. The daemon never trusts the client to have already scrubbed; it scrubs itself.

**Never scrub source.** Directory sync fills a project's main collection from files on disk, and it stays raw. Scrubbing source would wreck it — a redacted string in the middle of a test fixture, a home-directory path in documentation, or an email address in a changelog would all get mangled and stop being searchable. Sync is the third path and is out of scope for scrubbing on purpose.

**Captures never land in the main collection.** The scrubbed `.md` capture file is written inside the project tree, in `.punt-labs/quarry/captures/`, which is exactly the tree that directory sync walks. If sync ever picked up those files, every transcript would also get filed into the project's *main* collection, not just its captures collection. Right now the only thing preventing that is a line in this repo's `.gitignore`. That's fragile — a repo missing that line would silently fold transcripts into its main collection. This design makes the exclusion structural instead: the captures directory is added to sync's built-in ignore list, so sync skips it no matter what the repo's gitignore says.

**A failed scrub writes nothing.** Scrubbing happens before the text is embedded and stored. If scrubbing throws an error, the whole operation aborts before anything is written — there is never a half-redacted document left in the database.

**Raw text only crosses the wire on loopback or under TLS.** The full, pre-scrub transcript travels from the hook to the daemon before it's scrubbed. That's fine over loopback (it never leaves the machine) or over TLS (it's encrypted), but it must never go out over an unencrypted network connection. The daemon already guarantees this: it binds to localhost by default and refuses any network-facing bind unless the operator has set up a key and TLS. This design leans on that guarantee for capture content and adds a test to keep it honest.

**The hook imports no engine.** After this change, the capture and web-fetch paths of the hook import only the client, the shared API types, and lightweight standard-library helpers. No database, pipeline, or registry import survives on either path.

**A down daemon loses nothing.** The hook writes its durable local copies — the raw transcript archive and the scrubbed `.md` file — before it talks to the daemon. If the daemon is down, those are still written, and the database copy gets picked up later by `backfill-sessions`. Only the database write depends on the daemon being up.

**The hook never blocks compaction.** The hook sends its request, gets an immediate "accepted" response, and returns; the daemon does the actual embedding in the background. If the daemon is slow to answer or down, the hook gives up quickly and the capture is simply archived for later — it never stalls the session.

**One engine, always.** After this change there is exactly one engine process, the daemon. No per-compaction engine ever spawns again.

---

## How the shared path is built

**The pipeline gains a scrubbing step.** The inline-ingest function grows the same optional scrubber argument the URL-ingest function already has, applied to the text before it's chunked. This is a genuine simplification, not new machinery — it stops the redaction logic from being URL-only and lets both ingest functions share one scrub hook. The scrubbing runs on the worker thread, where the ingest already runs, so the roughly fifteen regex passes stay off the daemon's main event loop and don't stall other requests.

**The shared job is renamed for what it does.** The daemon job that scrubs and stores inline content is renamed from `RememberJob` to `ScrubbedIngestJob`, because it is no longer specific to remember — both front doors build it. It always scrubs. Both handlers construct this one job once they've worked out the collection, so the scrub-and-store logic exists in exactly one place.

**The two front doors keep distinct names.** Each is its own endpoint, request type, client method, and route handler, so nothing borrows the other's name:

| | `remember` | `capture` |
|---|-----------|-----------|
| Endpoint | `POST /v1/remember` (kept) | `POST /v1/capture` (new) |
| Request type | `RememberRequest` (kept) | `CaptureIngestRequest` (new) |
| Client method | `client.remember()` (kept) | `client.capture()` (new) |
| Handler | `IngestionRoutes.remember` (kept) | `CaptureRoutes.capture` (new) |

The new request type is called `CaptureIngestRequest` rather than the more natural `CaptureRequest` for one boring reason: `CaptureRequest` is already the name of the value object that writes the `.md` file, and reusing it would be genuinely confusing. This is the one spot where two "capture" names sit close together, and it's worth a second look during review.

**The real difference between the two doors is who picks the collection.** Remember is handed its collection explicitly — it already works that way. Capture can't work that way, because figuring out a project's collection means reading the sync registry, and that's an engine import the hook isn't allowed to do. So capture sends the daemon its working directory instead, and the daemon does the lookup: it walks up from that directory to the registered project root and derives the `<repo>-captures` collection. When the working directory isn't a registered project at all, the fallback is `default-captures`, produced by running the ordinary naming pattern with `default` as the repo name — not a special one-off name (see the fallback ruling below).

---

## The leak fix

Once the shared job always scrubs, every new remember and every new capture is scrubbed on the way in. That closes the leak going forward for both the memory collections and the capture collections.

It does **not** clean up the cleartext that's already in the database from before scrubbing existed. That was a deliberate call by the operator: this design is forward-only, and the existing leaked chunks will be handled by a future full data purge rather than a one-time scrub-and-sweep built here.

---

## Converting the hook

**The compaction hook** keeps everything that's durable and engine-free and drops the subprocess. It still validates its inputs, still writes the raw transcript archive, and still writes the scrubbed `.md` file locally. The one thing that changes: instead of spawning a detached engine process, it builds a capture request — the transcript text plus its working directory, so the daemon can derive the collection — connects to the local daemon, and sends it, then returns. The system message it shows becomes collection-generic, since the daemon now owns the collection and a quick "accepted" response doesn't carry one back. That's a minor wording change.

**The web-fetch hook** already scrubs, but it's still a fat client, so it converts to the same path. Doing that cleanly means spelling out four things the earlier draft glossed:

- The HTML extraction moves to the daemon. Today the hook runs the extractor itself, which is an engine import it's no longer allowed. Instead it sends the raw fetched HTML to the daemon and lets the daemon extract, scrub, and chunk.
- The fallback re-fetch goes through the URL-ingest route, not the capture route. When the fetch payload has no usable content, today's code re-fetches the URL. Re-fetching a URL is exactly the kind of request that has to pass the SSRF safety check, so it uses the URL-ingest endpoint. It does not hand a URL to the capture path, which only ever takes content the daemon won't go fetch.
- The dedup behavior stays, via a stable document name rather than a skip. `ingest_content`/`ingest_url` have no existence-check, so `overwrite=False` would *duplicate* a re-fetched URL, not suppress it. Instead the daemon files the capture under a stable, redacted-URL `document_name` and sends `overwrite=True` — the same stable-name dedup the compaction hook uses — so re-capturing a URL replaces the prior copy in place. This is the approach DES-041 records.
- The collection fallback matches capture's: `<repo>-captures` normally, `default-captures` when the directory isn't registered.

**What gets deleted.** The whole detached-subprocess machinery goes: the background-ingest class, its module, the subprocess-spawning helper, and the dead argument-parsing constants. Following the no-shims rule, the module is moved aside, the suite is confirmed green, and then it's deleted in a follow-up commit. The hook entry point collapses to a plain standard-library dispatcher.

**This is the part that ends the starvation.** After the change, a compaction is a cheap local POST to the one running daemon. There is no per-compaction 1.6 GB engine to spawn, so fourteen concurrent compactions become fourteen cheap POSTs against one warm engine, and the pile-up that drove the load to 77–97 simply can't happen.

### The remaining concurrency question

Cutting the hook over fixes the acute problem completely — the starvation was cold engine *processes*, and afterward there is one process. There's a smaller residual: under a burst, the daemon could still run several embeds at once in its worker pool. That's the job of the separate serialized-queue work (quarry-lxrk), which will make the daemon process captures one collection at a time. In the meantime the daemon's existing thread limits soften it. The order is: this cutover first, because it puts out the fire and unblocks the boundary PR; the queue after, to harden against bursts. This cutover does not depend on the queue to fix the starvation.

---

## The trust review (for djb)

- The content path can't be tricked into fetching a URL — it only takes content. The only URL fetch anywhere in here is the web-fetch fallback, and that deliberately goes through the SSRF-checked URL-ingest route.
- Each endpoint caps its request body before reading it. The capture endpoint gets its own ~4 MB cap, sized for the 500 KB transcript budget, separate from remember's much larger cap. Two endpoints means each carries the size limit that fits its payload — another small reason the names stay distinct even though the job is shared.
- Raw pre-scrub content only ever crosses loopback or TLS, guaranteed by the daemon's existing refusal to bind to a network address without a key and TLS. A test asserts a non-loopback bind without TLS is refused.
- The metadata the hook asserts — the agent handle, the working directory, and for remember the collection — is trusted only because the endpoint is loopback-bound and requires the local token file. A local process holding that token is the threat boundary, and captures are a local-machine concern, so that's acceptable.
- The security property is the scrub, and it fails closed: the daemon scrubs before writing and, if scrubbing throws, writes nothing.

---

## The decisions you ruled on

The remember/capture collapse dissolved the four decisions from the earlier draft. Two were already settled (dedup and where the `.md` is written — see the next section). Three remained, and you've now ruled on all three; each ruling is applied throughout this document.

**Naming — reuse the logic, never the name.** The shared scrubbing core is named for what it does (`ScrubbedIngestJob`), and remember and capture stay two distinct, correctly-named front doors, each with its own endpoint, request type, client method, and handler. This is explicitly *not* the "just reuse the remember endpoint" shape from an earlier draft: sharing the job is right, but overloading the *name* remember to also mean capture is not.

**The legacy-cleartext sweep — dropped.** This design only guarantees that everything from now on is scrubbed. It does not scrub or purge the cleartext already sitting in the memory and capture collections; a future full data purge covers that.

**The fallback name — use `default` through the normal pattern.** When the working directory isn't a registered project, the fallback uses `default` as the repo name run through the ordinary `<repo>-captures` pattern, giving `default-captures`. The old one-off names `session-notes` and `web-captures` are eliminated — they both collapse into `default-captures`. The deeper question of whether an unregistered directory should fall back at all, or instead nudge you to register it, is out of scope here; that's the separate bead quarry-czf3.

---

## What was already settled

**Dedup by stable name.** Today each capture is saved under a name with a timestamp in it, so every compaction creates a new document and the old ones have to be hunted down and deleted first. This switches to one fixed name per session with overwrite turned on, so re-capturing a session just replaces it — matching how the `.md` file already works, and deleting the whole find-and-delete dance.

**The `.md` file stays on the client.** The scrubbed `.md` and the raw archive are durable local copies that a daemon outage must not lose, so the hook writes them locally. The daemon scrubs the database copy independently — the same content is scrubbed on both surfaces, which is fine because scrubbing is idempotent.

---

## The files this touches

*(This is the proposed shape; the implementer owns the final form. The headline improvement is deleting a whole duplicate engine path and a module, collapsing two jobs into one, and unifying the two ingest functions on a single scrub hook.)*

Changed:

- The ingest pipeline gains the scrubber argument on its inline-content function.
- The daemon's `RememberJob` is renamed to `ScrubbedIngestJob` and always scrubs; the remember handler builds it with an explicit collection.
- The API types gain `CaptureIngestRequest` (the remember request is unchanged).
- The captures route gains a `capture` handler that derives the collection from the working directory and builds the shared job.
- The daemon context gains a small helper to resolve a captures collection from a working directory.
- The client gains a `capture()` method beside `remember()`.
- The hook replaces its subprocess spawn with a `capture()` call, and the web-fetch handler sends raw HTML and routes its fallback re-fetch to URL-ingest.
- The hook entry point loses the background-ingest class and its supporting constants.
- Sync's built-in ignore list gains the captures directory.

Deleted:

- The background-ingest module (moved aside, verified, then removed).

Tests: remember scrubs and capture scrubs (checking a stored chunk is actually redacted, for each door, including the free-form document name and summary at the pipeline choke point); a failed scrub writes zero chunks; a non-loopback bind without TLS is refused; sync skips the captures directory even with no gitignore line; the web-fetch content path and its fallback routing; an unregistered directory falls back to `default-captures`; the runtime engine-sabotage test for the hook; the stable-name overwrite dedup (a re-captured document replaces its prior copy in place via `overwrite=True` + a stable redacted-URL name — there is no separate "already captured, skip" path, so no skip test); the fail-closed overwrite guard (an extraction that chunks to zero keeps the prior document); and a field-name equivalence check per door so the client and daemon can't drift. No sweep tests, since that's out of scope.

Docs: an ADR entry in DESIGN.md, a changelog note, and a refreshed OpenAPI dump.

---

## Order of work

1. **This change** adds the scrubber to the pipeline, renames the job so it always scrubs, adds the capture front door with server-side collection resolution, converts both the compaction and web-fetch hooks, adds the sync exclusion, and deletes the background-ingest module. This is what ends the starvation and makes the boundary test pass.
2. **The boundary PR** then enforces the no-engine-in-clients rule, using the runtime engine-sabotage test as its gate.
3. **The serialized-queue work (quarry-lxrk)** hardens the daemon against bursts of concurrent captures. It comes after this and isn't blocked by it.
4. **The fallback-collection bead (quarry-czf3)** decides whether an unregistered directory should fall back to `default-captures` at all or nudge you to register instead. Separate from this; this design just uses the `default-captures` fallback via the normal pattern.

Cleaning up the cleartext already in the database is intentionally not here — that's the future full purge.
