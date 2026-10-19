# Changelog

## Unreleased — Identity, Runtime Data Isolation & Provenance Paths

- **Breaking: MCP tools act as the token's agent.** The tools took the caller's identity from their `agent_id` argument (default `mcp-client`), so a caller could record, review and update decisions as any agent. `recorded_by`, the reviewer and the graph-link creator are now the agent the bearer token maps to, as on `/cstp`; `agent_id` only labels deliberation buckets. New decisions recorded over MCP therefore appear under the token's agent name instead of `mcp-client`. Over stdio the tools act as `mcp-client`
- **Breaking: agents change only their own decisions, and operator methods need the admin role.** `updateDecision`, `reviewDecision`, `setPreserve`, `linkEvidence` and appending a thought to a recorded decision are limited to the agent that recorded it. `resetCircuit` (which let any agent lift a breaker that was blocking another), `reindex` (which wipes the vector collection), `attributeOutcomes` and `ingestEvidence` need an agent listed in `auth.admins` / `CSTP_ADMIN_AGENTS`; so does seeing other agents' sessions in `debugTracker`. Denied calls return `-32009`. **The dashboard's `CSTP_TOKEN` must map to an admin** for its Reset button and Deliberation page. Decisions recorded before this change as `mcp-client` or under a subagent label can be changed only by an admin, or by a token whose agent has that name
- **One token can no longer plant deliberation in another's decisions.** Tracker keys were built from the caller-supplied `agentId` / `decisionId`, so thoughts recorded under another agent's label, or under the decision id it was about to use, were consumed into that agent's next decision. Keys are now namespaced under the token's agent (`emerson|agent:planner`)
- **Observed evidence can't be forged by agents.** `cstp.ingestEvidence` accepted "observed" events from any caller, and a fabricated `pr_review_approved` with `actor_is_human: true` satisfied the SR 11-7 and NIST human-review controls. It is now admin only and stamps each payload with `ingested_by`. `cstp.linkEvidence` accepted any string as `decision_id`; the decision must now exist and belong to the caller
- **CEL guardrails fail closed.** A rule whose expression failed to compile or evaluate was silently skipped, so a broken block rule allowed everything. It now applies, with a message saying it could not be evaluated. That exposed the shipped rules: `require-deliberation`, `recommend-reasoning` and `low-quality-recording` errored on every check outside the record step because `phase` and the deliberation flags were missing (they now default), and `low-quality-recording` had never worked — the legacy `quality_score: "< 0.5"` was converted to a string equality. It is now a comparison, so low-quality recordings get its warning in `guardrail_warnings`
- **Unexpected errors no longer leak internals.** JSON-RPC and MCP errors returned the exception text and type, which can carry paths, SQL and backend detail. The caller now gets `Internal error (ref <id>)` and the detail is logged under that reference. `getDecision`'s YAML fallback stopped returning the server's file path in `_file`
- **The deliberation tracker is bounded.** Session keys are caller-chosen, so it grew until the 30-minute TTL caught up. Each agent now holds at most 200 sessions (the least recently active is dropped and listed as `evicted`) of at most 100 inputs, and thoughts are capped at 10,000 characters. See `tracker.max_sessions_per_agent` / `max_inputs_per_session`
- **`/mcp` requires the bearer token.** The MCP transport is mounted as a raw ASGI app, so the token dependency guarding `/cstp` never ran for it: with no `Authorization` header a caller could list and call every MCP tool, including `log_decision`, `update_decision` and `review_outcome`. The same token check now runs before the request reaches the MCP session manager. Clients that already send `Authorization: Bearer` (the FORGE plugin, the demo agent) are unaffected
- **Stored XSS on the dashboard deliberation page closed.** Tracker keys embed the caller's `agent_id` and were spliced into Alpine expressions with only single quotes escaped, so an `agent_id` ending in a backslash broke out of the string and ran script for whoever had the page open. Keys are now HTML-escaped data attributes that Alpine reads back as data. The breakers page's Reset button had the same construction (`openReset({{ b.scope|tojson }})` inside a double-quoted attribute) and parsed as `openReset(`, so it did nothing; it uses the same pattern now
- **Recording a decision no longer stalls the server on crafted text.** The bridge abstractor's file-path regex retried from every character of a long path-like run, quadratic in its length (64 KB took 1.7 s on the single-worker event loop). It now runs in linear time (5 ms), and free text is capped: `decision` / `action.description`, `pattern` and each reason's text at 10,000 characters, `context` at 50,000. The caps are checked before bridge extraction on `recordDecision`, `preAction` auto-record and the MCP tools; on `preAction` only the caps apply, so categories it has always accepted still record
- **Evidence bundle paths are server configuration.** `cstp.exportEvidenceBundle` took `output_dir` from the request, so any authenticated agent could make the server create directories and write files wherever it had access; `cstp.mapControls` and the export took `mappings_dir`, so pointing it at an empty directory produced a bundle with no control mappings that still read as clean. Both params are now ignored, like `db_path` before them. Bundles go to `PROVENANCE_BUNDLE_DIR` (default `~/.cstp/bundles`, previously the CWD-relative `./bundles`) and rules always load from the packaged mappings
- **The provenance store closes its connections.** `with sqlite3.connect(...) as conn` only commits or rolls back; ten call sites left the connection open until garbage collection, holding a file handle (and on Windows a lock on the evidence DB)
- **The test suite runs offline and leaves the checkout clean.** The root conftest injected only a memory decision store, so tests reaching `recordDecision`, `reviewDecision` or `preAction` called the Gemini API (6 requests per run, with whatever key the environment held), tried ChromaDB on localhost, and appended fixture edges to `data/graph_edges.jsonl`. Graph, vector and embedding backends are now in-memory per test
- **`cstp-mcp` (stdio) loads the decision graph.** Only the HTTP server's lifespan initialized the graph store, so over stdio `get_graph` / `get_neighbors` answered "Node not found" for saved decisions
- **Docker keeps all server state across container recreation.** Only the decision DB was in the mounted volume. Graph edges (stored nowhere else), circuit breakers, and the F055 evidence chain and bundles lived in the container layer; `GRAPH_DATA_PATH`, `CIRCUIT_BREAKER_DATA_PATH`, `PROVENANCE_DB` and `PROVENANCE_BUNDLE_DIR` now point into it. The Dockerfile creates the mount point so a fresh named volume is writable by the non-root user
- **The demo seed data loads.** `demo/seed_data.py` wrote a hand-copied schema without `created_at`, so the server could not open the database, and edge lines the graph loader rejected (0 of 13 loaded). It now reads the server's schema from source and writes the graph's JSONL format; a test loads its output through the real store
- **`.gitignore`** covers the root-level `data/`, `decisions/` and `.secrets/` (where `scripts/cstp.py` reads the CSTP token), anchored so same-named directories deeper in the tree stay tracked

## Unreleased — CI Truth, Dashboard Auth & Durability (F056–F058)

### F056: CI Truth & Supply Chain

- **112 previously-invisible tests now run.** `testpaths` covered only `tests/`, and CI passed an explicit `pytest tests/` path on top of that. `a2a/cstp/tests/` (47 tests, all green) and `dashboard/tests/` (65 tests) were excluded from every CI run and from the reported coverage number. Total: 1329 → 1460 tests; coverage 78% → 82%
- **Dashboard tests were uncollectable, not merely excluded** — `ModuleNotFoundError: No module named 'auth'`. `dashboard/conftest.py` now puts the dashboard directory on `sys.path`, matching how the Dockerfile loads the app, so the suite exercises production's import wiring
- **`uv.lock` deleted.** It pinned the package at 0.7.0 (eight minor versions stale) and contained no entries at all for `rank-bm25`, `networkx`, or `cel-python` — three required core dependencies. `uv sync --locked` produced an environment where BM25 retrieval, the decision graph, and CEL guardrails all failed to import. Nothing consumed it; CI installs directly
- **Coverage floor** — `--cov-fail-under=80`
- **mypy runs in CI**, non-blocking: `--strict` reports 184 pre-existing errors across 29 files. The step makes the count visible per-PR so it can be ratcheted down
- **Dependabot, CodeQL, and Trivy container scanning added** — there was no dependency or static security scanning of any kind

### F057: Dashboard Authentication

- **Empty-password bypass closed.** `config.validate()` — the only check rejecting an unset `DASHBOARD_PASS` — ran in `main()`, but `dashboard/Dockerfile` runs `gunicorn app:app`, which imports the module and never calls `main()`. The guard did not execute in any containerized deployment. Enforcement moved to import time via `enforce_security_config()`, which raises rather than warns
- **Constant-time credential comparison** — `secrets.compare_digest` on both fields, both always evaluated so no timing signal remains. An empty configured password never matches, independent of the startup guard
- **Login throttling** — 10 failed attempts per client per 5 minutes, then `429`. Process-local, so under `gunicorn -w N` the effective cap is N times that. Behind a reverse proxy set `DASHBOARD_TRUSTED_PROXIES` to the hop count, otherwise every user shares the proxy's address as one throttle bucket and a single attacker locks everyone out. The header is ignored unless that variable is set — an unvalidated `X-Forwarded-For` would let a caller mint a fresh bucket per request and skip throttling entirely
- **Werkzeug debugger no longer default.** `app.run(debug=True)` on `0.0.0.0` is now `debug=False` on `127.0.0.1` unless `DASHBOARD_DEBUG` / `DASHBOARD_HOST` say otherwise. This path is dev-only — gunicorn never reached it — so this is defence in depth, not the fix
- **CORS denies by default.** `cors_origins` was `["*"]` with `allow_credentials=True`; Starlette reflects the caller's `Origin` in that combination, meaning any origin with credentials. The middleware is now installed only for a non-empty allow-list, and credentials are enabled only when that list has no wildcard

### F058: Durability

- **Graph persistence is atomic.** `save_edges_to_jsonl` opened the destination with mode `"w"`, truncating it before the first byte was written — a crash mid-write destroyed **every** persisted edge, not just the in-flight one. It now writes to a temp file in the same directory, fsyncs, and `os.replace()`s
- **`CSTP_STORAGE` defaults to `sqlite`.** The default was `yaml`, which has no WAL, no FTS5, and no concurrent-write protection — while the vector, graph, and BM25 subsystems all assume a queryable, crash-safe store underneath. `yaml` remains available as explicit opt-in and logs a startup warning
- **`recordDecision` no longer acknowledges a failed store write.** It wrote YAML, then swallowed any error or `False` from `DecisionStore.save()` and still returned `success: true`. Harmless while YAML was both the write and read path; with SQLite as the read path it meant an acknowledged decision that `listDecisions`, `getStats`, and calibration could never return. The store write is now authoritative and the YAML file is left in place for re-import
- **YAML→SQLite auto-migration is resumable.** It ran only when the store was empty, but imports one record at a time with no transaction — so a crash after the first save left a non-empty store and every later startup skipped the rest, stranding those decisions. The gate is now "fewer records than YAML files", and saves are upserts, so an interrupted run resumes
- **Storage settings from `--config` YAML are now honoured.** The factory only ever read env vars, so `Config.storage` was ignored. That was harmless while both defaulted to `yaml`; with the new default it would have silently switched an explicitly-YAML deployment to SQLite. Precedence is env → YAML config → default
- **`docker-compose.yml` sets `CSTP_DB_PATH` inside the persistent volume.** The default `data/decisions.db` resolves to `/app/data`, which is not mounted — decisions would have lived in the container layer and vanished on recreate
- **Multi-worker refused rather than silently broken.** `DeliberationTracker` keeps session state in a process-local dict, so `preAction` → `recordThought` → `ready` must land on one process. `CSTP_WORKERS > 1` now exits with an explanation instead of losing deliberation state on whichever requests hit the wrong worker

## v0.16.0 - Provenance, CEL Guardrails & Circuit Breakers
*Unreleased — merged to `main`*

Audit-grade decision provenance mapped to SR 11-7 and NIST AI RMF, CEL expression guardrails, circuit breakers, and the full-stack Docker demo.

### F055: Decision Provenance & Control Evidence

Five new JSON-RPC methods that turn CSTP's decision history into an artifact an auditor can accept. JSON-RPC only — no MCP tools.

- **Two-class evidence model** — `observed` (third-party events the agent does not control) and `attested` (first-party CSTP records). Coverage is reported **separately per class**; there is no blended percentage anywhere in the API, the JSON bundle, or the PDF
- **`cstp.ingestEvidence`** - ingest observed events (GitHub PR opens, reviews, approvals, merges) into a SHA-256 hash chain
- **`cstp.linkEvidence`** - correlate an existing CSTP decision to observed events, stored as attested evidence
- **`cstp.mapControls`** - run the YAML rules engine over stored evidence; returns per-class coverage, `insufficient_evidence` entries, and `attested_only_controls`
- **`cstp.exportEvidenceBundle`** - emit the full bundle as JSON, or PDF with the optional `pdf` extra
- **`cstp.verifyEvidenceChain`** - verify chain integrity; accepts `expected_head_hash` and falls back to the last bundle's head hash so tail truncation is detectable
- **Control frameworks** - hand-written YAML rules for SR 11-7 (Federal Reserve MRM), NIST AI RMF 1.0, and CSTP-attested. Mappings are deliberately not ML-inferred
- **Honest gaps** - where evidence does not support a control, the mapper emits `INSUFFICIENT_EVIDENCE` with a plain-English reason instead of stretching the mapping
- **Separate store** - SQLite WAL database at `PROVENANCE_DB` (default `~/.cstp/provenance.db`), independent of the decision store

### F055 Security Hardening

13 verified findings from the PR #193 review were resolved before merge:

- **Injective hash preimage** - preimage is now canonical JSON of all six fields (chain format version 2). The previous newline-delimited preimage allowed collisions on embedded newlines. **Breaking:** version 1 chains fail `verify_chain` and must be re-ingested
- **Hash chain race** - `BEGIN IMMEDIATE` plus explicit `seq` serializes concurrent writers
- **`db_path` RPC escape removed** - the service always uses the server-configured `PROVENANCE_DB`; callers can no longer redirect reads or writes to an arbitrary file
- **Evidence class isolation** - mapping rules only match events of their own class
- **Bot approvals fail closed** - MV-3 / MEASURE-2.5 require `actor_is_human`, CM-1 / MANAGE-1.1 require a human approval count above zero
- Plus seq validation on `linkEvidence`, checkpoint state-machine fixes, non-dict payload rejection, atomic batch ingest, and collision-resistant bundle filenames

### F054: CEL Expression Guardrails

- **CEL conditions** - guardrail `condition` accepts a [CEL](https://github.com/google/cel-spec) expression string or `{"cel": "..."}` alongside the legacy key/value form
- **Reaches `action.context.*`** - fixes the gap where MCP clients could not pass context, so category-specific rules like `require-architecture-review` no longer always block through MCP
- **Legacy auto-conversion** - existing key/value conditions are converted to CEL at evaluation time; no guardrail files need changing and no migration is required
- **Fails open** - an expression that fails to compile or raises at runtime is skipped and logged, never treated as a block
- **Compiled once** - programs are cached per expression string
- **New dependency** - `cel-python>=0.4,<1.0`, now a core dependency

### F030: Circuit Breaker Guardrails

- `cstp.listBreakers`, `cstp.getCircuitState`, `cstp.resetCircuit` and the `get_circuit_state` / `list_breakers` MCP tools
- Category-specific code-review and architecture-review guardrails replace the earlier broad production guard
- Guardrail `context` is now threaded through both the JSON-RPC and MCP check paths

### F051: Docker-Compose Full Stack Demo

- `demo/` brings up CSTP server, ChromaDB, dashboard, and a reference FORGE-protocol MCP agent
- `demo/seed_data.py` generates sample decisions

### Website

- New [Nous](/nous) agent landing page, linked from the top nav
- Mermaid removed from the VitePress build; diagrams are inline SVG

### Bug Fixes

- **F041** - `build_wisdom()` accepts an optional `now` parameter, removing wall-clock coupling that made the wisdom recency filter fail as fixture dates aged past the compaction threshold
- **Encoding correctness on non-UTF-8 hosts** - every text-mode file operation across `a2a/`, `src/`, `scripts/`, and the test suite now passes `encoding="utf-8"` explicitly. Previously these relied on the platform default, which is cp1252 on Windows: F055 control rules loaded with 10 of 20 entries as mojibake, putting garbled regulatory text into auditor-facing bundles, and decision YAML could round-trip through a mis-decode into storage. Linux CI is UTF-8 by default, so none of it was visible there. Ruff `PLW1514` is now enabled to stop the defect class recurring

### Packaging

- `cel-python` added to core dependencies
- New `pdf` extra (`reportlab>=4.0`) for F055 PDF bundles, included in `[all]`

## v0.15.0 - SQLite Storage & Performance
*February 21, 2026*

SQLite-backed storage with 8-42x performance gains, enriched search results, YAML auto-migration, and dashboard server-side integration.

### F050: SQLite Storage Layer
- **SQLite backend with WAL mode** - Full ACID compliance, concurrent reads, ~900 lines of battle-tested storage code
- **Normalized schema** - Separate tables for tags, reasons, bridge definitions, and deliberation traces
- **FTS5 full-text search** - Keyword search on decision text, context, and tags
- **Factory pattern** - `CSTP_STORAGE=sqlite` env var switches backend; `CSTP_DB_PATH` for file location
- **Abstract `DecisionStore` ABC** - Clean interface for future storage backends

### Auto-Migration
- **YAML → SQLite migration on startup** - Automatic, safe, uses upserts (re-runnable)
- **Standalone migration script** - `scripts/migrate_yaml_to_sqlite.py` with 17 tests
- **Zero data loss** - All fields preserved including bridge definitions, tags, reasons, and project context

### Performance
- **queryDecisions: 0.37s** (was 3.16s with YAML — **8.5x faster**)
- **getCalibration: 0.06s** (was 2.54s — **42x faster**)
- **getDecision: 5.8ms** (was 27ms — **4.7x faster**)
- **listDecisions: 6.7ms**, getStats: 8ms

### Enriched Search
- **Bridge in search results** - `DecisionSummary` now includes structure/function bridge definitions (~200 bytes each)
- **Enriched pre_action** - Relevant decisions include outcome, reasons, and lessons learned
- **Deliberation on-demand** - Full traces (2-5KB) only via `getDecision`, not in list results

### Dashboard Integration
- **Server-side filtering** - Dashboard wired to `listDecisions`/`getStats` APIs instead of client-side YAML scanning
- **Decision detail page** - Full text, recorded_by attribution, strength bars, graph neighbor links
- **Calibration service refactored** - Uses `DecisionStore.list()` instead of YAML file globbing

### Bug Fixes
- Fix `dict`-type `project` field handling in SQLite storage
- Fix `reindex_decisions()` to delegate to `reindex_decision()` for full metadata rebuild
- Fix deliberation tracking in `pre_action` for MCP visibility
- Fix `safe_auto_link()` in `pre_action` auto_record path

## v0.14.0 - Multi-Agent Isolation & Live Deliberation
*February 16, 2026*

Multi-agent deliberation isolation, live deliberation viewer dashboard, memory compaction, decision graph with auto-linking, and quality enforcement.

### Multi-Agent Deliberation Isolation
- **Composite tracker keys** - `agent:{id}:decision:{id}` scoping prevents thought cross-contamination when multiple agents share an MCP connection
- **`agent_id` on all MCP tools** - `pre_action`, `get_session_context`, `ready`, `record_thought`, `log_decision` all accept `agent_id` for attribution and isolation
- **`decision_id` scoping** - `record_thought` and `log_decision` accept `decision_id` to scope deliberation consumption to specific decisions
- **`cstp.debugTracker`** - Live inspection endpoint for in-memory deliberation state

### F049: Live Deliberation Viewer
- New `/deliberation` dashboard page with real-time tracker state
- Session cards organized by composite key with agent/decision badges
- HTMX auto-refresh (5s) with Alpine.js expand state preservation
- Color-coded by age (fresh/stale), type badges for input sources
- Composite key parsing links decision IDs to detail pages

### F041: Memory Compaction
- Semantic decay: full → summary → digest → wisdom compaction levels
- `cstp.getCompacted` and `cstp.getWisdom` endpoints
- Wisdom and compacted results integrated into `get_session_context`
- Automatic compaction on startup and on review

### F044: Agent Work Discovery
- `cstp.ready` endpoint surfaces prioritized cognitive actions
- Action types: overdue reviews, calibration drift, stale decisions
- Filter by priority, type, category

### F045: Decision Graph Storage Layer
- `cstp.linkDecisions` - typed edges (`relates_to`, `supersedes`, `depends_on`)
- `cstp.getGraph` - subgraph queries with depth and edge type filters
- `cstp.getNeighbors` - lightweight neighbor queries
- Auto-linking on `recordDecision` from related decisions
- JSONL persistence, NetworkX backend, thread-safe

### F048: Multi-Vector-DB Support
- `VectorStore` and `EmbeddingProvider` abstractions
- ChromaDB and MemoryStore backends
- Factory pattern with `VECTOR_BACKEND` env var

### Quality & Process
- **`low-quality-recording` guardrail upgraded to block** - Decisions missing tags, pattern, or reasons are now rejected
- **`log_decision` demoted to last resort** - `pre_action(auto_record: true)` is the primary recording path
- **14+ MCP tools** (3 PRIMARY: `pre_action`, `get_session_context`, `ready`)

### Documentation
- Updated all docs with correct MCP flow: `pre_action` → `record_thought` → `update_decision`
- Multi-agent isolation guide
- Agent system prompt templates updated
- All 33+ feature specs on website

### No Breaking Changes
All features are additive. `agent_id` defaults to `"mcp-client"` when not provided. Existing clients work unchanged.

---

## v0.11.0 - Pre-Action API, Dashboard & Website
*February 15, 2026*

A complete agent workflow in two calls: `pre_action` (query + guardrails + record in one shot) and `get_session_context` (full cognitive context for session start). Plus a production dashboard, pluggable vector storage, and a documentation website.

### Features
- **F046: Pre-Action Hook** - All-in-one `cstp.preAction` combines query, guardrails, calibration, pattern extraction, and optional recording into a single call
- **F047: Session Context** - `cstp.getSessionContext` delivers agent profile, relevant decisions, guardrails, calibration by category, and confirmed patterns in JSON or markdown
- **F048 P1: Pluggable Storage** - `VectorStore` and `EmbeddingProvider` abstractions extracted from hardcoded ChromaDB/Gemini; in-memory backend for testing
- **F027: Decision Quality** - Tags, patterns, quality scoring, smart bridge extractors for better retrieval
- **F028: Reasoning Capture** - `cstp.recordThought` for chain-of-thought steps, quality enforcement guardrail

### Dashboard
- Full web dashboard (Flask + HTMX + Alpine.js + Chart.js)
- Decision explorer with search, filters, and detail views
- Calibration charts, analytics overview, date filter presets
- Dark theme design system

### MCP
- **11 MCP tools** (up from 7) via Streamable HTTP at `/mcp`
- `pre_action` and `get_session_context` marked as PRIMARY entry points
- Claude Code / Claude Desktop integration via `npx mcp-remote@latest`
- Fixed `$ref`/`$defs` schema issues for LLM compatibility

### Website
- Documentation site at [cognition-engines.ai](https://cognition-engines.ai)
- VitePress with dark theme, local search, Mermaid diagram support
- Guide, Reference, and Feature Specs sections

### Specs Added
- F029-F032: Research-driven specs (task routing, circuit breakers, trust scoring, error tracking)
- F033-F034: Censor layer, decomposed confidence
- F035-F039: Multi-agent federation (state transfer, reasoning continuity, collective innovation, protocol stack)
- F040-F045: Beads-inspired specs (task graphs, memory compaction, dependencies, distributed merge, work discovery, graph storage)
- F048: Multi-vector-DB support

### No Breaking Changes
All features are additive. Existing JSON-RPC and MCP clients work unchanged.

---

## v0.10.0 - Decision Intelligence with Auto-Capture
*February 8, 2026*

Every decision now automatically captures its full cognitive context - deliberation traces, bridge-definitions, and related decision links - with zero client changes.

### Features
- **F022: MCP Server** - 7 native MCP tools at `/mcp`
- **F023: Deliberation Traces** - auto-capture query/check as structured inputs
- **F024: Bridge-Definitions** - structure/function dual descriptions with directional search
- **F025: Related Decisions** - auto-populated graph edges from query results
- `cstp.getDecision` - full decision details by ID
- `cstp.getReasonStats` - reason-type calibration analytics
- Agent Quick Start Guide for onboarding other agents

### No Breaking Changes
All features are additive and backward-compatible.

## v0.8.0 - Decision Intelligence Platform
*February 5, 2026*

- CSTP server with JSON-RPC 2.0 API
- Hybrid retrieval (BM25 + semantic)
- Drift alerts and confidence variance monitoring
- Docker deployment with dashboard
