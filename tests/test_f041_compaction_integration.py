"""Tests for F041 P2: Compaction auto-wiring integration.

Tests the 4 integration points added by issue #124:
1. Wisdom in get_session_context (JSON + markdown)
2. Compacted query results (compacted flag, compaction_level annotation)
3. Auto-compact on startup (server lifespan)
4. Compact on reviewDecision (compactionLevel in response)
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from a2a.cstp.dispatcher import (
    CstpDispatcher,
    _annotate_compaction_levels,
    register_methods,
)
from a2a.cstp.models import (
    CompactRequest,
    DecisionSummary,
    QueryDecisionsRequest,
    QueryDecisionsResponse,
    SessionContextRequest,
    SessionContextResponse,
    WisdomEntry,
    WisdomPrinciple,
)
from a2a.cstp.session_context_service import _render_markdown
from a2a.models.jsonrpc import JsonRpcRequest


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 2, 15, 12, 0, 0, tzinfo=UTC)


def _make_decision(
    *,
    decision_id: str = "aabbccdd",
    age_days: int = 0,
    status: str = "reviewed",
    outcome: str = "success",
    category: str = "architecture",
    confidence: float = 0.9,
    pattern: str | None = None,
) -> dict[str, Any]:
    """Create a decision dict at a specific age."""
    decision_date = NOW - timedelta(days=age_days)
    d: dict[str, Any] = {
        "id": decision_id,
        "summary": f"Test decision {decision_id}",
        "decision": f"Test decision {decision_id}",
        "category": category,
        "confidence": confidence,
        "status": status,
        "outcome": outcome if status == "reviewed" else None,
        "date": decision_date.isoformat(),
    }
    if pattern:
        d["pattern"] = pattern
    return d


def _make_wisdom_decisions(
    count: int = 10,
    category: str = "architecture",
    pattern: str | None = "Consistent pattern",
) -> list[dict[str, Any]]:
    """Create reviewed wisdom-age (90+ day) decisions."""
    decisions = []
    for i in range(count):
        outcome = "success" if i % 3 != 0 else "failure"
        decisions.append(_make_decision(
            decision_id=f"wd{i:06d}",
            age_days=100 + i,
            category=category,
            outcome=outcome,
            confidence=0.8 + (i % 3) * 0.05,
            pattern=pattern if i < 6 else None,
        ))
    return decisions


@dataclass
class MockQueryResult:
    id: str = "abc12345"
    title: str = "Test decision"
    category: str = "architecture"
    confidence: float = 0.8
    stakes: str = "medium"
    status: str = "reviewed"
    outcome: str = "success"
    date: str = "2026-01-15"
    distance: float = 0.12
    reason_types: list[str] | None = None
    tags: list[str] | None = None
    pattern: str | None = None
    # F163: Enrichment fields
    lessons: str | None = None
    actual_result: str | None = None
    reasons: list[dict[str, str]] | None = None
    bridge: dict[str, str] | None = None


@dataclass
class MockQueryResponse:
    results: list[MockQueryResult] = field(default_factory=list)
    query: str = "test"
    query_time_ms: int = 10
    error: str | None = None


@pytest.fixture
def dispatcher() -> CstpDispatcher:
    d = CstpDispatcher()
    register_methods(d)
    return d


# ===========================================================================
# 1. Wisdom in get_session_context
# ===========================================================================


class TestWisdomInSessionContext:
    """Test wisdom data included in get_session_context response."""

    @pytest.mark.asyncio
    @patch("a2a.cstp.session_context_service.list_guardrails")
    @patch("a2a.cstp.session_context_service.query_decisions")
    @patch("a2a.cstp.session_context_service.load_all_decisions")
    async def test_wisdom_included_in_json_when_available(
        self,
        mock_load: AsyncMock,
        mock_query: AsyncMock,
        mock_guardrails: AsyncMock,
    ) -> None:
        """Wisdom entries should appear in JSON response when decisions exist."""
        mock_load.return_value = _make_wisdom_decisions(count=10)
        mock_query.return_value = MockQueryResponse()
        mock_guardrails.return_value = []

        from a2a.cstp.session_context_service import get_session_context

        req = SessionContextRequest.from_params({
            "include": ["wisdom"],
        })
        resp = await get_session_context(req, agent_id="test-agent")

        assert len(resp.wisdom_entries) > 0
        assert resp.wisdom_entries[0].category == "architecture"
        assert resp.wisdom_entries[0].decisions == 10

        # JSON format should include wisdom key
        data = resp.to_dict()
        assert "wisdom" in data
        assert len(data["wisdom"]) > 0
        assert data["wisdom"][0]["category"] == "architecture"

    @pytest.mark.asyncio
    @patch("a2a.cstp.session_context_service.list_guardrails")
    @patch("a2a.cstp.session_context_service.query_decisions")
    @patch("a2a.cstp.session_context_service.load_all_decisions")
    async def test_wisdom_empty_when_no_old_decisions(
        self,
        mock_load: AsyncMock,
        mock_query: AsyncMock,
        mock_guardrails: AsyncMock,
    ) -> None:
        """Wisdom should be empty when all decisions are recent."""
        from datetime import UTC, datetime, timedelta

        # Use genuinely recent dates (5 days ago from now) so the
        # wall-clock age check in build_wisdom correctly classifies them
        # as non-wisdom-age regardless of when the test runs.
        recent_date = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        mock_load.return_value = [
            {
                "id": f"r{i}",
                "summary": f"Test decision r{i}",
                "decision": f"Test decision r{i}",
                "category": "architecture",
                "confidence": 0.9,
                "status": "reviewed",
                "outcome": "success",
                "date": recent_date,
            }
            for i in range(10)
        ]
        mock_query.return_value = MockQueryResponse()
        mock_guardrails.return_value = []

        from a2a.cstp.session_context_service import get_session_context

        req = SessionContextRequest.from_params({
            "include": ["wisdom"],
        })
        resp = await get_session_context(req, agent_id="test-agent")

        assert len(resp.wisdom_entries) == 0

        # JSON should not include wisdom key when empty
        data = resp.to_dict()
        assert "wisdom" not in data

    @pytest.mark.asyncio
    @patch("a2a.cstp.session_context_service.list_guardrails")
    @patch("a2a.cstp.session_context_service.query_decisions")
    @patch("a2a.cstp.session_context_service.load_all_decisions")
    async def test_wisdom_not_fetched_when_not_in_include(
        self,
        mock_load: AsyncMock,
        mock_query: AsyncMock,
        mock_guardrails: AsyncMock,
    ) -> None:
        """Wisdom should not be computed if 'wisdom' not in include list."""
        mock_load.return_value = _make_wisdom_decisions(count=10)
        mock_query.return_value = MockQueryResponse()
        mock_guardrails.return_value = []

        from a2a.cstp.session_context_service import get_session_context

        req = SessionContextRequest.from_params({
            "include": ["patterns"],
        })
        resp = await get_session_context(req, agent_id="test-agent")

        assert len(resp.wisdom_entries) == 0

    @pytest.mark.asyncio
    @patch("a2a.cstp.session_context_service.list_guardrails")
    @patch("a2a.cstp.session_context_service.query_decisions")
    @patch("a2a.cstp.session_context_service.load_all_decisions")
    async def test_wisdom_in_markdown_format(
        self,
        mock_load: AsyncMock,
        mock_query: AsyncMock,
        mock_guardrails: AsyncMock,
    ) -> None:
        """Wisdom should appear in markdown output with correct formatting."""
        mock_load.return_value = _make_wisdom_decisions(count=10)
        mock_query.return_value = MockQueryResponse()
        mock_guardrails.return_value = []

        from a2a.cstp.session_context_service import get_session_context

        req = SessionContextRequest.from_params({
            "include": ["wisdom"],
            "format": "markdown",
        })
        resp = await get_session_context(req, agent_id="test-agent")

        assert resp.markdown is not None
        assert "### Wisdom" in resp.markdown
        assert "**architecture**" in resp.markdown
        assert "decisions" in resp.markdown
        assert "success" in resp.markdown

    @pytest.mark.asyncio
    @patch("a2a.cstp.session_context_service.list_guardrails")
    @patch("a2a.cstp.session_context_service.query_decisions")
    @patch("a2a.cstp.session_context_service.load_all_decisions")
    async def test_wisdom_markdown_includes_principles(
        self,
        mock_load: AsyncMock,
        mock_query: AsyncMock,
        mock_guardrails: AsyncMock,
    ) -> None:
        """Wisdom markdown should include key principles and failure modes."""
        decisions = _make_wisdom_decisions(count=10, pattern="Search before deciding")
        mock_load.return_value = decisions
        mock_query.return_value = MockQueryResponse()
        mock_guardrails.return_value = []

        from a2a.cstp.session_context_service import get_session_context

        req = SessionContextRequest.from_params({
            "include": ["wisdom"],
            "format": "markdown",
        })
        resp = await get_session_context(req, agent_id="test-agent")

        assert resp.markdown is not None
        assert "confirmations" in resp.markdown

    @pytest.mark.asyncio
    @patch("a2a.cstp.session_context_service.build_wisdom")
    @patch("a2a.cstp.session_context_service.list_guardrails")
    @patch("a2a.cstp.session_context_service.query_decisions")
    @patch("a2a.cstp.session_context_service.load_all_decisions")
    async def test_wisdom_error_handled_gracefully(
        self,
        mock_load: AsyncMock,
        mock_query: AsyncMock,
        mock_guardrails: AsyncMock,
        mock_wisdom: MagicMock,
    ) -> None:
        """If build_wisdom raises, session context should still work."""
        mock_load.return_value = []
        mock_query.return_value = MockQueryResponse()
        mock_guardrails.return_value = []
        mock_wisdom.side_effect = RuntimeError("Wisdom build failed")

        from a2a.cstp.session_context_service import get_session_context

        req = SessionContextRequest.from_params({
            "include": ["wisdom"],
        })
        # Should not raise
        resp = await get_session_context(req, agent_id="test-agent")
        assert len(resp.wisdom_entries) == 0

    def test_wisdom_opt_in_not_in_default_include(self) -> None:
        """'wisdom' should NOT be in the default include list (opt-in only)."""
        req = SessionContextRequest.from_params({})
        assert "wisdom" not in req.include

    def test_render_markdown_with_wisdom(self) -> None:
        """Test _render_markdown includes wisdom section."""
        from a2a.cstp.models import AgentProfile

        resp = SessionContextResponse(
            agent_profile=AgentProfile(),
            wisdom_entries=[
                WisdomEntry(
                    category="architecture",
                    decisions=15,
                    success_rate=0.85,
                    key_principles=[
                        WisdomPrinciple(
                            text="Search before deciding",
                            confirmations=5,
                            example_ids=["a1", "a2"],
                        ),
                    ],
                    common_failure_mode="Skipped pre-check",
                    avg_confidence=0.82,
                    brier_score=0.04,
                ),
            ],
        )
        md = _render_markdown(resp, "test-agent")
        assert "### Wisdom" in md
        assert "**architecture**" in md
        assert "15 decisions" in md
        assert "85% success" in md
        assert "Search before deciding" in md
        assert "5 confirmations" in md
        assert "Failure mode: Skipped pre-check" in md

    def test_render_markdown_no_wisdom(self) -> None:
        """Test _render_markdown omits wisdom section when empty."""
        from a2a.cstp.models import AgentProfile

        resp = SessionContextResponse(
            agent_profile=AgentProfile(),
            wisdom_entries=[],
        )
        md = _render_markdown(resp, "test-agent")
        assert "### Wisdom" not in md

    def test_session_context_response_to_dict_with_wisdom(self) -> None:
        """Test to_dict includes wisdom when entries exist."""
        from a2a.cstp.models import AgentProfile

        resp = SessionContextResponse(
            agent_profile=AgentProfile(),
            wisdom_entries=[
                WisdomEntry(category="arch", decisions=5),
            ],
        )
        data = resp.to_dict()
        assert "wisdom" in data
        assert len(data["wisdom"]) == 1

    def test_session_context_response_to_dict_without_wisdom(self) -> None:
        """Test to_dict excludes wisdom key when no entries."""
        from a2a.cstp.models import AgentProfile

        resp = SessionContextResponse(
            agent_profile=AgentProfile(),
            wisdom_entries=[],
        )
        data = resp.to_dict()
        assert "wisdom" not in data


# ===========================================================================
# 2. Compacted query results
# ===========================================================================


class TestCompactedQueryResults:
    """Test compacted=true/false on query results and pre_action."""

    def test_query_request_compacted_default_false(self) -> None:
        """compacted defaults to False for backward compatibility."""
        req = QueryDecisionsRequest.from_params({"query": "test"})
        assert req.compacted is False

    def test_query_request_compacted_true(self) -> None:
        """compacted can be set to True."""
        req = QueryDecisionsRequest.from_params({
            "query": "test",
            "compacted": True,
        })
        assert req.compacted is True

    def test_decision_summary_compaction_level_field(self) -> None:
        """DecisionSummary should have compaction_level field."""
        ds = DecisionSummary(
            id="abc",
            title="Test",
            category="arch",
            confidence=0.8,
            stakes="medium",
            status="reviewed",
            outcome="success",
            date="2026-02-10",
            distance=0.1,
            compaction_level="full",
        )
        data = ds.to_dict()
        assert data["compactionLevel"] == "full"

    def test_decision_summary_no_compaction_level(self) -> None:
        """compactionLevel should not appear when None."""
        ds = DecisionSummary(
            id="abc",
            title="Test",
            category="arch",
            confidence=0.8,
            stakes="medium",
            status="reviewed",
            outcome="success",
            date="2026-02-10",
            distance=0.1,
        )
        data = ds.to_dict()
        assert "compactionLevel" not in data

    def test_annotate_compaction_levels_sets_level(self) -> None:
        """_annotate_compaction_levels sets compaction_level on each summary."""
        result = QueryDecisionsResponse(
            decisions=[
                DecisionSummary(
                    id="d1", title="Recent", category="arch",
                    confidence=0.8, stakes="medium",
                    status="reviewed", outcome="success",
                    date=datetime.now(UTC).isoformat()[:10],
                    distance=0.1,
                ),
            ],
            total=1,
            query="test",
            query_time_ms=10,
            agent="test",
        )
        _annotate_compaction_levels(result)

        assert len(result.decisions) == 1
        assert result.decisions[0].compaction_level == "full"

    def test_annotate_compaction_levels_filters_wisdom(self) -> None:
        """Wisdom-level decisions are excluded from annotated results."""
        old_date = (datetime.now(UTC) - timedelta(days=100)).isoformat()[:10]
        result = QueryDecisionsResponse(
            decisions=[
                DecisionSummary(
                    id="d1", title="Recent", category="arch",
                    confidence=0.8, stakes="medium",
                    status="reviewed", outcome="success",
                    date=datetime.now(UTC).isoformat()[:10],
                    distance=0.1,
                ),
                DecisionSummary(
                    id="d2", title="Old", category="arch",
                    confidence=0.8, stakes="medium",
                    status="reviewed", outcome="success",
                    date=old_date,
                    distance=0.2,
                ),
            ],
            total=2,
            query="test",
            query_time_ms=10,
            agent="test",
        )
        _annotate_compaction_levels(result)

        assert result.total == 1
        assert len(result.decisions) == 1
        assert result.decisions[0].id == "d1"
        assert result.decisions[0].compaction_level == "full"

    def test_annotate_compaction_levels_empty(self) -> None:
        """Empty decision list should work."""
        result = QueryDecisionsResponse(
            decisions=[],
            total=0,
            query="test",
            query_time_ms=10,
            agent="test",
        )
        _annotate_compaction_levels(result)
        assert result.total == 0
        assert result.decisions == []

    def test_annotate_sets_summary_level(self) -> None:
        """Decisions 7-30 days old should get 'summary' level."""
        mid_date = (datetime.now(UTC) - timedelta(days=15)).isoformat()[:10]
        result = QueryDecisionsResponse(
            decisions=[
                DecisionSummary(
                    id="d1", title="Mid", category="arch",
                    confidence=0.8, stakes="medium",
                    status="reviewed", outcome="success",
                    date=mid_date,
                    distance=0.1,
                ),
            ],
            total=1,
            query="test",
            query_time_ms=10,
            agent="test",
        )
        _annotate_compaction_levels(result)

        assert result.decisions[0].compaction_level == "summary"

    @pytest.mark.asyncio
    @patch("a2a.cstp.preaction_service.record_decision")
    @patch("a2a.cstp.preaction_service.get_calibration")
    @patch("a2a.cstp.preaction_service.evaluate_guardrails")
    @patch("a2a.cstp.preaction_service.query_decisions")
    @patch("a2a.cstp.preaction_service.log_guardrail_check")
    async def test_pre_action_annotates_compaction_level(
        self,
        mock_log: AsyncMock,
        mock_query: AsyncMock,
        mock_guard: AsyncMock,
        mock_cal: AsyncMock,
        mock_record: AsyncMock,
    ) -> None:
        """pre_action always annotates compaction_level on relevant decisions."""
        mock_query.return_value = MockQueryResponse(
            results=[MockQueryResult(
                id="recent1",
                date=datetime.now(UTC).isoformat()[:10],
                status="reviewed",
            )],
        )

        @dataclass
        class MockEvalResult:
            allowed: bool = True
            violations: list = field(default_factory=list)  # type: ignore[type-arg]
            warnings: list = field(default_factory=list)  # type: ignore[type-arg]
            evaluated: int = 3

        @dataclass
        class MockCalOverall:
            brier_score: float = 0.05
            accuracy: float = 0.90
            calibration_gap: float = -0.02
            interpretation: str = "well_calibrated"
            reviewed_decisions: int = 15
            total_decisions: int = 20

        @dataclass
        class MockCalResponse:
            overall: MockCalOverall | None = field(
                default_factory=MockCalOverall,
            )
            by_confidence_bucket: list = field(default_factory=list)  # type: ignore[type-arg]
            recommendations: list = field(default_factory=list)  # type: ignore[type-arg]
            confidence_stats: None = None
            query_time: str = "2026-01-15T00:00:00"

        @dataclass
        class MockRecordResponse:
            success: bool = True
            id: str = "preact1"
            path: str = "decisions/2026/02/test.yaml"
            indexed: bool = True
            timestamp: str = "2026-02-14T00:00:00"
            error: str | None = None
            quality: dict | None = None  # type: ignore[type-arg]
            guardrail_warnings: list | None = None  # type: ignore[type-arg]

        mock_guard.return_value = MockEvalResult(allowed=True)
        mock_cal.return_value = MockCalResponse()
        mock_record.return_value = MockRecordResponse()

        from a2a.cstp.models import PreActionRequest
        from a2a.cstp.preaction_service import pre_action

        req = PreActionRequest.from_params({
            "action": {
                "description": "Test compaction annotation",
                "category": "architecture",
                "confidence": 0.8,
            },
        })
        resp = await pre_action(req, agent_id="test-compact-agent")

        assert resp.allowed is True
        assert len(resp.relevant_decisions) == 1
        assert resp.relevant_decisions[0].compaction_level == "full"

    @pytest.mark.asyncio
    async def test_dispatcher_compacted_false_no_annotation(
        self,
        dispatcher: CstpDispatcher,
    ) -> None:
        """When compacted=false, no compactionLevel in results."""
        req = JsonRpcRequest(
            id="1",
            method="cstp.queryDecisions",
            params={"query": "", "compacted": False},
        )
        resp = await dispatcher.dispatch(req, "test-agent")
        assert resp.result is not None
        # When compacted is false, individual decisions should not have
        # compactionLevel
        for d in resp.result.get("decisions", []):
            assert "compactionLevel" not in d

    @pytest.mark.asyncio
    async def test_dispatcher_compacted_true_adds_level(
        self,
        dispatcher: CstpDispatcher,
    ) -> None:
        """When compacted=true, decisions get compactionLevel annotation."""
        req = JsonRpcRequest(
            id="2",
            method="cstp.queryDecisions",
            params={"query": "", "compacted": True},
        )
        resp = await dispatcher.dispatch(req, "test-agent")
        assert resp.result is not None
        # All remaining decisions should have compactionLevel
        for d in resp.result.get("decisions", []):
            assert "compactionLevel" in d


# ===========================================================================
# 3. Auto-compact on startup
# ===========================================================================


class TestAutoCompactOnStartup:
    """Test that compaction runs during server startup."""

    @pytest.mark.asyncio
    async def test_startup_calls_run_compaction(self) -> None:
        """Server lifespan should call run_compaction on startup."""
        import sys

        from a2a.cstp.models import CompactLevelCount, CompactResponse

        mock_response = CompactResponse(
            compacted=10,
            preserved=2,
            levels=CompactLevelCount(full=3, summary=2, digest=2, wisdom=3),
        )

        # Ensure mcp module is available for patching (not installed in CI)
        mcp_mock = MagicMock()
        mcp_modules = {
            "mcp": mcp_mock,
            "mcp.server": mcp_mock.server,
            "mcp.server.streamable_http_manager": mcp_mock.server.streamable_http_manager,
        }
        with (
            patch.dict(sys.modules, mcp_modules),
            patch(
                "a2a.cstp.compaction_service.run_compaction",
                new_callable=AsyncMock,
                return_value=mock_response,
            ) as mock_compact,
            patch("a2a.server.AuthManager"),
            patch("a2a.server.set_auth_manager"),
            patch("a2a.server.get_dispatcher") as mock_disp,
            patch("a2a.server.register_methods"),
            patch(
                "a2a.cstp.graphdb.factory.get_graph_store",
                side_effect=ImportError("no graph"),
            ),
        ):
            mock_disp.return_value = MagicMock()

            from a2a.server import lifespan

            app = MagicMock()
            app.state = MagicMock()
            app.state.config = MagicMock()

            async with lifespan(app):
                mock_compact.assert_called_once()
                call_args = mock_compact.call_args[0]
                assert isinstance(call_args[0], CompactRequest)

    @pytest.mark.asyncio
    async def test_startup_compaction_error_does_not_crash(self) -> None:
        """If compaction fails on startup, server should still start."""
        import sys

        # Ensure mcp module is available for patching (not installed in CI)
        mcp_mock = MagicMock()
        mcp_modules = {
            "mcp": mcp_mock,
            "mcp.server": mcp_mock.server,
            "mcp.server.streamable_http_manager": mcp_mock.server.streamable_http_manager,
        }
        with (
            patch.dict(sys.modules, mcp_modules),
            patch(
                "a2a.cstp.compaction_service.run_compaction",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB unavailable"),
            ),
            patch("a2a.server.AuthManager"),
            patch("a2a.server.set_auth_manager"),
            patch("a2a.server.get_dispatcher") as mock_disp,
            patch("a2a.server.register_methods"),
            patch(
                "a2a.cstp.graphdb.factory.get_graph_store",
                side_effect=ImportError("no graph"),
            ),
        ):
            mock_disp.return_value = MagicMock()

            from a2a.server import lifespan

            app = MagicMock()
            app.state = MagicMock()
            app.state.config = MagicMock()

            # Should not raise even though run_compaction fails
            async with lifespan(app):
                pass

    @pytest.mark.asyncio
    async def test_startup_compaction_with_no_decisions(self) -> None:
        """First run with no decisions should return 0 compacted."""
        from a2a.cstp.compaction_service import run_compaction

        req = CompactRequest()
        resp = await run_compaction(req, preloaded_decisions=[])

        assert resp.compacted == 0
        assert resp.levels.full == 0
        assert resp.levels.summary == 0
        assert resp.levels.digest == 0
        assert resp.levels.wisdom == 0


# ===========================================================================
# 4. Compact on reviewDecision
# ===========================================================================


class TestCompactOnReviewDecision:
    """Test compactionLevel annotation after reviewDecision."""

    @pytest.mark.asyncio
    @patch("a2a.cstp.decision_service.find_decision")
    @patch("a2a.cstp.dispatcher.review_decision")
    async def test_review_response_includes_compaction_level(
        self,
        mock_review: AsyncMock,
        mock_find: AsyncMock,
        dispatcher: CstpDispatcher,
    ) -> None:
        """After review, response should include compactionLevel."""

        @dataclass
        class MockReviewResponse:
            success: bool = True
            id: str = "rev12345"
            error: str | None = None

            def to_dict(self) -> dict[str, Any]:
                return {"success": True, "id": self.id}

        mock_review.return_value = MockReviewResponse()

        # The decision is recent, so should be "full"
        mock_find.return_value = (
            MagicMock(),  # file path
            {
                "id": "rev12345",
                "recorded_by": "test-agent",
                "status": "reviewed",
                "date": datetime.now(UTC).isoformat(),
                "outcome": "success",
            },
        )

        req = JsonRpcRequest(
            id="1",
            method="cstp.reviewDecision",
            params={
                "id": "rev12345",
                "outcome": "success",
            },
        )
        resp = await dispatcher.dispatch(req, "test-agent")

        assert resp.result is not None
        assert resp.result.get("compactionLevel") == "full"

    @pytest.mark.asyncio
    @patch("a2a.cstp.decision_service.find_decision")
    @patch("a2a.cstp.dispatcher.review_decision")
    async def test_review_old_decision_gets_correct_level(
        self,
        mock_review: AsyncMock,
        mock_find: AsyncMock,
        dispatcher: CstpDispatcher,
    ) -> None:
        """Reviewing a 50-day-old decision should assign 'digest' level."""

        @dataclass
        class MockReviewResponse:
            success: bool = True
            id: str = "old12345"
            error: str | None = None

            def to_dict(self) -> dict[str, Any]:
                return {"success": True, "id": self.id}

        mock_review.return_value = MockReviewResponse()

        old_date = (datetime.now(UTC) - timedelta(days=50)).isoformat()
        mock_find.return_value = (
            MagicMock(),
            {
                "id": "old12345",
                "recorded_by": "test-agent",
                "status": "reviewed",
                "date": old_date,
                "outcome": "success",
            },
        )

        req = JsonRpcRequest(
            id="2",
            method="cstp.reviewDecision",
            params={
                "id": "old12345",
                "outcome": "success",
            },
        )
        resp = await dispatcher.dispatch(req, "test-agent")

        assert resp.result is not None
        assert resp.result.get("compactionLevel") == "digest"

    @pytest.mark.asyncio
    @patch("a2a.cstp.decision_service.find_decision")
    @patch("a2a.cstp.dispatcher.review_decision")
    async def test_review_find_failure_no_crash(
        self,
        mock_review: AsyncMock,
        mock_find: AsyncMock,
        dispatcher: CstpDispatcher,
    ) -> None:
        """If find_decision fails after review, response still returned."""

        @dataclass
        class MockReviewResponse:
            success: bool = True
            id: str = "err12345"
            error: str | None = None

            def to_dict(self) -> dict[str, Any]:
                return {"success": True, "id": self.id}

        mock_review.return_value = MockReviewResponse()
        # The ownership check reads the decision first; the lookup after the
        # review is the one that fails.
        owned = (MagicMock(), {"id": "err12345", "recorded_by": "test-agent"})
        mock_find.side_effect = [owned, RuntimeError("File not found")]

        req = JsonRpcRequest(
            id="3",
            method="cstp.reviewDecision",
            params={
                "id": "err12345",
                "outcome": "success",
            },
        )
        resp = await dispatcher.dispatch(req, "test-agent")

        assert resp.result is not None
        assert resp.result["success"] is True
        # compactionLevel should not be present when find fails
        assert "compactionLevel" not in resp.result

    @pytest.mark.asyncio
    @patch("a2a.cstp.decision_service.find_decision")
    @patch("a2a.cstp.dispatcher.review_decision")
    async def test_review_not_found_no_compaction_level(
        self,
        mock_review: AsyncMock,
        mock_find: AsyncMock,
        dispatcher: CstpDispatcher,
    ) -> None:
        """If decision not found after review, no compactionLevel."""

        @dataclass
        class MockReviewResponse:
            success: bool = True
            id: str = "nf12345"
            error: str | None = None

            def to_dict(self) -> dict[str, Any]:
                return {"success": True, "id": self.id}

        mock_review.return_value = MockReviewResponse()
        mock_find.return_value = None

        req = JsonRpcRequest(
            id="4",
            method="cstp.reviewDecision",
            params={
                "id": "nf12345",
                "outcome": "failure",
            },
        )
        resp = await dispatcher.dispatch(req, "test-agent")

        assert resp.result is not None
        assert "compactionLevel" not in resp.result
