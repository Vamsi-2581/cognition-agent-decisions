"""CSTP request/response models for JSON-RPC methods."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Upper bounds on caller-supplied free text. Recording runs regex bridge
# extraction synchronously on the (single-worker) event loop, so an unbounded
# field let one request stall every other caller. MAX_TEXT_CHARS applies to the
# decision / action description, the pattern, and each reason's text.
MAX_TEXT_CHARS = 10_000
MAX_CONTEXT_CHARS = 50_000


@dataclass(slots=True)
class QueryFilters:
    """Filters for cstp.queryDecisions."""

    category: str | None = None
    min_confidence: float = 0.0
    max_confidence: float = 1.0
    date_after: datetime | None = None
    date_before: datetime | None = None
    stakes: list[str] | None = None
    status: list[str] | None = None
    # F010: Project context filters
    project: str | None = None
    feature: str | None = None
    pr: int | None = None
    has_outcome: bool | None = None
    # F027: Tag filter
    tags: list[str] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "QueryFilters":
        """Create QueryFilters from dict."""
        if not data:
            return cls()
        return cls(
            category=data.get("category"),
            min_confidence=data.get("minConfidence", 0.0),
            max_confidence=data.get("maxConfidence", 1.0),
            date_after=_parse_datetime(data.get("dateAfter")),
            date_before=_parse_datetime(data.get("dateBefore")),
            stakes=data.get("stakes"),
            status=data.get("status"),
            # F010: Project context filters
            project=data.get("project"),
            feature=data.get("feature"),
            pr=data.get("pr"),
            has_outcome=data.get("hasOutcome"),
            # F027: Tag filter
            tags=data.get("tags"),
        )


@dataclass(slots=True)
class QueryDecisionsRequest:
    """Request for cstp.queryDecisions."""

    query: str
    filters: QueryFilters = field(default_factory=QueryFilters)
    limit: int = 10
    include_reasons: bool = False
    # F017: Hybrid retrieval
    retrieval_mode: str = "semantic"  # semantic | keyword | hybrid
    hybrid_weight: float = 0.7  # semantic weight (keyword = 1 - this)
    # F024: Bridge-side search
    bridge_side: str | None = None  # structure | function | None (both)
    # F041 P2: Compaction level annotation
    compacted: bool = False  # When true, annotate results with compaction level
    # F163 P3: Include actual_result in results
    include_detail: bool = False

    @property
    def effective_query(self) -> str:
        """Query text with bridge-side prefix for directional search.

        Prepends 'Structure: ' or 'Function: ' to bias semantic search
        toward the matching side of bridge-definitions.
        Use for semantic search only - keyword search should use raw query.
        """
        if self.bridge_side and self.query.strip():
            if self.bridge_side == "structure":
                return f"Structure: {self.query}"
            if self.bridge_side == "function":
                return f"Function: {self.query}"
        return self.query

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "QueryDecisionsRequest":
        """Create request from JSON-RPC params."""
        query = params.get("query", "")
        # Allow empty query for listing all decisions

        limit = params.get("limit", params.get("top_k", 10))
        # Higher limit allowed for empty queries (list-all mode)
        max_limit = 500 if not query.strip() else 50
        if not 1 <= limit <= max_limit:
            limit = max(1, min(max_limit, limit))

        # F017: Parse retrieval mode
        retrieval_mode = params.get("retrievalMode", params.get("retrieval_mode", "semantic"))
        if retrieval_mode not in ("semantic", "keyword", "hybrid"):
            retrieval_mode = "semantic"

        hybrid_weight = float(params.get("hybridWeight", params.get("hybrid_weight", 0.7)))
        hybrid_weight = max(0.0, min(1.0, hybrid_weight))

        # F024: Parse bridge_side
        bridge_side = params.get("bridgeSide", params.get("bridge_side"))
        if bridge_side and bridge_side not in ("structure", "function"):
            bridge_side = None

        return cls(
            query=query,
            filters=QueryFilters.from_dict(params.get("filters")),
            limit=limit,
            include_reasons=params.get("includeReasons", False),
            retrieval_mode=retrieval_mode,
            hybrid_weight=hybrid_weight,
            bridge_side=bridge_side,
            compacted=bool(params.get("compacted", False)),
            include_detail=bool(
                params.get("includeDetail", params.get("include_detail", False))
            ),
        )


@dataclass(slots=True)
class DecisionSummary:
    """Summary of a decision in query results."""

    id: str
    title: str
    category: str
    confidence: float | None
    stakes: str | None
    status: str
    outcome: str | None
    date: str
    distance: float
    reasons: list[dict[str, Any]] | None = None
    # F027: Tags and pattern in results
    tags: list[str] | None = None
    pattern: str | None = None
    # F041 P2: Compaction level annotation
    compaction_level: str | None = None
    # F163: Enrichment fields
    lessons: str | None = None
    actual_result: str | None = None
    # F169: Bridge (structure/function) in search results
    bridge: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON response."""
        result: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "confidence": self.confidence,
            "stakes": self.stakes,
            "status": self.status,
            "date": self.date,
            "distance": self.distance,
        }
        if self.outcome:
            result["outcome"] = self.outcome
        if self.reasons:
            result["reasons"] = self.reasons
        if self.tags:
            result["tags"] = self.tags
        if self.pattern:
            result["pattern"] = self.pattern
        if self.compaction_level:
            result["compactionLevel"] = self.compaction_level
        if self.lessons:
            result["lessons"] = self.lessons
        if self.actual_result:
            result["actualResult"] = self.actual_result
        if self.bridge:
            result["bridge"] = self.bridge
        return result


@dataclass(slots=True)
class QueryDecisionsResponse:
    """Response for cstp.queryDecisions."""

    decisions: list[DecisionSummary]
    total: int
    query: str
    query_time_ms: int
    agent: str
    # F017: Hybrid retrieval metadata
    retrieval_mode: str = "semantic"
    scores: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON response."""
        result = {
            "decisions": [d.to_dict() for d in self.decisions],
            "total": self.total,
            "query": self.query,
            "queryTimeMs": self.query_time_ms,
            "agent": self.agent,
            "retrievalMode": self.retrieval_mode,
        }
        if self.scores:
            result["scores"] = self.scores
        return result


# ============================================================================
# F003: checkGuardrails models
# ============================================================================


@dataclass(slots=True)
class ActionContext:
    """Action context for guardrail evaluation."""

    description: str
    category: str | None = None
    stakes: str = "medium"
    confidence: float | None = None
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionContext":
        """Create ActionContext from dict.

        Known fields (description, category, stakes, confidence) are extracted.
        An explicit 'context' dict is merged in.
        Any unknown top-level fields are also passed into context automatically,
        so callers can write architecture_review=true at the top level.
        """
        description = data.get("description", "")
        if not description:
            raise ValueError("Missing required field: action.description")
        if len(description) > MAX_TEXT_CHARS:
            raise ValueError(
                f"action.description: must be at most {MAX_TEXT_CHARS} characters"
            )

        known_fields = {"description", "category", "stakes", "confidence", "context"}
        extra = {k: v for k, v in data.items() if k not in known_fields}
        context = data.get("context", {})
        context.update(extra)

        return cls(
            description=description,
            category=data.get("category"),
            stakes=data.get("stakes", "medium"),
            confidence=data.get("confidence"),
            context=context,
        )


@dataclass(slots=True)
class AgentInfo:
    """Information about the requesting agent."""

    id: str | None = None
    url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AgentInfo":
        """Create AgentInfo from dict."""
        if not data:
            return cls()
        return cls(
            id=data.get("id"),
            url=data.get("url"),
        )


@dataclass(slots=True)
class CheckGuardrailsRequest:
    """Request for cstp.checkGuardrails."""

    action: ActionContext
    agent: AgentInfo = field(default_factory=AgentInfo)

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "CheckGuardrailsRequest":
        """Create request from JSON-RPC params."""
        action_data = params.get("action", {})
        if not action_data:
            raise ValueError("Missing required parameter: action")

        return cls(
            action=ActionContext.from_dict(action_data),
            agent=AgentInfo.from_dict(params.get("agent")),
        )


@dataclass(slots=True)
class GuardrailViolation:
    """A guardrail violation or warning."""

    guardrail_id: str
    name: str
    message: str
    severity: str = "block"
    suggestion: str | None = None
    # F030: Circuit breaker fields (optional)
    type: str | None = None  # "static" or "circuit_breaker"
    state: str | None = None  # "open", "half_open"
    failure_rate: float | None = None
    reset_at: str | None = None  # ISO datetime

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result: dict[str, Any] = {
            "guardrailId": self.guardrail_id,
            "name": self.name,
            "message": self.message,
            "severity": self.severity,
        }
        if self.suggestion:
            result["suggestion"] = self.suggestion
        # F030: Include circuit breaker fields when present
        if self.type:
            result["type"] = self.type
        if self.state:
            result["state"] = self.state
        if self.failure_rate is not None:
            result["failureRate"] = self.failure_rate
        if self.reset_at:
            result["resetAt"] = self.reset_at
        return result


@dataclass(slots=True)
class CheckGuardrailsResponse:
    """Response for cstp.checkGuardrails."""

    allowed: bool
    violations: list[GuardrailViolation]
    warnings: list[GuardrailViolation]
    evaluated: int
    evaluated_at: datetime
    agent: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON response."""
        return {
            "allowed": self.allowed,
            "violations": [v.to_dict() for v in self.violations],
            "warnings": [w.to_dict() for w in self.warnings],
            "evaluated": self.evaluated,
            "evaluatedAt": self.evaluated_at.isoformat(),
            "agent": self.agent,
        }


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse ISO datetime string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ============================================================================
# F046: preAction models
# ============================================================================


@dataclass(slots=True)
class PreActionOptions:
    """Options for cstp.preAction."""

    query_limit: int = 5
    auto_record: bool = True
    include_patterns: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PreActionOptions":
        """Create from dict with camelCase support."""
        if not data:
            return cls()
        return cls(
            query_limit=data.get("queryLimit", data.get("query_limit", 5)),
            auto_record=data.get("autoRecord", data.get("auto_record", True)),
            include_patterns=data.get(
                "includePatterns", data.get("include_patterns", True)
            ),
        )


@dataclass(slots=True)
class PreActionRequest:
    """Request for cstp.preAction."""

    action: ActionContext
    options: PreActionOptions = field(default_factory=PreActionOptions)
    reasons: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    pattern: str | None = None

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "PreActionRequest":
        """Create from JSON-RPC params."""
        action_data = params.get("action", {})
        if not action_data:
            raise ValueError("Missing required parameter: action")
        return cls(
            action=ActionContext.from_dict(action_data),
            options=PreActionOptions.from_dict(params.get("options")),
            reasons=params.get("reasons", []),
            tags=params.get("tags", []),
            pattern=params.get("pattern"),
        )


@dataclass(slots=True)
class CalibrationContext:
    """Calibration context for a category in pre-action response."""

    brier_score: float | None = None
    accuracy: float | None = None
    calibration_gap: float | None = None
    interpretation: str | None = None
    reviewed_decisions: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result: dict[str, Any] = {
            "reviewedDecisions": self.reviewed_decisions,
        }
        if self.brier_score is not None:
            result["brierScore"] = self.brier_score
        if self.accuracy is not None:
            result["accuracy"] = self.accuracy
        if self.calibration_gap is not None:
            result["calibrationGap"] = self.calibration_gap
        if self.interpretation:
            result["interpretation"] = self.interpretation
        return result


@dataclass(slots=True)
class PatternSummary:
    """A confirmed pattern from similar decisions."""

    pattern: str
    count: int
    example_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return {
            "pattern": self.pattern,
            "count": self.count,
            "exampleIds": self.example_ids,
        }


@dataclass(slots=True)
class PreActionResponse:
    """Response for cstp.preAction."""

    allowed: bool
    decision_id: str | None
    relevant_decisions: list[DecisionSummary]
    guardrail_results: list[GuardrailViolation]
    calibration_context: CalibrationContext
    patterns_summary: list[PatternSummary]
    block_reasons: list[str] = field(default_factory=list)
    query_time_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON response."""
        result: dict[str, Any] = {
            "allowed": self.allowed,
            "decisionId": self.decision_id,
            "relevantDecisions": [d.to_dict() for d in self.relevant_decisions],
            "guardrailResults": [g.to_dict() for g in self.guardrail_results],
            "calibrationContext": self.calibration_context.to_dict(),
            "patternsSummary": [p.to_dict() for p in self.patterns_summary],
            "queryTimeMs": self.query_time_ms,
        }
        if self.block_reasons:
            result["blockReasons"] = self.block_reasons
        return result


# ============================================================================
# F047: getSessionContext models
# ============================================================================


@dataclass(slots=True)
class SessionContextRequest:
    """Request for cstp.getSessionContext."""

    task_description: str | None = None
    include: list[str] = field(default_factory=lambda: [
        "decisions", "guardrails", "calibration", "ready", "patterns",
    ])
    decisions_limit: int = 10
    ready_limit: int = 5
    format: str = "json"

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "SessionContextRequest":
        """Create from JSON-RPC params."""
        include = params.get("include", [
            "decisions", "guardrails", "calibration", "ready", "patterns",
        ])
        decisions_limit = params.get(
            "decisionsLimit", params.get("decisions_limit", 10)
        )
        decisions_limit = max(1, min(50, decisions_limit))
        ready_limit = params.get("readyLimit", params.get("ready_limit", 5))
        ready_limit = max(1, min(20, ready_limit))
        fmt = params.get("format", "json")
        if fmt not in ("json", "markdown"):
            fmt = "json"
        return cls(
            task_description=params.get(
                "taskDescription", params.get("task_description")
            ),
            include=include,
            decisions_limit=decisions_limit,
            ready_limit=ready_limit,
            format=fmt,
        )


@dataclass(slots=True)
class AgentProfile:
    """Agent cognitive profile for session context."""

    total_decisions: int = 0
    reviewed: int = 0
    overall_accuracy: float | None = None
    brier_score: float | None = None
    tendency: str | None = None
    strongest_category: str | None = None
    weakest_category: str | None = None
    active_since: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result: dict[str, Any] = {
            "totalDecisions": self.total_decisions,
            "reviewed": self.reviewed,
        }
        if self.overall_accuracy is not None:
            result["overallAccuracy"] = self.overall_accuracy
        if self.brier_score is not None:
            result["brierScore"] = self.brier_score
        if self.tendency:
            result["tendency"] = self.tendency
        if self.strongest_category:
            result["strongestCategory"] = self.strongest_category
        if self.weakest_category:
            result["weakestCategory"] = self.weakest_category
        if self.active_since:
            result["activeSince"] = self.active_since
        return result


@dataclass(slots=True)
class ReadyQueueItem:
    """An item in the ready queue needing attention."""

    id: str
    title: str
    reason: str
    date: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return {
            "id": self.id,
            "title": self.title,
            "reason": self.reason,
            "date": self.date,
            "detail": self.detail,
        }


# ============================================================================
# F044: Ready (Agent Work Discovery) models
# ============================================================================


@dataclass(slots=True)
class ReadyRequest:
    """Request for cstp.ready endpoint (F044)."""

    min_priority: str = "low"  # low, medium, high
    action_types: list[str] = field(default_factory=list)  # empty = all types
    limit: int = 20
    category: str | None = None

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "ReadyRequest":
        """Create from JSON-RPC params (camelCase support)."""
        min_priority = str(params.get("minPriority", params.get("min_priority", "low")))
        if min_priority not in ("low", "medium", "high"):
            min_priority = "low"

        action_types = params.get("actionTypes", params.get("action_types", []))
        if not isinstance(action_types, list):
            action_types = []

        limit = int(params.get("limit", 20))
        limit = max(1, min(limit, 50))

        return cls(
            min_priority=min_priority,
            action_types=action_types,
            limit=limit,
            category=params.get("category"),
        )


@dataclass(slots=True)
class ReadyAction:
    """A prioritized cognitive action from cstp.ready (F044)."""

    type: str  # review_outcome, calibration_drift, stale_pending
    priority: str  # low, medium, high
    reason: str
    suggestion: str
    decision_id: str | None = None
    category: str | None = None
    date: str | None = None
    title: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        result: dict[str, Any] = {
            "type": self.type,
            "priority": self.priority,
            "reason": self.reason,
            "suggestion": self.suggestion,
        }
        if self.decision_id:
            result["decisionId"] = self.decision_id
        if self.category:
            result["category"] = self.category
        if self.date:
            result["date"] = self.date
        if self.title:
            result["title"] = self.title
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass(slots=True)
class ReadyResponse:
    """Response from cstp.ready endpoint (F044)."""

    actions: list[ReadyAction]
    total: int
    filtered: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result: dict[str, Any] = {
            "actions": [a.to_dict() for a in self.actions],
            "total": self.total,
            "filtered": self.filtered,
        }
        if self.warnings:
            result["warnings"] = self.warnings
        return result


@dataclass(slots=True)
class ConfirmedPattern:
    """A pattern confirmed across 2+ decisions."""

    pattern: str
    count: int
    categories: list[str] = field(default_factory=list)
    example_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return {
            "pattern": self.pattern,
            "count": self.count,
            "categories": self.categories,
            "exampleIds": self.example_ids,
        }


@dataclass(slots=True)
class SessionContextResponse:
    """Response for cstp.getSessionContext."""

    agent_profile: AgentProfile
    relevant_decisions: list[DecisionSummary] = field(default_factory=list)
    active_guardrails: list[dict[str, Any]] = field(default_factory=list)
    calibration_by_category: dict[str, Any] = field(default_factory=dict)
    ready_queue: list[ReadyQueueItem] = field(default_factory=list)
    confirmed_patterns: list[ConfirmedPattern] = field(default_factory=list)
    wisdom_entries: list["WisdomEntry"] = field(default_factory=list)
    circuit_breakers: list[dict[str, Any]] = field(default_factory=list)
    query_time_ms: int = 0
    markdown: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON response."""
        if self.markdown is not None:
            return {"markdown": self.markdown, "queryTimeMs": self.query_time_ms}
        result: dict[str, Any] = {
            "agentProfile": self.agent_profile.to_dict(),
            "relevantDecisions": [d.to_dict() for d in self.relevant_decisions],
            "activeGuardrails": self.active_guardrails,
            "calibrationByCategory": self.calibration_by_category,
            "readyQueue": [r.to_dict() for r in self.ready_queue],
            "confirmedPatterns": [p.to_dict() for p in self.confirmed_patterns],
            "queryTimeMs": self.query_time_ms,
        }
        if self.wisdom_entries:
            result["wisdom"] = [w.to_dict() for w in self.wisdom_entries]
        if self.circuit_breakers:
            result["circuitBreakers"] = self.circuit_breakers
        return result


# ---------------------------------------------------------------------------
# F045: Decision Graph Storage Layer
# ---------------------------------------------------------------------------

# Valid edge types for P1
_GRAPH_EDGE_TYPES = frozenset({"relates_to", "supersedes", "depends_on"})


@dataclass(slots=True)
class LinkDecisionsRequest:
    """Request for cstp.linkDecisions (F045 P1)."""

    source_id: str
    target_id: str
    edge_type: str
    weight: float = 1.0
    context: str | None = None

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "LinkDecisionsRequest":
        """Create from JSON-RPC params (camelCase support)."""
        source_id = str(params.get("sourceId") or params.get("source_id", ""))
        target_id = str(params.get("targetId") or params.get("target_id", ""))
        edge_type = str(params.get("edgeType") or params.get("edge_type", ""))
        weight = float(params.get("weight", 1.0))
        context = params.get("context")

        return cls(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            weight=weight,
            context=context,
        )

    def validate(self) -> list[str]:
        """Validate request fields. Returns list of error messages."""
        errors: list[str] = []
        if not self.source_id:
            errors.append("sourceId is required")
        if not self.target_id:
            errors.append("targetId is required")
        if self.source_id and self.target_id and self.source_id == self.target_id:
            errors.append("sourceId and targetId must be different (no self-loops)")
        if not self.edge_type:
            errors.append("edgeType is required")
        elif self.edge_type not in _GRAPH_EDGE_TYPES:
            errors.append(
                f"edgeType must be one of: {', '.join(sorted(_GRAPH_EDGE_TYPES))}"
            )
        if self.weight <= 0:
            errors.append("weight must be positive")
        return errors


@dataclass(slots=True)
class GetGraphRequest:
    """Request for cstp.getGraph (F045 P1)."""

    node_id: str
    depth: int = 1
    edge_types: list[str] | None = None
    direction: str = "both"

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "GetGraphRequest":
        """Create from JSON-RPC params (camelCase support)."""
        node_id = str(params.get("nodeId") or params.get("node_id", ""))
        depth = int(params.get("depth", 1))

        edge_types_raw = params.get("edgeTypes") or params.get("edge_types")
        edge_types: list[str] | None = None
        if isinstance(edge_types_raw, list):
            edge_types = [str(t) for t in edge_types_raw]

        direction = str(params.get("direction", "both"))

        return cls(
            node_id=node_id,
            depth=max(1, min(depth, 5)),
            edge_types=edge_types,
            direction=direction,
        )

    def validate(self) -> list[str]:
        """Validate request fields. Returns list of error messages."""
        errors: list[str] = []
        if not self.node_id:
            errors.append("nodeId is required")
        if self.direction not in ("outgoing", "incoming", "both"):
            errors.append("direction must be: outgoing, incoming, or both")
        if self.edge_types:
            invalid = [t for t in self.edge_types if t not in _GRAPH_EDGE_TYPES]
            if invalid:
                errors.append(
                    f"Invalid edgeTypes: {invalid}. "
                    f"Must be one of: {', '.join(sorted(_GRAPH_EDGE_TYPES))}"
                )
        return errors


@dataclass(slots=True)
class GetNeighborsRequest:
    """Request for cstp.getNeighbors (F045 follow-up)."""

    node_id: str
    direction: str = "both"
    edge_type: str | None = None
    limit: int = 20

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "GetNeighborsRequest":
        """Create from JSON-RPC params (camelCase support)."""
        node_id = str(params.get("nodeId") or params.get("node_id", ""))
        direction = str(params.get("direction", "both"))
        edge_type_raw = params.get("edgeType") or params.get("edge_type")
        edge_type = str(edge_type_raw) if edge_type_raw else None
        limit = int(params.get("limit", 20))

        return cls(
            node_id=node_id,
            direction=direction,
            edge_type=edge_type,
            limit=max(1, min(limit, 100)),
        )

    def validate(self) -> list[str]:
        """Validate request fields. Returns list of error messages."""
        errors: list[str] = []
        if not self.node_id:
            errors.append("nodeId is required")
        if self.direction not in ("outgoing", "incoming", "both"):
            errors.append("direction must be: outgoing, incoming, or both")
        if self.edge_type and self.edge_type not in _GRAPH_EDGE_TYPES:
            errors.append(
                f"edgeType must be one of: {', '.join(sorted(_GRAPH_EDGE_TYPES))}"
            )
        return errors


# ---------------------------------------------------------------------------
# F041: Memory Compaction
# ---------------------------------------------------------------------------

# Valid compaction levels (ordered by detail, most → least)
COMPACTION_LEVELS = ("full", "summary", "digest", "wisdom")

# Age thresholds in days for each compaction level
COMPACTION_THRESHOLDS: dict[str, int | None] = {
    "full": 7,       # < 7 days
    "summary": 30,   # 7-30 days
    "digest": 90,    # 30-90 days
    "wisdom": None,  # 90+ days (no upper bound)
}


@dataclass(slots=True)
class CompactRequest:
    """Request for cstp.compact (F041 P1)."""

    category: str | None = None
    dry_run: bool = False

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "CompactRequest":
        """Create from JSON-RPC params (camelCase support)."""
        return cls(
            category=params.get("category"),
            dry_run=bool(params.get("dryRun", params.get("dry_run", False))),
        )


@dataclass(slots=True)
class CompactLevelCount:
    """Count of decisions at each compaction level."""

    full: int = 0
    summary: int = 0
    digest: int = 0
    wisdom: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return {
            "full": self.full,
            "summary": self.summary,
            "digest": self.digest,
            "wisdom": self.wisdom,
        }


@dataclass(slots=True)
class CompactResponse:
    """Response from cstp.compact (F041 P1)."""

    compacted: int
    preserved: int
    levels: CompactLevelCount
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        result: dict[str, Any] = {
            "compacted": self.compacted,
            "preserved": self.preserved,
            "levels": self.levels.to_dict(),
            "dryRun": self.dry_run,
        }
        if self.errors:
            result["errors"] = self.errors
        return result


@dataclass(slots=True)
class GetCompactedRequest:
    """Request for cstp.getCompacted (F041 P1)."""

    category: str | None = None
    level: str | None = None  # Force a specific level; None = auto by age
    limit: int = 50
    include_preserved: bool = True

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "GetCompactedRequest":
        """Create from JSON-RPC params (camelCase support)."""
        level = params.get("level")
        if level and level not in COMPACTION_LEVELS:
            level = None

        limit = int(params.get("limit", 50))
        limit = max(1, min(limit, 500))

        return cls(
            category=params.get("category"),
            level=level,
            limit=limit,
            include_preserved=bool(
                params.get(
                    "includePreserved", params.get("include_preserved", True)
                )
            ),
        )


@dataclass(slots=True)
class CompactedDecision:
    """A decision shaped at a specific compaction level (F041 P1).

    Shape varies by level:
    - full: All fields populated (complete decision)
    - summary: decision, outcome, pattern, confidence, actual_confidence
    - digest: one_line summary only
    - wisdom: Not used here (wisdom is aggregated via WisdomEntry)
    """

    id: str
    level: str  # full, summary, digest
    decision: str
    category: str
    date: str
    preserved: bool = False
    # summary-level fields
    outcome: str | None = None
    confidence: float | None = None
    actual_confidence: float | None = None
    pattern: str | None = None
    stakes: str | None = None
    # full-level fields
    context: str | None = None
    reasons: list[dict[str, Any]] | None = None
    tags: list[str] | None = None
    bridge: dict[str, Any] | None = None
    deliberation: dict[str, Any] | None = None
    # digest-level: one-line summary
    one_line: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys, shaped by level."""
        result: dict[str, Any] = {
            "id": self.id,
            "level": self.level,
            "decision": self.decision,
            "category": self.category,
            "date": self.date,
        }
        if self.preserved:
            result["preserved"] = True

        if self.level == "digest":
            if self.one_line:
                result["oneLine"] = self.one_line
            return result

        # summary and full both include these
        if self.outcome:
            result["outcome"] = self.outcome
        if self.confidence is not None:
            result["confidence"] = self.confidence
        if self.actual_confidence is not None:
            result["actualConfidence"] = self.actual_confidence
        if self.pattern:
            result["pattern"] = self.pattern
        if self.stakes:
            result["stakes"] = self.stakes

        # full only
        if self.level == "full":
            if self.context:
                result["context"] = self.context
            if self.reasons:
                result["reasons"] = self.reasons
            if self.tags:
                result["tags"] = self.tags
            if self.bridge:
                result["bridge"] = self.bridge
            if self.deliberation:
                result["deliberation"] = self.deliberation

        return result


@dataclass(slots=True)
class GetCompactedResponse:
    """Response from cstp.getCompacted (F041 P1)."""

    decisions: list[CompactedDecision]
    total: int
    levels: CompactLevelCount

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return {
            "decisions": [d.to_dict() for d in self.decisions],
            "total": self.total,
            "levels": self.levels.to_dict(),
        }


@dataclass(slots=True)
class SetPreserveRequest:
    """Request for cstp.setPreserve (F041 P1)."""

    decision_id: str
    preserve: bool = True

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "SetPreserveRequest":
        """Create from JSON-RPC params (camelCase support)."""
        decision_id = str(
            params.get("decisionId")
            or params.get("decision_id")
            or params.get("id")
            or ""
        )
        return cls(
            decision_id=decision_id,
            preserve=bool(params.get("preserve", True)),
        )

    def validate(self) -> list[str]:
        """Validate request fields. Returns list of error messages."""
        errors: list[str] = []
        if not self.decision_id:
            errors.append("decisionId is required")
        return errors


@dataclass(slots=True)
class SetPreserveResponse:
    """Response from cstp.setPreserve (F041 P1)."""

    success: bool
    decision_id: str
    preserve: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        result: dict[str, Any] = {
            "success": self.success,
            "decisionId": self.decision_id,
            "preserve": self.preserve,
        }
        if self.error:
            result["error"] = self.error
        return result


@dataclass(slots=True)
class WisdomPrinciple:
    """A distilled principle from a category of decisions."""

    text: str
    confirmations: int
    example_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        return {
            "text": self.text,
            "confirmations": self.confirmations,
            "exampleIds": self.example_ids,
        }


@dataclass(slots=True)
class WisdomEntry:
    """Category-level wisdom aggregate (F041 P1)."""

    category: str
    decisions: int
    success_rate: float | None = None
    key_principles: list[WisdomPrinciple] = field(default_factory=list)
    common_failure_mode: str | None = None
    avg_confidence: float | None = None
    brier_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        result: dict[str, Any] = {
            "category": self.category,
            "decisions": self.decisions,
        }
        if self.success_rate is not None:
            result["successRate"] = self.success_rate
        if self.key_principles:
            result["keyPrinciples"] = [p.to_dict() for p in self.key_principles]
        if self.common_failure_mode:
            result["commonFailureMode"] = self.common_failure_mode
        if self.avg_confidence is not None:
            result["avgConfidence"] = self.avg_confidence
        if self.brier_score is not None:
            result["brierScore"] = self.brier_score
        return result


@dataclass(slots=True)
class GetWisdomRequest:
    """Request for cstp.getWisdom (F041 P1)."""

    category: str | None = None
    min_decisions: int = 5

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "GetWisdomRequest":
        """Create from JSON-RPC params (camelCase support)."""
        min_decisions = int(
            params.get("minDecisions", params.get("min_decisions", 5))
        )
        min_decisions = max(1, min(min_decisions, 100))
        return cls(
            category=params.get("category"),
            min_decisions=min_decisions,
        )


@dataclass(slots=True)
class GetWisdomResponse:
    """Response from cstp.getWisdom (F041 P1)."""

    wisdom: list[WisdomEntry]
    total_decisions: int
    categories_analyzed: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        return {
            "wisdom": [w.to_dict() for w in self.wisdom],
            "totalDecisions": self.total_decisions,
            "categoriesAnalyzed": self.categories_analyzed,
        }


# ============================================================================
# F129: RecordThought params (agent_id + decision_id for multi-agent isolation)
# ============================================================================


@dataclass(slots=True)
class RecordThoughtParams:
    """Parsed params for cstp.recordThought (F129).

    Supports optional agent_id and decision_id for composite tracker keys,
    enabling multi-agent deliberation isolation.
    """

    text: str
    decision_id: str | None = None
    agent_id: str | None = None

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "RecordThoughtParams":
        """Create from JSON-RPC params (camelCase support)."""
        text = params.get("text", "")
        if not text:
            raise ValueError("Missing required parameter: text")

        return cls(
            text=text,
            decision_id=params.get("decisionId") or params.get("decision_id"),
            agent_id=params.get("agentId") or params.get("agent_id"),
        )


# ============================================================================
# F126: Debug Tracker models
# ============================================================================


@dataclass(slots=True)
class DebugTrackerRequest:
    """Request for cstp.debugTracker (F126)."""

    key: str | None = None
    include_consumed: bool = False

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "DebugTrackerRequest":
        """Create from JSON-RPC params."""
        return cls(
            key=params.get("key"),
            include_consumed=bool(params.get("includeConsumed", False)),
        )


@dataclass(slots=True)
class TrackerInputDetail:
    """Detail of a single tracked input in a debug session."""

    id: str
    type: str
    text: str
    source: str
    age_seconds: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "source": self.source,
            "ageSeconds": self.age_seconds,
        }

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "TrackerInputDetail":
        """Create from raw dict (as returned by debug_sessions)."""
        return cls(
            id=raw["id"],
            type=raw["type"],
            text=raw["text"],
            source=raw["source"],
            age_seconds=raw["ageSeconds"],
        )


@dataclass(slots=True)
class TrackerSessionDetail:
    """Detail of a single tracker session."""

    key: str
    input_count: int
    inputs: list[TrackerInputDetail] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        return {
            "key": self.key,
            "inputCount": self.input_count,
            "inputs": [i.to_dict() for i in self.inputs],
        }

    @classmethod
    def from_raw(cls, key: str, raw: dict[str, Any]) -> "TrackerSessionDetail":
        """Create from raw dict (as returned by debug_sessions)."""
        return cls(
            key=key,
            input_count=raw["inputCount"],
            inputs=[TrackerInputDetail.from_raw(i) for i in raw["inputs"]],
        )


@dataclass(slots=True)
class ConsumedSessionDetail:
    """A consumed/expired tracker session in debug output."""

    key: str
    consumed_at_seconds: int  # age in seconds
    input_count: int
    agent_id: str | None
    decision_id: str | None
    status: str  # "consumed" | "expired"
    inputs_summary: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        return {
            "key": self.key,
            "consumedAt": self.consumed_at_seconds,
            "inputCount": self.input_count,
            "agentId": self.agent_id,
            "decisionId": self.decision_id,
            "status": self.status,
            "inputsSummary": self.inputs_summary,
        }

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "ConsumedSessionDetail":
        """Create from raw dict."""
        return cls(
            key=raw["key"],
            consumed_at_seconds=raw["consumedAt"],
            input_count=raw["inputCount"],
            agent_id=raw.get("agentId"),
            decision_id=raw.get("decisionId"),
            status=raw.get("status", "consumed"),
            inputs_summary=raw.get("inputsSummary", []),
        )


@dataclass(slots=True)
class DebugTrackerResponse:
    """Response from cstp.debugTracker (F126)."""

    sessions: list[str]
    session_count: int
    detail: dict[str, TrackerSessionDetail] = field(default_factory=dict)
    consumed: list[ConsumedSessionDetail] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        result: dict[str, Any] = {
            "sessions": self.sessions,
            "sessionCount": self.session_count,
            "detail": {k: v.to_dict() for k, v in self.detail.items()},
        }
        if self.consumed:
            result["consumed"] = [c.to_dict() for c in self.consumed]
        return result

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "DebugTrackerResponse":
        """Create from raw dict (as returned by debug_sessions)."""
        detail = {
            k: TrackerSessionDetail.from_raw(k, v)
            for k, v in raw.get("detail", {}).items()
        }
        consumed = [
            ConsumedSessionDetail.from_raw(c)
            for c in raw.get("consumed", [])
        ]
        return cls(
            sessions=raw["sessions"],
            session_count=raw["sessionCount"],
            detail=detail,
            consumed=consumed,
        )


# ============================================================================
# F050: listDecisions / getStats models
# ============================================================================


@dataclass(slots=True)
class ListDecisionsRequest:
    """Request for cstp.listDecisions (F050)."""

    limit: int = 20
    offset: int = 0
    category: str | None = None
    stakes: str | None = None
    status: str | None = None
    agent: str | None = None
    tags: list[str] = field(default_factory=list)
    project: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    search: str | None = None
    sort: str = "created_at"
    order: str = "desc"

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "ListDecisionsRequest":
        """Create from JSON-RPC params (camelCase support)."""
        limit = int(params.get("limit", 20))
        limit = max(1, min(limit, 500))
        offset = int(params.get("offset", 0))
        offset = max(0, offset)

        sort = str(params.get("sort", "created_at"))
        allowed_sorts = {
            "created_at", "confidence", "category", "stakes", "status",
        }
        if sort not in allowed_sorts:
            sort = "created_at"

        order = str(params.get("order", "desc")).lower()
        if order not in ("asc", "desc"):
            order = "desc"

        tags_raw = params.get("tags", [])
        tags = list(tags_raw) if isinstance(tags_raw, list) else []

        return cls(
            limit=limit,
            offset=offset,
            category=params.get("category"),
            stakes=params.get("stakes"),
            status=params.get("status"),
            agent=params.get("agent"),
            tags=tags,
            project=params.get("project"),
            date_from=params.get("dateFrom") or params.get("date_from"),
            date_to=params.get("dateTo") or params.get("date_to"),
            search=params.get("search"),
            sort=sort,
            order=order,
        )


@dataclass(slots=True)
class ListDecisionsResponse:
    """Response from cstp.listDecisions (F050)."""

    decisions: list[dict[str, Any]]
    total: int
    limit: int
    offset: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        return {
            "decisions": self.decisions,
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
        }


@dataclass(slots=True)
class GetStatsRequest:
    """Request for cstp.getStats (F050)."""

    date_from: str | None = None
    date_to: str | None = None
    project: str | None = None

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "GetStatsRequest":
        """Create from JSON-RPC params (camelCase support)."""
        return cls(
            date_from=params.get("dateFrom") or params.get("date_from"),
            date_to=params.get("dateTo") or params.get("date_to"),
            project=params.get("project"),
        )


@dataclass(slots=True)
class GetStatsResponse:
    """Response from cstp.getStats (F050)."""

    total: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_stakes: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    by_agent: dict[str, int] = field(default_factory=dict)
    by_day: list[dict[str, Any]] = field(default_factory=list)
    top_tags: list[dict[str, Any]] = field(default_factory=list)
    recent_activity: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        return {
            "total": self.total,
            "byCategory": self.by_category,
            "byStakes": self.by_stakes,
            "byStatus": self.by_status,
            "byAgent": self.by_agent,
            "byDay": self.by_day,
            "topTags": self.top_tags,
            "recentActivity": self.recent_activity,
        }


# ============================================================================
# F030: Circuit Breaker models
# ============================================================================


@dataclass(slots=True)
class GetCircuitStateRequest:
    """Request for cstp.getCircuitState (F030)."""

    scope: str

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "GetCircuitStateRequest":
        """Create from JSON-RPC params."""
        scope = params.get("scope", "")
        if not scope:
            raise ValueError("Missing required parameter: scope")
        return cls(scope=scope)


@dataclass(slots=True)
class GetCircuitStateResponse:
    """Response for cstp.getCircuitState (F030)."""

    scope: str
    state: str
    failure_count: int
    failure_threshold: int
    window_ms: int
    cooldown_ms: int
    last_failure: str | None = None
    opened_at: str | None = None
    reset_at: str | None = None
    cooldown_remaining_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        result: dict[str, Any] = {
            "scope": self.scope,
            "state": self.state,
            "failureCount": self.failure_count,
            "failureThreshold": self.failure_threshold,
            "windowMs": self.window_ms,
            "cooldownMs": self.cooldown_ms,
        }
        if self.last_failure:
            result["lastFailure"] = self.last_failure
        if self.opened_at:
            result["openedAt"] = self.opened_at
        if self.reset_at:
            result["resetAt"] = self.reset_at
        if self.cooldown_remaining_ms is not None:
            result["cooldownRemainingMs"] = self.cooldown_remaining_ms
        return result


@dataclass(slots=True)
class ResetCircuitRequest:
    """Request for cstp.resetCircuit (F030)."""

    scope: str
    probe_first: bool = False

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "ResetCircuitRequest":
        """Create from JSON-RPC params (camelCase support)."""
        scope = params.get("scope", "")
        if not scope:
            raise ValueError("Missing required parameter: scope")
        probe_first = bool(
            params.get("probeFirst", params.get("probe_first", False))
        )
        return cls(scope=scope, probe_first=probe_first)


@dataclass(slots=True)
class ResetCircuitResponse:
    """Response for cstp.resetCircuit (F030)."""

    scope: str
    previous_state: str
    new_state: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with camelCase keys."""
        return {
            "scope": self.scope,
            "previousState": self.previous_state,
            "newState": self.new_state,
            "message": self.message,
        }
