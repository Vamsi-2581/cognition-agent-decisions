"""Unit tests for decision_service.py."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from a2a.cstp.decision_service import (
    PreDecisionProtocol,
    ProjectContext,
    Reason,
    RecordDecisionRequest,
    build_decision_yaml,
    build_embedding_text,
    calculate_review_date,
    generate_decision_id,
    record_decision,
    write_decision_file,
)
from a2a.cstp.embeddings import EmbeddingProvider
from a2a.cstp.embeddings.factory import set_embedding_provider


class TestGenerateDecisionId:
    """Tests for generate_decision_id."""

    def test_generates_8_char_hex(self) -> None:
        """ID is 8 character hex string."""
        id1 = generate_decision_id()
        assert len(id1) == 8
        assert all(c in "0123456789abcdef" for c in id1)

    def test_generates_unique_ids(self) -> None:
        """Each call generates unique ID."""
        ids = [generate_decision_id() for _ in range(100)]
        assert len(set(ids)) == 100


class TestCalculateReviewDate:
    """Tests for calculate_review_date."""

    def test_days(self) -> None:
        """7d calculates correctly."""
        result = calculate_review_date("7d")
        assert result is not None
        # Just verify it's a valid date format
        assert len(result) == 10  # YYYY-MM-DD

    def test_weeks(self) -> None:
        """2w calculates correctly."""
        result = calculate_review_date("2w")
        assert result is not None

    def test_months(self) -> None:
        """1m calculates correctly."""
        result = calculate_review_date("1m")
        assert result is not None

    def test_none_input(self) -> None:
        """None input returns None."""
        assert calculate_review_date(None) is None

    def test_invalid_unit(self) -> None:
        """Invalid unit returns None."""
        assert calculate_review_date("5x") is None


class TestReason:
    """Tests for Reason dataclass."""

    def test_from_dict(self) -> None:
        """Create from dictionary."""
        data = {"type": "analysis", "text": "Test reason", "strength": 0.9}
        reason = Reason.from_dict(data)
        assert reason.type == "analysis"
        assert reason.text == "Test reason"
        assert reason.strength == 0.9

    def test_to_dict(self) -> None:
        """Convert to dictionary."""
        reason = Reason(type="pattern", text="Similar to X", strength=0.7)
        d = reason.to_dict()
        assert d["type"] == "pattern"
        assert d["text"] == "Similar to X"
        assert d["strength"] == 0.7

    def test_default_strength(self) -> None:
        """Default strength is 0.8."""
        reason = Reason.from_dict({"type": "analysis", "text": "Test"})
        assert reason.strength == 0.8


class TestPreDecisionProtocol:
    """Tests for PreDecisionProtocol dataclass."""

    def test_from_dict_snake_case(self) -> None:
        """Parse snake_case keys."""
        data = {
            "query_run": True,
            "similar_found": 3,
            "guardrails_checked": True,
            "guardrails_passed": True,
        }
        pdp = PreDecisionProtocol.from_dict(data)
        assert pdp.query_run is True
        assert pdp.similar_found == 3
        assert pdp.guardrails_checked is True
        assert pdp.guardrails_passed is True

    def test_from_dict_camel_case(self) -> None:
        """Parse camelCase keys (JSON-RPC style)."""
        data = {
            "queryRun": True,
            "similarFound": 2,
            "guardrailsChecked": True,
            "guardrailsPassed": False,
        }
        pdp = PreDecisionProtocol.from_dict(data)
        assert pdp.query_run is True
        assert pdp.similar_found == 2
        assert pdp.guardrails_passed is False


class TestRecordDecisionRequest:
    """Tests for RecordDecisionRequest."""

    def test_from_dict_minimal(self) -> None:
        """Parse minimal request."""
        data = {
            "decision": "Use PostgreSQL",
            "confidence": 0.85,
            "category": "architecture",
        }
        req = RecordDecisionRequest.from_dict(data)
        assert req.decision == "Use PostgreSQL"
        assert req.confidence == 0.85
        assert req.category == "architecture"
        assert req.stakes == "medium"  # default

    def test_from_dict_full(self) -> None:
        """Parse full request with all fields."""
        data = {
            "decision": "Use PostgreSQL",
            "confidence": 0.85,
            "category": "architecture",
            "stakes": "high",
            "context": "Choosing database",
            "reasons": [{"type": "analysis", "text": "ACID needed"}],
            "kpiIndicators": ["latency"],
            "mentalState": "deliberate",
            "reviewIn": "30d",
            "tags": ["database"],
            "preDecision": {"queryRun": True, "similarFound": 1},
        }
        req = RecordDecisionRequest.from_dict(data, agent_id="emerson")
        assert req.stakes == "high"
        assert req.context == "Choosing database"
        assert len(req.reasons) == 1
        assert req.kpi_indicators == ["latency"]
        assert req.mental_state == "deliberate"
        assert req.review_in == "30d"
        assert req.tags == ["database"]
        assert req.pre_decision is not None
        assert req.pre_decision.query_run is True
        assert req.agent_id == "emerson"

    def test_validate_success(self) -> None:
        """Valid request passes validation."""
        req = RecordDecisionRequest(
            decision="Test decision",
            confidence=0.85,
            category="architecture",
        )
        errors = req.validate()
        assert errors == []

    def test_validate_missing_decision(self) -> None:
        """Missing decision fails validation."""
        req = RecordDecisionRequest(
            decision="",
            confidence=0.85,
            category="architecture",
        )
        errors = req.validate()
        assert any("decision" in e for e in errors)

    def test_validate_invalid_confidence(self) -> None:
        """Invalid confidence fails validation."""
        req = RecordDecisionRequest(
            decision="Test",
            confidence=1.5,
            category="architecture",
        )
        errors = req.validate()
        assert any("confidence" in e for e in errors)

    def test_validate_invalid_category(self) -> None:
        """Invalid category fails validation."""
        req = RecordDecisionRequest(
            decision="Test",
            confidence=0.85,
            category="invalid",
        )
        errors = req.validate()
        assert any("category" in e for e in errors)

    def test_validate_invalid_stakes(self) -> None:
        """Invalid stakes fails validation."""
        req = RecordDecisionRequest(
            decision="Test",
            confidence=0.85,
            category="architecture",
            stakes="extreme",
        )
        errors = req.validate()
        assert any("stakes" in e for e in errors)

    def test_validate_invalid_reason_type(self) -> None:
        """Invalid reason type fails validation."""
        req = RecordDecisionRequest(
            decision="Test",
            confidence=0.85,
            category="architecture",
            reasons=[Reason(type="invalid", text="test")],
        )
        errors = req.validate()
        assert any("reasons[0].type" in e for e in errors)


class TestBuildDecisionYaml:
    """Tests for build_decision_yaml."""

    def test_minimal(self) -> None:
        """Build YAML with minimal fields."""
        req = RecordDecisionRequest(
            decision="Test decision",
            confidence=0.85,
            category="architecture",
        )
        yaml_data = build_decision_yaml(req, "abc12345")
        assert yaml_data["id"] == "abc12345"
        assert yaml_data["summary"] == "Test decision"
        assert yaml_data["decision"] == "Test decision"
        assert yaml_data["category"] == "architecture"
        assert yaml_data["confidence"] == 0.85
        assert yaml_data["status"] == "pending"
        assert "date" in yaml_data

    def test_with_reasons(self) -> None:
        """Build YAML with reasons."""
        req = RecordDecisionRequest(
            decision="Test",
            confidence=0.85,
            category="architecture",
            reasons=[Reason(type="analysis", text="Because X", strength=0.9)],
        )
        yaml_data = build_decision_yaml(req, "test1234")
        assert "reasons" in yaml_data
        assert len(yaml_data["reasons"]) == 1
        assert yaml_data["reasons"][0]["type"] == "analysis"

    def test_with_agent_id(self) -> None:
        """Build YAML with agent attribution."""
        req = RecordDecisionRequest(
            decision="Test",
            confidence=0.85,
            category="architecture",
            agent_id="emerson",
        )
        yaml_data = build_decision_yaml(req, "test1234")
        assert yaml_data["recorded_by"] == "emerson"


class TestWriteDecisionFile:
    """Tests for write_decision_file."""

    def test_creates_file(self, tmp_path: Path) -> None:
        """Creates YAML file in correct location."""
        yaml_data = {
            "id": "test1234",
            "summary": "Test decision",
            "category": "architecture",
        }
        path = write_decision_file(yaml_data, "test1234", str(tmp_path))

        assert Path(path).exists()
        assert "test1234" in path
        assert path.endswith(".yaml")

        # Verify content
        with open(path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert loaded["id"] == "test1234"
        assert loaded["summary"] == "Test decision"

    def test_creates_directory_structure(self, tmp_path: Path) -> None:
        """Creates YYYY/MM directory structure."""
        yaml_data = {"id": "test1234"}
        path = write_decision_file(yaml_data, "test1234", str(tmp_path))

        # Should have year/month directories
        parts = Path(path).parts
        assert any(p.isdigit() and len(p) == 4 for p in parts)  # year
        assert any(p.isdigit() and len(p) == 2 for p in parts)  # month


class TestBuildEmbeddingText:
    """Tests for build_embedding_text."""

    def test_minimal(self) -> None:
        """Build text with minimal fields."""
        req = RecordDecisionRequest(
            decision="Use PostgreSQL",
            confidence=0.85,
            category="architecture",
        )
        text = build_embedding_text(req)
        assert "Decision: Use PostgreSQL" in text
        assert "Category: architecture" in text

    def test_with_context(self) -> None:
        """Include context in embedding text."""
        req = RecordDecisionRequest(
            decision="Use PostgreSQL",
            confidence=0.85,
            category="architecture",
            context="Choosing database",
        )
        text = build_embedding_text(req)
        assert "Context: Choosing database" in text

    def test_with_reasons(self) -> None:
        """Include reasons in embedding text."""
        req = RecordDecisionRequest(
            decision="Test",
            confidence=0.85,
            category="architecture",
            reasons=[
                Reason(type="analysis", text="ACID needed"),
                Reason(type="pattern", text="Similar to prior choice"),
            ],
        )
        text = build_embedding_text(req)
        assert "Reasons:" in text
        assert "ACID needed" in text
        assert "Similar to prior choice" in text

    def test_with_tags(self) -> None:
        """Include tags in embedding text."""
        req = RecordDecisionRequest(
            decision="Test",
            confidence=0.85,
            category="architecture",
            tags=["database", "infrastructure"],
        )
        text = build_embedding_text(req)
        assert "Tags:" in text
        assert "database" in text


class TestRecordDecision:
    """Integration tests for record_decision."""

    @pytest.mark.asyncio
    async def test_creates_file(self, tmp_path: Path) -> None:
        """Records decision and creates file."""
        req = RecordDecisionRequest(
            decision="Test decision for integration",
            confidence=0.85,
            category="architecture",
            stakes="high",
            context="Testing the API",
            agent_id="test-agent",
        )

        response = await record_decision(req, decisions_path=str(tmp_path))

        assert response.success is True
        assert len(response.id) == 8
        assert response.path.endswith(".yaml")
        assert Path(response.path).exists()
        # indexed may be False since we don't have ChromaDB in tests
        assert response.timestamp is not None

    @pytest.mark.asyncio
    async def test_file_content(self, tmp_path: Path) -> None:
        """Verify file content matches request."""
        req = RecordDecisionRequest(
            decision="Use Redis for caching",
            confidence=0.75,
            category="integration",
            stakes="medium",
            reasons=[Reason(type="analysis", text="Fast lookups needed")],
        )

        response = await record_decision(req, decisions_path=str(tmp_path))

        with open(response.path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        assert data["summary"] == "Use Redis for caching"
        assert data["confidence"] == 0.75
        assert data["category"] == "integration"
        assert len(data["reasons"]) == 1

    @pytest.mark.asyncio
    async def test_graceful_degradation_without_chromadb(self, tmp_path: Path) -> None:
        """Decision is saved even when ChromaDB indexing fails."""
        req = RecordDecisionRequest(
            decision="Test graceful degradation",
            confidence=0.80,
            category="process",
        )

        failing = AsyncMock(spec=EmbeddingProvider)
        failing.embed.side_effect = RuntimeError("embedding service unavailable")
        set_embedding_provider(failing)

        response = await record_decision(req, decisions_path=str(tmp_path))

        assert response.success is True
        assert Path(response.path).exists()
        assert response.indexed is False


class TestProjectContext:
    """Tests for ProjectContext dataclass."""

    def test_from_dict_full(self) -> None:
        """Parses all project context fields."""
        data = {
            "project": "owner/repo",
            "feature": "cstp-feedback",
            "pr": 8,
            "file": "api.py",
            "line": 42,
            "commit": "abc123d",
        }
        ctx = ProjectContext.from_dict(data)

        assert ctx.project == "owner/repo"
        assert ctx.feature == "cstp-feedback"
        assert ctx.pr == 8
        assert ctx.file == "api.py"
        assert ctx.line == 42
        assert ctx.commit == "abc123d"

    def test_from_dict_partial(self) -> None:
        """Parses partial project context."""
        data = {"project": "owner/repo", "pr": 5}
        ctx = ProjectContext.from_dict(data)

        assert ctx.project == "owner/repo"
        assert ctx.pr == 5
        assert ctx.feature is None
        assert ctx.file is None

    def test_to_dict_excludes_none(self) -> None:
        """to_dict excludes None values."""
        ctx = ProjectContext(project="owner/repo", pr=8)
        result = ctx.to_dict()

        assert result == {"project": "owner/repo", "pr": 8}
        assert "feature" not in result
        assert "file" not in result

    def test_has_any_true(self) -> None:
        """has_any returns True when any field set."""
        ctx = ProjectContext(project="owner/repo")
        assert ctx.has_any() is True

    def test_has_any_false(self) -> None:
        """has_any returns False when empty."""
        ctx = ProjectContext()
        assert ctx.has_any() is False


class TestRecordDecisionRequestWithProjectContext:
    """Tests for project context in RecordDecisionRequest."""

    def test_from_dict_with_project_context(self) -> None:
        """Parses project context from flat params."""
        data = {
            "decision": "Use async",
            "confidence": 0.85,
            "category": "architecture",
            "project": "owner/repo",
            "feature": "api-v2",
            "pr": 42,
            "file": "src/api.py",
            "line": 100,
            "commit": "abc123d",
        }
        req = RecordDecisionRequest.from_dict(data)

        assert req.project_context is not None
        assert req.project_context.project == "owner/repo"
        assert req.project_context.feature == "api-v2"
        assert req.project_context.pr == 42
        assert isinstance(req.project_context.pr, int)
        assert req.project_context.file == "src/api.py"
        assert req.project_context.line == 100
        assert isinstance(req.project_context.line, int)
        assert req.project_context.commit == "abc123d"

    def test_from_dict_casts_string_pr_to_int(self) -> None:
        """String PR number is cast to int."""
        data = {
            "decision": "Test",
            "confidence": 0.8,
            "category": "architecture",
            "pr": "42",  # String, not int
            "line": "100",  # String, not int
        }
        req = RecordDecisionRequest.from_dict(data)

        assert req.project_context is not None
        assert req.project_context.pr == 42
        assert isinstance(req.project_context.pr, int)
        assert req.project_context.line == 100
        assert isinstance(req.project_context.line, int)

    def test_from_dict_without_project_context(self) -> None:
        """No project context when fields absent."""
        data = {
            "decision": "Use async",
            "confidence": 0.85,
            "category": "architecture",
        }
        req = RecordDecisionRequest.from_dict(data)
        assert req.project_context is None


class TestBuildDecisionYamlWithProjectContext:
    """Tests for project context in build_decision_yaml."""

    def test_includes_project_fields(self) -> None:
        """YAML includes project context fields."""
        req = RecordDecisionRequest(
            decision="Test decision",
            confidence=0.8,
            category="architecture",
            project_context=ProjectContext(
                project="owner/repo",
                feature="my-feature",
                pr=10,
                file="src/main.py",
                line=50,
                commit="def456",
            ),
        )
        data = build_decision_yaml(req, "test123")

        assert data["project"] == "owner/repo"
        assert data["feature"] == "my-feature"
        assert data["pr"] == 10
        assert data["file"] == "src/main.py"
        assert data["line"] == 50
        assert data["commit"] == "def456"

    def test_excludes_none_project_fields(self) -> None:
        """YAML excludes None project fields."""
        req = RecordDecisionRequest(
            decision="Test decision",
            confidence=0.8,
            category="architecture",
            project_context=ProjectContext(project="owner/repo"),
        )
        data = build_decision_yaml(req, "test123")

        assert data["project"] == "owner/repo"
        assert "feature" not in data
        assert "pr" not in data


class TestBuildEmbeddingTextWithProjectContext:
    """Tests for project context in embedding text."""

    def test_includes_project_in_embedding(self) -> None:
        """Embedding text includes project context."""
        req = RecordDecisionRequest(
            decision="Test decision",
            confidence=0.8,
            category="architecture",
            project_context=ProjectContext(
                project="owner/repo",
                feature="api-v2",
                file="src/api.py",
                pr=42,
            ),
        )
        text = build_embedding_text(req)

        assert "Project: owner/repo" in text
        assert "Feature: api-v2" in text
        assert "File: src/api.py" in text
        assert "PR #42" in text
