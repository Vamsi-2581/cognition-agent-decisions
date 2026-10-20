"""Tests for F002: cstp.queryDecisions implementation."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from a2a.auth import AuthManager, set_auth_manager
from a2a.config import AgentConfig, AuthConfig, AuthToken, Config, ServerConfig
from a2a.cstp import get_dispatcher, register_methods
from a2a.cstp.models import (
    DecisionSummary,
    QueryDecisionsRequest,
    QueryDecisionsResponse,
    QueryFilters,
)
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

    # Initialize auth and dispatcher
    auth_manager = AuthManager(config)
    set_auth_manager(auth_manager)
    app.state.auth_manager = auth_manager
    app.state.config = config
    app.state.start_time = 0.0

    dispatcher = get_dispatcher()
    register_methods(dispatcher)
    app.state.dispatcher = dispatcher

    return TestClient(app)


class TestQueryDecisionsRequest:
    """Tests for QueryDecisionsRequest parsing."""

    def test_from_params_minimal(self) -> None:
        """Minimal params should parse."""
        params = {"query": "test query"}
        req = QueryDecisionsRequest.from_params(params)
        assert req.query == "test query"
        assert req.limit == 10
        assert req.include_reasons is False

    def test_from_params_full(self) -> None:
        """Full params should parse."""
        params = {
            "query": "database migration",
            "filters": {
                "category": "architecture",
                "minConfidence": 0.7,
                "stakes": ["high", "medium"],
            },
            "limit": 25,
            "includeReasons": True,
        }
        req = QueryDecisionsRequest.from_params(params)
        assert req.query == "database migration"
        assert req.filters.category == "architecture"
        assert req.filters.min_confidence == 0.7
        assert req.filters.stakes == ["high", "medium"]
        assert req.limit == 25
        assert req.include_reasons is True

    def test_from_params_empty_query_allowed(self) -> None:
        """Empty query should be allowed for listing all decisions."""
        req = QueryDecisionsRequest.from_params({})
        assert req.query == ""

    def test_limit_clamped(self) -> None:
        """Limit should be clamped to 1-50."""
        req = QueryDecisionsRequest.from_params({"query": "test", "limit": 100})
        assert req.limit == 50

        req = QueryDecisionsRequest.from_params({"query": "test", "limit": 0})
        assert req.limit == 1


class TestQueryFilters:
    """Tests for QueryFilters parsing."""

    def test_from_dict_empty(self) -> None:
        """Empty dict should create default filters."""
        filters = QueryFilters.from_dict({})
        assert filters.category is None
        assert filters.min_confidence == 0.0
        assert filters.max_confidence == 1.0

    def test_from_dict_none(self) -> None:
        """None should create default filters."""
        filters = QueryFilters.from_dict(None)
        assert filters.category is None

    def test_from_dict_full(self) -> None:
        """Full dict should parse all fields."""
        data = {
            "category": "security",
            "minConfidence": 0.8,
            "maxConfidence": 0.95,
            "dateAfter": "2026-01-01T00:00:00Z",
            "stakes": ["critical"],
            "status": ["decided"],
        }
        filters = QueryFilters.from_dict(data)
        assert filters.category == "security"
        assert filters.min_confidence == 0.8
        assert filters.max_confidence == 0.95
        assert filters.date_after is not None
        assert filters.stakes == ["critical"]
        assert filters.status == ["decided"]


class TestDecisionSummary:
    """Tests for DecisionSummary serialization."""

    def test_to_dict_minimal(self) -> None:
        """Minimal summary should serialize."""
        summary = DecisionSummary(
            id="abc12345",
            title="Test decision",
            category="architecture",
            confidence=0.9,
            stakes="high",
            status="decided",
            outcome=None,
            date="2026-01-20T14:00:00Z",
            distance=0.23,
        )
        data = summary.to_dict()
        assert data["id"] == "abc12345"
        assert data["title"] == "Test decision"
        assert "outcome" not in data
        assert "reasons" not in data

    def test_to_dict_with_outcome_and_reasons(self) -> None:
        """Full summary should serialize all fields."""
        summary = DecisionSummary(
            id="abc12345",
            title="Test decision",
            category="architecture",
            confidence=0.9,
            stakes="high",
            status="reviewed",
            outcome="success",
            date="2026-01-20T14:00:00Z",
            distance=0.23,
            reasons=[{"type": "pattern", "text": "Matches known pattern"}, {"type": "analysis", "text": "Analyzed alternatives"}],
        )
        data = summary.to_dict()
        assert data["outcome"] == "success"
        assert data["reasons"] == [{"type": "pattern", "text": "Matches known pattern"}, {"type": "analysis", "text": "Analyzed alternatives"}]


class TestQueryDecisionsResponse:
    """Tests for QueryDecisionsResponse serialization."""

    def test_to_dict(self) -> None:
        """Response should serialize correctly."""
        response = QueryDecisionsResponse(
            decisions=[
                DecisionSummary(
                    id="abc12345",
                    title="Test",
                    category="arch",
                    confidence=0.9,
                    stakes="high",
                    status="decided",
                    outcome=None,
                    date="2026-01-20T14:00:00Z",
                    distance=0.23,
                )
            ],
            total=1,
            query="test query",
            query_time_ms=45,
            agent="test-agent",
        )
        data = response.to_dict()
        assert data["total"] == 1
        assert data["query"] == "test query"
        assert data["queryTimeMs"] == 45
        assert data["agent"] == "test-agent"
        assert len(data["decisions"]) == 1


class TestQueryDecisionsEndpoint:
    """Integration tests for cstp.queryDecisions endpoint."""

    def test_query_empty_param_lists_decisions(self, client: TestClient) -> None:
        """Empty query param should list all decisions."""
        response = client.post(
            "/cstp",
            json={
                "jsonrpc": "2.0",
                "method": "cstp.queryDecisions",
                "id": "test-1",
                "params": {},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        data = response.json()
        assert "result" in data
        assert data["result"]["retrievalMode"] == "list"

    @patch("a2a.cstp.dispatcher.query_decisions")
    def test_query_returns_results(
        self, mock_query: AsyncMock, client: TestClient
    ) -> None:
        """Valid query should return results."""
        # Mock query response
        mock_query.return_value = QueryResponse(
            results=[
                QueryResult(
                    id="dec-12345678",
                    title="Use blue-green deployment",
                    category="architecture",
                    confidence=0.9,
                    stakes="high",
                    status="reviewed",
                    outcome="success",
                    date="2026-01-20T14:00:00Z",
                    distance=0.23,
                    reason_types=["pattern", "analysis"],
                )
            ],
            query="database migration",
            query_time_ms=45,
        )

        response = client.post(
            "/cstp",
            json={
                "jsonrpc": "2.0",
                "method": "cstp.queryDecisions",
                "id": "test-1",
                "params": {"query": "database migration"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        data = response.json()

        assert "result" in data
        assert data["result"]["total"] == 1
        assert data["result"]["query"] == "database migration"
        assert len(data["result"]["decisions"]) == 1
        assert data["result"]["decisions"][0]["title"] == "Use blue-green deployment"

    @patch("a2a.cstp.dispatcher.query_decisions")
    def test_query_with_filters(
        self, mock_query: AsyncMock, client: TestClient
    ) -> None:
        """Query with filters should pass them to query_decisions."""
        mock_query.return_value = QueryResponse(
            results=[],
            query="test",
            query_time_ms=10,
        )

        client.post(
            "/cstp",
            json={
                "jsonrpc": "2.0",
                "method": "cstp.queryDecisions",
                "id": "test-1",
                "params": {
                    "query": "test",
                    "filters": {
                        "category": "security",
                        "minConfidence": 0.8,
                        "stakes": ["high"],
                    },
                    "limit": 20,
                },
            },
            headers={"Authorization": "Bearer test-token"},
        )

        # Verify filters were passed
        mock_query.assert_called_once()
        call_kwargs = mock_query.call_args.kwargs
        assert call_kwargs["category"] == "security"
        assert call_kwargs["min_confidence"] == 0.8
        assert call_kwargs["stakes"] == ["high"]
        assert call_kwargs["n_results"] == 20

    @patch("a2a.cstp.dispatcher.query_decisions")
    def test_query_error_returns_internal_error(
        self, mock_query: AsyncMock, client: TestClient
    ) -> None:
        """Query error should return internal error."""
        mock_query.return_value = QueryResponse(
            results=[],
            query="test",
            query_time_ms=0,
            error="ChromaDB unavailable",
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

        assert "error" in data
        assert data["error"]["code"] == -32603  # Internal error
        # Backend detail stays in the server log; the caller gets a reference.
        assert data["error"]["message"].startswith("Internal error (ref ")
        assert "ChromaDB" not in json.dumps(data)
