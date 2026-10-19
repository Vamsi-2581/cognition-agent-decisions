"""Root pytest configuration.

Sets safe dashboard credentials before any test module can import the dashboard
`config` module.

`dashboard/config.py` builds its `Config` dataclass defaults from `os.environ` at
class-definition time and exposes a module-level `config` singleton, so the very
first import of that module freezes the values for the whole session. Two test
trees import it — `tests/test_f049_deliberation.py` (flat `config`) and
`dashboard/tests/conftest.py` (via `dashboard.app`) — and since F057 an empty
DASHBOARD_PASS makes `enforce_security_config()` raise at import. That left the
combined suite depending on which tree imported first, with a collection error as
the failure mode.

`setdefault` keeps any value a developer or CI job has already exported.
"""

import os

import pytest

os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASS", "test-pass")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("CSTP_TOKEN", "test-token")

from a2a.cstp.embeddings import EmbeddingProvider  # noqa: E402


class OfflineEmbeddings(EmbeddingProvider):
    """Fixed vectors, so no test reaches the Gemini API."""

    async def embed(self, text: str) -> list[float]:
        return [0.1] * 768

    @property
    def dimensions(self) -> int:
        return 768

    @property
    def model_name(self) -> str:
        return "offline-test"


@pytest.fixture(autouse=True)
def _isolated_decision_store():
    """Give every test a fresh in-memory DecisionStore.

    CLAUDE.md already prescribes factory injection over patching internals, but
    nothing enforced it, so tests that never injected a store fell through to
    whatever `create_decision_store()` produced from the environment. That was
    invisible while `record_decision` swallowed store failures; now that the
    store write is authoritative, an uninitialized default backend surfaces as a
    genuine failure. Injecting here fixes the whole class at once and gives
    per-test isolation for free.

    Tests that need a specific backend still call `set_decision_store()`
    themselves — the later call wins for the rest of that test.
    """
    from a2a.cstp.storage.factory import set_decision_store
    from a2a.cstp.storage.memory import MemoryDecisionStore

    set_decision_store(MemoryDecisionStore())
    yield
    set_decision_store(None)


@pytest.fixture(autouse=True)
def _isolated_index_backends():
    """Give every test in-memory graph and vector stores and offline embeddings.

    The defaults reach outside the test: `NetworkXGraphStore` appends edges to
    the CWD-relative `data/graph_edges.jsonl`, `ChromaDBStore` talks to
    localhost:8000, and `GeminiEmbeddings` calls the Gemini API with whatever key
    the environment holds. Any test that reached `recordDecision`,
    `reviewDecision` or `pre_action` without injecting its own backends hit all
    three. CLAUDE.md requires tests to run offline.

    As above, a test's own `set_*()` call wins for the rest of that test.
    """
    from a2a.cstp.embeddings.factory import set_embedding_provider
    from a2a.cstp.graphdb.factory import set_graph_store
    from a2a.cstp.graphdb.memory import MemoryGraphStore
    from a2a.cstp.vectordb.factory import set_vector_store
    from a2a.cstp.vectordb.memory import MemoryStore

    set_graph_store(MemoryGraphStore())
    set_vector_store(MemoryStore())
    set_embedding_provider(OfflineEmbeddings())
    yield
    set_graph_store(None)
    set_vector_store(None)
    set_embedding_provider(None)
