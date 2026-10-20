"""Pydantic input schemas for MCP tool definitions.

These models define the JSON Schema that MCP clients see when discovering tools.
They map to the existing CSTP dataclass models but use Pydantic for automatic
schema generation required by the MCP protocol.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from .cstp.models import MAX_CONTEXT_CHARS, MAX_TEXT_CHARS


class QueryFiltersInput(BaseModel):
    """Optional filters for narrowing decision queries."""

    category: str | None = Field(
        default=None,
        description="Filter by category: architecture, process, integration, tooling, security",
    )
    stakes: list[str] | None = Field(
        default=None,
        description="Filter by stakes level: low, medium, high, critical",
    )
    status: list[str] | None = Field(
        default=None,
        description="Filter by status: pending, reviewed",
    )
    project: str | None = Field(
        default=None,
        description="Filter by project (owner/repo format)",
    )
    feature: str | None = Field(
        default=None,
        description="Filter by feature/epic name",
    )
    pr: int | None = Field(
        default=None,
        description="Filter by PR number",
    )
    min_confidence: float | None = Field(
        default=None,
        description="Minimum confidence threshold (0.0-1.0)",
    )
    max_confidence: float | None = Field(
        default=None,
        description="Maximum confidence threshold (0.0-1.0)",
    )
    has_outcome: bool | None = Field(
        default=None,
        description="Filter to decisions with/without recorded outcomes",
    )


class QueryDecisionsInput(BaseModel):
    """Input for the query_decisions tool."""

    query: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description="Natural language query to find similar past decisions",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum number of results to return (1-50)",
    )
    retrieval_mode: Literal["semantic", "keyword", "hybrid"] = Field(
        default="hybrid",
        description="Search mode: semantic (embedding similarity), keyword (BM25), or hybrid (both)",
    )
    filters: QueryFiltersInput | None = Field(
        default=None,
        description="Optional filters to narrow results",
    )
    bridge_side: Literal["structure", "function"] | None = Field(
        default=None,
        description="Search by bridge side: 'structure' (what pattern looks like) or 'function' (what problem it solves)",
    )


class CheckActionInput(BaseModel):
    """Input for the check_action tool."""

    description: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description="Description of the action you intend to take",
    )
    category: str | None = Field(
        default=None,
        description="Action category: architecture, process, integration, tooling, security",
    )
    stakes: Literal["low", "medium", "high", "critical"] = Field(
        default="medium",
        description="Stakes level of the action",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Your confidence in this action (0.0 to 1.0)",
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description="Additional context for guardrail evaluation (e.g. {code_review: true, architecture_review: true})",
    )


# ============================================================================
# Phase 2: log_decision, review_outcome, get_stats
# ============================================================================


class ReasonInput(BaseModel):
    """A reason supporting a decision."""

    type: Literal[
        "authority", "analogy", "analysis", "pattern",
        "intuition", "empirical", "elimination", "constraint",
    ] = Field(
        ...,
        description="Type of reasoning: authority, analogy, analysis, pattern, intuition, empirical, elimination, or constraint",
    )
    text: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description="Explanation of this reason",
    )


class DeliberationInputSchema(BaseModel):
    """An input/evidence gathered during deliberation."""

    id: str = Field(
        ...,
        description="Short identifier (e.g., 'i1', 'i2')",
    )
    text: str = Field(
        ...,
        min_length=1,
        description="Description of the input/evidence",
    )
    source: str | None = Field(
        default=None,
        description="Where the input came from (url, file, memory, api, etc.)",
    )


class DeliberationStepSchema(BaseModel):
    """A step in the deliberation process."""

    step: int = Field(
        ...,
        ge=1,
        description="Step number (1-indexed)",
    )
    thought: str = Field(
        ...,
        min_length=1,
        description="What was considered at this step",
    )
    inputs_used: list[str] | None = Field(
        default=None,
        description="Which input IDs contributed to this step (e.g., ['i1', 'i2'])",
    )
    type: str | None = Field(
        default=None,
        description="Reasoning type used: analysis, pattern, empirical, etc.",
    )
    conclusion: bool = Field(
        default=False,
        description="Whether this step produced the final conclusion",
    )


class DeliberationSchema(BaseModel):
    """Full deliberation trace capturing chain-of-thought."""

    inputs: list[DeliberationInputSchema] | None = Field(
        default=None,
        description="Evidence/inputs gathered during deliberation",
    )
    steps: list[DeliberationStepSchema] | None = Field(
        default=None,
        description="Reasoning steps showing how inputs were combined",
    )
    total_duration_ms: int | None = Field(
        default=None,
        ge=0,
        description="Total time spent deliberating in milliseconds",
    )


class LogDecisionInput(BaseModel):
    """Input for the log_decision tool."""

    decision: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description="What you decided — state the choice, not the question",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Your confidence in this decision (0.0 to 1.0)",
    )
    category: Literal["architecture", "process", "integration", "tooling", "security"] = Field(
        ...,
        description="Decision category",
    )
    stakes: Literal["low", "medium", "high", "critical"] = Field(
        default="medium",
        description="Stakes level of the decision",
    )
    context: str | None = Field(
        default=None,
        max_length=MAX_CONTEXT_CHARS,
        description="Situation context — what led to this decision",
    )
    reasons: list[ReasonInput] | None = Field(
        default=None,
        description="Reasons supporting the decision (aim for 2+ different types)",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Tags for categorization",
    )
    project: str | None = Field(
        default=None,
        description="Project in owner/repo format",
    )
    feature: str | None = Field(
        default=None,
        description="Feature or epic name",
    )
    pr: int | None = Field(
        default=None,
        ge=1,
        description="Pull request number",
    )
    agent_id: str | None = Field(
        default=None,
        description="Label for multi-agent deliberation isolation within this "
        "connection's token; the decision is recorded under the token's agent. "
        "Use when multiple agents share an MCP connection.",
    )
    decision_id: str | None = Field(
        default=None,
        description="Decision ID from pre_action to scope deliberation consumption. "
        "Ensures only thoughts tracked for THIS decision are attached.",
    )
    deliberation: DeliberationSchema | None = Field(
        default=None,
        description="Chain-of-thought trace: inputs gathered, reasoning steps, and timing",
    )
    bridge: "BridgeSchema | None" = Field(
        default=None,
        description="Bridge-definition: connects structure (what it looks like) to function (what it solves)",
    )


class BridgeSchema(BaseModel):
    """Minsky Ch 12 bridge-definition: connects structure to function."""

    structure: str = Field(
        ...,
        min_length=1,
        description="What the pattern looks like - recognizable form, files, tools, code shape",
    )
    function: str = Field(
        ...,
        min_length=1,
        description="What problem it solves - purpose, goal, constraint addressed",
    )
    tolerance: list[str] | None = Field(
        default=None,
        description="Features that DON'T MATTER for this pattern (Minsky Ch 12.3)",
    )
    enforcement: list[str] | None = Field(
        default=None,
        description="Features that MUST be present for the pattern to apply (Minsky Ch 12.3)",
    )
    prevention: list[str] | None = Field(
        default=None,
        description="Features that MUST NOT be present (Minsky Ch 12.3)",
    )


# Rebuild LogDecisionInput model to resolve forward ref
LogDecisionInput.model_rebuild()


class ReviewOutcomeInput(BaseModel):
    """Input for the review_outcome tool."""

    id: str = Field(
        ...,
        min_length=1,
        description="Decision ID to review (8-char hex)",
    )
    outcome: Literal["success", "partial", "failure", "abandoned"] = Field(
        ...,
        description="Outcome of the decision",
    )
    actual_result: str | None = Field(
        default=None,
        description="What actually happened",
    )
    lessons: str | None = Field(
        default=None,
        description="Lessons learned from this decision",
    )
    notes: str | None = Field(
        default=None,
        description="Additional review notes",
    )


class GetStatsInput(BaseModel):
    """Input for the get_stats tool."""

    category: str | None = Field(
        default=None,
        description="Filter stats by category: architecture, process, integration, tooling, security",
    )
    project: str | None = Field(
        default=None,
        description="Filter stats by project (owner/repo format)",
    )
    window: Literal["30d", "60d", "90d", "all"] | None = Field(
        default=None,
        description="Rolling time window for stats calculation",
    )


class GetDecisionInput(BaseModel):
    """Input for the get_decision tool."""

    id: str = Field(
        ...,
        min_length=1,
        description="Decision ID to retrieve (8-char hex, e.g. 'b02d10ba')",
    )


class ReasonStatsFiltersInput(BaseModel):
    """Optional filters for reason-type stats."""

    category: str | None = Field(
        default=None,
        description="Filter by category: architecture, process, integration, tooling, security",
    )
    stakes: str | None = Field(
        default=None,
        description="Filter by stakes level: low, medium, high, critical",
    )
    project: str | None = Field(
        default=None,
        description="Filter by project (owner/repo format)",
    )


class GetReasonStatsInput(BaseModel):
    """Input for the get_reason_stats tool."""

    filters: ReasonStatsFiltersInput | None = Field(
        default=None,
        description="Optional filters to narrow analysis",
    )
    min_reviewed: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Minimum reviewed decisions to include a reason type in stats",
    )


class UpdateDecisionInput(BaseModel):
    """Input for the update_decision tool."""

    id: str = Field(
        ...,
        min_length=1,
        description="Decision ID to update (8-char hex, e.g. 'b02d10ba')",
    )
    decision: str | None = Field(
        default=None,
        min_length=1,
        description="Updated decision text (what was actually decided/done)",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Updated confidence level (0.0-1.0)",
    )
    context: str | None = Field(
        default=None,
        description="Updated context (situation + what was done)",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Tags to set on the decision (replaces existing)",
    )
    pattern: str | None = Field(
        default=None,
        description="Abstract pattern this decision represents",
    )


class RecordThoughtInput(BaseModel):
    """Input for the record_thought tool (F028)."""

    text: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description="Reasoning/chain-of-thought text to record",
    )
    agent_id: str | None = Field(
        default=None,
        description=(
            "Label for multi-agent isolation within this connection's token. "
            "Thoughts from different agent_ids are tracked separately. "
            "Required when multiple agents share an MCP connection."
        ),
    )
    decision_id: str | None = Field(
        default=None,
        description=(
            "Decision ID to scope thought to. In post-decision mode "
            "(decision already recorded), appends to existing deliberation. "
            "In pre-decision mode, scopes the tracker bucket so only "
            "recordDecision with matching decision_id consumes these thoughts."
        ),
    )


# ============================================================================
# F046: pre_action tool
# ============================================================================


class PreActionActionInput(BaseModel):
    """Action description for the pre_action tool."""

    description: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description="Description of the action you intend to take",
    )
    category: str | None = Field(
        default=None,
        description=(
            "Action category: architecture, process, integration, tooling, security"
        ),
    )
    stakes: Literal["low", "medium", "high", "critical"] = Field(
        default="medium",
        description="Stakes level of the action",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Your confidence in this action (0.0 to 1.0)",
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description="Additional context for guardrail evaluation (e.g. {code_review: true, architecture_review: true})",
    )


class PreActionOptionsInput(BaseModel):
    """Options for the pre_action tool."""

    query_limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Max similar past decisions to return (1-20)",
    )
    auto_record: bool = Field(
        default=True,
        description="Automatically record the decision if guardrails allow it",
    )


class PreActionInput(BaseModel):
    """Input for the pre_action tool."""

    action: PreActionActionInput = Field(
        ...,
        description="The action you intend to take",
    )
    options: PreActionOptionsInput | None = Field(
        default=None,
        description="Options for the pre-action check",
    )
    reasons: list[ReasonInput] | None = Field(
        default=None,
        description="Reasons supporting this action (aim for 2+ different types)",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Tags for categorization",
    )
    pattern: str | None = Field(
        default=None,
        max_length=MAX_TEXT_CHARS,
        description="Abstract pattern this action represents",
    )
    agent_id: str | None = Field(
        default=None,
        description=(
            "Label for this agent's deliberation bucket when several agents "
            "share one MCP connection. Decisions are always recorded under the "
            "connection's token, not this label."
        ),
    )


# ============================================================================
# F047: get_session_context tool
# ============================================================================


class GetSessionContextInput(BaseModel):
    """Input for the get_session_context tool."""

    task_description: str | None = Field(
        default=None,
        description=(
            "What you're working on this session. Used to find relevant "
            "past decisions via semantic search."
        ),
    )
    include: list[
        Literal["decisions", "guardrails", "calibration", "ready", "patterns"]
    ] | None = Field(
        default=None,
        description="Which sections to include (default: all)",
    )
    decisions_limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Max relevant past decisions to return (1-50)",
    )
    ready_limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Max ready queue items to return (1-20)",
    )
    format: Literal["json", "markdown"] = Field(
        default="markdown",
        description="Response format: 'json' for structured data, 'markdown' for system prompt injection",
    )
    agent_id: str | None = Field(
        default=None,
        description=(
            "Ignored: the result is built for the connection's token. "
            "Accepted so existing clients still validate."
        ),
    )


# ============================================================================
# F044: ready tool
# ============================================================================


class ReadyInput(BaseModel):
    """Input for the ready tool (F044)."""

    min_priority: Literal["low", "medium", "high"] = Field(
        default="low",
        description="Minimum priority level: low (all), medium (skip low), high (only high)",
    )
    action_types: list[str] = Field(
        default_factory=list,
        description=(
            "Filter to specific action types: review_outcome, "
            "calibration_drift, stale_pending (default: all)"
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum actions to return (1-50)",
    )
    category: str | None = Field(
        default=None,
        description=(
            "Filter to specific category: architecture, process, "
            "integration, tooling, security"
        ),
    )
    agent_id: str | None = Field(
        default=None,
        description=(
            "Ignored: the result is built for the connection's token. "
            "Accepted so existing clients still validate."
        ),
    )


# ============================================================================
# F045: Graph tools
# ============================================================================


class LinkDecisionsInput(BaseModel):
    """Input for the link_decisions tool."""

    source_id: str = Field(
        ...,
        min_length=1,
        description="Source decision ID (8-char hex)",
    )
    target_id: str = Field(
        ...,
        min_length=1,
        description="Target decision ID (8-char hex)",
    )
    edge_type: Literal["relates_to", "supersedes", "depends_on"] = Field(
        ...,
        description="Type of relationship: relates_to, supersedes, or depends_on",
    )
    weight: float = Field(
        default=1.0,
        gt=0.0,
        le=1.0,
        description="Edge weight (0.0-1.0, higher = stronger relationship)",
    )
    context: str | None = Field(
        default=None,
        description="Optional context explaining the relationship",
    )


class GetGraphInput(BaseModel):
    """Input for the get_graph tool."""

    node_id: str = Field(
        ...,
        min_length=1,
        description="Center node ID to start traversal from (8-char hex)",
    )
    depth: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Maximum traversal depth (1-5 hops)",
    )
    edge_types: list[Literal["relates_to", "supersedes", "depends_on"]] | None = Field(
        default=None,
        description="Filter to specific edge types. None = all types.",
    )
    direction: Literal["outgoing", "incoming", "both"] = Field(
        default="both",
        description="Traversal direction: outgoing, incoming, or both",
    )


class GetNeighborsInput(BaseModel):
    """Input for the get_neighbors tool."""

    node_id: str = Field(
        ...,
        min_length=1,
        description="Node ID to find neighbors of (8-char hex)",
    )
    direction: Literal["outgoing", "incoming", "both"] = Field(
        default="both",
        description="Which direction to look: outgoing, incoming, or both",
    )
    edge_type: Literal["relates_to", "supersedes", "depends_on"] | None = Field(
        default=None,
        description="Filter to a specific edge type",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum neighbors to return (1-100)",
    )


# ============================================================================
# F030: Circuit breaker tools
# ============================================================================


class GetCircuitStateInput(BaseModel):
    """Input for the get_circuit_state tool."""

    scope: str = Field(
        ...,
        min_length=1,
        description="Circuit breaker scope to query (e.g. 'stakes:high', 'category:tooling', 'global')",
    )


class ListBreakersInput(BaseModel):
    """Input for the list_breakers tool (no required fields)."""
