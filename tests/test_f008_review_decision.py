"""Integration tests for F008 reviewDecision endpoint."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from a2a.cstp.decision_service import (
    RecordDecisionRequest,
    ReviewDecisionRequest,
    find_decision,
    record_decision,
    review_decision,
)
from a2a.cstp.dispatcher import CstpDispatcher, register_methods
from a2a.models.jsonrpc import JsonRpcRequest


@pytest.fixture
def dispatcher() -> CstpDispatcher:
    """Create dispatcher with registered methods."""
    d = CstpDispatcher()
    register_methods(d)
    return d


class TestFindDecision:
    """Tests for find_decision function."""

    @pytest.mark.asyncio
    async def test_finds_existing_decision(self, tmp_path: Path) -> None:
        """Find a decision by ID."""
        # Create a test decision
        req = RecordDecisionRequest(
            decision="Test decision to find",
            confidence=0.85,
            category="architecture",
        )
        with patch("a2a.cstp.decision_service.DECISIONS_PATH", str(tmp_path)):
            response = await record_decision(req, decisions_path=str(tmp_path))

        # Find it
        result = await find_decision(response.id, decisions_path=str(tmp_path))

        assert result is not None
        path, data = result
        assert path.exists()
        assert data["id"] == response.id

    @pytest.mark.asyncio
    async def test_returns_none_for_missing(self, tmp_path: Path) -> None:
        """Returns None for non-existent decision."""
        result = await find_decision("nonexistent123", decisions_path=str(tmp_path))
        assert result is None


class TestReviewDecisionRequest:
    """Tests for ReviewDecisionRequest validation."""

    def test_validate_success(self) -> None:
        """Valid request passes validation."""
        req = ReviewDecisionRequest(id="abc12345", outcome="success")
        errors = req.validate()
        assert errors == []

    def test_validate_missing_id(self) -> None:
        """Missing ID fails validation."""
        req = ReviewDecisionRequest(id="", outcome="success")
        errors = req.validate()
        assert any("id" in e for e in errors)

    def test_validate_invalid_outcome(self) -> None:
        """Invalid outcome fails validation."""
        req = ReviewDecisionRequest(id="abc12345", outcome="invalid")
        errors = req.validate()
        assert any("outcome" in e for e in errors)

    def test_from_dict_camel_case(self) -> None:
        """Parse camelCase keys."""
        data = {
            "id": "test123",
            "outcome": "partial",
            "actualResult": "It worked partially",
            "affectedKpis": {"latency": -0.2},
        }
        req = ReviewDecisionRequest.from_dict(data, reviewer_id="emerson")
        assert req.actual_result == "It worked partially"
        assert req.affected_kpis == {"latency": -0.2}
        assert req.reviewer_id == "emerson"


class TestReviewDecision:
    """Tests for review_decision function."""

    @pytest.mark.asyncio
    async def test_review_existing_decision(self, tmp_path: Path) -> None:
        """Review an existing decision."""
        # First, create a decision
        create_req = RecordDecisionRequest(
            decision="Decision to review",
            confidence=0.85,
            category="architecture",
            agent_id="creator-agent",
        )
        with patch("a2a.cstp.decision_service.DECISIONS_PATH", str(tmp_path)):
            create_response = await record_decision(create_req, decisions_path=str(tmp_path))

        # Now review it
        review_req = ReviewDecisionRequest(
            id=create_response.id,
            outcome="success",
            actual_result="Worked perfectly",
            lessons="Always test first",
            reviewer_id="reviewer-agent",
        )
        review_response = await review_decision(review_req, decisions_path=str(tmp_path))

        assert review_response.success is True
        assert review_response.status == "reviewed"

        # Verify file was updated
        with open(review_response.path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        assert data["status"] == "reviewed"
        assert data["outcome"] == "success"
        assert data["actual_result"] == "Worked perfectly"
        assert data["lessons"] == "Always test first"
        assert data["reviewed_by"] == "reviewer-agent"
        assert "reviewed_at" in data

    @pytest.mark.asyncio
    async def test_review_nonexistent_decision(self, tmp_path: Path) -> None:
        """Review fails for non-existent decision."""
        review_req = ReviewDecisionRequest(
            id="nonexistent123",
            outcome="success",
        )
        response = await review_decision(review_req, decisions_path=str(tmp_path))

        assert response.success is False
        assert "not found" in response.error.lower()

    @pytest.mark.asyncio
    async def test_review_with_kpis(self, tmp_path: Path) -> None:
        """Review with affected KPIs."""
        # Create decision
        create_req = RecordDecisionRequest(
            decision="Performance decision",
            confidence=0.80,
            category="architecture",
        )
        with patch("a2a.cstp.decision_service.DECISIONS_PATH", str(tmp_path)):
            create_response = await record_decision(create_req, decisions_path=str(tmp_path))

        # Review with KPIs
        review_req = ReviewDecisionRequest(
            id=create_response.id,
            outcome="success",
            affected_kpis={"latency": -0.3, "throughput": 0.2},
        )
        review_response = await review_decision(review_req, decisions_path=str(tmp_path))

        assert review_response.success is True

        with open(review_response.path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        assert data["affected_kpis"]["latency"] == -0.3
        assert data["affected_kpis"]["throughput"] == 0.2


class TestReviewDecisionEndpoint:
    """Tests for cstp.reviewDecision JSON-RPC endpoint."""

    @pytest.mark.asyncio
    async def test_method_registered(self, dispatcher: CstpDispatcher) -> None:
        """reviewDecision method is registered."""
        assert "cstp.reviewDecision" in dispatcher._methods

    @pytest.mark.asyncio
    async def test_full_workflow(
        self, dispatcher: CstpDispatcher, tmp_path: Path
    ) -> None:
        """Record then review a decision."""
        # Record
        record_request = JsonRpcRequest(
            jsonrpc="2.0",
            method="cstp.recordDecision",
            params={
                "decision": "Test workflow decision",
                "confidence": 0.85,
                "category": "process",
            },
            id=1,
        )

        with patch("a2a.cstp.decision_service.DECISIONS_PATH", str(tmp_path)):
            record_response = await dispatcher.dispatch(record_request, "test-agent")

        assert record_response.error is None
        decision_id = record_response.result["id"]

        # Review
        review_request = JsonRpcRequest(
            jsonrpc="2.0",
            method="cstp.reviewDecision",
            params={
                "id": decision_id,
                "outcome": "success",
                "actualResult": "Everything worked",
                "lessons": "Trust the process",
            },
            id=2,
        )

        with patch("a2a.cstp.decision_service.DECISIONS_PATH", str(tmp_path)):
            review_response = await dispatcher.dispatch(review_request, "test-agent")

        assert review_response.error is None
        assert review_response.result["success"] is True
        assert review_response.result["status"] == "reviewed"

    @pytest.mark.asyncio
    async def test_validation_error(self, dispatcher: CstpDispatcher) -> None:
        """Invalid outcome returns error."""
        request = JsonRpcRequest(
            jsonrpc="2.0",
            method="cstp.reviewDecision",
            params={
                "id": "test123",
                "outcome": "invalid_outcome",
            },
            id=3,
        )

        response = await dispatcher.dispatch(request, "test-agent")

        assert response.error is not None
        assert "outcome" in response.error.message.lower()
