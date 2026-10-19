"""Tests for issue #144: agent identity in MCP handlers.

Issue #144 forwarded the agent_id argument to the services as the caller's
identity. Identity now comes from the transport (the bearer token's agent, or
"mcp-client" for calls without an HTTP request), so these tests check that
_handle_pre_action_mcp, _handle_get_session_context_mcp and _handle_ready_mcp
pass the caller through, and that agent_id only labels deliberation buckets.
"""

import importlib.util
import sys
from unittest.mock import AsyncMock, MagicMock

# Skip entire module if mcp is not installed (CI environment)
_mcp_available = False
try:
    _mcp_available = importlib.util.find_spec("mcp") is not None
except (ValueError, ModuleNotFoundError):
    pass
if not _mcp_available:
    mock_mcp = MagicMock()
    sys.modules["mcp"] = mock_mcp
    sys.modules["mcp.server"] = mock_mcp.server
    sys.modules["mcp.server.stdio"] = mock_mcp.server.stdio
    sys.modules["mcp.server.streamable_http_manager"] = mock_mcp.server.streamable_http_manager
    sys.modules["mcp.types"] = mock_mcp.types
    mock_mcp.types.TextContent = type(
        "TextContent",
        (),
        {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
    )

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402


class _OutsideAnyRequest:
    """Stands in for the mocked SDK's Server outside a request."""

    @property
    def request_context(self):  # type: ignore[no-untyped-def]
        raise LookupError


@pytest.fixture
def _outside_an_mcp_request(monkeypatch):
    """Handlers called directly run outside any MCP request.

    The real Server.request_context raises LookupError there, which makes the
    caller the local agent; the mocked SDK used without mcp would hand back a
    mock request instead.
    """
    if not _mcp_available:
        import a2a.mcp_server as server

        monkeypatch.setattr(server, "mcp_app", _OutsideAnyRequest())


# ---------------------------------------------------------------------------
# pre_action handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_outside_an_mcp_request")
async def test_pre_action_agent_id_is_only_a_label() -> None:
    """pre_action keeps agent_id as the bucket label, not the identity."""
    mock_response = MagicMock()
    mock_response.to_dict.return_value = {"allowed": True, "decisionId": "abc12345"}

    with patch(
        "a2a.cstp.preaction_service.pre_action",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_fn:
        from a2a.mcp_server import _handle_pre_action_mcp

        await _handle_pre_action_mcp({
            "action": {"description": "test action"},
            "agent_id": "my-agent",
        })

        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs["agent_id"] == "mcp-client"
        assert kwargs["tracker_key"] == "mcp-client|mcp:my-agent"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_outside_an_mcp_request")
async def test_pre_action_defaults_to_mcp_client() -> None:
    """pre_action handler defaults agent_id to 'mcp-client' when omitted."""
    mock_response = MagicMock()
    mock_response.to_dict.return_value = {"allowed": True, "decisionId": "abc12345"}

    with patch(
        "a2a.cstp.preaction_service.pre_action",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_fn:
        from a2a.mcp_server import _handle_pre_action_mcp

        await _handle_pre_action_mcp({
            "action": {"description": "test action"},
        })

        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs["agent_id"] == "mcp-client"


# ---------------------------------------------------------------------------
# get_session_context handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_outside_an_mcp_request")
async def test_get_session_context_ignores_agent_id_for_identity() -> None:
    """get_session_context passes the caller, whatever agent_id says."""
    mock_response = MagicMock()
    mock_response.to_dict.return_value = {"markdown": "# Context"}

    with patch(
        "a2a.cstp.session_context_service.get_session_context",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_fn:
        from a2a.mcp_server import _handle_get_session_context_mcp

        await _handle_get_session_context_mcp({
            "agent_id": "planner-agent",
        })

        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs["agent_id"] == "mcp-client"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_outside_an_mcp_request")
async def test_get_session_context_defaults_to_mcp_client() -> None:
    """get_session_context handler defaults agent_id to 'mcp-client' when omitted."""
    mock_response = MagicMock()
    mock_response.to_dict.return_value = {"markdown": "# Context"}

    with patch(
        "a2a.cstp.session_context_service.get_session_context",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_fn:
        from a2a.mcp_server import _handle_get_session_context_mcp

        await _handle_get_session_context_mcp({})

        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs["agent_id"] == "mcp-client"


# ---------------------------------------------------------------------------
# ready handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_outside_an_mcp_request")
async def test_ready_ignores_agent_id_for_identity() -> None:
    """ready passes the caller, whatever agent_id says."""
    mock_response = MagicMock()
    mock_response.to_dict.return_value = {"actions": [], "total": 0}

    with patch(
        "a2a.cstp.ready_service.get_ready_actions",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_fn:
        from a2a.mcp_server import _handle_ready_mcp

        await _handle_ready_mcp({
            "agent_id": "dev-agent",
        })

        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs["agent_id"] == "mcp-client"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_outside_an_mcp_request")
async def test_ready_defaults_to_mcp_client() -> None:
    """ready handler defaults agent_id to 'mcp-client' when omitted."""
    mock_response = MagicMock()
    mock_response.to_dict.return_value = {"actions": [], "total": 0}

    with patch(
        "a2a.cstp.ready_service.get_ready_actions",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_fn:
        from a2a.mcp_server import _handle_ready_mcp

        await _handle_ready_mcp({})

        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs["agent_id"] == "mcp-client"


class TestStdioInitializesDecisionStore:
    """Regression: `cstp-mcp` never enters the FastAPI lifespan.

    With SQLite as the default backend an uninitialized store serves every
    request with `_conn is None`, so authoritative store writes fail — and
    record_decision then deletes the YAML it just wrote, believing nothing
    landed.
    """

    async def test_store_is_initialized_and_closed(self) -> None:
        import contextlib
        from unittest.mock import patch

        from a2a import mcp_server
        from a2a.cstp.storage.factory import set_decision_store
        from a2a.cstp.storage.memory import MemoryDecisionStore

        store = MemoryDecisionStore()
        calls: list[str] = []

        async def _init() -> None:
            calls.append("initialize")

        async def _close() -> None:
            calls.append("close")

        store.initialize = _init  # type: ignore[method-assign]
        store.close = _close  # type: ignore[method-assign]
        set_decision_store(store)

        @contextlib.asynccontextmanager
        async def _fake_stdio():
            yield (object(), object())

        async def _fake_run(*args: object, **kwargs: object) -> None:
            calls.append("serve")

        with (
            patch.object(mcp_server, "stdio_server", _fake_stdio),
            patch.object(mcp_server.mcp_app, "run", _fake_run),
            patch.object(mcp_server.mcp_app, "create_initialization_options", lambda: {}),
        ):
            await mcp_server.run_stdio()

        assert calls == ["initialize", "serve", "close"]

    async def test_store_is_closed_even_if_serving_raises(self) -> None:
        import contextlib
        from unittest.mock import patch

        from a2a import mcp_server
        from a2a.cstp.storage.factory import set_decision_store
        from a2a.cstp.storage.memory import MemoryDecisionStore

        store = MemoryDecisionStore()
        closed: list[bool] = []

        async def _init() -> None:
            return None

        async def _close() -> None:
            closed.append(True)

        store.initialize = _init  # type: ignore[method-assign]
        store.close = _close  # type: ignore[method-assign]
        set_decision_store(store)

        @contextlib.asynccontextmanager
        async def _fake_stdio():
            yield (object(), object())

        async def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("transport died")

        with (
            patch.object(mcp_server, "stdio_server", _fake_stdio),
            patch.object(mcp_server.mcp_app, "run", _boom),
            patch.object(mcp_server.mcp_app, "create_initialization_options", lambda: {}),
            pytest.raises(RuntimeError, match="transport died"),
        ):
            await mcp_server.run_stdio()

        assert closed == [True]


class TestStdioInitializesGraphStore:
    """Regression: `cstp-mcp` never loaded the persisted graph.

    The FastAPI lifespan initializes the graph store; the stdio entrypoint did
    not, so get_graph/get_neighbors reported saved decisions as "Node not found".
    """

    async def test_persisted_edges_are_loaded_before_serving(self, tmp_path) -> None:
        import contextlib

        from a2a import mcp_server
        from a2a.cstp.graphdb import GraphEdge
        from a2a.cstp.graphdb.factory import get_graph_store, set_graph_store
        from a2a.cstp.graphdb.networkx_store import NetworkXGraphStore
        from a2a.cstp.graphdb.persistence import save_edges_to_jsonl

        edges_path = tmp_path / "graph_edges.jsonl"
        save_edges_to_jsonl(
            [GraphEdge(source_id="aaaa1111", target_id="bbbb2222", edge_type="relates_to")],
            edges_path,
        )
        set_graph_store(NetworkXGraphStore(persistence_path=str(edges_path)))

        seen: dict[str, object] = {}

        @contextlib.asynccontextmanager
        async def _fake_stdio():
            yield (object(), object())

        async def _fake_run(*args: object, **kwargs: object) -> None:
            store = get_graph_store()
            seen["edges"] = await store.edge_count()
            seen["node"] = await store.get_node("aaaa1111")

        with (
            patch.object(mcp_server, "stdio_server", _fake_stdio),
            patch.object(mcp_server.mcp_app, "run", _fake_run),
            patch.object(mcp_server.mcp_app, "create_initialization_options", lambda: {}),
        ):
            await mcp_server.run_stdio()

        assert seen["edges"] == 1
        assert seen["node"] is not None

    async def test_graph_failure_does_not_stop_serving(self) -> None:
        import contextlib

        from a2a import mcp_server
        from a2a.cstp.graphdb.factory import set_graph_store
        from a2a.cstp.graphdb.memory import MemoryGraphStore

        store = MemoryGraphStore()

        async def _broken_init() -> None:
            raise OSError("graph file unreadable")

        store.initialize = _broken_init  # type: ignore[method-assign]
        set_graph_store(store)
        served: list[bool] = []

        @contextlib.asynccontextmanager
        async def _fake_stdio():
            yield (object(), object())

        async def _fake_run(*args: object, **kwargs: object) -> None:
            served.append(True)

        with (
            patch.object(mcp_server, "stdio_server", _fake_stdio),
            patch.object(mcp_server.mcp_app, "run", _fake_run),
            patch.object(mcp_server.mcp_app, "create_initialization_options", lambda: {}),
        ):
            await mcp_server.run_stdio()

        assert served == [True]
