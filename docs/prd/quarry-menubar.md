# PRD: Quarry Menu Bar App (quarry-menubar)

**Status**: Draft — Pending Hive-Mind Consensus
**Bead**: quarry-9z5
**Priority**: P1
**Date**: 2026-02-14

---

## Problem Statement

Quarry's document search is only accessible via CLI (`quarry search`) and MCP server
(for Claude Code/Desktop). Both interfaces require technical fluency. The PR/FAQ
identifies **Value risk** as the primary concern: users cannot see what Quarry finds,
so they cannot share it or build habits around it.

A macOS menu bar app makes retrieval **visible and instant** — summon with a hotkey,
type a query, see results, act on them. This widens the discovery funnel from CLI power
users to anyone with a Mac, creating the shareable moments that drive word-of-mouth.

## Success Criteria

| Metric | Target |
|--------|--------|
| Time from hotkey to first result | < 1 second (warm), < 3 seconds (cold start) |
| Search result relevance | Same as CLI (identical backend) |
| Daily active searches (self-reported) | > 5 per day after 1 week |
| Setup friction | Zero config if `quarry install` has been run |

## Architecture Decision: HTTP Bridge

**Decision**: Add a `quarry serve` command to the Python package that starts a
lightweight local HTTP server. The Swift menu bar app spawns this server on launch and
communicates via HTTP/JSON.

**Why not the alternatives**:

| Option | Verdict | Reason |
|--------|---------|--------|
| CLI subprocess per query | Rejected | ~3-5s Python startup per search. Unacceptable. |
| MCP client in Swift | Over-engineered | MCP protocol is designed for LLM tool use. Building a full Swift MCP client (handshake, capabilities, JSON-RPC) is substantial work for simple search queries. |
| Unix socket / XPC | Over-engineered | Non-standard protocols with no advantage over HTTP for this use case. |
| Rust FFI to LanceDB | Future option | Eliminates Python dependency but massive upfront work. Consider for v2 if Python proves problematic. |
| **HTTP bridge** | **Selected** | Standard protocol (URLSession), persistent process (model loaded once), minimal new Python code (~100 LOC), reuses existing library. |

**How it works**:

```
┌──────────────────────┐     HTTP/JSON      ┌─────────────────────┐
│  quarry-menubar      │ ◄─────────────────► │  quarry serve       │
│  (SwiftUI macOS app) │   localhost:PORT    │  (Python HTTP API)  │
│                      │                     │                     │
│  MenuBarExtra panel  │                     │  Same library as    │
│  Global hotkey       │                     │  CLI + MCP server   │
│  Result display      │                     │  LanceDB + ONNX     │
└──────────────────────┘                     └─────────────────────┘
         │                                            │
         │  Process management                        │
         │  (spawn on launch, health check,           │
         │   restart on crash, kill on quit)           │
         └────────────────────────────────────────────┘
```

## User Stories

### P0 — Must Have (MVP)

1. **Global hotkey search**: As a user, I press a keyboard shortcut (default:
   Cmd+Shift+Q) and a search panel appears from the menu bar. I type a query,
   results appear within 1 second, showing document name, snippet, similarity score,
   and collection. I press Esc to dismiss.

2. **Result actions**: As a user, I click a search result to see the full chunk text.
   I can copy the text to clipboard or click to reveal the source file in Finder.

3. **Menu bar presence**: As a user, I see a Quarry icon in my menu bar. Clicking it
   opens the search panel. The app has no dock icon and no main window.

4. **Auto-start backend**: As a user, the app automatically starts the Quarry search
   backend when I launch it. I don't need to run any terminal commands.

5. **Zero configuration**: As a user who has already run `quarry install` and indexed
   documents, the menu bar app discovers my database automatically at
   `~/.quarry/data/default/lancedb` with no setup.

### P1 — Should Have

6. **Collection filter**: As a user, I can filter search results by collection using
   a dropdown or chip selector alongside the search field.

7. **Keyboard navigation**: As a user, I navigate results with arrow keys, press
   Enter to open/copy, Tab to move between search field and filters.

8. **Login item**: As a user, I can enable "launch at login" so the app is always
   available.

9. **Database selector**: As a user with multiple named databases (`quarry --db`), I
   can switch between them from a menu.

### P2 — Nice to Have

10. **Status indicator**: The menu bar icon shows a subtle badge when the backend is
    loading or unavailable.

11. **Recent searches**: The panel shows my recent search queries for quick re-search.

12. **Source format icons**: Results show icons for the source format (PDF, markdown,
    code, etc.) using SF Symbols.

## Interaction Model

```
Click menu bar icon OR press Cmd+Shift+Q
    │
    ▼
┌─────────────────────────┐
│ 🔍 Search Quarry...     │  ← Auto-focused text field
│─────────────────────────│
│ [all collections ▾]     │  ← P1: Collection filter
│─────────────────────────│
│ 📄 report.pdf  p.3      │  ← Result: doc name, page, similarity
│    "The quarterly rev…" │     Truncated snippet
│    [research] 0.87       │     Collection tag, score
│─────────────────────────│
│ 📄 notes.md  §2          │
│    "Meeting notes fro…"  │
│    [default] 0.82        │
│─────────────────────────│
│ ...                      │
│─────────────────────────│
│ ⌨ ↑↓ Navigate  ⏎ Open  │  ← Keyboard hints
│   ⎋ Dismiss             │
└─────────────────────────┘
```

Panel dimensions: ~400px wide, height adapts to content (max ~500px).
Visual style: native macOS, NSVisualEffectView translucency, system fonts, SF Symbols.
Automatic dark/light mode.

## Empty & Error States

| State | Display |
|-------|---------|
| No database found | "No Quarry database found. Run `quarry install` in Terminal to get started." |
| Database empty | "No documents indexed yet. Run `quarry ingest <file>` to add your first document." |
| Backend starting | Spinner with "Starting Quarry..." (during cold start) |
| Backend crashed | "Backend unavailable. Restarting..." + auto-restart |
| No results | "No matches for '{query}'" with suggestion to try broader terms |
| Python not found | "Python not found. Quarry requires Python 3.10+." with install link |

## Technical Specifications

### Repository: `quarry-menubar`

New GitHub repository under `jmf-pobox`. Separate from the `ocr` (quarry) repo because:
- Different language (Swift vs Python)
- Different build toolchain (Xcode vs uv/hatch)
- Different release cadence
- Clean separation of concerns

### Python Side (in `ocr` repo)

Add a `quarry serve` command:

```
quarry serve [--port PORT] [--db NAME]
```

- Starts HTTP server on `localhost:<port>` (default: random available port)
- Writes port number to `~/.quarry/data/<db>/serve.port` for client discovery
- Endpoints:
  - `GET /health` — liveness check
  - `GET /search?q=<query>&limit=<n>&collection=<c>` — vector search
  - `GET /documents?collection=<c>` — list documents
  - `GET /collections` — list collections
  - `GET /status` — database stats
- Uses existing `quarry.database` and `quarry.embeddings` modules directly
- Graceful shutdown on SIGTERM/SIGINT

### Swift Side (in `quarry-menubar` repo)

| Component | Technology |
|-----------|-----------|
| Project generation | XcodeGen (`project.yml`) |
| Build system | Makefile (like koch-trainer-swift) |
| UI framework | SwiftUI with MenuBarExtra (.window style) |
| Target | macOS 14+ (Sonoma) |
| Language | Swift 5.9+ |
| HTTP client | URLSession async/await |
| Global hotkey | HotKey package (Swift wrapper for Carbon API) |
| Linting | SwiftLint |
| Formatting | SwiftFormat |
| Architecture | MVVM |

Project structure:

```
quarry-menubar/
├── project.yml            # XcodeGen config
├── Makefile               # generate, build, test, format, lint
├── CLAUDE.md
├── QuarryMenuBar/
│   ├── App/
│   │   └── QuarryMenuBarApp.swift    # @main, MenuBarExtra
│   ├── Models/
│   │   ├── SearchResult.swift         # Codable result model
│   │   └── QuarryStatus.swift         # Database status model
│   ├── ViewModels/
│   │   └── SearchViewModel.swift      # Search state, HTTP calls
│   ├── Views/
│   │   ├── SearchPanel.swift          # Main search UI
│   │   ├── ResultRow.swift            # Individual result display
│   │   ├── ResultDetail.swift         # Expanded result view
│   │   └── EmptyState.swift           # Empty/error states
│   ├── Services/
│   │   ├── QuarryClient.swift         # HTTP client for quarry serve
│   │   ├── DaemonManager.swift        # Python process lifecycle
│   │   └── HotkeyManager.swift        # Global shortcut registration
│   └── Resources/
│       └── Assets.xcassets/
├── QuarryMenuBarTests/
│   ├── QuarryClientTests.swift
│   ├── DaemonManagerTests.swift
│   └── SearchViewModelTests.swift
└── .swiftformat, .swiftlint.yml
```

### Process Lifecycle

1. **App launch**: DaemonManager looks for existing `serve.port` file and health-checks.
   If no healthy daemon, spawns `quarry serve --port <random>`.
2. **Health monitoring**: Periodic `/health` pings every 30 seconds.
3. **Crash recovery**: If health check fails 3 times, respawn the daemon.
4. **App quit**: Send SIGTERM to daemon process, wait 5 seconds, SIGKILL if needed.
   Clean up `serve.port` file.

## Scope Boundaries

**In scope (v1)**:
- Search panel with results
- Global hotkey
- Backend process management
- Menu bar icon with popover
- Zero-config database discovery
- macOS 14+ (Sonoma)

**Explicitly out of scope (v1)**:
- Document ingestion from the menu bar app
- Collection management (create, delete)
- Settings UI beyond hotkey configuration
- Mac App Store distribution (direct download only for v1)
- Auto-update mechanism (manual for v1)
- Sync/watch functionality
- Multiple simultaneous databases

## Definition of Done

- [ ] `quarry serve` command added to Python package with all endpoints
- [ ] `quarry serve` has integration tests
- [ ] quarry-menubar repo created on GitHub with XcodeGen project
- [ ] Search panel renders results from live quarry serve backend
- [ ] Global hotkey summons/dismisses the panel
- [ ] DaemonManager handles start/stop/crash recovery
- [ ] All empty/error states render correctly
- [ ] Keyboard navigation works (arrows, Enter, Esc)
- [ ] SwiftLint + SwiftFormat pass with zero violations
- [ ] Unit tests for QuarryClient, DaemonManager, SearchViewModel
- [ ] App runs as menu-bar-only (no dock icon)
- [ ] README with installation and usage instructions
