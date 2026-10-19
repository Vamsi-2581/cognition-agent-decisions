"""The demo seed output must load through the server's real storage code.

`demo/seed_data.py` is stdlib-only so the quick start needs no install, which
means nothing imports it in normal runs. Its hand-written schema drifted from
`SQLiteDecisionStore` (no `created_at`, so `initialize()` failed on the seeded
file) and its edge lines used `source`/`target`/`type`, so 0 of 13 edges loaded.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from a2a.cstp.graphdb.networkx_store import NetworkXGraphStore
from a2a.cstp.graphdb.persistence import load_edges_from_jsonl
from a2a.cstp.storage import ListQuery
from a2a.cstp.storage.sqlite import SQLiteDecisionStore

SEED_SCRIPT = Path(__file__).resolve().parent.parent / "demo" / "seed_data.py"


@pytest.fixture
def seed() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_seed_data", SEED_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_seeded_database_opens_in_the_server_store(seed: ModuleType, tmp_path: Path) -> None:
    db_path = tmp_path / "decisions.db"
    seed.create_database(str(db_path))

    store = SQLiteDecisionStore(str(db_path))
    await store.initialize()
    try:
        listed = await store.list(ListQuery(limit=100))
        reviewed = await store.list(ListQuery(limit=100, status="reviewed"))
        found = await store.list(ListQuery(limit=100, search="Redis"))
    finally:
        await store.close()

    assert listed.total == len(seed.DECISIONS)
    assert reviewed.total == sum(1 for d in seed.DECISIONS if d.get("outcome"))
    assert found.total >= 1, "seeded rows must be in the FTS index"


async def test_every_seeded_edge_loads_into_the_graph(seed: ModuleType, tmp_path: Path) -> None:
    db_path = tmp_path / "decisions.db"
    seed.create_database(str(db_path))
    edges_path = tmp_path / "graph_edges.jsonl"

    edges = load_edges_from_jsonl(edges_path)
    graph = NetworkXGraphStore(persistence_path=str(edges_path))
    await graph.initialize()

    store = SQLiteDecisionStore(str(db_path))
    await store.initialize()
    try:
        ids = {d["id"] for d in (await store.list(ListQuery(limit=100))).decisions}
    finally:
        await store.close()

    assert len(edges) == len(seed.GRAPH_EDGES)
    assert await graph.edge_count() == len(edges)
    assert {e.source_id for e in edges} | {e.target_id for e in edges} <= ids
