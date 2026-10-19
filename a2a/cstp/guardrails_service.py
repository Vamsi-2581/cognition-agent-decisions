"""Guardrails evaluation service for CSTP.

Wraps the existing check.py guardrail evaluation logic.
F054: CEL expression guardrails — CelGuardrailEvaluator added.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# F054: CEL support (optional — fails open if not installed)
try:
    import celpy

    _CEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CEL_AVAILABLE = False

# Configure audit logger
_audit_logger = logging.getLogger("cstp.guardrails.audit")

# Guardrail cache (cleared on process restart)
_guardrails_cache: dict[str, tuple[list["Guardrail"], float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minute cache

# Configurable guardrails paths
GUARDRAILS_PATHS = os.getenv(
    "GUARDRAILS_PATHS",
    "",
).split(":") if os.getenv("GUARDRAILS_PATHS") else []


@dataclass
class GuardrailCondition:
    """Condition that triggers a guardrail."""

    field: str
    operator: str
    value: Any

    def evaluate(self, context: dict[str, Any]) -> bool:
        """Evaluate condition against context."""
        actual = context.get(self.field)
        if actual is None:
            return False

        if self.operator == "eq":
            return actual == self.value
        elif self.operator == "ne":
            return actual != self.value
        elif self.operator == "lt":
            return float(actual) < float(self.value)
        elif self.operator == "gt":
            return float(actual) > float(self.value)
        elif self.operator == "lte":
            return float(actual) <= float(self.value)
        elif self.operator == "gte":
            return float(actual) >= float(self.value)
        return False


@dataclass
class GuardrailRequirement:
    """Requirement that must be met."""

    field: str
    expected: Any

    def check(self, context: dict[str, Any]) -> tuple[bool, str]:
        """Check requirement against context."""
        actual = context.get(self.field)
        if actual is None:
            return False, f"Missing: {self.field}"

        if isinstance(self.expected, bool):
            passed = bool(actual) == self.expected
        elif isinstance(self.expected, str) and self.expected.startswith(
            (">=", "<=", ">", "<")
        ):
            match = re.match(r"([><]=?)\s*([\d.]+)", self.expected)
            if match:
                op, val = match.groups()
                val = float(val)
                actual = float(actual)
                if op == ">=":
                    passed = actual >= val
                elif op == "<=":
                    passed = actual <= val
                elif op == ">":
                    passed = actual > val
                else:
                    passed = actual < val
            else:
                passed = False
        else:
            passed = actual == self.expected

        return passed, "" if passed else f"{self.field}: expected {self.expected}, got {actual}"


@dataclass
class Guardrail:
    """A guardrail definition."""

    id: str
    description: str
    conditions: list[GuardrailCondition] = field(default_factory=list)
    requirements: list[GuardrailRequirement] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    action: str = "warn"
    message: str = ""
    # F054: CEL expression (if set, CEL evaluation replaces legacy logic)
    cel_expression: str | None = None

    def applies_to(self, context: dict[str, Any]) -> bool:
        """Check if guardrail applies to this context."""
        if self.scope:
            project = context.get("project", context.get("scope", ""))
            if project and project not in self.scope:
                return False

        for cond in self.conditions:
            if not cond.evaluate(context):
                return False
        return True

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        """Evaluate guardrail against context."""
        if not self.applies_to(context):
            return {"id": self.id, "matched": False, "action": "skip"}

        if self.requirements:
            failed = []
            for req in self.requirements:
                passed, msg = req.check(context)
                if not passed:
                    failed.append(msg)

            if failed:
                message = self.message or f"{self.id}: {'; '.join(failed)}"
                for k, v in context.items():
                    message = message.replace(f"{{{k}}}", str(v))
                return {
                    "id": self.id,
                    "matched": True,
                    "passed": False,
                    "action": self.action,
                    "message": message,
                    "description": self.description,
                }
            return {"id": self.id, "matched": True, "passed": True, "action": "pass"}

        # No requirements = condition match means violation
        message = self.message or f"Guardrail {self.id} triggered"
        for k, v in context.items():
            message = message.replace(f"{{{k}}}", str(v))
        return {
            "id": self.id,
            "matched": True,
            "passed": False,
            "action": self.action,
            "message": message,
            "description": self.description,
        }


# F054: Fields that live directly under action.* in the CEL activation context.
# Everything else from the flat evaluation context goes into action.context.*.
_STANDARD_ACTION_FIELDS: frozenset[str] = frozenset({
    "description", "stakes", "confidence", "category", "tags",
    "reason_count", "pattern", "quality_score", "has_pattern", "has_tags",
    "phase", "deliberation_inputs_count", "has_deliberation", "has_reasoning",
    "scope", "project",
})

# Suffix → CEL operator
_SUFFIX_TO_CEL_OP: dict[str, str] = {
    "_lt": "<",
    "_gt": ">",
    "_lte": "<=",
    "_gte": ">=",
}

# Comparison prefix in a legacy string value → CEL operator
_COMPARISON_TO_CEL_OP: dict[str, str] = {
    "<": "<",
    ">": ">",
    "<=": "<=",
    ">=": ">=",
    "==": "==",
    "!=": "!=",
}

# Special field name remapping for legacy JSONB keys
_LEGACY_FIELD_REMAP: dict[str, str] = {
    "quality_lt": "quality_score",
    "quality_gt": "quality_score",
    "quality_lte": "quality_score",
    "quality_gte": "quality_score",
}


def _cel_literal(value: Any) -> str:
    """Render a Python value as a CEL literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("'", "\\'")
        return f"'{escaped}'"
    return str(value)


def _jsonb_condition_to_cel(cond_dict: dict[str, Any]) -> str:
    """Convert a legacy JSONB condition dict to a CEL expression string.

    Handles formats from the spec migration table:
      {"stakes": "high", "confidence_lt": 0.5}  →  "action.stakes == 'high' && action.confidence < 0.5"
    """
    parts: list[str] = []
    for key, value in cond_dict.items():
        if key == "cel":
            # Already CEL — handled by caller
            continue

        # Check for suffix operators (_lt, _gt, _lte, _gte)
        cel_op: str | None = None
        field_name = key
        for suffix, op in _SUFFIX_TO_CEL_OP.items():
            if key.endswith(suffix):
                cel_op = op
                base = key[: -len(suffix)]
                # Apply field remapping (e.g. quality_lt → quality_score)
                field_name = _LEGACY_FIELD_REMAP.get(key, base)
                break

        cel_field = (
            f"action.{field_name}"
            if field_name in _STANDARD_ACTION_FIELDS
            else f"action.context.{field_name}"
        )

        # Comparison strings such as quality_score: "< 0.5", read the same way
        # as the flat format's _parse_condition. Rendered as an equality they
        # compared a number to a string and errored on every evaluation.
        if cel_op is None and isinstance(value, str) and value.startswith(("<", ">", "=")):
            match = re.match(r"([<>=!]+)\s*(.*)", value)
            if match:
                op_str, raw = match.groups()
                cel_op = _COMPARISON_TO_CEL_OP.get(op_str, "==")
                try:
                    value = float(raw)
                except ValueError:
                    value = raw

        if cel_op:
            parts.append(f"{cel_field} {cel_op} {_cel_literal(value)}")
        else:
            parts.append(f"{cel_field} == {_cel_literal(value)}")

    return " && ".join(parts) if parts else "true"


def _build_cel_activation(flat_ctx: dict[str, Any]) -> dict[str, Any]:
    """Build the CEL activation dict from the flat evaluation context.

    The flat context mixes standard action fields (stakes, confidence, …)
    with caller-supplied extras (code_review, architecture_review, …).
    This function separates them into action.* and action.context.*.
    """
    action: dict[str, Any] = {}
    extra: dict[str, Any] = {}

    for k, v in flat_ctx.items():
        if k in _STANDARD_ACTION_FIELDS:
            action[k] = v
        else:
            extra[k] = v

    # Ensure numeric/bool defaults so CEL comparisons don't error on None
    action.setdefault("description", "")
    action.setdefault("stakes", "medium")
    action["confidence"] = float(action.get("confidence") or 0.0)
    action.setdefault("category", "")
    action.setdefault("tags", [])
    action.setdefault("reason_count", 0)
    action.setdefault("pattern", "")
    action.setdefault("quality_score", 0.0)
    # Record-phase fields: absent when checking an action before it is taken.
    # A missing field is a CEL error, and errors now trigger the rule.
    action.setdefault("phase", "")
    action.setdefault("deliberation_inputs_count", 0)
    action.setdefault("has_deliberation", False)
    action.setdefault("has_reasoning", False)
    action.setdefault("project", "")
    action["has_tags"] = bool(action.get("tags"))
    action["has_pattern"] = bool(action.get("pattern"))
    action["context"] = extra

    return {"action": action}


_logger = logging.getLogger(__name__)


class CelEvaluationError(Exception):
    """A guardrail's CEL expression could not be compiled or evaluated."""


class CelGuardrailEvaluator:
    """F054: Evaluate guardrail CEL expressions.

    Compiles each unique CEL expression once and caches the program.
    Builds a structured activation context (action.*) from the flat
    evaluation context dict used by evaluate_guardrails().

    Raises CelEvaluationError when an expression is invalid or errors during
    evaluation, so the caller can fail closed instead of skipping the rule.
    """

    def __init__(self) -> None:
        # Cache: expression string → compiled CEL program
        self._programs: dict[str, Any] = {}

    def _get_program(self, expression: str) -> Any | None:
        """Compile (or retrieve cached) CEL program for expression."""
        if not _CEL_AVAILABLE:
            return None
        if expression in self._programs:
            return self._programs[expression]
        try:
            env = celpy.Environment()
            ast = env.compile(expression)
            prog = env.program(ast)
            self._programs[expression] = prog
            return prog
        except Exception as exc:
            _logger.warning("CEL compile error for expression %r: %s", expression, exc)
            self._programs[expression] = None
            return None

    def evaluate(
        self,
        guardrail_id: str,
        expression: str,
        flat_ctx: dict[str, Any],
    ) -> bool:
        """Evaluate a CEL expression against the flat context.

        Returns True if the guardrail should trigger, False otherwise.

        Raises:
            CelEvaluationError: The expression does not compile (or CEL is
                not installed) or raised during evaluation.
        """
        prog = self._get_program(expression)
        if prog is None:
            raise CelEvaluationError(f"guardrail {guardrail_id!r}: expression does not compile")

        activation_data = _build_cel_activation(flat_ctx)
        try:
            activation = celpy.json_to_cel(activation_data)
            result = prog.evaluate(activation)
            return bool(result)
        except Exception as exc:
            _logger.warning(
                "CEL eval error for guardrail %r expression %r: %s",
                guardrail_id,
                expression,
                exc,
            )
            raise CelEvaluationError(f"guardrail {guardrail_id!r}: {exc}") from exc


# Module-level evaluator (program cache persists across calls)
_cel_evaluator = CelGuardrailEvaluator()


def _parse_condition(field_name: str, value: Any) -> GuardrailCondition:
    """Parse a condition from YAML."""
    if isinstance(value, str) and value.startswith(("<", ">", "=")):
        match = re.match(r"([<>=!]+)\s*(.*)", value)
        if match:
            op_str, val = match.groups()
            op_map = {"<": "lt", ">": "gt", "<=": "lte", ">=": "gte", "==": "eq", "!=": "ne"}
            op = op_map.get(op_str, "eq")
            try:
                val = float(val)
            except ValueError:
                pass
            return GuardrailCondition(field_name, op, val)
    return GuardrailCondition(field_name, "eq", value)


def _parse_guardrail(data: dict[str, Any]) -> Guardrail:
    """Parse a guardrail from dict.

    F054: Detects CEL expressions in the condition field (string, dict with
    'cel' key, or legacy JSONB dict) and stores them in cel_expression.
    Flat condition_*/requires_* fields continue to use legacy evaluation.
    """
    conditions: list[GuardrailCondition] = []
    requirements: list[GuardrailRequirement] = []
    scope: list[str] = []
    cel_expression: str | None = None

    raw_condition = data.get("condition")

    if isinstance(raw_condition, str):
        # F054: CEL string condition — use directly
        cel_expression = raw_condition
    elif isinstance(raw_condition, dict):
        if "cel" in raw_condition:
            # F054: Explicit CEL dict — {"cel": "action.stakes == 'high'"}
            cel_expression = str(raw_condition["cel"])
        else:
            # F054: Legacy JSONB dict — auto-convert to CEL
            cel_expression = _jsonb_condition_to_cel(raw_condition)

    # If no CEL from condition, fall through to flat legacy format
    if cel_expression is None:
        # Support flat format: condition_field, requires_field
        for key, value in data.items():
            if key.startswith("condition_"):
                conditions.append(_parse_condition(key[10:], value))
            elif key.startswith("requires_"):
                requirements.append(GuardrailRequirement(key[9:], value))

    # Support nested requires: {field: value, ...} dict (legacy, always parsed)
    if "requires" in data and isinstance(data["requires"], dict):
        for field_name, value in data["requires"].items():
            requirements.append(GuardrailRequirement(field_name, value))

    if "scope" in data:
        scope = data["scope"] if isinstance(data["scope"], list) else [data["scope"]]

    return Guardrail(
        id=data.get("id", "unknown"),
        description=data.get("description", ""),
        conditions=conditions,
        requirements=requirements,
        scope=scope,
        action=data.get("action", "warn"),
        message=data.get("message", ""),
        cel_expression=cel_expression,
    )


def _get_guardrails_paths(guardrails_dir: Path | None = None) -> list[Path]:
    """Get list of guardrail directories to search.

    If guardrails_dir is provided, ONLY that directory is used (for testing).
    Otherwise, uses GUARDRAILS_PATHS env var + default locations.
    """
    # If custom directory provided, use ONLY that (for testing isolation)
    if guardrails_dir:
        return [guardrails_dir]

    paths: list[Path] = []

    # Configurable paths from environment
    for p in GUARDRAILS_PATHS:
        if p.strip():
            paths.append(Path(p.strip()).expanduser())

    # Default paths (relative to package and cwd)
    paths.extend([
        Path(__file__).parent.parent.parent / "guardrails",
        Path.cwd() / "guardrails",
    ])

    return paths


def _load_guardrails_from_paths(paths: list[Path]) -> list[Guardrail]:
    """Load guardrails from YAML files in given paths."""
    guardrails: list[Guardrail] = []
    seen_ids: set[str] = set()

    for dir_path in paths:
        if not dir_path.exists():
            continue
        for yaml_path in dir_path.glob("*.yaml"):
            try:
                content = yaml_path.read_text(encoding="utf-8")

                # Use PyYAML if available (preferred)
                try:
                    import yaml
                    items = yaml.safe_load(content)
                    if items is None:
                        continue
                    if not isinstance(items, list):
                        items = [items]
                except ImportError:
                    # Fallback: skip if no yaml and content is complex
                    _audit_logger.warning(
                        f"PyYAML not installed, skipping {yaml_path}. "
                        "Install pyyaml for guardrail support."
                    )
                    continue

                for item in items:
                    if isinstance(item, dict):
                        g = _parse_guardrail(item)
                        if g.id not in seen_ids:
                            guardrails.append(g)
                            seen_ids.add(g.id)
            except Exception as e:
                _audit_logger.warning(f"Failed to load {yaml_path}: {e}")

    return guardrails


def _load_guardrails(guardrails_dir: Path | None = None) -> list[Guardrail]:
    """Load guardrails with caching.

    Guardrails are cached for 5 minutes to avoid repeated disk I/O.
    """
    import time

    cache_key = str(guardrails_dir) if guardrails_dir else "__default__"
    now = time.time()

    # Check cache
    if cache_key in _guardrails_cache:
        cached_guardrails, cached_at = _guardrails_cache[cache_key]
        if now - cached_at < _CACHE_TTL_SECONDS:
            return cached_guardrails

    # Load from disk
    paths = _get_guardrails_paths(guardrails_dir)
    guardrails = _load_guardrails_from_paths(paths)

    # Update cache
    _guardrails_cache[cache_key] = (guardrails, now)

    return guardrails


def clear_guardrails_cache() -> None:
    """Clear the guardrails cache. Use after modifying guardrail files."""
    _guardrails_cache.clear()


@dataclass(slots=True)
class GuardrailResult:
    """Result of guardrail evaluation."""

    guardrail_id: str
    name: str
    message: str
    severity: str
    suggestion: str | None = None


@dataclass(slots=True)
class EvaluationResult:
    """Result of full guardrail evaluation."""

    allowed: bool
    violations: list[GuardrailResult]
    warnings: list[GuardrailResult]
    evaluated: int


def list_guardrails(scope: str | None = None) -> list[dict[str, Any]]:
    """List active guardrails, optionally filtered by scope.

    Args:
        scope: Optional project/scope filter. If provided, returns only
               guardrails that would apply to this scope (or are global).

    Returns:
        List of guardrail definitions as dicts.
    """
    guardrails = _load_guardrails()
    result = []

    for g in guardrails:
        # Filter by scope if requested
        if scope:
            # If guardrail has specific scopes, and requested scope isn't in them
            if g.scope and scope not in g.scope:
                continue

        # Convert to dict
        g_dict = {
            "id": g.id,
            "description": g.description,
            "action": g.action,
            "scope": g.scope,
            "conditions": [
                {"field": c.field, "operator": c.operator, "value": c.value}
                for c in g.conditions
            ],
            "requirements": [
                {"field": r.field, "expected": r.expected}
                for r in g.requirements
            ],
        }
        result.append(g_dict)

    return result


async def evaluate_guardrails(
    context: dict[str, Any],
    guardrails_dir: Path | None = None,
) -> EvaluationResult:
    """Evaluate context against all guardrails.

    Args:
        context: Evaluation context (category, stakes, confidence, etc.)
        guardrails_dir: Optional custom guardrails directory.

    Returns:
        EvaluationResult with violations, warnings, and allow/block decision.

    Note:
        Guardrails are cached for 5 minutes. Call clear_guardrails_cache()
        after modifying guardrail files.
    """
    guardrails = _load_guardrails(guardrails_dir)

    violations: list[GuardrailResult] = []
    warnings: list[GuardrailResult] = []
    allowed = True

    for g in guardrails:
        if g.cel_expression is not None:
            # F054: CEL evaluation path
            try:
                triggered = _cel_evaluator.evaluate(g.id, g.cel_expression, context)
                eval_failed = False
            except CelEvaluationError:
                # Fail closed: a rule that cannot be evaluated applies, so a
                # broken block rule blocks instead of silently allowing.
                triggered = eval_failed = True
            if triggered:
                if eval_failed:
                    message = f"Guardrail {g.id} could not be evaluated, so it was applied"
                else:
                    message = g.message or f"Guardrail {g.id} triggered"
                    for k, v in context.items():
                        message = message.replace(f"{{{k}}}", str(v))
                gr = GuardrailResult(
                    guardrail_id=g.id,
                    name=g.description or g.id,
                    message=message,
                    severity=g.action,
                    suggestion=None,
                )
                if g.action == "block":
                    violations.append(gr)
                    allowed = False
                else:
                    warnings.append(gr)
        else:
            # Legacy evaluation path (condition_*/requires_* flat format)
            result = g.evaluate(context)
            if result.get("matched") and result.get("action") != "skip":
                if not result.get("passed", True):
                    gr = GuardrailResult(
                        guardrail_id=result["id"],
                        name=result.get("description", result["id"]),
                        message=result.get("message", ""),
                        severity=result["action"],
                        suggestion=None,
                    )
                    if result["action"] == "block":
                        violations.append(gr)
                        allowed = False
                    else:
                        warnings.append(gr)

    # F030: Circuit breaker evaluation
    try:
        from .circuit_breaker_service import get_circuit_breaker_manager

        mgr = await get_circuit_breaker_manager()
        if mgr.is_initialized:
            cb_results = await mgr.check(context)
            for cbr in cb_results:
                if cbr.blocked:
                    violations.append(GuardrailResult(
                        guardrail_id=f"circuit_breaker:{cbr.scope}",
                        name=f"Circuit breaker ({cbr.scope})",
                        message=cbr.message,
                        severity="block",
                    ))
                    allowed = False
    except Exception:
        logging.getLogger(__name__).debug(
            "Circuit breaker evaluation failed", exc_info=True,
        )

    return EvaluationResult(
        allowed=allowed,
        violations=violations,
        warnings=warnings,
        evaluated=len(guardrails),
    )


async def evaluate_record_guardrails(
    request: Any,
) -> list[dict[str, Any]]:
    """F026: Evaluate guardrails against a recordDecision request.

    Builds record context from the request and returns guardrail warnings.
    Shared between JSON-RPC dispatcher and MCP server.

    Args:
        request: RecordDecisionRequest with deliberation, decision, etc.

    Returns:
        List of warning dicts (empty if no guardrails triggered).
    """
    delib_input_count = len(request.deliberation.inputs) if request.deliberation else 0
    has_reasoning = False
    if request.deliberation and request.deliberation.steps:
        has_reasoning = any(
            s.type == "reasoning" for s in request.deliberation.steps
        )
    record_context = {
        "description": request.decision,
        "category": request.category,
        "stakes": request.stakes,
        "confidence": request.confidence,
        "deliberation_inputs_count": delib_input_count,
        "has_deliberation": delib_input_count > 0 or has_reasoning,
        "has_reasoning": has_reasoning,
        "phase": "record",
    }

    # F027 P3: Add quality score to context for guardrail evaluation
    if hasattr(request, "pattern") or hasattr(request, "tags"):
        from .decision_service import score_decision_quality
        quality = score_decision_quality(request)
        record_context["quality_score"] = quality["score"]

    record_eval = await evaluate_guardrails(record_context)
    warnings: list[dict[str, Any]] = []
    for v in record_eval.violations:
        warnings.append({
            "guardrail_id": v.guardrail_id,
            "message": v.message,
            "severity": "block",
        })
    for w in record_eval.warnings:
        warnings.append({
            "guardrail_id": w.guardrail_id,
            "message": w.message,
        })
    return warnings


def log_guardrail_check(
    requesting_agent: str,
    action_description: str,
    allowed: bool,
    violations: list[GuardrailResult],
    evaluated: int,
) -> None:
    """Log guardrail check for audit purposes.

    Args:
        requesting_agent: Agent ID that requested the check.
        action_description: What action was being checked.
        allowed: Whether the action was allowed.
        violations: List of violations if any.
        evaluated: Number of guardrails evaluated.
    """
    audit_entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": "guardrail_check",
        "requesting_agent": requesting_agent,
        "action": action_description,
        "allowed": allowed,
        "violations": [v.guardrail_id for v in violations],
        "evaluated": evaluated,
    }
    _audit_logger.info(json.dumps(audit_entry))
