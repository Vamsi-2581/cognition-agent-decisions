"""Decision recording service for CSTP.

Creates decision YAML files and indexes them to ChromaDB.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import yaml

from .embeddings.factory import get_embedding_provider
from .models import MAX_CONTEXT_CHARS, MAX_TEXT_CHARS
from .storage.factory import get_decision_store
from .vectordb.factory import get_vector_store

# Environment configuration
DECISIONS_PATH = os.getenv("DECISIONS_PATH", "decisions")

logger = logging.getLogger(__name__)


def _restore_yaml(file_path: Any, original_bytes: bytes) -> str:
    """Put a decision file back the way it was after a failed store write.

    Used by the non-idempotent mutation paths, where leaving the YAML changed
    would make a retry either duplicate work or be skipped entirely.

    Returns a short note describing what happened, for the error payload.
    """
    try:
        file_path.write_bytes(original_bytes)
    except Exception as e:
        logger.error("Rollback of %s failed: %s", file_path, e)
        return (
            f"The YAML file at {file_path} was modified and could not be rolled "
            f"back ({e}); it may now disagree with the store."
        )
    return f"The YAML file at {file_path} was rolled back; the call is safe to retry."


async def _persist_store_mutation(
    decision_id: str,
    what: str,
    coro: Any,
) -> str | None:
    """Await a DecisionStore mutation and return an error string, or None on success.

    Since F050 the store — not the YAML file — is what listDecisions, getStats,
    queryDecisions, and calibration read. A mutation that only reaches YAML is
    therefore invisible to every read path, so acknowledging one as success
    reports a change the caller can never observe.

    Both failure modes matter. Backends raise on some errors, and
    SQLiteDecisionStore converts others into a `False` return, so checking only
    for exceptions misses half of them.

    Args:
        decision_id: Decision being mutated, for the log line.
        what: Short description of the mutation, used in the error message.
        coro: Awaitable performing the store write.

    Returns:
        An error message suitable for a response payload, or None if it landed.
    """
    try:
        result = await coro
    except Exception as e:
        logger.error("DecisionStore %s failed for %s: %s", what, decision_id, e)
        return f"Failed to persist {what} to store: {e}"

    if result is False:
        logger.error("DecisionStore rejected %s for %s", what, decision_id)
        return f"Decision store rejected the {what}."

    return None


@dataclass
class Reason:
    """A reason supporting a decision."""

    type: str  # authority, analogy, analysis, pattern, intuition
    text: str
    strength: float = 0.8

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for YAML."""
        return {
            "type": self.type,
            "text": self.text,
            "strength": self.strength,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Reason":
        """Create from dictionary."""
        return cls(
            type=data.get("type") or "analysis",
            text=data.get("text") or "",
            strength=float(data.get("strength") or 0.8),
        )


@dataclass
class PreDecisionProtocol:
    """Pre-decision protocol tracking."""

    query_run: bool = False
    similar_found: int = 0
    guardrails_checked: bool = False
    guardrails_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for YAML."""
        return {
            "query_run": self.query_run,
            "similar_found": self.similar_found,
            "guardrails_checked": self.guardrails_checked,
            "guardrails_passed": self.guardrails_passed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PreDecisionProtocol":
        """Create from dictionary."""
        return cls(
            query_run=bool(data.get("query_run") or data.get("queryRun") or False),
            similar_found=int(data.get("similar_found") or data.get("similarFound") or 0),
            guardrails_checked=bool(
                data.get("guardrails_checked") or data.get("guardrailsChecked") or False
            ),
            guardrails_passed=bool(
                data.get("guardrails_passed") or data.get("guardrailsPassed") or False
            ),
        )


@dataclass
class ProjectContext:
    """Project context for a decision."""

    project: str | None = None  # owner/repo format
    feature: str | None = None  # Feature or epic name
    pr: int | None = None  # PR number
    file: str | None = None  # File path relative to repo root
    line: int | None = None  # Line number in file
    commit: str | None = None  # Commit SHA (7+ chars)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectContext":
        """Create from dictionary."""
        # Cast pr and line to int to prevent type pollution from external inputs
        pr_val = data.get("pr")
        line_val = data.get("line")

        return cls(
            project=data.get("project"),
            feature=data.get("feature"),
            pr=int(pr_val) if pr_val is not None else None,
            file=data.get("file"),
            line=int(line_val) if line_val is not None else None,
            commit=data.get("commit"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        result: dict[str, Any] = {}
        if self.project:
            result["project"] = self.project
        if self.feature:
            result["feature"] = self.feature
        if self.pr is not None:
            result["pr"] = self.pr
        if self.file:
            result["file"] = self.file
        if self.line is not None:
            result["line"] = self.line
        if self.commit:
            result["commit"] = self.commit
        return result

    def has_any(self) -> bool:
        """Check if any project context is set."""
        return any([
            self.project,
            self.feature,
            self.pr is not None,
            self.file,
            self.line is not None,
            self.commit,
        ])


@dataclass
class ReasoningStep:
    """A single step in the reasoning trace."""

    step: int
    thought: str
    output: str | None = None
    confidence: float | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result: dict[str, Any] = {
            "step": self.step,
            "thought": self.thought,
        }
        if self.output:
            result["output"] = self.output
        if self.confidence is not None:
            result["confidence"] = self.confidence
        if self.tags:
            result["tags"] = self.tags
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReasoningStep":
        """Create from dictionary."""
        return cls(
            step=int(data.get("step") or 0),
            thought=data.get("thought") or "",
            output=data.get("output"),
            confidence=float(data.get("confidence")) if data.get("confidence") is not None else None,
            tags=data.get("tags") or [],
        )


@dataclass
class DeliberationInput:
    """An input/evidence item gathered during deliberation.

    Attributes:
        id: Short identifier (e.g., "i1", "i2").
        text: Description of the input.
        source: Where the input came from (url, file, memory, api, etc.).
        timestamp: When this input was gathered.
    """

    id: str
    text: str
    source: str | None = None
    timestamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
        }
        if self.source:
            result["source"] = self.source
        if self.timestamp:
            result["timestamp"] = self.timestamp
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliberationInput":
        """Create from dictionary."""
        return cls(
            id=data.get("id") or "",
            text=data.get("text") or "",
            source=data.get("source"),
            timestamp=data.get("timestamp"),
        )


@dataclass
class DeliberationStep:
    """A step in the deliberation process.

    Attributes:
        step: Step number (1-indexed).
        thought: What was considered at this step.
        inputs_used: Which input IDs contributed to this step.
        timestamp: When this step occurred.
        duration_ms: How long this step took.
        type: Reasoning type used (maps to reason types).
        conclusion: Whether this step produced the final conclusion.
    """

    step: int
    thought: str
    inputs_used: list[str] = field(default_factory=list)
    timestamp: str | None = None
    duration_ms: int | None = None
    type: str | None = None
    conclusion: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result: dict[str, Any] = {
            "step": self.step,
            "thought": self.thought,
        }
        if self.inputs_used:
            result["inputs_used"] = self.inputs_used
        if self.timestamp:
            result["timestamp"] = self.timestamp
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        if self.type:
            result["type"] = self.type
        if self.conclusion:
            result["conclusion"] = self.conclusion
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliberationStep":
        """Create from dictionary."""
        duration_raw = data.get("duration_ms") or data.get("durationMs")
        return cls(
            step=int(data.get("step") or 0),
            thought=data.get("thought") or "",
            inputs_used=data.get("inputs_used") or data.get("inputsUsed") or [],
            timestamp=data.get("timestamp"),
            duration_ms=int(duration_raw) if duration_raw is not None else None,
            type=data.get("type"),
            conclusion=bool(data.get("conclusion", False)),
        )


@dataclass
class Deliberation:
    """Full deliberation trace for a decision (F023).

    Captures the chain-of-thought: which inputs were gathered,
    how they were combined step-by-step, and timing information.

    Attributes:
        inputs: Evidence/inputs gathered during deliberation.
        steps: Reasoning steps showing how inputs were combined.
        total_duration_ms: Total time spent deliberating.
        convergence_point: Step number where inputs converged to decision.
    """

    inputs: list[DeliberationInput] = field(default_factory=list)
    steps: list[DeliberationStep] = field(default_factory=list)
    total_duration_ms: int | None = None
    convergence_point: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result: dict[str, Any] = {
            "inputs": [i.to_dict() for i in self.inputs],
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.total_duration_ms is not None:
            result["total_duration_ms"] = self.total_duration_ms
        if self.convergence_point is not None:
            result["convergence_point"] = self.convergence_point
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Deliberation":
        """Create from dictionary."""
        inputs_data = data.get("inputs") or []
        steps_data = data.get("steps") or []

        duration_raw = data.get("total_duration_ms") or data.get("totalDurationMs")
        convergence_raw = data.get("convergence_point") or data.get("convergencePoint")

        return cls(
            inputs=[DeliberationInput.from_dict(i) for i in inputs_data],
            steps=[DeliberationStep.from_dict(s) for s in steps_data],
            total_duration_ms=int(duration_raw) if duration_raw is not None else None,
            convergence_point=int(convergence_raw) if convergence_raw is not None else None,
        )

    def has_content(self) -> bool:
        """Check if deliberation has any content."""
        return bool(self.inputs or self.steps)


@dataclass
class BridgeDefinition:
    """Minsky Ch 12 bridge-definition: connects structure to function.

    Bridges between two descriptions of a decision:
    - Structure: what the pattern looks like (recognizable form)
    - Function: what problem it solves (purpose, goal)

    Plus three operators from Ch 12.3 (Uniframes):
    - Enforcement: features that MUST be present
    - Prevention: features that MUST NOT be present
    - Tolerance: features that DON'T MATTER
    """

    structure: str  # What it looks like / recognizable pattern
    function: str  # What problem it solves / purpose
    tolerance: list[str] = field(default_factory=list)
    enforcement: list[str] = field(default_factory=list)
    prevention: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for YAML."""
        result: dict[str, Any] = {
            "structure": self.structure,
            "function": self.function,
        }
        if self.tolerance:
            result["tolerance"] = self.tolerance
        if self.enforcement:
            result["enforcement"] = self.enforcement
        if self.prevention:
            result["prevention"] = self.prevention
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeDefinition":
        """Create from dictionary."""
        return cls(
            structure=data.get("structure") or "",
            function=data.get("function") or data.get("purpose") or "",
            tolerance=data.get("tolerance") or [],
            enforcement=data.get("enforcement") or [],
            prevention=data.get("prevention") or [],
        )

    def has_content(self) -> bool:
        """Check if bridge has meaningful content."""
        return bool(self.structure or self.function)


@dataclass
class RelatedDecision:
    """A decision related to the current one.

    Auto-populated from pre-decision query results (deliberation trace).
    Provides lightweight graph edges without a full graph database.
    """

    id: str
    summary: str
    distance: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for YAML."""
        return {
            "id": self.id,
            "summary": self.summary,
            "distance": round(self.distance, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelatedDecision":
        """Create from dictionary."""
        return cls(
            id=data.get("id") or "",
            summary=data.get("summary") or "",
            distance=float(data.get("distance") or 0.0),
        )


@dataclass
class RecordDecisionRequest:
    """Request to record a new decision."""

    decision: str
    confidence: float
    category: str
    stakes: str = "medium"
    context: str | None = None
    reasons: list[Reason] = field(default_factory=list)
    trace: list[ReasoningStep] = field(default_factory=list)  # F020: Reasoning trace
    deliberation: Deliberation | None = None  # F023: Full deliberation trace
    bridge: BridgeDefinition | None = None  # F024: Bridge-definition (structure/function)
    related_to: list[RelatedDecision] = field(default_factory=list)  # F025: Related decisions
    kpi_indicators: list[str] = field(default_factory=list)
    mental_state: str | None = None
    review_in: str | None = None
    tags: list[str] = field(default_factory=list)
    pattern: str | None = None  # F027: Abstract pattern this decision represents
    pre_decision: PreDecisionProtocol | None = None
    project_context: ProjectContext | None = None  # F010: Project context
    agent_id: str | None = None  # Set from auth

    @classmethod
    def from_dict(cls, data: dict[str, Any], agent_id: str | None = None) -> "RecordDecisionRequest":
        """Create from dictionary (JSON-RPC params)."""
        reasons_data = data.get("reasons") or []
        reasons = [
            Reason.from_dict(r) if isinstance(r, dict) else r
            for r in reasons_data
        ]

        trace_data = data.get("trace") or []
        trace = [
            ReasoningStep.from_dict(t) if isinstance(t, dict) else t
            for t in trace_data
        ]

        # F023: Parse deliberation trace
        deliberation = None
        delib_data = data.get("deliberation")
        if delib_data and isinstance(delib_data, dict):
            deliberation = Deliberation.from_dict(delib_data)

        # F024: Parse bridge-definition
        bridge = None
        bridge_data = data.get("bridge")
        if bridge_data and isinstance(bridge_data, dict):
            bridge = BridgeDefinition.from_dict(bridge_data)

        pre_decision = None
        if "preDecision" in data or "pre_decision" in data:
            pd_data = data.get("preDecision") or data.get("pre_decision") or {}
            if pd_data:
                pre_decision = PreDecisionProtocol.from_dict(pd_data)

        # F010: Parse project context fields
        project_context = None
        project_fields = ["project", "feature", "pr", "file", "line", "commit"]
        if any(data.get(f) is not None for f in project_fields):
            project_context = ProjectContext.from_dict(data)

        return cls(
            decision=data.get("decision") or "",
            confidence=float(data.get("confidence") or 0.5),
            category=data.get("category") or "process",
            stakes=data.get("stakes") or "medium",
            context=data.get("context"),
            reasons=reasons,
            trace=trace,
            deliberation=deliberation,
            bridge=bridge,
            kpi_indicators=data.get("kpiIndicators") or data.get("kpi_indicators") or [],
            mental_state=data.get("mentalState") or data.get("mental_state"),
            review_in=data.get("reviewIn") or data.get("review_in"),
            tags=data.get("tags") or [],
            pattern=data.get("pattern"),
            pre_decision=pre_decision,
            project_context=project_context,
            agent_id=agent_id,
        )

    def length_errors(self) -> list[str]:
        """Return errors for free-text fields over their caps (see models.py).

        Split out from validate() for the pre_action auto-record path, which
        must enforce the caps without newly rejecting categories or stakes it
        has always accepted.
        """
        errors = []
        if self.decision and len(self.decision) > MAX_TEXT_CHARS:
            errors.append(f"decision: must be at most {MAX_TEXT_CHARS} characters")
        if self.context and len(self.context) > MAX_CONTEXT_CHARS:
            errors.append(f"context: must be at most {MAX_CONTEXT_CHARS} characters")
        if self.pattern and len(self.pattern) > MAX_TEXT_CHARS:
            errors.append(f"pattern: must be at most {MAX_TEXT_CHARS} characters")
        for i, reason in enumerate(self.reasons):
            if reason.text and len(reason.text) > MAX_TEXT_CHARS:
                errors.append(f"reasons[{i}].text: must be at most {MAX_TEXT_CHARS} characters")
        return errors

    def validate(self) -> list[str]:
        """Validate the request. Returns list of errors."""
        errors = self.length_errors()

        if not self.decision or not self.decision.strip():
            errors.append("decision: required field")

        if not 0.0 <= self.confidence <= 1.0:
            errors.append("confidence: must be between 0.0 and 1.0")

        valid_categories = {"architecture", "process", "integration", "tooling", "security"}
        if self.category not in valid_categories:
            errors.append(f"category: must be one of {valid_categories}")

        valid_stakes = {"low", "medium", "high", "critical"}
        if self.stakes not in valid_stakes:
            errors.append(f"stakes: must be one of {valid_stakes}")

        valid_reason_types = {
            "authority", "analogy", "analysis", "pattern",
            "intuition", "empirical", "elimination", "constraint",
        }
        for i, reason in enumerate(self.reasons):
            if reason.type not in valid_reason_types:
                errors.append(f"reasons[{i}].type: must be one of {valid_reason_types}")
            if not reason.text:
                errors.append(f"reasons[{i}].text: required")

        valid_mental_states = {"deliberate", "reactive", "exploratory", "habitual", "pressured"}
        if self.mental_state and self.mental_state not in valid_mental_states:
            errors.append(f"mentalState: must be one of {valid_mental_states}")

        return errors


@dataclass
class RecordDecisionResponse:
    """Response from recording a decision."""

    success: bool
    id: str
    path: str
    indexed: bool
    timestamp: str
    error: str | None = None
    quality: dict[str, Any] | None = None  # F027 P3: Quality score
    guardrail_warnings: list[dict[str, Any]] | None = None  # F026

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON-RPC response."""
        result: dict[str, Any] = {
            "success": self.success,
            "id": self.id,
            "path": self.path,
            "indexed": self.indexed,
            "timestamp": self.timestamp,
        }
        if self.error:
            result["error"] = self.error
        if self.quality:
            result["quality"] = self.quality
        if self.guardrail_warnings:
            result["guardrail_warnings"] = self.guardrail_warnings
        return result


def score_decision_quality(request: "RecordDecisionRequest") -> dict[str, Any]:
    """Score the quality of a decision recording (F027 P3).

    Returns a dict with score (0.0-1.0) and suggestions for improvement.
    """
    score = 0.0
    suggestions: list[str] = []

    # Pattern provided? (+0.15)
    if request.pattern:
        score += 0.15
    else:
        suggestions.append(
            "Add --pattern for the abstract principle "
            "(e.g. 'Override defaults when they don't match workload')"
        )

    # Tags provided? (+0.15)
    if request.tags:
        score += 0.15
    else:
        suggestions.append(
            "Add --tag keywords for cross-domain retrieval "
            "(e.g. --tag timeout --tag infrastructure)"
        )

    # 2+ distinct reason types? (+0.15)
    if request.reasons:
        reason_types = {r.type for r in request.reasons}
        if len(reason_types) >= 2:
            score += 0.15
        else:
            suggestions.append(
                f"Only 1 reason type ({next(iter(reason_types))}). "
                "Diverse reasons improve robustness - try analysis + empirical, "
                "or pattern + analogy"
            )
    else:
        suggestions.append("No reasons provided - add -r 'type:explanation'")

    # Explicit bridge? (+0.10)
    if request.bridge and request.bridge.has_content():
        score += 0.10

    # Decision text length > 20 chars? (+0.1)
    if len(request.decision) > 20:
        score += 0.1
    else:
        suggestions.append("Decision text is very short - be more descriptive")

    # Context provided? (+0.1)
    if request.context and len(request.context) > 10:
        score += 0.1
    else:
        suggestions.append("Add --context with what was actually done")

    # Project context? (+0.1)
    if request.project_context and request.project_context.has_any():
        score += 0.1

    # Deliberation inputs? (+0.05)
    has_deliberation = (
        (request.deliberation and request.deliberation.has_content())
        or request.pre_decision
    )
    if has_deliberation:
        score += 0.05

    # F028: Reasoning steps in deliberation? (+0.10)
    has_reasoning = False
    if request.deliberation and request.deliberation.steps:
        has_reasoning = any(
            s.type == "reasoning" for s in request.deliberation.steps
        )
    if has_reasoning:
        score += 0.10
    else:
        suggestions.append(
            "No reasoning captured - use cstp.py think \"your thought process\" "
            "to record chain-of-thought before recording"
        )

    return {
        "score": round(score, 2),
        "suggestions": suggestions,
    }


def generate_decision_id() -> str:
    """Generate a short unique decision ID."""
    return uuid.uuid4().hex[:8]


def calculate_review_date(review_in: str | None) -> str | None:
    """Calculate review date from relative string like '7d', '2w', '1m'."""
    if not review_in:
        return None

    now = datetime.now(UTC)
    value = int(review_in[:-1])
    unit = review_in[-1].lower()

    if unit == "d":
        days = value
    elif unit == "w":
        days = value * 7
    elif unit == "m":
        days = value * 30
    else:
        return None

    from datetime import timedelta
    review_date = now + timedelta(days=days)
    return review_date.strftime("%Y-%m-%d")


def build_decision_yaml(request: RecordDecisionRequest, decision_id: str) -> dict[str, Any]:
    """Build the decision YAML structure."""
    now = datetime.now(UTC)

    decision_data: dict[str, Any] = {
        "id": decision_id,
        "summary": request.decision,
        "decision": request.decision,  # Alias for compatibility
        "category": request.category,
        "confidence": request.confidence,
        "stakes": request.stakes,
        "status": "pending",
        "date": now.isoformat(),
    }

    if request.context:
        decision_data["context"] = request.context

    if request.reasons:
        decision_data["reasons"] = [r.to_dict() for r in request.reasons]

    # F020: Add reasoning trace
    if request.trace:
        decision_data["trace"] = [t.to_dict() for t in request.trace]

    # F023: Add deliberation trace
    if request.deliberation and request.deliberation.has_content():
        decision_data["deliberation"] = request.deliberation.to_dict()

    # F024: Add bridge-definition
    if request.bridge and request.bridge.has_content():
        decision_data["bridge"] = request.bridge.to_dict()

    # F025: Add related decisions
    if request.related_to:
        decision_data["related_to"] = [r.to_dict() for r in request.related_to]

    if request.kpi_indicators:
        decision_data["kpi_indicators"] = request.kpi_indicators

    if request.mental_state:
        decision_data["mental_state"] = request.mental_state

    if request.review_in:
        review_date = calculate_review_date(request.review_in)
        if review_date:
            decision_data["review_by"] = review_date

    if request.tags:
        decision_data["tags"] = request.tags

    # F027: Pattern field
    if request.pattern:
        decision_data["pattern"] = request.pattern

    if request.pre_decision:
        decision_data["pre_decision"] = request.pre_decision.to_dict()

    if request.agent_id:
        decision_data["recorded_by"] = request.agent_id

    # F010: Add project context fields
    if request.project_context and request.project_context.has_any():
        pc = request.project_context
        if pc.project:
            decision_data["project"] = pc.project
        if pc.feature:
            decision_data["feature"] = pc.feature
        if pc.pr is not None:
            decision_data["pr"] = pc.pr
        if pc.file:
            decision_data["file"] = pc.file
        if pc.line is not None:
            decision_data["line"] = pc.line
        if pc.commit:
            decision_data["commit"] = pc.commit

    return decision_data


def write_decision_file(
    decision_data: dict[str, Any],
    decision_id: str,
    base_path: str | None = None,
) -> str:
    """Write decision to YAML file. Returns file path."""
    base = Path(base_path or DECISIONS_PATH)
    now = datetime.now(UTC)

    # Create directory structure: decisions/YYYY/MM/
    year_month_dir = base / str(now.year) / f"{now.month:02d}"
    year_month_dir.mkdir(parents=True, exist_ok=True)

    # File name: YYYY-MM-DD-decision-<id>.yaml
    filename = f"{now.strftime('%Y-%m-%d')}-decision-{decision_id}.yaml"
    file_path = year_month_dir / filename

    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(decision_data, f, default_flow_style=False, sort_keys=False)

    return str(file_path)


@dataclass
class GetDecisionRequest:
    """Request to get a single decision by ID."""

    decision_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GetDecisionRequest":
        """Create from JSON-RPC params dict."""
        decision_id = data.get("id") or data.get("decision_id") or ""
        if not decision_id:
            raise ValueError("Missing required parameter: id")
        # Validate ID is alphanumeric (prevent path traversal)
        clean_id = decision_id.replace("-", "")
        if not clean_id.isalnum():
            raise ValueError(f"Invalid decision ID: {decision_id}")
        return cls(decision_id=decision_id)


@dataclass
class GetDecisionResponse:
    """Response for getting a single decision."""

    found: bool
    decision: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        result: dict[str, Any] = {"found": self.found}
        if self.decision is not None:
            result["decision"] = self.decision
        if self.error:
            result["error"] = self.error
        return result


async def get_decision(request: GetDecisionRequest) -> GetDecisionResponse:
    """Get a single decision by ID, returning full contents.

    Tries DecisionStore first, falls back to YAML rglob if not found.

    Args:
        request: Contains the decision ID to look up.

    Returns:
        Full decision data if found, or error.
    """
    # F050: Try DecisionStore first
    try:
        store = get_decision_store()
        data = await store.get(request.decision_id)
        if data:
            return GetDecisionResponse(found=True, decision=data)
    except Exception as e:
        logger.debug("DecisionStore get failed, falling back to YAML: %s", e)

    base = Path(DECISIONS_PATH)
    if not base.exists():
        return GetDecisionResponse(found=False, error="Decisions directory not found")

    # Search for matching file (glob handles both exact and prefix matches)
    pattern = f"*-decision-{request.decision_id}*.yaml"
    matches = list(base.rglob(pattern))

    if not matches:
        return GetDecisionResponse(
            found=False,
            error=f"Decision not found: {request.decision_id}",
        )

    # Read the first match
    yaml_file = matches[0]
    try:
        with open(yaml_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            return GetDecisionResponse(
                found=False,
                error=f"Empty decision file: {yaml_file.name}",
            )

        # Extract ID from filename
        filename = yaml_file.stem
        parts = filename.rsplit("-decision-", 1)
        if len(parts) == 2:
            data["id"] = parts[1]

        return GetDecisionResponse(found=True, decision=data)

    except yaml.YAMLError as e:
        return GetDecisionResponse(
            found=False,
            error=f"Failed to parse YAML: {e}",
        )
    except OSError as e:
        return GetDecisionResponse(
            found=False,
            error=f"Failed to read file: {e}",
        )


async def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding using the configured EmbeddingProvider."""
    try:
        provider = get_embedding_provider()
        return await provider.embed(text)
    except Exception as e:
        logger.warning("Failed to generate embedding: %s", e)
        return None


def build_embedding_text(request: RecordDecisionRequest) -> str:
    """Build text for embedding generation."""
    parts = [f"Decision: {request.decision}"]

    if request.context:
        parts.append(f"Context: {request.context}")

    parts.append(f"Category: {request.category}")

    if request.reasons:
        reasons_text = " | ".join(r.text for r in request.reasons)
        parts.append(f"Reasons: {reasons_text}")

    # F020: Include reasoning trace in embedding
    if request.trace:
        trace_text = "\n".join(
            f"Step {t.step}: {t.thought} -> {t.output or ''}"
            for t in request.trace
        )
        parts.append(f"Reasoning Trace:\n{trace_text}")

    if request.tags:
        parts.append(f"Tags: {', '.join(request.tags)}")

    # F027: Pattern improves embedding quality
    if request.pattern:
        parts.append(f"Pattern: {request.pattern}")

    # F010: Include project context in embedding text
    if request.project_context:
        pc = request.project_context
        if pc.project:
            parts.append(f"Project: {pc.project}")
        if pc.feature:
            parts.append(f"Feature: {pc.feature}")
        if pc.file:
            parts.append(f"File: {pc.file}")
        if pc.pr is not None:
            parts.append(f"PR #{pc.pr}")

    # F024: Include bridge-definition in embedding
    if request.bridge and request.bridge.has_content():
        if request.bridge.structure:
            parts.append(f"Structure: {request.bridge.structure}")
        if request.bridge.function:
            parts.append(f"Function: {request.bridge.function}")

    return "\n".join(parts)


async def index_to_chromadb(
    decision_id: str,
    embedding_text: str,
    metadata: dict[str, Any],
    embedding: list[float] | None = None,
) -> bool:
    """Index decision to the configured VectorStore. Returns success status."""
    # Generate embedding if not provided
    if embedding is None:
        embedding = await generate_embedding(embedding_text)

    if embedding is None:
        return False

    store = get_vector_store()

    # Ensure collection/store is initialized
    coll_id = await store.get_collection_id()
    if not coll_id:
        await store.initialize()

    return await store.upsert(decision_id, embedding_text, embedding, metadata)


async def record_decision(
    request: RecordDecisionRequest,
    decisions_path: str | None = None,
) -> RecordDecisionResponse:
    """Record a new decision.

    Creates YAML file and indexes to ChromaDB.

    Args:
        request: The decision to record.
        decisions_path: Override for decisions directory.

    Returns:
        Response with decision ID and status.
    """
    now = datetime.now(UTC)
    decision_id = generate_decision_id()

    # Build and write YAML
    decision_data = build_decision_yaml(request, decision_id)

    try:
        file_path = write_decision_file(decision_data, decision_id, decisions_path)
    except Exception as e:
        return RecordDecisionResponse(
            success=False,
            id=decision_id,
            path="",
            indexed=False,
            timestamp=now.isoformat(),
            error=f"Failed to write decision file: {e}",
        )

    # F050: write to the DecisionStore. This is authoritative, not best-effort:
    # listDecisions, getStats, and calibration all read from the store, so a
    # swallowed failure here would acknowledge a decision that no query can ever
    # return. The YAML file is left in place — it is the only remaining copy and
    # a later migration can still pick it up — but the call reports failure.
    store_error = await _persist_store_mutation(
        decision_id,
        "decision",
        get_decision_store().save(decision_id, decision_data),
    )
    if store_error:
        # Discard the file we just created. A retry mints a fresh decision_id, so
        # keeping this one would leave an orphan that a later migration imports as
        # a second, phantom copy of the same decision. Nothing is lost: the record
        # was never acknowledged, and the caller still holds the payload.
        try:
            Path(file_path).unlink(missing_ok=True)
            note = "The partial YAML file was removed; the call is safe to retry."
        except Exception as e:
            logger.error("Could not remove orphaned decision file %s: %s", file_path, e)
            note = f"An orphaned YAML file remains at {file_path} ({e})."
        return RecordDecisionResponse(
            success=False,
            id=decision_id,
            path="",
            indexed=False,
            timestamp=now.isoformat(),
            error=f"{store_error} {note}",
        )

    # Index to ChromaDB
    embedding_text = build_embedding_text(request)
    metadata = {
        "path": file_path,
        "title": request.decision[:500],
        "category": request.category,
        "stakes": request.stakes,
        "confidence": request.confidence,
        "status": "pending",
        "date": now.strftime("%Y-%m-%d"),
    }
    if request.agent_id:
        metadata["agent"] = request.agent_id

    # F010: Add project context to ChromaDB metadata
    if request.project_context:
        pc = request.project_context
        if pc.project:
            metadata["project"] = pc.project
        if pc.feature:
            metadata["feature"] = pc.feature
        if pc.pr is not None:
            metadata["pr"] = pc.pr
        if pc.file:
            metadata["file"] = pc.file

    # F027: Tags and pattern in metadata
    if request.tags:
        metadata["tags"] = ",".join(request.tags)
    if request.pattern:
        metadata["pattern"] = request.pattern[:500]

    # F163: reason_types and reasons_json at record time
    if request.reasons:
        rtypes = {r.type for r in request.reasons if r.type}
        if rtypes:
            metadata["reason_types"] = ",".join(sorted(rtypes))
        reason_objs = [
            {"type": r.type, "text": r.text}
            for r in request.reasons if r.type
        ]
        if reason_objs:
            metadata["reasons_json"] = json.dumps(reason_objs)[:2000]

    # F169: Bridge (structure/function) in metadata
    if request.bridge and request.bridge.has_content():
        bridge_obj: dict[str, str] = {}
        if request.bridge.structure:
            bridge_obj["structure"] = request.bridge.structure
        if request.bridge.function:
            bridge_obj["function"] = request.bridge.function
        if bridge_obj:
            metadata["bridge_json"] = json.dumps(bridge_obj)[:1000]

    indexed = await index_to_chromadb(decision_id, embedding_text, metadata)

    # F027 P3: Score recording quality
    quality = score_decision_quality(request)

    return RecordDecisionResponse(
        success=True,
        id=decision_id,
        path=file_path,
        indexed=indexed,
        timestamp=now.isoformat(),
        quality=quality,
    )


# =============================================================================
# Review Decision
# =============================================================================


@dataclass
class ReviewDecisionRequest:
    """Request to review an existing decision with outcome data."""

    id: str
    outcome: str  # success, partial, failure, abandoned
    actual_result: str | None = None
    lessons: str | None = None
    notes: str | None = None
    affected_kpis: dict[str, float] | None = None
    reviewer_id: str | None = None  # Set from auth

    @classmethod
    def from_dict(cls, data: dict[str, Any], reviewer_id: str | None = None) -> "ReviewDecisionRequest":
        """Create from dictionary (JSON-RPC params)."""
        return cls(
            id=data.get("id", ""),
            outcome=data.get("outcome", ""),
            actual_result=data.get("actualResult", data.get("actual_result")),
            lessons=data.get("lessons"),
            notes=data.get("notes"),
            affected_kpis=data.get("affectedKpis", data.get("affected_kpis")),
            reviewer_id=reviewer_id,
        )

    def validate(self) -> list[str]:
        """Validate the request. Returns list of errors."""
        errors = []

        if not self.id or not self.id.strip():
            errors.append("id: required field")

        valid_outcomes = {"success", "partial", "failure", "abandoned"}
        if self.outcome not in valid_outcomes:
            errors.append(f"outcome: must be one of {valid_outcomes}")

        return errors


@dataclass
class ReviewDecisionResponse:
    """Response from reviewing a decision."""

    success: bool
    id: str
    path: str
    status: str
    reviewed_at: str
    reindexed: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON-RPC response."""
        result: dict[str, Any] = {
            "success": self.success,
            "id": self.id,
            "path": self.path,
            "status": self.status,
            "reviewedAt": self.reviewed_at,
            "reindexed": self.reindexed,
        }
        if self.error:
            result["error"] = self.error
        return result


async def find_decision(
    decision_id: str,
    decisions_path: str | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    """Find a decision by ID.

    Searches decisions directory for matching ID.

    Args:
        decision_id: The decision ID to find (must be alphanumeric).
        decisions_path: Override for decisions directory.

    Returns:
        Tuple of (path, data) or None if not found.

    Raises:
        ValueError: If decision_id contains invalid characters.
    """
    # Validate decision_id to prevent path traversal
    if not decision_id or not decision_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"Invalid decision ID format: {decision_id}")
    base = Path(decisions_path or DECISIONS_PATH)

    if not base.exists():
        return None

    # Search pattern: decisions/YYYY/MM/*-decision-{id}.yaml
    for yaml_file in base.rglob(f"*-decision-{decision_id}.yaml"):
        try:
            with open(yaml_file, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return (yaml_file, data)
        except Exception as e:
            logger.warning("Failed to read decision file %s: %s", yaml_file, e)

    return None


async def reindex_decision(
    decision_id: str,
    data: dict[str, Any],
    file_path: str,
) -> bool:
    """Re-index a decision with updated metadata.

    Args:
        decision_id: The decision ID.
        data: The updated decision data.
        file_path: Path to the decision file.

    Returns:
        True if indexing succeeded.
    """
    # Build embedding text from decision data
    parts = [f"Decision: {data.get('summary', data.get('decision', ''))}"]

    if data.get("context"):
        parts.append(f"Context: {data['context']}")

    parts.append(f"Category: {data.get('category', 'unknown')}")

    if data.get("reasons"):
        reasons_text = " | ".join(
            r.get("text", "") for r in data["reasons"] if isinstance(r, dict)
        )
        if reasons_text:
            parts.append(f"Reasons: {reasons_text}")

    # Add outcome for reviewed decisions
    if data.get("outcome"):
        parts.append(f"Outcome: {data['outcome']}")

    if data.get("lessons"):
        parts.append(f"Lessons: {data['lessons']}")

    # F024: Include bridge-definition in reindex
    bridge_data = data.get("bridge")
    if bridge_data and isinstance(bridge_data, dict):
        if bridge_data.get("structure"):
            parts.append(f"Structure: {bridge_data['structure']}")
        if bridge_data.get("function"):
            parts.append(f"Function: {bridge_data['function']}")

    # F027: Include tags and pattern in embedding
    if data.get("tags"):
        tags = data["tags"]
        if isinstance(tags, list):
            parts.append(f"Tags: {', '.join(tags)}")
        elif isinstance(tags, str):
            parts.append(f"Tags: {tags}")
    if data.get("pattern"):
        parts.append(f"Pattern: {data['pattern']}")

    embedding_text = "\n".join(parts)

    # Build metadata — use fallback chain for title (decision > summary)
    title = str(
        data.get("title")
        or data.get("decision")
        or data.get("summary")
        or ""
    )
    metadata: dict[str, Any] = {
        "path": file_path,
        "title": title[:500] if title else "",
        "category": data.get("category", "unknown"),
        "stakes": data.get("stakes", "medium"),
        "confidence": data.get("confidence", 0.5),
        "date": data.get("date", "")[:10] if data.get("date") else "",
        "status": data.get("status", "pending"),
    }

    if data.get("outcome"):
        metadata["outcome"] = data["outcome"]

    # F163: Enrichment — lessons, actual_result, reasons in metadata
    if data.get("lessons"):
        metadata["lessons"] = str(data["lessons"])[:500]
    if data.get("actual_result"):
        metadata["actual_result"] = str(data["actual_result"])[:500]
    reasons = data.get("reasons")
    if reasons and isinstance(reasons, list):
        rtypes = {r.get("type") for r in reasons if isinstance(r, dict) and r.get("type")}
        if rtypes:
            metadata["reason_types"] = ",".join(sorted(rtypes))
        reason_objs = [
            {"type": r.get("type", ""), "text": r.get("text", "")}
            for r in reasons if isinstance(r, dict) and r.get("type")
        ]
        if reason_objs:
            metadata["reasons_json"] = json.dumps(reason_objs)[:2000]

    if data.get("recorded_by"):
        metadata["agent"] = data["recorded_by"]

    # F027: Tags and pattern in metadata
    if data.get("tags"):
        tags = data["tags"]
        if isinstance(tags, list):
            metadata["tags"] = ",".join(tags)
        elif isinstance(tags, str):
            metadata["tags"] = tags
    if data.get("pattern"):
        metadata["pattern"] = str(data["pattern"])[:500]

    # F169: Bridge (structure/function) in metadata
    bridge_data = data.get("bridge")
    if bridge_data and isinstance(bridge_data, dict):
        bridge_obj: dict[str, str] = {}
        if bridge_data.get("structure"):
            bridge_obj["structure"] = bridge_data["structure"]
        if bridge_data.get("function"):
            bridge_obj["function"] = bridge_data["function"]
        if bridge_obj:
            metadata["bridge_json"] = json.dumps(bridge_obj)[:1000]

    return await index_to_chromadb(decision_id, embedding_text, metadata)


async def update_decision(
    decision_id: str,
    updates: dict[str, Any],
    decisions_path: str | None = None,
) -> dict[str, Any]:
    """Update specific fields on an existing decision (F027 backfill).

    Finds the YAML file, merges updates, writes back, and re-indexes.

    Args:
        decision_id: The decision ID to update.
        updates: Dict of fields to update (e.g. tags, pattern).
        decisions_path: Override for decisions directory.

    Returns:
        Dict with success status and updated fields.
    """
    result = await find_decision(decision_id, decisions_path)
    if not result:
        return {"success": False, "error": f"Decision {decision_id} not found"}

    file_path, data = result

    # Merge updates
    allowed_fields = {"decision", "confidence", "tags", "pattern", "context", "reasons", "bridge"}
    applied: list[str] = []
    for key, value in updates.items():
        if key in allowed_fields:
            # Guard against empty decision text
            if key == "decision" and (not value or not str(value).strip()):
                continue
            data[key] = value
            applied.append(key)
            # Keep summary in sync with decision text
            if key == "decision":
                data["summary"] = value

    if not applied:
        return {"success": False, "error": "No valid fields to update"}

    # Write back
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        return {"success": False, "error": f"Failed to write: {e}"}

    # F050: authoritative — the store is the read path (see _persist_store_mutation).
    store_error = await _persist_store_mutation(
        decision_id,
        "field update",
        get_decision_store().update_fields(
            decision_id, **{k: updates[k] for k in applied}
        ),
    )
    if store_error:
        return {
            "success": False,
            "id": decision_id,
            "error": (
                f"{store_error} The YAML file at {file_path} was updated "
                "and can be re-imported."
            ),
        }

    # Re-index
    indexed = await reindex_decision(decision_id, data, str(file_path))

    return {
        "success": True,
        "id": decision_id,
        "updated_fields": applied,
        "indexed": indexed,
    }


async def append_thought(
    decision_id: str,
    text: str,
    decisions_path: str | None = None,
) -> dict[str, Any]:
    """Append a reasoning step to a decision's deliberation trace (F028).

    Append-only: cannot overwrite or delete existing steps.

    Args:
        decision_id: The decision to append to.
        text: Reasoning/chain-of-thought text.
        decisions_path: Override for decisions directory.

    Returns:
        Dict with success status and step number.
    """
    result = await find_decision(decision_id, decisions_path)
    if not result:
        return {"success": False, "error": f"Decision {decision_id} not found"}

    file_path, data = result

    delib = data.get("deliberation", {})
    if not delib:
        delib = {"inputs": [], "steps": []}

    steps = delib.get("steps", [])
    max_step = max((s.get("step", 0) for s in steps), default=0)
    new_step = {
        "step": max_step + 1,
        "thought": text,
        "type": "reasoning",
        "timestamp": datetime.now(UTC).isoformat(),
        "conclusion": False,
    }
    steps.append(new_step)
    delib["steps"] = steps
    data["deliberation"] = delib

    # Appending is not idempotent, so the YAML mutation has to be undone if the
    # store write fails — otherwise a client retrying after a transient failure
    # rereads the already-appended YAML and adds the same thought again at the
    # next step number, duplicating it in the trace once the store recovers.
    try:
        original_bytes = file_path.read_bytes()
    except Exception as e:
        return {"success": False, "error": f"Failed to read for rollback: {e}"}

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        return {"success": False, "error": f"Failed to write: {e}"}

    # F050: authoritative — the store is the read path (see _persist_store_mutation).
    store_error = await _persist_store_mutation(
        decision_id,
        "deliberation update",
        get_decision_store().update_fields(decision_id, deliberation=delib),
    )
    if store_error:
        rollback_note = _restore_yaml(file_path, original_bytes)
        return {
            "success": False,
            "decision_id": decision_id,
            "error": f"{store_error} {rollback_note}",
        }

    return {
        "success": True,
        "decision_id": decision_id,
        "step_number": max_step + 1,
    }


async def review_decision(
    request: ReviewDecisionRequest,
    decisions_path: str | None = None,
) -> ReviewDecisionResponse:
    """Add outcome data to an existing decision.

    Args:
        request: The review data.
        decisions_path: Override for decisions directory.

    Returns:
        Response with review status.
    """
    now = datetime.now(UTC)

    # Find decision
    result = await find_decision(request.id, decisions_path)
    if not result:
        return ReviewDecisionResponse(
            success=False,
            id=request.id,
            path="",
            status="not_found",
            reviewed_at=now.isoformat(),
            reindexed=False,
            error=f"Decision not found: {request.id}",
        )

    path, data = result

    # Update decision data
    data["status"] = "reviewed"
    data["outcome"] = request.outcome
    data["reviewed_at"] = now.isoformat()

    if request.actual_result:
        data["actual_result"] = request.actual_result

    if request.lessons:
        data["lessons"] = request.lessons

    if request.notes:
        data["review_notes"] = request.notes

    if request.affected_kpis:
        data["affected_kpis"] = request.affected_kpis

    if request.reviewer_id:
        data["reviewed_by"] = request.reviewer_id

    # Write updated YAML atomically (write to temp, then replace)
    try:
        import tempfile
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".yaml",
            dir=path.parent,
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            os.replace(temp_path, path)
        except Exception:
            # Clean up temp file on failure
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
    except Exception as e:
        return ReviewDecisionResponse(
            success=False,
            id=request.id,
            path=str(path),
            status="write_failed",
            reviewed_at=now.isoformat(),
            reindexed=False,
            error=f"Failed to write updated decision: {e}",
        )

    # F050: write the outcome to the DecisionStore. Authoritative for the same
    # reason as record_decision: calibration and listDecisions read the store, so
    # a swallowed failure here leaves an acknowledged review that never affects
    # any Brier score. SQLiteDecisionStore.update_outcome() converts database
    # errors into a False return, so the return value matters as much as the
    # exception.
    try:
        store = get_decision_store()
        updated = await store.update_outcome(
            decision_id=request.id,
            outcome=request.outcome,
            result=request.actual_result,
            lessons=request.lessons,
            notes=request.notes,
        )
    except Exception as e:
        logger.error("DecisionStore update_outcome failed for %s: %s", request.id, e)
        return ReviewDecisionResponse(
            success=False,
            id=request.id,
            path=str(path),
            status="store_write_failed",
            reviewed_at=now.isoformat(),
            reindexed=False,
            error=(
                f"Failed to persist outcome to store: {e}. "
                f"The YAML file at {path} was updated and can be re-imported."
            ),
        )

    if updated is False:
        logger.error("DecisionStore rejected outcome update for %s", request.id)
        return ReviewDecisionResponse(
            success=False,
            id=request.id,
            path=str(path),
            status="store_write_failed",
            reviewed_at=now.isoformat(),
            reindexed=False,
            error=(
                "Decision store rejected the outcome update. "
                f"The YAML file at {path} was updated and can be re-imported."
            ),
        )

    # Re-index with outcome metadata
    reindexed = await reindex_decision(request.id, data, str(path))

    return ReviewDecisionResponse(
        success=True,
        id=request.id,
        path=str(path),
        status="reviewed",
        reviewed_at=now.isoformat(),
        reindexed=reindexed,
    )
