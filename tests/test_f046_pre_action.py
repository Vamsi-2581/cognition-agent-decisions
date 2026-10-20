"""Tests for F046: cstp.preAction implementation."""

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

import pytest

from a2a.cstp.dispatcher import CstpDispatcher, register_methods
from a2a.cstp.models import (
    CalibrationContext,
    GuardrailViolation,
    PatternSummary,
    PreActionOptions,
    PreActionRequest,
    PreActionResponse,
)
from a2a.models.jsonrpc import JsonRpcRequest


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestPreActionOptions:
    def test_defaults(self) -> None:
        opts = PreActionOptions()
        assert opts.query_limit == 5
        assert opts.auto_record is True
        assert opts.include_patterns is True

    def test_from_dict_camel_case(self) -> None:
        opts = PreActionOptions.from_dict({
            "queryLimit": 10,
            "autoRecord": False,
            "includePatterns": False,
        })
        assert opts.query_limit == 10
        assert opts.auto_record is False
        assert opts.include_patterns is False

    def test_from_dict_none(self) -> None:
        opts = PreActionOptions.from_dict(None)
        assert opts.query_limit == 5


class TestPreActionRequest:
    def test_from_params_minimal(self) -> None:
        req = PreActionRequest.from_params({
            "action": {"description": "Deploy to production"},
        })
        assert req.action.description == "Deploy to production"
        assert req.options.query_limit == 5
        assert req.options.auto_record is True
        assert req.tags == []
        assert req.pattern is None

    def test_from_params_full(self) -> None:
        req = PreActionRequest.from_params({
            "action": {
                "description": "Refactor auth module",
                "category": "architecture",
                "stakes": "high",
                "confidence": 0.85,
            },
            "options": {"queryLimit": 10, "autoRecord": False},
            "reasons": [{"type": "analysis", "text": "Simpler code"}],
            "tags": ["auth", "refactor"],
            "pattern": "Extract and simplify",
        })
        assert req.action.category == "architecture"
        assert req.action.confidence == 0.85
        assert req.options.query_limit == 10
        assert req.options.auto_record is False
        assert len(req.reasons) == 1
        assert req.tags == ["auth", "refactor"]
        assert req.pattern == "Extract and simplify"

    def test_from_params_missing_action(self) -> None:
        with pytest.raises(ValueError, match="Missing required parameter"):
            PreActionRequest.from_params({})


class TestPreActionResponse:
    def test_to_dict_allowed(self) -> None:
        resp = PreActionResponse(
            allowed=True,
            decision_id="abc12345",
            relevant_decisions=[],
            guardrail_results=[],
            calibration_context=CalibrationContext(),
            patterns_summary=[],
            query_time_ms=42,
        )
        data = resp.to_dict()
        assert data["allowed"] is True
        assert data["decisionId"] == "abc12345"
        assert data["queryTimeMs"] == 42
        assert "blockReasons" not in data

    def test_to_dict_blocked(self) -> None:
        resp = PreActionResponse(
            allowed=False,
            decision_id=None,
            relevant_decisions=[],
            guardrail_results=[
                GuardrailViolation(
                    guardrail_id="g1",
                    name="test-guardrail",
                    message="Blocked for testing",
                    severity="block",
                ),
            ],
            calibration_context=CalibrationContext(),
            patterns_summary=[],
            block_reasons=["Blocked for testing"],
        )
        data = resp.to_dict()
        assert data["allowed"] is False
        assert data["decisionId"] is None
        assert data["blockReasons"] == ["Blocked for testing"]
        assert len(data["guardrailResults"]) == 1

    def test_to_dict_with_patterns(self) -> None:
        resp = PreActionResponse(
            allowed=True,
            decision_id="x1234567",
            relevant_decisions=[],
            guardrail_results=[],
            calibration_context=CalibrationContext(
                brier_score=0.05,
                accuracy=0.90,
                interpretation="well_calibrated",
                reviewed_decisions=10,
            ),
            patterns_summary=[
                PatternSummary(
                    pattern="Stateless auth scales better",
                    count=3,
                    example_ids=["a1", "a2", "a3"],
                ),
            ],
        )
        data = resp.to_dict()
        assert data["calibrationContext"]["brierScore"] == 0.05
        assert data["calibrationContext"]["accuracy"] == 0.90
        assert len(data["patternsSummary"]) == 1
        assert data["patternsSummary"][0]["count"] == 3


class TestCalibrationContext:
    def test_to_dict_empty(self) -> None:
        ctx = CalibrationContext()
        data = ctx.to_dict()
        assert data["reviewedDecisions"] == 0
        assert "brierScore" not in data

    def test_to_dict_full(self) -> None:
        ctx = CalibrationContext(
            brier_score=0.03,
            accuracy=0.91,
            calibration_gap=-0.05,
            interpretation="slightly_overconfident",
            reviewed_decisions=18,
        )
        data = ctx.to_dict()
        assert data["brierScore"] == 0.03
        assert data["accuracy"] == 0.91
        assert data["calibrationGap"] == -0.05
        assert data["interpretation"] == "slightly_overconfident"
        assert data["reviewedDecisions"] == 18


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


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


@dataclass
class MockGuardrailResult:
    guardrail_id: str
    name: str
    message: str
    severity: str
    suggestion: str | None = None


@dataclass
class MockEvalResult:
    allowed: bool = True
    violations: list[MockGuardrailResult] = field(default_factory=list)
    warnings: list[MockGuardrailResult] = field(default_factory=list)
    evaluated: int = 3


@dataclass
class MockCalibrationOverall:
    brier_score: float = 0.05
    accuracy: float = 0.90
    calibration_gap: float = -0.02
    interpretation: str = "well_calibrated"
    reviewed_decisions: int = 15
    total_decisions: int = 20


@dataclass
class MockCalibrationResponse:
    overall: MockCalibrationOverall | None = field(
        default_factory=MockCalibrationOverall,
    )
    by_confidence_bucket: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)
    confidence_stats: None = None
    query_time: str = "2026-01-15T00:00:00"


@dataclass
class MockRecordResponse:
    success: bool = True
    id: str = "new12345"
    path: str = "decisions/2026/02/test.yaml"
    indexed: bool = True
    timestamp: str = "2026-02-14T00:00:00"
    error: str | None = None
    quality: dict | None = None
    guardrail_warnings: list | None = None


class TestPreActionService:
    @pytest.mark.asyncio
    @patch("a2a.cstp.preaction_service.record_decision")
    @patch("a2a.cstp.preaction_service.get_calibration")
    @patch("a2a.cstp.preaction_service.evaluate_guardrails")
    @patch("a2a.cstp.preaction_service.query_decisions")
    @patch("a2a.cstp.preaction_service.log_guardrail_check")
    async def test_allowed_with_auto_record(
        self,
        mock_log: AsyncMock,
        mock_query: AsyncMock,
        mock_guard: AsyncMock,
        mock_cal: AsyncMock,
        mock_record: AsyncMock,
    ) -> None:
        """When allowed and auto_record=True, should record and return decision_id."""
        mock_query.return_value = MockQueryResponse(
            results=[MockQueryResult(pattern="Stateless auth")],
        )
        mock_guard.return_value = MockEvalResult(allowed=True)
        mock_cal.return_value = MockCalibrationResponse()
        mock_record.return_value = MockRecordResponse(success=True, id="new12345")

        from a2a.cstp.preaction_service import pre_action

        req = PreActionRequest.from_params({
            "action": {
                "description": "Use JWT",
                "category": "architecture",
                "stakes": "high",
                "confidence": 0.85,
            },
        })
        resp = await pre_action(req, agent_id="test-agent")

        assert resp.allowed is True
        assert resp.decision_id == "new12345"
        assert len(resp.relevant_decisions) == 1
        assert len(resp.patterns_summary) == 1
        assert resp.patterns_summary[0].pattern == "Stateless auth"
        mock_record.assert_called_once()

    @pytest.mark.asyncio
    @patch("a2a.cstp.graph_service.safe_auto_link", new_callable=AsyncMock)
    @patch("a2a.cstp.preaction_service.record_decision")
    @patch("a2a.cstp.preaction_service.get_calibration")
    @patch("a2a.cstp.preaction_service.evaluate_guardrails")
    @patch("a2a.cstp.preaction_service.query_decisions")
    @patch("a2a.cstp.preaction_service.log_guardrail_check")
    async def test_auto_record_calls_safe_auto_link(
        self,
        mock_log: AsyncMock,
        mock_query: AsyncMock,
        mock_guard: AsyncMock,
        mock_cal: AsyncMock,
        mock_record: AsyncMock,
        mock_auto_link: AsyncMock,
    ) -> None:
        """Issue #157: auto_record should call safe_auto_link after recording."""
        mock_query.return_value = MockQueryResponse()
        mock_guard.return_value = MockEvalResult(allowed=True)
        mock_cal.return_value = MockCalibrationResponse()
        mock_record.return_value = MockRecordResponse(success=True, id="new12345")
        mock_auto_link.return_value = 2

        from a2a.cstp.preaction_service import pre_action

        req = PreActionRequest.from_params({
            "action": {
                "description": "Use JWT for auth",
                "category": "architecture",
                "stakes": "high",
                "confidence": 0.85,
            },
            "tags": ["auth"],
            "pattern": "Stateless auth",
        })
        resp = await pre_action(req, agent_id="test-agent")

        assert resp.allowed is True
        assert resp.decision_id == "new12345"
        mock_auto_link.assert_called_once()
        call_kwargs = mock_auto_link.call_args.kwargs
        assert call_kwargs["response_id"] == "new12345"
        assert call_kwargs["category"] == "architecture"
        assert call_kwargs["stakes"] == "high"
        assert call_kwargs["confidence"] == 0.85
        assert call_kwargs["tags"] == ["auth"]
        assert call_kwargs["pattern"] == "Stateless auth"

    @pytest.mark.asyncio
    @patch("a2a.cstp.preaction_service.record_decision")
    @patch("a2a.cstp.preaction_service.get_calibration")
    @patch("a2a.cstp.preaction_service.evaluate_guardrails")
    @patch("a2a.cstp.preaction_service.query_decisions")
    @patch("a2a.cstp.preaction_service.log_guardrail_check")
    async def test_blocked_no_record(
        self,
        mock_log: AsyncMock,
        mock_query: AsyncMock,
        mock_guard: AsyncMock,
        mock_cal: AsyncMock,
        mock_record: AsyncMock,
    ) -> None:
        """When guardrail blocks, should NOT record."""
        mock_query.return_value = MockQueryResponse()
        mock_guard.return_value = MockEvalResult(
            allowed=False,
            violations=[MockGuardrailResult(
                guardrail_id="g1",
                name="high-stakes-low-conf",
                message="Stakes=high but confidence too low",
                severity="block",
            )],
        )
        mock_cal.return_value = MockCalibrationResponse()

        from a2a.cstp.preaction_service import pre_action

        req = PreActionRequest.from_params({
            "action": {
                "description": "Deploy untested code",
                "stakes": "high",
                "confidence": 0.3,
            },
        })
        resp = await pre_action(req, agent_id="test-agent")

        assert resp.allowed is False
        assert resp.decision_id is None
        assert len(resp.block_reasons) == 1
        assert "confidence too low" in resp.block_reasons[0]
        mock_record.assert_not_called()

    @pytest.mark.asyncio
    @patch("a2a.cstp.preaction_service.record_decision")
    @patch("a2a.cstp.preaction_service.get_calibration")
    @patch("a2a.cstp.preaction_service.evaluate_guardrails")
    @patch("a2a.cstp.preaction_service.query_decisions")
    @patch("a2a.cstp.preaction_service.log_guardrail_check")
    async def test_auto_record_false(
        self,
        mock_log: AsyncMock,
        mock_query: AsyncMock,
        mock_guard: AsyncMock,
        mock_cal: AsyncMock,
        mock_record: AsyncMock,
    ) -> None:
        """When auto_record=false, should not call record even if allowed."""
        mock_query.return_value = MockQueryResponse()
        mock_guard.return_value = MockEvalResult(allowed=True)
        mock_cal.return_value = MockCalibrationResponse()

        from a2a.cstp.preaction_service import pre_action

        req = PreActionRequest.from_params({
            "action": {"description": "Read-only check"},
            "options": {"autoRecord": False},
        })
        resp = await pre_action(req, agent_id="test-agent")

        assert resp.allowed is True
        assert resp.decision_id is None
        mock_record.assert_not_called()

    @pytest.mark.asyncio
    @patch("a2a.cstp.preaction_service.get_calibration")
    @patch("a2a.cstp.preaction_service.evaluate_guardrails")
    @patch("a2a.cstp.preaction_service.query_decisions")
    @patch("a2a.cstp.preaction_service.log_guardrail_check")
    async def test_concurrent_failures_handled(
        self,
        mock_log: AsyncMock,
        mock_query: AsyncMock,
        mock_guard: AsyncMock,
        mock_cal: AsyncMock,
    ) -> None:
        """If services fail, should still return a valid response.

        Query and calibration failures degrade the response; a guardrail
        failure blocks, since allowing would skip every block rule.
        """
        mock_query.side_effect = RuntimeError("ChromaDB down")
        mock_guard.side_effect = RuntimeError("Guardrails error")
        mock_cal.side_effect = RuntimeError("Calibration error")

        from a2a.cstp.preaction_service import pre_action

        req = PreActionRequest.from_params({
            "action": {"description": "Test resilience"},
            "options": {"autoRecord": False},
        })
        resp = await pre_action(req, agent_id="test-agent")

        # Fail closed: blocked when guardrails error
        assert resp.allowed is False
        assert resp.block_reasons
        assert resp.decision_id is None
        assert len(resp.relevant_decisions) == 0

    @pytest.mark.asyncio
    @patch("a2a.cstp.preaction_service.get_calibration")
    @patch("a2a.cstp.preaction_service.evaluate_guardrails")
    @patch("a2a.cstp.preaction_service.query_decisions")
    @patch("a2a.cstp.preaction_service.log_guardrail_check")
    async def test_pattern_extraction(
        self,
        mock_log: AsyncMock,
        mock_query: AsyncMock,
        mock_guard: AsyncMock,
        mock_cal: AsyncMock,
    ) -> None:
        """Patterns from query results should be grouped correctly."""
        mock_query.return_value = MockQueryResponse(
            results=[
                MockQueryResult(id="a1", pattern="Use caching"),
                MockQueryResult(id="a2", pattern="Use caching"),
                MockQueryResult(id="a3", pattern="Prefer composition"),
            ],
        )
        mock_guard.return_value = MockEvalResult(allowed=True)
        mock_cal.return_value = MockCalibrationResponse()

        from a2a.cstp.preaction_service import pre_action

        req = PreActionRequest.from_params({
            "action": {"description": "Add caching"},
            "options": {"autoRecord": False},
        })
        resp = await pre_action(req, agent_id="test-agent")

        assert len(resp.patterns_summary) == 2
        caching_pattern = next(
            p for p in resp.patterns_summary if p.pattern == "Use caching"
        )
        assert caching_pattern.count == 2


# ---------------------------------------------------------------------------
# Dispatcher integration tests
# ---------------------------------------------------------------------------


class TestPreActionEndpoint:
    @pytest.fixture
    def dispatcher(self) -> CstpDispatcher:
        d = CstpDispatcher()
        register_methods(d)
        return d

    def test_method_registered(self, dispatcher: CstpDispatcher) -> None:
        assert "cstp.preAction" in dispatcher._methods

    @pytest.mark.asyncio
    @patch("a2a.cstp.preaction_service.record_decision")
    @patch("a2a.cstp.preaction_service.get_calibration")
    @patch("a2a.cstp.preaction_service.evaluate_guardrails")
    @patch("a2a.cstp.preaction_service.query_decisions")
    @patch("a2a.cstp.preaction_service.log_guardrail_check")
    async def test_dispatch_round_trip(
        self,
        mock_log: AsyncMock,
        mock_query: AsyncMock,
        mock_guard: AsyncMock,
        mock_cal: AsyncMock,
        mock_record: AsyncMock,
        dispatcher: CstpDispatcher,
    ) -> None:
        """Full dispatch round-trip should work."""
        mock_query.return_value = MockQueryResponse()
        mock_guard.return_value = MockEvalResult(allowed=True)
        mock_cal.return_value = MockCalibrationResponse()
        mock_record.return_value = MockRecordResponse()

        request = JsonRpcRequest(
            id="1",
            method="cstp.preAction",
            params={
                "action": {"description": "Test dispatch"},
            },
        )
        response = await dispatcher.dispatch(request, agent_id="test")

        assert response.error is None
        assert response.result is not None
        assert response.result["allowed"] is True
