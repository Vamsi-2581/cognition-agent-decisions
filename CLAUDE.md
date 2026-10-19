# CLAUDE.md — Cognition Engines for Agent-Decisions

## Project overview

Decision intelligence layer for AI agents providing semantic search, guardrails, and pattern detection over the CSTP (Cognition State Transfer Protocol). Built on Python 3.11+, ChromaDB, and FastAPI.

- **Core library** (`src/cognition_engines/`) — Pure Python, no web dependencies
- **Protocol server** (`a2a/`) — FastAPI JSON-RPC 2.0 server with bearer auth
- **Dashboard** (`dashboard/`) — Standalone Flask web UI
- **Website** (`website/`) — VitePress documentation site

## Build and run

```bash
# Install all dependencies (core + a2a + mcp + dev + pdf)
pip install -e ".[all]"

# PDF evidence bundles only (F055) — reportlab
pip install -e ".[pdf]"

# Run CSTP server (defaults: VECTOR_BACKEND=chromadb, EMBEDDING_PROVIDER=gemini)
cstp-server
# or: python -m a2a.server --host 0.0.0.0 --port 8100

# Run with in-memory backend (no ChromaDB required, useful for dev/testing)
VECTOR_BACKEND=memory cstp-server

# Docker (server + ChromaDB)
docker-compose up -d --build
```

## Test

```bash
# Full test suite (pytest with coverage)
python -m pytest

# Specific test file
python -m pytest tests/test_guardrails.py -v

# Specific test
python -m pytest tests/test_guardrails.py::test_function_name -v
```

Run `pytest` with **no path argument** — `testpaths` covers all three trees (`tests/`,
`a2a/cstp/tests/`, `dashboard/tests/`). Passing an explicit path overrides `testpaths` and
silently skips the other two; that was the F056 CI bug.

Pytest config is in `pyproject.toml`: async mode is auto, test paths are `tests/`, `a2a/cstp/tests/`, and `dashboard/tests/`, default flags are `-v --cov=a2a --cov-report=term-missing`. The 80% coverage floor is applied by CI (`--cov-fail-under=80` on the CI command), not in `addopts` — a global floor would fail focused runs like `pytest tests/test_guardrails.py`, which cannot cover 80% of the package. PYTHONPATH must include `src` (CI sets `PYTHONPATH=src`). Dashboard tests need `flask` and `flask-wtf`; `dashboard/conftest.py` puts the dashboard directory on `sys.path` so its production-style flat imports resolve under pytest.

## Lint and type check

```bash
# Lint
ruff check src/ tests/ a2a/

# Lint with auto-fix
ruff check --fix src/ tests/ a2a/

# Type check
mypy src/ a2a/
```

Ruff config: line-length 100, target Python 3.11, rules `E F I N W UP B C4 SIM PLW1514`. `PLW1514` (text IO must name an encoding) is preview-gated, so `preview` and `explicit-preview-rules` are both on — the latter keeps the rest of the preview ruleset out. See `pyproject.toml [tool.ruff]` for ignored rules.

`scripts/` and `dashboard/` are outside the lint scope; `scripts/` currently has 40 pre-existing style errors.

## Architecture

```
AI Agents → POST /cstp (JSON-RPC 2.0, Bearer auth)
  → a2a/server.py → CstpDispatcher → *_service.py handlers
    → VectorStore (chromadb | memory)  + EmbeddingProvider (gemini)
    → GraphStore (networkx | memory)   + JSONL persistence (F045)
    → Provenance store (SQLite WAL, hash-chained) — F055, separate DB
    → src/cognition_engines/ (SemanticIndex, GuardrailEngine, PatternDetector)
    → YAML files (decisions/guardrails)
```

Vector storage and embeddings are abstracted behind `VectorStore` and `EmbeddingProvider` ABCs (F048). Graph storage is abstracted behind `GraphStore` ABC (F045). Services access backends via factory singletons (`get_vector_store()`, `get_embedding_provider()`, `get_graph_store()`). Backend selection is driven by `VECTOR_BACKEND`, `EMBEDDING_PROVIDER`, and `GRAPH_BACKEND` env vars.

F055 provenance evidence lives in its own SQLite WAL database at `PROVENANCE_DB` (default `~/.cstp/provenance.db`), deliberately separate from the decision store. The path is server-configured only — RPC callers cannot override it. The same goes for the bundle directory (`PROVENANCE_BUNDLE_DIR`, default `~/.cstp/bundles`) and the mapping rules directory.

Identity is the bearer token's agent on both `/cstp` and `/mcp` (stdio acts as `mcp-client`); `agent_id` arguments only label deliberation buckets, and tracker keys are namespaced `{agent}|...`. Agents may change only decisions they recorded; agents in `auth.admins` (`CSTP_ADMIN_AGENTS`) may change any and run `resetCircuit`, `reindex`, `attributeOutcomes` and `ingestEvidence`. Checks live in `a2a/cstp/access.py` and run in the transport handlers (dispatcher and `mcp_server`), so a new mutating method needs one there.

**Key constraint**: `src/cognition_engines/` must never import from `a2a/`. Core uses dataclasses, the a2a layer uses Pydantic.

## Code conventions

- Python 3.11+ features (`match`, `X | Y` unions, type aliases)
- Type hints on all function signatures; `mypy --strict` must pass
- Line length: 100 chars max
- Naming: modules `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE`, CSTP methods `cstp.camelCase`, handlers `_handle_snake_case`
- Test files: `test_<module>.py` or `test_f0XX_<feature>.py`
- Use `pathlib.Path` for file operations (cross-platform)
- **Always pass `encoding="utf-8"` to text-mode file IO** (`open`, `os.fdopen`, `read_text`, `write_text`). The platform default is cp1252 on Windows, which silently mis-decodes non-ASCII rather than raising — and Linux CI cannot see it. Enforced by ruff `PLW1514`, but that rule does not catch `os.fdopen` or untyped `read_text`, so it is a safety net, not a guarantee
- Mock all external APIs (ChromaDB, Gemini) in tests — tests must run offline
- Use `MemoryStore` + mock `EmbeddingProvider` via factory injection (`set_vector_store()` / `set_embedding_provider()`) instead of patching internal HTTP functions
- Config pattern: YAML → env var → default (see `a2a/config.py`)

## Adding a new CSTP method

1. Create service logic in `a2a/cstp/*_service.py`
2. Add handler `_handle_<name>` in `a2a/cstp/dispatcher.py`
3. Register in `register_methods()`
4. Add Pydantic models to `a2a/cstp/models.py`
5. Write tests in `tests/test_f0XX_<feature>.py`
6. Run full suite: `python -m pytest`
7. Verify lint: `ruff check src/ tests/ a2a/`

## Key files

- `a2a/cstp/dispatcher.py` — All CSTP method handlers and routing
- `a2a/cstp/vectordb/__init__.py` — `VectorStore` ABC + `VectorResult` dataclass
- `a2a/cstp/vectordb/chromadb.py` — ChromaDB HTTP backend
- `a2a/cstp/vectordb/memory.py` — In-memory backend (tests/dev)
- `a2a/cstp/vectordb/factory.py` — Backend selection via `VECTOR_BACKEND` env var
- `a2a/cstp/embeddings/__init__.py` — `EmbeddingProvider` ABC
- `a2a/cstp/embeddings/gemini.py` — Gemini embedding provider
- `a2a/cstp/embeddings/factory.py` — Provider selection via `EMBEDDING_PROVIDER` env var
- `src/cognition_engines/accelerators/semantic_index.py` — Semantic search core (separate from a2a vectordb)
- `src/cognition_engines/guardrails/engine.py` — Guardrail engine core
- `a2a/cstp/graphdb/__init__.py` — `GraphStore` ABC + `GraphNode`/`GraphEdge` dataclasses
- `a2a/cstp/graphdb/networkx_store.py` — NetworkX graph backend with JSONL persistence
- `a2a/cstp/graphdb/memory.py` — In-memory graph backend (tests/dev)
- `a2a/cstp/graphdb/factory.py` — Graph backend selection via `GRAPH_BACKEND` env var
- `a2a/cstp/graph_service.py` — Graph business logic (link, query, init from YAML)
- `a2a/cstp/guardrails_service.py` — Guardrail evaluation + `CelGuardrailEvaluator` (F054)
- `a2a/cstp/provenance_service.py` — F055 evidence ingest, control mapping, bundle export
- `a2a/cstp/provenance/mapping.py` — F055 YAML rules engine
- `a2a/cstp/provenance/mappings/` — SR 11-7, NIST AI RMF, CSTP-attested control rules
- `a2a/cstp/storage/provenance.py` — F055 hash-chained SQLite evidence store
- `a2a/config.py` — Server configuration (YAML + env)
- `guardrails/cornerstone.yaml` — Default guardrail rules
- `config/server.yaml` — Default server config
