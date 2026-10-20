"""Tests for F054: CEL expression guardrails.

Tests cover:
- Legacy JSONB condition dict auto-conversion to CEL
- CEL string condition evaluation
- CEL dict condition ({"cel": "..."}) evaluation
- action.context.* access via CEL
- Invalid CEL fails closed (the rule applies)
- Program caching (same expression compiled once)
- Complex CEL expressions (AND/OR/NOT/in/contains/size)
- Legacy flat condition_*/requires_* format unaffected
- MCP context field forwarding
"""

import pytest

from a2a.cstp.guardrails_service import (
    CelEvaluationError,
    CelGuardrailEvaluator,
    Guardrail,
    _build_cel_activation,
    _jsonb_condition_to_cel,
    _parse_guardrail,
    clear_guardrails_cache,
    evaluate_guardrails,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_guardrail(
    cel: str,
    action: str = "block",
    message: str = "",
    scope: list[str] | None = None,
) -> Guardrail:
    return Guardrail(
        id="test-guardrail",
        description="Test guardrail",
        action=action,
        message=message,
        scope=scope or [],
        cel_expression=cel,
    )


# ---------------------------------------------------------------------------
# _build_cel_activation
# ---------------------------------------------------------------------------


class TestBuildCelActivation:
    def test_standard_fields_under_action(self):
        flat = {"stakes": "high", "confidence": 0.8, "category": "architecture"}
        act = _build_cel_activation(flat)
        assert act["action"]["stakes"] == "high"
        assert act["action"]["confidence"] == 0.8
        assert act["action"]["category"] == "architecture"

    def test_extra_fields_under_action_context(self):
        flat = {"stakes": "medium", "code_review": True, "architecture_review": False}
        act = _build_cel_activation(flat)
        ctx = act["action"]["context"]
        assert ctx["code_review"] is True
        assert ctx["architecture_review"] is False

    def test_none_confidence_becomes_zero(self):
        flat = {"confidence": None}
        act = _build_cel_activation(flat)
        assert act["action"]["confidence"] == 0.0

    def test_has_tags_derived(self):
        flat = {"tags": ["deploy", "infra"]}
        act = _build_cel_activation(flat)
        assert act["action"]["has_tags"] is True

    def test_has_pattern_derived(self):
        flat = {"pattern": "strangler-fig"}
        act = _build_cel_activation(flat)
        assert act["action"]["has_pattern"] is True

    def test_empty_tags_has_tags_false(self):
        flat = {"tags": []}
        act = _build_cel_activation(flat)
        assert act["action"]["has_tags"] is False


# ---------------------------------------------------------------------------
# _jsonb_condition_to_cel
# ---------------------------------------------------------------------------


class TestJsonbConditionToCel:
    def test_simple_equality(self):
        result = _jsonb_condition_to_cel({"stakes": "high"})
        assert result == "action.stakes == 'high'"

    def test_category_equality(self):
        result = _jsonb_condition_to_cel({"category": "tooling"})
        assert result == "action.category == 'tooling'"

    def test_confidence_lt(self):
        result = _jsonb_condition_to_cel({"confidence_lt": 0.5})
        assert result == "action.confidence < 0.5"

    def test_reason_count_lt(self):
        result = _jsonb_condition_to_cel({"reason_count_lt": 1})
        assert result == "action.reason_count < 1"

    def test_quality_lt_remaps_to_quality_score(self):
        result = _jsonb_condition_to_cel({"quality_lt": 0.5})
        assert "quality_score" in result
        assert "< 0.5" in result

    def test_multiple_conditions_joined_with_and(self):
        result = _jsonb_condition_to_cel({"stakes": "high", "confidence_lt": 0.5})
        # Both parts must appear (order may vary)
        assert "action.stakes == 'high'" in result
        assert "action.confidence < 0.5" in result
        assert "&&" in result

    def test_empty_dict_returns_true(self):
        result = _jsonb_condition_to_cel({})
        assert result == "true"

    def test_cel_key_skipped(self):
        result = _jsonb_condition_to_cel({"cel": "action.stakes == 'high'"})
        # The "cel" key should be skipped in conversion
        assert result == "true"

    def test_context_field_is_guarded_with_has(self):
        assert _jsonb_condition_to_cel({"deploy_env": "production"}) == (
            "has(action.context.deploy_env) && action.context.deploy_env == 'production'"
        )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("< 0.5", "action.quality_score < 0.5"),
            (">=0.8", "action.quality_score >= 0.8"),
            ("== 1", "action.quality_score == 1.0"),
        ],
    )
    def test_comparison_string_becomes_operator(self, value, expected):
        # guardrails/quality.yaml writes quality_score: "< 0.5"; rendered as an
        # equality it compared a number to a string and errored every time.
        assert _jsonb_condition_to_cel({"quality_score": value}) == expected


# ---------------------------------------------------------------------------
# _parse_guardrail: CEL detection
# ---------------------------------------------------------------------------


class TestParseGuardrailCelDetection:
    def test_string_condition_stored_as_cel(self):
        data = {
            "id": "test",
            "description": "Test",
            "condition": "action.stakes == 'high'",
            "action": "block",
        }
        g = _parse_guardrail(data)
        assert g.cel_expression == "action.stakes == 'high'"
        assert g.conditions == []

    def test_dict_with_cel_key(self):
        data = {
            "id": "test",
            "description": "Test",
            "condition": {"cel": "action.confidence < 0.5"},
            "action": "block",
        }
        g = _parse_guardrail(data)
        assert g.cel_expression == "action.confidence < 0.5"

    def test_legacy_jsonb_dict_auto_converted(self):
        data = {
            "id": "test",
            "description": "Test",
            "condition": {"stakes": "high", "confidence_lt": 0.5},
            "action": "block",
        }
        g = _parse_guardrail(data)
        assert g.cel_expression is not None
        assert "action.stakes == 'high'" in g.cel_expression
        assert "action.confidence < 0.5" in g.cel_expression

    def test_flat_format_has_no_cel_expression(self):
        data = {
            "id": "test",
            "description": "Test",
            "condition_stakes": "high",
            "condition_confidence": "< 0.5",
            "action": "block",
        }
        g = _parse_guardrail(data)
        assert g.cel_expression is None
        assert len(g.conditions) == 2


# ---------------------------------------------------------------------------
# CelGuardrailEvaluator
# ---------------------------------------------------------------------------


class TestCelGuardrailEvaluator:
    def setup_method(self):
        self.evaluator = CelGuardrailEvaluator()

    def test_simple_equality_triggers(self):
        result = self.evaluator.evaluate(
            "g1",
            "action.stakes == 'high'",
            {"stakes": "high"},
        )
        assert result is True

    def test_simple_equality_no_trigger(self):
        result = self.evaluator.evaluate(
            "g1",
            "action.stakes == 'high'",
            {"stakes": "medium"},
        )
        assert result is False

    def test_confidence_lt(self):
        result = self.evaluator.evaluate(
            "g1",
            "action.confidence < 0.5",
            {"confidence": 0.3},
        )
        assert result is True

        result2 = self.evaluator.evaluate(
            "g1",
            "action.confidence < 0.5",
            {"confidence": 0.7},
        )
        assert result2 is False

    def test_and_expression(self):
        expr = "action.stakes == 'high' && action.confidence < 0.5"
        assert self.evaluator.evaluate("g1", expr, {"stakes": "high", "confidence": 0.3}) is True
        assert self.evaluator.evaluate("g1", expr, {"stakes": "high", "confidence": 0.8}) is False
        assert self.evaluator.evaluate("g1", expr, {"stakes": "low", "confidence": 0.3}) is False

    def test_context_field_access(self):
        expr = "action.category == 'tooling' && !action.context.code_review"
        # Code review not done → triggers
        assert self.evaluator.evaluate(
            "g1", expr, {"category": "tooling", "code_review": False}
        ) is True
        # Code review done → no trigger
        assert self.evaluator.evaluate(
            "g1", expr, {"category": "tooling", "code_review": True}
        ) is False

    def test_context_field_missing_raises(self):
        """Missing context key is a CEL error, reported to the caller."""
        expr = "action.context.architecture_review == true"
        with pytest.raises(CelEvaluationError):
            self.evaluator.evaluate("g1", expr, {"category": "architecture"})

    def test_invalid_cel_syntax_raises(self):
        expr = "THIS IS NOT VALID CEL !!!"
        with pytest.raises(CelEvaluationError):
            self.evaluator.evaluate("g1", expr, {"stakes": "high"})

    def test_program_caching(self):
        """Same expression compiled only once — second call uses cache."""
        expr = "action.stakes == 'high'"
        self.evaluator.evaluate("g1", expr, {"stakes": "high"})
        self.evaluator.evaluate("g1", expr, {"stakes": "low"})
        # Cache should have exactly one entry for this expression
        assert expr in self.evaluator._programs
        assert len([k for k in self.evaluator._programs if k == expr]) == 1

    def test_in_operator(self):
        expr = "action.stakes in ['high', 'critical']"
        assert self.evaluator.evaluate("g1", expr, {"stakes": "high"}) is True
        assert self.evaluator.evaluate("g1", expr, {"stakes": "critical"}) is True
        assert self.evaluator.evaluate("g1", expr, {"stakes": "low"}) is False

    def test_size_function(self):
        expr = "size(action.tags) == 0 && action.stakes != 'low'"
        assert self.evaluator.evaluate(
            "g1", expr, {"tags": [], "stakes": "medium"}
        ) is True
        assert self.evaluator.evaluate(
            "g1", expr, {"tags": ["deploy"], "stakes": "medium"}
        ) is False

    def test_has_tags_field(self):
        expr = "!action.has_tags && action.stakes != 'low'"
        assert self.evaluator.evaluate(
            "g1", expr, {"tags": [], "stakes": "high"}
        ) is True
        assert self.evaluator.evaluate(
            "g1", expr, {"tags": ["deploy"], "stakes": "high"}
        ) is False

    def test_not_operator(self):
        expr = "!action.has_pattern"
        assert self.evaluator.evaluate("g1", expr, {"pattern": ""}) is True
        assert self.evaluator.evaluate("g1", expr, {"pattern": "strangler-fig"}) is False


# ---------------------------------------------------------------------------
# evaluate_guardrails integration (async)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_cache():
    clear_guardrails_cache()
    yield
    clear_guardrails_cache()


@pytest.mark.asyncio
async def test_cel_string_blocks(tmp_path):
    """CEL string condition in YAML blocks correctly."""
    yaml_content = """
- id: high-stakes-cel
  description: Block high stakes via CEL
  condition: "action.stakes == 'high'"
  action: block
  message: High stakes blocked
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    result = await evaluate_guardrails({"stakes": "high"}, guardrails_dir=tmp_path)
    assert not result.allowed
    assert len(result.violations) == 1
    assert result.violations[0].guardrail_id == "high-stakes-cel"


@pytest.mark.asyncio
async def test_cel_string_allows_when_not_triggered(tmp_path):
    yaml_content = """
- id: high-stakes-cel
  description: Block high stakes via CEL
  condition: "action.stakes == 'high'"
  action: block
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    result = await evaluate_guardrails({"stakes": "medium"}, guardrails_dir=tmp_path)
    assert result.allowed
    assert result.violations == []


@pytest.mark.asyncio
async def test_cel_dict_condition(tmp_path):
    """CEL dict format {"cel": "..."} works."""
    yaml_content = """
- id: cel-dict
  description: CEL dict format
  condition:
    cel: "action.confidence < 0.5"
  action: warn
  message: Low confidence warning
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    result = await evaluate_guardrails({"confidence": 0.3}, guardrails_dir=tmp_path)
    assert result.allowed  # warn does not block
    assert len(result.warnings) == 1
    assert result.warnings[0].guardrail_id == "cel-dict"


@pytest.mark.asyncio
async def test_legacy_jsonb_dict_auto_converts(tmp_path):
    """Legacy JSONB dict condition auto-converts to CEL and evaluates correctly."""
    yaml_content = """
- id: legacy-jsonb
  description: Legacy JSONB auto-convert
  condition:
    stakes: high
    confidence_lt: 0.5
  action: block
  message: Legacy JSONB converted
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    # Should block: high stakes + low confidence
    result = await evaluate_guardrails(
        {"stakes": "high", "confidence": 0.3}, guardrails_dir=tmp_path
    )
    assert not result.allowed

    # Should allow: high stakes + high confidence
    result2 = await evaluate_guardrails(
        {"stakes": "high", "confidence": 0.9}, guardrails_dir=tmp_path
    )
    assert result2.allowed


@pytest.mark.asyncio
async def test_context_dict_via_cel(tmp_path):
    """CEL can access action.context.* fields."""
    yaml_content = """
- id: require-arch-review
  description: Require architecture review
  condition: "action.category == 'architecture' && !action.context.architecture_review"
  action: block
  message: Architecture review required
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    # No review → blocked
    result = await evaluate_guardrails(
        {"category": "architecture", "architecture_review": False},
        guardrails_dir=tmp_path,
    )
    assert not result.allowed

    # Review done → allowed
    result2 = await evaluate_guardrails(
        {"category": "architecture", "architecture_review": True},
        guardrails_dir=tmp_path,
    )
    assert result2.allowed


@pytest.mark.asyncio
async def test_invalid_cel_fails_closed(tmp_path):
    """A block rule that cannot be evaluated blocks instead of being skipped."""
    yaml_content = """
- id: broken-cel
  description: Bad CEL expression
  condition: "THIS IS @@@ NOT VALID CEL"
  action: block
  message: Should not appear
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    result = await evaluate_guardrails({"stakes": "high"}, guardrails_dir=tmp_path)
    assert not result.allowed
    [violation] = result.violations
    assert violation.guardrail_id == "broken-cel"
    assert "could not be evaluated" in violation.message


@pytest.mark.asyncio
async def test_dict_condition_on_unsent_context_field_is_unmet(tmp_path):
    """Fail-closed must not turn an optional context field into a blanket block."""
    yaml_content = """
- id: prod-high
  description: High-stakes production deploys
  condition:
    deploy_env: production
    stakes: high
  action: block
  message: Blocked
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    without = await evaluate_guardrails({"stakes": "high"}, guardrails_dir=tmp_path)
    production = await evaluate_guardrails(
        {"stakes": "high", "deploy_env": "production"}, guardrails_dir=tmp_path
    )

    assert without.allowed
    assert not production.allowed


@pytest.mark.asyncio
async def test_dict_condition_on_scope_is_unmet_when_not_sent(tmp_path):
    """scope is a standard field, so it needs a default rather than has()."""
    yaml_content = """
- id: trader-only
  description: CryptoTrader trading actions
  condition:
    scope: CryptoTrader
    category: trading
  action: block
  message: Blocked
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    result = await evaluate_guardrails({"category": "trading"}, guardrails_dir=tmp_path)

    assert result.allowed


@pytest.mark.asyncio
async def test_cel_runtime_error_in_warn_rule_warns(tmp_path):
    yaml_content = """
- id: broken-warn
  description: Compares a number to a string
  condition: "action.confidence == 'high'"
  action: warn
  message: Should not appear
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    result = await evaluate_guardrails({"confidence": 0.9}, guardrails_dir=tmp_path)
    assert result.allowed
    assert [w.guardrail_id for w in result.warnings] == ["broken-warn"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        # Checking an action before it is taken: no record-phase fields.
        {"category": "process", "stakes": "medium", "confidence": 0.8},
        {
            "category": "process", "stakes": "medium", "confidence": 0.8,
            "phase": "record", "quality_score": 0.9,
            "has_deliberation": True, "has_reasoning": True,
        },
    ],
)
async def test_shipped_cel_guardrails_evaluate_cleanly(context):
    """Errors now trigger rules, so the shipped rules must never error."""
    clear_guardrails_cache()
    result = await evaluate_guardrails(context)
    triggered = [g.guardrail_id for g in result.violations + result.warnings]
    assert not {"require-deliberation", "recommend-reasoning", "low-quality-recording"} & set(
        triggered
    )


@pytest.mark.asyncio
async def test_low_quality_recording_fires_on_low_scores():
    clear_guardrails_cache()
    result = await evaluate_guardrails({
        "category": "process", "stakes": "medium", "confidence": 0.8,
        "phase": "record", "quality_score": 0.2,
        "has_deliberation": True, "has_reasoning": True,
    })
    [violation] = [v for v in result.violations if v.guardrail_id == "low-quality-recording"]
    assert "score 0.2" in violation.message


@pytest.mark.asyncio
async def test_legacy_flat_format_still_works(tmp_path):
    """Existing flat condition_*/requires_* YAML format continues to work."""
    yaml_content = """
- id: legacy-flat
  description: Legacy flat format
  condition_stakes: high
  condition_confidence: "< 0.5"
  action: block
  message: Legacy flat blocked
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    result = await evaluate_guardrails(
        {"stakes": "high", "confidence": 0.3}, guardrails_dir=tmp_path
    )
    assert not result.allowed
    assert result.violations[0].guardrail_id == "legacy-flat"


@pytest.mark.asyncio
async def test_legacy_requires_still_works(tmp_path):
    """Existing requires_* YAML format continues to work."""
    yaml_content = """
- id: legacy-requires
  description: Requires code review
  condition_category: tooling
  requires_code_review: true
  action: block
  message: Code review required
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    # code_review missing → blocked
    result = await evaluate_guardrails({"category": "tooling"}, guardrails_dir=tmp_path)
    assert not result.allowed

    # code_review=true → allowed
    result2 = await evaluate_guardrails(
        {"category": "tooling", "code_review": True}, guardrails_dir=tmp_path
    )
    assert result2.allowed


@pytest.mark.asyncio
async def test_mcp_context_forwarded_to_cel(tmp_path):
    """Simulates MCP context forwarding: context dict merged into flat ctx."""
    yaml_content = """
- id: require-arch-review
  description: Require architecture review
  condition: "action.category == 'architecture' && !action.context.architecture_review"
  action: block
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    # Simulate how _handle_check_action builds the flat context:
    # context dict items merged at top level
    flat_ctx: dict = {
        "category": "architecture",
        "stakes": "medium",
        "confidence": 0.9,
    }
    # MCP caller passes context={architecture_review: True}
    flat_ctx.update({"architecture_review": True})

    result = await evaluate_guardrails(flat_ctx, guardrails_dir=tmp_path)
    assert result.allowed


@pytest.mark.asyncio
async def test_warn_action_does_not_block(tmp_path):
    yaml_content = """
- id: warn-only
  description: Warning guardrail
  condition: "action.stakes == 'high'"
  action: warn
  message: High stakes warning
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    result = await evaluate_guardrails({"stakes": "high"}, guardrails_dir=tmp_path)
    assert result.allowed  # warn doesn't block
    assert len(result.warnings) == 1
    assert result.warnings[0].severity == "warn"


@pytest.mark.asyncio
async def test_complex_in_expression(tmp_path):
    yaml_content = """
- id: complex-in
  description: Multiple stakes
  condition: "action.stakes in ['high', 'critical']"
  action: block
"""
    (tmp_path / "test.yaml").write_text(yaml_content, encoding="utf-8")

    result_high = await evaluate_guardrails({"stakes": "high"}, guardrails_dir=tmp_path)
    assert not result_high.allowed

    result_critical = await evaluate_guardrails({"stakes": "critical"}, guardrails_dir=tmp_path)
    assert not result_critical.allowed

    result_medium = await evaluate_guardrails({"stakes": "medium"}, guardrails_dir=tmp_path)
    assert result_medium.allowed
