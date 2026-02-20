# SPARC Plan: Quarry Menu Bar App

## S — Specification

### Problem

Quarry search is invisible to non-CLI users. A macOS menu bar app surfaces it with a
global hotkey, making retrieval instant and shareable.

### Success Criteria

- Global hotkey summons search panel in < 100ms
- First search result appears in < 1s (warm backend)
- Cold start to ready in < 5s
- Zero configuration for users who have run `quarry install`
- All SwiftLint/SwiftFormat/tests pass

### Constraints

- macOS 14+ (Sonoma) — MenuBarExtra .window style
- Swift 5.9+, SwiftUI
- No external dependencies beyond HotKey (global shortcut)
- Direct distribution (not App Store — needs to spawn Python)
- Quarry Python package must be installed (`pip install quarry` or `uv`)

---

## P — Pseudocode

### Python: `quarry serve` HTTP endpoints

```python
# New file: src/quarry/http_server.py

GET /health
  → {"status": "ok", "uptime_seconds": N}

GET /search?q=<query>&limit=10&collection=&page_type=&source_format=
  → embed query using existing OnnxEmbeddingBackend
  → search LanceDB using existing database.search()
  → return JSON: {query, total_results, results: [{document_name, collection,
      page_number, chunk_index, text, page_type, source_format, similarity}]}

GET /documents?collection=
  → call existing database.list_documents()
  → return JSON: {total_documents, documents: [...]}

GET /collections
  → call existing database.list_collections()
  → return JSON: {total_collections, collections: [...]}

GET /status
  → aggregate: doc count, chunk count, collection count, db size
  → return JSON: {document_count, chunk_count, collection_count, ...}

Server lifecycle:
  1. Parse --port (default: 0 = OS assigns), --db
  2. Load settings, initialize embedding backend (model loads here)
  3. Write port to ~/.quarry/data/<db>/serve.port
  4. Start stdlib http.server on localhost:<port>
  5. On SIGTERM/SIGINT: delete serve.port, shutdown gracefully
```

### Swift: Core flow

```swift
// App launch
QuarryMenuBarApp:
  1. Initialize DaemonManager
  2. DaemonManager checks for existing serve.port → health check
  3. If no healthy daemon → spawn "quarry serve --port 0 --db default"
  4. Wait for serve.port file to appear → read port
  5. Health check /health → ready

// Search flow
SearchViewModel:
  1. User types in search field
  2. Debounce 300ms
  3. Call QuarryClient.search(query, limit: 10)
  4. QuarryClient: GET http://localhost:<port>/search?q=...
  5. Decode JSON → [SearchResult]
  6. Update published results array → SwiftUI re-renders

// Process management
DaemonManager:
  1. spawn(): Process() with "quarry" "serve" arguments
  2. Monitor stdout/stderr for logging
  3. Periodic /health ping every 30s
  4. On 3 consecutive failures → respawn
  5. On app termination → SIGTERM, wait 5s, SIGKILL
```

---

## A — Architecture

### Two Repositories

```text
ocr/                          (existing quarry Python repo)
├── src/quarry/
│   ├── http_server.py        ← NEW: HTTP server entry point
│   └── __main__.py           ← MODIFIED: add "serve" command
└── tests/
    └── test_http_server.py   ← NEW: integration tests

quarry-menubar/               (new Swift repo)
├── project.yml
├── Makefile
├── CLAUDE.md
├── QuarryMenuBar/
│   ├── App/
│   │   └── QuarryMenuBarApp.swift
│   ├── Models/
│   │   ├── SearchResult.swift
│   │   └── QuarryStatus.swift
│   ├── ViewModels/
│   │   └── SearchViewModel.swift
│   ├── Views/
│   │   ├── SearchPanel.swift
│   │   ├── ResultRow.swift
│   │   ├── ResultDetail.swift
│   │   └── EmptyStateView.swift
│   └── Services/
│       ├── QuarryClient.swift
│       ├── DaemonManager.swift
│       └── HotkeyManager.swift
└── QuarryMenuBarTests/
```

### Data Flow

```text
User → Hotkey/Click → SearchPanel → SearchViewModel
                                         │
                                    debounce 300ms
                                         │
                                    QuarryClient ─── HTTP GET ──→ quarry serve
                                         │                            │
                                    [SearchResult]              database.search()
                                         │                     embeddings.embed_query()
                                    SwiftUI render              LanceDB
```

### Key Interfaces

```swift
// QuarryClient.swift
protocol QuarryClientProtocol {
    func search(query: String, limit: Int, collection: String?) async throws -> SearchResponse
    func documents(collection: String?) async throws -> DocumentsResponse
    func collections() async throws -> CollectionsResponse
    func status() async throws -> StatusResponse
    func health() async throws -> Bool
}

// DaemonManager.swift
@Observable
class DaemonManager {
    var state: DaemonState  // .starting, .ready, .error(String), .stopped
    func start() async
    func stop()
    func healthCheck() async -> Bool
}

// SearchViewModel.swift
@Observable
class SearchViewModel {
    var query: String
    var results: [SearchResult]
    var isSearching: Bool
    var errorMessage: String?
    func search()
}
```

---

## R — Refinement

### Edge Cases

- **quarry not in PATH**: DaemonManager searches common locations:
  `~/.local/bin/quarry`, `~/.cargo/bin/quarry`, `/usr/local/bin/quarry`,
  and checks if `python3 -m quarry` works as fallback.
- **Port conflict**: Using port 0 (OS-assigned) eliminates conflicts.
- **Multiple app instances**: Only one instance runs (check for existing
  `serve.port` and health-check before spawning).
- **Database locked**: LanceDB supports concurrent reads. The HTTP server
  and CLI can coexist.
- **Large result sets**: Cap at 50 results (same as MCP server limit).
- **Long queries**: Embedding model truncates at 512 tokens — no crash,
  just truncated semantic matching.

### Error Handling

- HTTP errors → QuarryClient throws typed errors → SearchViewModel shows message
- Process crash → DaemonManager auto-restarts (max 3 attempts, then show error)
- No database → Status endpoint returns zero counts → empty state view
- Network timeout → 5s timeout on search, 2s on health check

### Testing Strategy

- **Python (ocr repo)**: Integration tests for HTTP server endpoints using
  stdlib `urllib` client. Test search, documents, collections, status, health.
  Mock LanceDB for unit tests.
- **Swift (quarry-menubar)**: Unit tests for QuarryClient (mock URLProtocol),
  DaemonManager (mock Process), SearchViewModel (mock QuarryClient).
  No UI tests for v1 (MenuBarExtra testing is limited).

---

## C — Completion

### Task Breakdown (Beads)

#### Epic: quarry-9z5 (existing — macOS menu bar companion app)

#### Phase A: Python HTTP Server (ocr repo)

1. **Add `quarry serve` HTTP server** — New `http_server.py` with stdlib `http.server`,
   endpoints for /health, /search, /documents, /collections, /status.
   Port file management. SIGTERM handling. `__main__.py` gets `serve` command.
   Priority: P1, Type: feature.

2. **Integration tests for `quarry serve`** — Test all endpoints with real
   embedded backend (using test fixtures). Test port file lifecycle, graceful
   shutdown. Priority: P1, Type: task.

#### Phase B: Swift Project Scaffold (quarry-menubar repo)

1. **Create quarry-menubar GitHub repo** — Initialize repo, XcodeGen project.yml,
   Makefile (generate, build, test, format, lint), CLAUDE.md, .swiftformat,
   .swiftlint.yml. Empty app target + test target. Priority: P1, Type: task.

2. **QuarryClient HTTP client** — URLSession-based client implementing
   QuarryClientProtocol. Codable models for all response types. Error handling.
   Unit tests with mock URLProtocol. Priority: P1, Type: task.

3. **DaemonManager process lifecycle** — Spawn quarry serve, monitor health,
   restart on crash, shutdown on quit. Port file discovery. Unit tests.
   Priority: P1, Type: task.

#### Phase C: Search UI (quarry-menubar repo)

1. **MenuBarExtra app shell** — QuarryMenuBarApp with MenuBarExtra(.window),
   status bar icon, LSUIElement=true, DaemonManager initialization.
   Priority: P1, Type: feature.

2. **SearchPanel + SearchViewModel** — Search text field with debounce, results
   list, loading/empty/error states. MVVM with @Observable.
   Priority: P1, Type: feature.

3. **ResultRow + ResultDetail views** — Result display with document name,
   snippet, similarity, collection tag. Detail view with full text and
   copy/reveal-in-Finder actions. Priority: P1, Type: task.

#### Phase D: Global Hotkey + Polish

1. **HotkeyManager global shortcut** — HotKey package integration,
   Cmd+Shift+Q default, toggle panel visibility. Priority: P1, Type: feature.

2. **Empty/error state views** — All states from PRD: no database, empty
    database, backend starting, backend crashed, no results, Python not found.
    Priority: P1, Type: task.

### Dependencies

```text
1 (quarry serve)
  └─► 2 (tests for serve)
  └─► 4 (QuarryClient — needs serve API to exist)
        └─► 7 (SearchPanel — needs client)

3 (repo scaffold)
  └─► 4, 5, 6, 7, 8, 9, 10 (all Swift work needs repo)

5 (DaemonManager)
  └─► 6 (app shell — needs daemon)

6 (app shell)
  └─► 7 (search panel lives in app shell)
  └─► 9 (hotkey toggles app shell panel)

7 (search panel)
  └─► 8 (result views are children of search panel)

8 (result views)
  └─► 10 (empty states are part of result display)
```

### Acceptance Criteria (per task)

| # | Task | Acceptance |
|---|------|------------|
| 1 | quarry serve | `quarry serve` starts, responds to all 5 endpoints, writes/cleans port file |
| 2 | serve tests | All endpoints tested, port file lifecycle tested, ≥90% coverage of http_server.py |
| 3 | repo scaffold | `make generate && make build` succeeds, empty app launches |
| 4 | QuarryClient | All 5 methods work against live server, unit tests pass with mocks |
| 5 | DaemonManager | Spawns process, detects ready state, restarts on crash, cleans up on quit |
| 6 | App shell | MenuBarExtra visible, no dock icon, DaemonManager starts backend |
| 7 | Search panel | Type query → see results, debounce works, loading state shows |
| 8 | Result views | Click result → see full text, copy works, reveal in Finder works |
| 9 | Global hotkey | Cmd+Shift+Q toggles panel, works from any app |
| 10 | Empty states | All 6 states from PRD render correctly |

### Evaluation

| Dimension | Assessment |
|-----------|-----------|
| **Usability** | Excellent — summon/search/dismiss is 3 keystrokes. Zero config. |
| **Value** | High — directly addresses PR/FAQ Value risk. Makes retrieval visible. |
| **Feasibility** | High — HTTP bridge is well-understood. ~100 LOC Python, standard Swift. |
| **Viability** | Good — two repos add maintenance but clean separation. HTTP API is stable. |
