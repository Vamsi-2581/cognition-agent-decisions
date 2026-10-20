"""Tests for CSTP server infrastructure (F001)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from a2a.auth import AuthManager, set_auth_manager
from a2a.config import AgentConfig, AuthConfig, AuthToken, Config, ServerConfig
from a2a.cstp import get_dispatcher, register_methods
from a2a.cstp.query_service import QueryResponse, QueryResult
from a2a.server import create_app


@pytest.fixture
def config() -> Config:
    """Create test configuration."""
    return Config(
        server=ServerConfig(host="127.0.0.1", port=8100),
        agent=AgentConfig(
            name="test-agent",
            description="Test agent",
            version="0.7.0",
            url="http://localhost:8100",
        ),
        auth=AuthConfig(
            enabled=True,
            tokens=[AuthToken(agent="test", token="test-token")],
        ),
    )


@pytest.fixture
def client(config: Config) -> TestClient:
    """Create test client with configured app."""
    app = create_app(config)

    # Manually initialize auth manager for tests (lifespan may not run)
    auth_manager = AuthManager(config)
    set_auth_manager(auth_manager)
    app.state.auth_manager = auth_manager
    app.state.config = config
    app.state.start_time = 0.0

    # Initialize dispatcher
    dispatcher = get_dispatcher()
    register_methods(dispatcher)
    app.state.dispatcher = dispatcher

    return TestClient(app)


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_returns_200(self, client: TestClient) -> None:
        """Health endpoint should return 200."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_returns_status(self, client: TestClient) -> None:
        """Health endpoint should return healthy status."""
        response = client.get("/health")
        data = response.json()
        assert data["status"] == "healthy"

    def test_health_returns_version(self, client: TestClient) -> None:
        """Health endpoint should return version."""
        response = client.get("/health")
        data = response.json()
        assert data["version"] == "0.7.0"

    def test_health_returns_uptime(self, client: TestClient) -> None:
        """Health endpoint should return uptime."""
        response = client.get("/health")
        data = response.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))


class TestAgentCardEndpoint:
    """Tests for GET /.well-known/agent.json."""

    def test_agent_card_returns_200(self, client: TestClient) -> None:
        """Agent card endpoint should return 200."""
        response = client.get("/.well-known/agent.json")
        assert response.status_code == 200

    def test_agent_card_returns_name(self, client: TestClient) -> None:
        """Agent card should include name."""
        response = client.get("/.well-known/agent.json")
        data = response.json()
        assert data["name"] == "test-agent"

    def test_agent_card_returns_version(self, client: TestClient) -> None:
        """Agent card should include version."""
        response = client.get("/.well-known/agent.json")
        data = response.json()
        assert data["version"] == "0.7.0"

    def test_agent_card_returns_capabilities(self, client: TestClient) -> None:
        """Agent card should include CSTP capabilities."""
        response = client.get("/.well-known/agent.json")
        data = response.json()
        assert "capabilities" in data
        assert "cstp" in data["capabilities"]
        assert "methods" in data["capabilities"]["cstp"]

    def test_agent_card_lists_methods(self, client: TestClient) -> None:
        """Agent card should list available CSTP methods."""
        response = client.get("/.well-known/agent.json")
        data = response.json()
        methods = data["capabilities"]["cstp"]["methods"]
        assert "cstp.queryDecisions" in methods
        assert "cstp.checkGuardrails" in methods


class TestCstpEndpointAuth:
    """Tests for POST /cstp authentication."""

    def test_cstp_requires_auth(self, client: TestClient) -> None:
        """CSTP endpoint should require authentication."""
        response = client.post("/cstp", json={"jsonrpc": "2.0", "method": "cstp.queryDecisions"})
        assert response.status_code == 401

    def test_cstp_rejects_invalid_token(self, client: TestClient) -> None:
        """CSTP endpoint should reject invalid tokens."""
        response = client.post(
            "/cstp",
            json={"jsonrpc": "2.0", "method": "cstp.queryDecisions"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    def test_cstp_rejects_non_ascii_token(self, client: TestClient) -> None:
        """CSTP endpoint should reject a non-ASCII bearer token with 401."""
        response = client.post(
            "/cstp",
            json={"jsonrpc": "2.0", "method": "cstp.queryDecisions"},
            headers=[(b"Authorization", b"Bearer \xff")],
        )
        assert response.status_code == 401

    @patch("a2a.cstp.dispatcher.query_decisions")
    def test_cstp_accepts_valid_token(
        self, mock_query: AsyncMock, client: TestClient
    ) -> None:
        """CSTP endpoint should accept valid tokens."""
        mock_query.return_value = QueryResponse(
            results=[], query="test", query_time_ms=0
        )
        response = client.post(
            "/cstp",
            json={
                "jsonrpc": "2.0",
                "method": "cstp.queryDecisions",
                "id": "test-1",
                "params": {"query": "test"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200


class TestCstpEndpointJsonRpc:
    """Tests for POST /cstp JSON-RPC handling."""

    @patch("a2a.cstp.dispatcher.query_decisions")
    def test_cstp_returns_jsonrpc_response(
        self, mock_query: AsyncMock, client: TestClient
    ) -> None:
        """CSTP endpoint should return JSON-RPC response."""
        mock_query.return_value = QueryResponse(
            results=[], query="test", query_time_ms=0
        )
        response = client.post(
            "/cstp",
            json={
                "jsonrpc": "2.0",
                "method": "cstp.queryDecisions",
                "id": "test-1",
                "params": {"query": "test"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        data = response.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == "test-1"

    def test_cstp_rejects_invalid_json(self, client: TestClient) -> None:
        """CSTP endpoint should reject invalid JSON."""
        response = client.post(
            "/cstp",
            content="not json",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == -32700  # Parse error

    def test_cstp_rejects_unknown_method(self, client: TestClient) -> None:
        """CSTP endpoint should reject unknown methods."""
        response = client.post(
            "/cstp",
            json={
                "jsonrpc": "2.0",
                "method": "cstp.unknownMethod",
                "id": "test-1",
                "params": {},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == -32601  # Method not found

    def test_cstp_rejects_non_cstp_method(self, client: TestClient) -> None:
        """CSTP endpoint should reject non-CSTP methods."""
        response = client.post(
            "/cstp",
            json={
                "jsonrpc": "2.0",
                "method": "other.method",
                "id": "test-1",
                "params": {},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == -32601


class TestQueryDecisions:
    """Tests for cstp.queryDecisions (with mocked service)."""

    @patch("a2a.cstp.dispatcher.query_decisions")
    def test_query_decisions_returns_result(
        self, mock_query: AsyncMock, client: TestClient
    ) -> None:
        """queryDecisions should return result."""
        mock_query.return_value = QueryResponse(
            results=[
                QueryResult(
                    id="dec-123",
                    title="Test decision",
                    category="arch",
                    confidence=0.9,
                    stakes="high",
                    status="decided",
                    outcome=None,
                    date="2026-01-20",
                    distance=0.2,
                )
            ],
            query="test query",
            query_time_ms=45,
        )
        response = client.post(
            "/cstp",
            json={
                "jsonrpc": "2.0",
                "method": "cstp.queryDecisions",
                "id": "test-1",
                "params": {"query": "test query"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        data = response.json()
        assert "result" in data
        assert data["result"]["query"] == "test query"
        assert "decisions" in data["result"]
        assert len(data["result"]["decisions"]) == 1


class TestCheckGuardrails:
    """Tests for cstp.checkGuardrails (with mocked service)."""

    @patch("a2a.cstp.dispatcher.log_guardrail_check")
    @patch("a2a.cstp.dispatcher.evaluate_guardrails")
    def test_check_guardrails_returns_result(
        self, mock_eval: AsyncMock, mock_log: AsyncMock, client: TestClient
    ) -> None:
        """checkGuardrails should return result."""
        from a2a.cstp.guardrails_service import EvaluationResult

        mock_eval.return_value = EvaluationResult(
            allowed=True,
            violations=[],
            warnings=[],
            evaluated=5,
        )
        response = client.post(
            "/cstp",
            json={
                "jsonrpc": "2.0",
                "method": "cstp.checkGuardrails",
                "id": "test-1",
                "params": {"action": {"description": "test action"}},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        data = response.json()
        assert "result" in data
        assert data["result"]["allowed"] is True
        assert "violations" in data["result"]


class _RecordingSessionManager:
    """Stands in for StreamableHTTPSessionManager; records what reached it."""

    def __init__(self) -> None:
        self.calls = 0
        self.agent_ids: list[str | None] = []

    async def handle_request(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        from starlette.responses import JSONResponse as StarletteJSON

        self.calls += 1
        self.agent_ids.append(scope.get("state", {}).get("agent_id"))
        await StarletteJSON({"ok": True})(scope, receive, send)


class TestMcpEndpointAuth:
    """Regression: /mcp is a raw ASGI mount, so the /cstp auth dependency never ran.

    Unauthenticated callers could list and call every MCP tool, including
    log_decision and update_decision.
    """

    _INITIALIZE = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}

    def test_missing_token_is_rejected_before_mcp(self, client: TestClient) -> None:
        manager = _RecordingSessionManager()
        client.app.state.mcp_session_manager = manager  # type: ignore[attr-defined]

        response = client.post("/mcp/", json=self._INITIALIZE)

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert manager.calls == 0

    def test_invalid_token_is_rejected_before_mcp(self, client: TestClient) -> None:
        manager = _RecordingSessionManager()
        client.app.state.mcp_session_manager = manager  # type: ignore[attr-defined]

        response = client.post(
            "/mcp/", json=self._INITIALIZE, headers={"Authorization": "Bearer wrong"}
        )

        assert response.status_code == 401
        assert manager.calls == 0

    def test_non_ascii_token_is_rejected_before_mcp(self, client: TestClient) -> None:
        manager = _RecordingSessionManager()
        client.app.state.mcp_session_manager = manager  # type: ignore[attr-defined]

        response = client.post(
            "/mcp/", json=self._INITIALIZE, headers=[(b"Authorization", b"Bearer \xff")]
        )

        assert response.status_code == 401
        assert manager.calls == 0

    def test_token_agent_reaches_mcp_in_request_state(self, client: TestClient) -> None:
        # MCP tools read their identity from here, not from tool arguments.
        manager = _RecordingSessionManager()
        client.app.state.mcp_session_manager = manager  # type: ignore[attr-defined]

        response = client.post(
            "/mcp/", json=self._INITIALIZE, headers={"Authorization": "Bearer test-token"}
        )

        assert response.status_code == 200
        assert manager.agent_ids == ["test"]

    def test_valid_token_passes_auth(self, client: TestClient) -> None:
        # No session manager: a 503 means the request got past the auth check.
        client.app.state.mcp_session_manager = None  # type: ignore[attr-defined]

        response = client.post(
            "/mcp/", json=self._INITIALIZE, headers={"Authorization": "Bearer test-token"}
        )

        assert response.status_code == 503
