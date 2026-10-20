"""MCP Server for CSTP Decision Intelligence.

Exposes CSTP capabilities as MCP tools so any MCP-compliant agent
(Claude Desktop, OpenClaw, etc.) can discover and use decision
intelligence natively.

Transports:
    - stdio: ``python -m a2a.mcp_server`` (local / Docker exec)
    - Streamable HTTP: mounted at ``/mcp`` on the FastAPI server (remote)

The ``mcp_app`` Server instance is importable for mounting into other
ASGI applications (see ``a2a/server.py``).
"""

import asyncio
import json
import logging
import sys
import uuid
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
)

from .cstp.access import PermissionDeniedError
from .mcp_schemas import (
    CheckActionInput,
    GetCircuitStateInput,
    GetDecisionInput,
    GetGraphInput,
    GetNeighborsInput,
    GetReasonStatsInput,
    GetSessionContextInput,
    GetStatsInput,
    LinkDecisionsInput,
    ListBreakersInput,
    LogDecisionInput,
    PreActionInput,
    QueryDecisionsInput,
    ReadyInput,
    RecordThoughtInput,
    ReviewOutcomeInput,
    UpdateDecisionInput,
)


def _deref_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Dereference $ref/$defs in a JSON Schema for LLM API compatibility.

    Many LLM APIs (OpenAI, Anthropic, etc.) don't support $ref references
    in tool schemas. This function inlines all $ref references and removes
    the $defs block, producing a flat schema.
    """
    defs = schema.get("$defs", {})
    if not defs:
        return schema

    # Work on a copy to avoid mutating the input
    result = {k: v for k, v in schema.items() if k != "$defs"}

    def _resolve(node: Any, seen: frozenset[str] = frozenset()) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_path = node["$ref"]  # e.g., "#/$defs/ReasonInput"
                ref_name = ref_path.rsplit("/", 1)[-1]
                if ref_name in seen:
                    # Circular reference - return empty object to break loop
                    return {"type": "object"}
                if ref_name in defs:
                    return _resolve(dict(defs[ref_name]), seen | {ref_name})
                return node
            return {k: _resolve(v, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(item, seen) for item in node]
        return node

    return _resolve(result)

# Configure logging to stderr (stdout is reserved for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("cstp-mcp")

# Server instance — importable for mounting in ASGI apps (see a2a/server.py)
mcp_app = Server("cstp-decisions")

# Agent for tool calls that carry no HTTP request: the stdio transport, which
# runs as the local user without a bearer token, and direct in-process calls.
LOCAL_AGENT_ID = "mcp-client"

# Set by run_stdio. Without it, a tool call inside an MCP session must arrive
# on an authenticated HTTP request.
_stdio_transport = False


def get_caller_agent_id() -> str:
    """Return the authenticated agent for the current tool call.

    Identity comes from the transport, never from tool arguments: over HTTP
    it is the agent `handle_mcp` resolved from the bearer token. A tool's
    `agent_id` argument only labels deliberation buckets within that agent.

    Raises:
        PermissionDeniedError: A session tool call arrived without an
            authenticated HTTP request outside the stdio transport.
    """
    try:
        request = mcp_app.request_context.request
    except LookupError:
        return LOCAL_AGENT_ID  # called directly, not through an MCP session
    if request is None:
        if _stdio_transport:
            return LOCAL_AGENT_ID
        raise PermissionDeniedError("MCP request is not authenticated")
    agent_id = getattr(request.state, "agent_id", None)
    if not agent_id:
        raise PermissionDeniedError("MCP request is not authenticated")
    return str(agent_id)


def get_mcp_tracker_key() -> str:
    """Get the deliberation tracker key for the current MCP session.

    Read per call from the request: tool calls run in the session's
    long-lived task, so state set per HTTP request does not reach them.
    """
    from .cstp.deliberation_tracker import principal_key

    agent_id = get_caller_agent_id()
    try:
        request = mcp_app.request_context.request
    except LookupError:
        request = None
    session_id = request.headers.get("mcp-session-id") if request is not None else None
    return principal_key(agent_id, f"mcp:{session_id or 'default'}")


def _build_query_params(args: QueryDecisionsInput) -> dict[str, Any]:
    """Convert Pydantic input to CSTP query params dict."""
    params: dict[str, Any] = {
        "query": args.query,
        "limit": args.limit,
        "retrievalMode": args.retrieval_mode,
    }
    if args.filters:
        filters: dict[str, Any] = {}
        if args.filters.category:
            filters["category"] = args.filters.category
        if args.filters.stakes:
            filters["stakes"] = args.filters.stakes
        if args.filters.status:
            filters["status"] = args.filters.status
        if args.filters.project:
            filters["project"] = args.filters.project
        if args.filters.feature:
            filters["feature"] = args.filters.feature
        if args.filters.pr is not None:
            filters["pr"] = args.filters.pr
        if args.filters.min_confidence is not None:
            filters["minConfidence"] = args.filters.min_confidence
        if args.filters.max_confidence is not None:
            filters["maxConfidence"] = args.filters.max_confidence
        if args.filters.has_outcome is not None:
            filters["hasOutcome"] = args.filters.has_outcome
        if filters:
            params["filters"] = filters
    # F024: Pass bridge_side
    if args.bridge_side:
        params["bridgeSide"] = args.bridge_side
    return params


def _build_guardrails_params(args: CheckActionInput) -> dict[str, Any]:
    """Convert Pydantic input to CSTP guardrails params dict."""
    action: dict[str, Any] = {
        "description": args.description,
        "stakes": args.stakes,
    }
    if args.category:
        action["category"] = args.category
    if args.confidence is not None:
        action["confidence"] = args.confidence
    if args.context:
        action["context"] = args.context
    return {"action": action}


@mcp_app.list_tools()
async def list_tools() -> list[Tool]:
    """List available CSTP tools for MCP discovery."""
    return [
        Tool(
            name="query_decisions",
            description=(
                "Low-level: Search similar past decisions using semantic search, keyword "
                "matching, or hybrid retrieval. Returns matching decisions with confidence "
                "scores, categories, and outcomes. Prefer pre_action for decision-making "
                "workflows (combines query + guardrails + record in one call). Use this "
                "directly only for exploratory search without recording."
            ),
            inputSchema=_deref_schema(QueryDecisionsInput.model_json_schema()),
        ),
        Tool(
            name="check_action",
            description=(
                "Low-level: Validate an intended action against safety guardrails and "
                "policies. Returns whether the action is allowed, any violations "
                "(blocking), and warnings. Prefer pre_action which includes guardrail "
                "checks automatically. Use this directly only for standalone what-if "
                "guardrail checks without querying or recording."
            ),
            inputSchema=_deref_schema(CheckActionInput.model_json_schema()),
        ),
        Tool(
            name="log_decision",
            description=(
                "Low-level: Record a decision to the immutable decision log. Include "
                "what you decided, your confidence level, category, and supporting "
                "reasons. Prefer pre_action with auto_record=true which queries + "
                "checks guardrails + records in one call. Use this directly only for "
                "after-the-fact recording when pre_action was not used."
            ),
            inputSchema=_deref_schema(LogDecisionInput.model_json_schema()),
        ),
        Tool(
            name="review_outcome",
            description=(
                "Record the outcome of a past decision. Provide the decision ID, "
                "whether it succeeded or failed, what actually happened, and lessons "
                "learned. Builds calibration data over time."
            ),
            inputSchema=_deref_schema(ReviewOutcomeInput.model_json_schema()),
        ),
        Tool(
            name="get_stats",
            description=(
                "Low-level: Get calibration statistics: Brier score, accuracy, "
                "confidence distribution, and decision counts. Optionally filter by "
                "category, project, or time window. Prefer get_session_context which "
                "includes calibration alongside decisions, guardrails, and patterns. "
                "Use this directly for monitoring dashboards or CI checks."
            ),
            inputSchema=_deref_schema(GetStatsInput.model_json_schema()),
        ),
        Tool(
            name="get_decision",
            description=(
                "Retrieve full details of a single decision by ID. Returns the "
                "complete record including context, reasons, project metadata, "
                "outcome, and review information. Use when you need the full "
                "decision content beyond what query_decisions returns."
            ),
            inputSchema=_deref_schema(GetDecisionInput.model_json_schema()),
        ),
        Tool(
            name="get_reason_stats",
            description=(
                "Analyze which reason types (analysis, pattern, empirical, etc.) "
                "correlate with better decision outcomes. Shows per-type success "
                "rates, Brier scores, and diversity analysis (do decisions with "
                "more diverse reasoning perform better?). Use to improve "
                "decision-making by understanding which reasoning approaches work."
            ),
            inputSchema=_deref_schema(GetReasonStatsInput.model_json_schema()),
        ),
        Tool(
            name="update_decision",
            description=(
                "Update specific fields on an existing decision. Currently "
                "supports updating tags and pattern. Use for backfilling "
                "decisions with missing metadata."
            ),
            inputSchema=_deref_schema(UpdateDecisionInput.model_json_schema()),
        ),
        Tool(
            name="record_thought",
            description=(
                "Record a reasoning/chain-of-thought step in the deliberation "
                "trace. In pre-decision mode (no decision_id), thoughts "
                "accumulate and auto-attach to the next recorded decision. "
                "In post-decision mode (with decision_id), appends to the "
                "existing decision's deliberation trace."
            ),
            inputSchema=_deref_schema(RecordThoughtInput.model_json_schema()),
        ),
        # F046: Pre-action hook
        Tool(
            name="pre_action",
            description=(
                "PRIMARY - Call this BEFORE any significant decision. All-in-one: "
                "queries similar past decisions, evaluates guardrails, fetches "
                "calibration context, extracts confirmed patterns, and optionally "
                "records the decision. One round-trip replaces separate calls to "
                "query_decisions + check_action + log_decision."
            ),
            inputSchema=_deref_schema(PreActionInput.model_json_schema()),
        ),
        # F047: Session context
        Tool(
            name="get_session_context",
            description=(
                "PRIMARY - Call at session start or when switching tasks. Returns "
                "full cognitive context: agent profile (accuracy, Brier score, "
                "tendency), relevant past decisions, active guardrails, calibration "
                "by category, overdue reviews, and confirmed patterns. Available in "
                "JSON or markdown format for direct system prompt injection."
            ),
            inputSchema=_deref_schema(GetSessionContextInput.model_json_schema()),
        ),
        # F044: Agent Work Discovery
        Tool(
            name="ready",
            description=(
                "PRIMARY - Returns prioritized cognitive actions needing attention: "
                "outcome reviews (overdue decisions), calibration drift (per-category "
                "degradation), and stale pending decisions. Call during idle periods "
                "or after completing tasks to discover maintenance work. Filter by "
                "priority (low/medium/high), action types, or category."
            ),
            inputSchema=_deref_schema(ReadyInput.model_json_schema()),
        ),
        # F045: Graph tools
        Tool(
            name="link_decisions",
            description=(
                "Create a typed relationship edge between two decisions. "
                "Edge types: relates_to (topical similarity), supersedes "
                "(newer replaces older), depends_on (prerequisite). "
                "Builds the decision knowledge graph over time."
            ),
            inputSchema=_deref_schema(LinkDecisionsInput.model_json_schema()),
        ),
        Tool(
            name="get_graph",
            description=(
                "Get a subgraph of related decisions around a center node. "
                "Traverses the decision knowledge graph up to a specified "
                "depth. Returns nodes with metadata and edges with types. "
                "Use for understanding decision clusters and dependencies."
            ),
            inputSchema=_deref_schema(GetGraphInput.model_json_schema()),
        ),
        Tool(
            name="get_neighbors",
            description=(
                "Get immediate neighbors of a decision in the knowledge "
                "graph. Lighter-weight than get_graph -- returns a flat "
                "list of directly connected decisions with their "
                "relationship edges. Use for quick context lookup."
            ),
            inputSchema=_deref_schema(GetNeighborsInput.model_json_schema()),
        ),
        # F030: Circuit breaker tools (read-only)
        Tool(
            name="get_circuit_state",
            description=(
                "Query the current state of a circuit breaker by scope. "
                "Returns state, failure count, threshold, cooldown remaining, "
                "and configuration details. Use to inspect a specific breaker."
            ),
            inputSchema=_deref_schema(GetCircuitStateInput.model_json_schema()),
        ),
        Tool(
            name="list_breakers",
            description=(
                "List all circuit breakers with their current state. "
                "Returns each breaker's scope, state, failure count, "
                "threshold, and configuration. Use for overview of all "
                "circuit breaker health."
            ),
            inputSchema=_deref_schema(ListBreakersInput.model_json_schema()),
        ),
    ]


@mcp_app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool calls to CSTP services."""
    logger.info("Tool called: %s", name)

    try:
        if name == "query_decisions":
            return await _handle_query_decisions(arguments)

        if name == "check_action":
            return await _handle_check_action(arguments)

        if name == "log_decision":
            return await _handle_log_decision(arguments)

        if name == "review_outcome":
            return await _handle_review_outcome(arguments)

        if name == "get_stats":
            return await _handle_get_stats(arguments)

        if name == "get_decision":
            return await _handle_get_decision_mcp(arguments)

        if name == "get_reason_stats":
            return await _handle_get_reason_stats_mcp(arguments)

        if name == "update_decision":
            return await _handle_update_decision_mcp(arguments)

        if name == "record_thought":
            return await _handle_record_thought_mcp(arguments)

        if name == "pre_action":
            return await _handle_pre_action_mcp(arguments)

        if name == "get_session_context":
            return await _handle_get_session_context_mcp(arguments)

        if name == "ready":
            return await _handle_ready_mcp(arguments)

        if name == "link_decisions":
            return await _handle_link_decisions_mcp(arguments)

        if name == "get_graph":
            return await _handle_get_graph_mcp(arguments)

        if name == "get_neighbors":
            return await _handle_get_neighbors_mcp(arguments)

        if name == "get_circuit_state":
            return await _handle_get_circuit_state_mcp(arguments)

        if name == "list_breakers":
            return await _handle_list_breakers_mcp(arguments)

        raise ValueError(f"Unknown tool: {name}")

    except ValueError as e:
        logger.warning("Validation error in tool %s: %s", name, e)
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "validation_error", "message": str(e)}),
            )
        ]
    except PermissionDeniedError as e:
        logger.warning("Permission denied in tool %s: %s", name, e)
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "permission_denied", "message": str(e)}),
            )
        ]
    except Exception as e:
        # Catches pydantic.ValidationError and any other unexpected errors
        if "ValidationError" in type(e).__name__:
            logger.warning("Validation error in tool %s: %s", name, e)
            payload = {"error": "validation_error", "message": str(e)}
        else:
            # Unexpected errors can carry paths, SQL or backend detail, so the
            # caller gets a reference to the server log instead of the message.
            ref = uuid.uuid4().hex[:12]
            logger.error("Tool %s failed (ref %s)", name, ref, exc_info=True)
            payload = {"error": "internal_error", "message": f"Internal error (ref {ref})"}
        return [TextContent(type="text", text=json.dumps(payload))]


async def _handle_query_decisions(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle query_decisions tool call."""
    import time

    from .cstp.models import (
        DecisionSummary,
        QueryDecisionsRequest,
        QueryDecisionsResponse,
    )
    from .cstp.query_service import query_decisions

    start_time = time.time()

    # Validate input via Pydantic
    args = QueryDecisionsInput(**arguments)

    # Convert to CSTP request
    params = _build_query_params(args)
    request = QueryDecisionsRequest.from_params(params)

    # Execute query - use effective_query for bridge-side prefix (F024)
    response = await query_decisions(
        query=request.effective_query,
        n_results=request.limit,
        category=request.filters.category,
        min_confidence=(
            request.filters.min_confidence
            if request.filters.min_confidence > 0
            else None
        ),
        max_confidence=(
            request.filters.max_confidence
            if request.filters.max_confidence < 1
            else None
        ),
        stakes=request.filters.stakes,
        status_filter=request.filters.status,
        project=request.filters.project,
        feature=request.filters.feature,
        pr=request.filters.pr,
        has_outcome=request.filters.has_outcome,
    )

    # P2: Surface query errors instead of masking them
    if response.error:
        raise RuntimeError(response.error)

    query_time_ms = int((time.time() - start_time) * 1000)

    # Convert QueryResponse results to DecisionSummary list
    decisions = []
    for r in response.results:
        decisions.append(
            DecisionSummary(
                id=r.id[:8] if len(r.id) > 8 else r.id,
                title=r.title[:50],
                category=r.category or "",
                confidence=r.confidence,
                stakes=r.stakes,
                status=r.status or "",
                outcome=r.outcome,
                date=r.date or "",
                distance=r.distance,
                reasons=None,
                tags=r.tags,
                pattern=r.pattern,
            )
        )

    # Build proper response with to_dict()
    result_obj = QueryDecisionsResponse(
        decisions=decisions,
        total=len(decisions),
        query=request.query,
        query_time_ms=query_time_ms,
        agent=get_caller_agent_id(),
        retrieval_mode=request.retrieval_mode,
        scores={},
    )
    result = result_obj.to_dict()

    # F023 Phase 2: Track query for auto-deliberation
    from .cstp.deliberation_tracker import track_query

    track_query(
        key=get_mcp_tracker_key(),
        query=request.query,
        result_count=result_obj.total,
        top_ids=[d.id for d in result_obj.decisions[:5]],
        retrieval_mode=request.retrieval_mode,
    )

    return [
        TextContent(
            type="text",
            text=json.dumps(result, indent=2, default=str),
        )
    ]


async def _handle_check_action(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle check_action tool call."""
    from .cstp.guardrails_service import evaluate_guardrails, log_guardrail_check
    from .cstp.models import CheckGuardrailsRequest

    # Validate input via Pydantic
    args = CheckActionInput(**arguments)

    # Convert to CSTP request
    params = _build_guardrails_params(args)
    request = CheckGuardrailsRequest.from_params(params)

    # Evaluate guardrails (same context as the dispatcher builds)
    context = request.action.guardrail_context(get_caller_agent_id())
    eval_result = await evaluate_guardrails(context)

    # Log the check
    log_guardrail_check(
        requesting_agent=get_caller_agent_id(),
        action_description=request.action.description,
        allowed=eval_result.allowed,
        violations=eval_result.violations,
        evaluated=eval_result.evaluated,
    )

    # Map to response format
    violations = [
        {
            "guardrail_id": v.guardrail_id,
            "name": v.name,
            "message": v.message,
            "severity": v.severity,
        }
        for v in eval_result.violations
    ]

    warnings = [
        {
            "guardrail_id": w.guardrail_id,
            "name": w.name,
            "message": w.message,
            "severity": w.severity,
        }
        for w in eval_result.warnings
    ]

    result = {
        "allowed": eval_result.allowed,
        "violations": violations,
        "warnings": warnings,
        "evaluated": eval_result.evaluated,
    }

    # F023 Phase 2: Track guardrail check for auto-deliberation
    from .cstp.deliberation_tracker import track_guardrail

    track_guardrail(
        key=get_mcp_tracker_key(),
        description=request.action.description,
        allowed=eval_result.allowed,
        violation_count=len(eval_result.violations),
    )

    return [
        TextContent(
            type="text",
            text=json.dumps(result, indent=2, default=str),
        )
    ]


async def _handle_log_decision(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle log_decision tool call."""
    from .cstp.decision_service import RecordDecisionRequest, record_decision

    # Validate input via Pydantic
    args = LogDecisionInput(**arguments)

    # Build params dict for CSTP
    params: dict[str, Any] = {
        "decision": args.decision,
        "confidence": args.confidence,
        "category": args.category,
        "stakes": args.stakes,
    }
    if args.context:
        params["context"] = args.context
    if args.reasons:
        params["reasons"] = [{"type": r.type, "text": r.text} for r in args.reasons]
    if args.tags:
        params["tags"] = args.tags
    if args.project:
        params["project"] = args.project
    if args.feature:
        params["feature"] = args.feature
    if args.pr is not None:
        params["pr"] = args.pr

    # F023: Pass deliberation trace
    if args.deliberation:
        delib: dict[str, Any] = {}
        if args.deliberation.inputs:
            delib["inputs"] = [
                {"id": i.id, "text": i.text, **({"source": i.source} if i.source else {})}
                for i in args.deliberation.inputs
            ]
        if args.deliberation.steps:
            delib["steps"] = [
                {
                    "step": s.step,
                    "thought": s.thought,
                    **({"inputs_used": s.inputs_used} if s.inputs_used else {}),
                    **({"type": s.type} if s.type else {}),
                    **({"conclusion": s.conclusion} if s.conclusion else {}),
                }
                for s in args.deliberation.steps
            ]
        if args.deliberation.total_duration_ms is not None:
            delib["total_duration_ms"] = args.deliberation.total_duration_ms
        if delib:
            params["deliberation"] = delib

    # F024: Pass bridge-definition
    if args.bridge:
        bridge_data: dict[str, Any] = {
            "structure": args.bridge.structure,
            "function": args.bridge.function,
        }
        if args.bridge.tolerance:
            bridge_data["tolerance"] = args.bridge.tolerance
        if args.bridge.enforcement:
            bridge_data["enforcement"] = args.bridge.enforcement
        if args.bridge.prevention:
            bridge_data["prevention"] = args.bridge.prevention
        params["bridge"] = bridge_data

    # Create request and record. The decision belongs to the authenticated
    # caller; args.agent_id only labels its deliberation bucket.
    agent_id = get_caller_agent_id()
    request = RecordDecisionRequest.from_dict(params, agent_id=agent_id)

    # Build composite tracker key for multi-agent isolation
    from .cstp.deliberation_tracker import build_tracker_key, principal_key

    tracker_key = build_tracker_key(
        agent_id=args.agent_id,
        decision_id=args.decision_id,
        transport_key=principal_key(agent_id, "mcp-session"),
        principal=agent_id,
    )

    # F025: Extract related decisions BEFORE consuming tracker
    from .cstp.deliberation_tracker import extract_related_from_tracker

    if not request.related_to:
        related_raw = extract_related_from_tracker(tracker_key)
        if related_raw:
            from .cstp.decision_service import RelatedDecision

            request.related_to = [
                RelatedDecision.from_dict(r) for r in related_raw
            ]

    # F023 Phase 2: Auto-attach deliberation from tracked inputs
    from .cstp.deliberation_tracker import auto_attach_deliberation

    request.deliberation, auto_captured = auto_attach_deliberation(
        key=tracker_key,
        deliberation=request.deliberation,
    )

    # F027 P2: Smart bridge extraction
    from .cstp.bridge_hook import maybe_smart_extract_bridge

    bridge_auto, bridge_method = await maybe_smart_extract_bridge(request)

    response = await record_decision(request)

    # F045 follow-up: Auto-link decision in graph
    auto_linked = 0
    if response.success and response.id:
        from .cstp.graph_service import safe_auto_link

        related_dicts = [r.to_dict() for r in request.related_to] if request.related_to else []
        auto_linked = await safe_auto_link(
            response_id=response.id,
            category=request.category,
            stakes=request.stakes,
            confidence=request.confidence,
            tags=list(request.tags),
            pattern=request.pattern,
            related_to=related_dicts,
        )

    # Format response
    result = response.to_dict()
    if auto_captured and request.deliberation:
        result["deliberation_auto"] = True
        result["deliberation_inputs_count"] = len(request.deliberation.inputs)

    # F026: Run guardrails against record context
    from .cstp.guardrails_service import evaluate_record_guardrails
    record_warnings = await evaluate_record_guardrails(request)
    if record_warnings:
        result["guardrail_warnings"] = record_warnings

    if bridge_auto and request.bridge:
        result["bridge_auto"] = True
        result["bridge_method"] = bridge_method

    if request.related_to:
        result["related_count"] = len(request.related_to)

    if auto_linked > 0:
        result["graph_edges_created"] = auto_linked

    return [
        TextContent(
            type="text",
            text=json.dumps(result, indent=2, default=str),
        )
    ]


async def _handle_review_outcome(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle review_outcome tool call."""
    from .cstp.decision_service import ReviewDecisionRequest, review_decision

    # Validate input via Pydantic
    args = ReviewOutcomeInput(**arguments)

    # Build params dict for CSTP
    params: dict[str, Any] = {
        "id": args.id,
        "outcome": args.outcome,
    }
    if args.actual_result:
        params["actualResult"] = args.actual_result
    if args.lessons:
        params["lessons"] = args.lessons
    if args.notes:
        params["notes"] = args.notes

    # Create request and review
    from .cstp.access import require_decision_owner

    agent_id = get_caller_agent_id()
    await require_decision_owner(args.id, agent_id)
    request = ReviewDecisionRequest.from_dict(params, reviewer_id=agent_id)
    response = await review_decision(request)

    # Format response
    result = response.to_dict()
    return [
        TextContent(
            type="text",
            text=json.dumps(result, indent=2, default=str),
        )
    ]


async def _handle_get_stats(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle get_stats tool call."""
    from .cstp.calibration_service import GetCalibrationRequest, get_calibration

    # Validate input via Pydantic
    args = GetStatsInput(**arguments)

    # Build request directly
    request = GetCalibrationRequest(
        category=args.category,
        project=args.project,
        window=args.window,
    )

    # Get calibration stats
    response = await get_calibration(request)

    # Format response
    result = response.to_dict()
    return [
        TextContent(
            type="text",
            text=json.dumps(result, indent=2, default=str),
        )
    ]


async def _handle_get_decision_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle get_decision tool call."""
    from .cstp.decision_service import GetDecisionRequest, get_decision

    # Validate input via Pydantic
    args = GetDecisionInput(**arguments)

    # Create request and fetch
    request = GetDecisionRequest.from_dict({"id": args.id})
    response = await get_decision(request)

    # F023 Phase 2: Track lookup for auto-deliberation
    if response.found:
        from .cstp.deliberation_tracker import track_lookup

        dec = response.decision or {}
        track_lookup(
            key=get_mcp_tracker_key(),
            decision_id=args.id,
            title=dec.get("summary", dec.get("decision", ""))[:50],
        )

    # Format response
    result = response.to_dict()
    return [
        TextContent(
            type="text",
            text=json.dumps(result, indent=2, default=str),
        )
    ]


async def _handle_get_reason_stats_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle get_reason_stats tool call."""
    from .cstp.reason_stats_service import GetReasonStatsRequest, get_reason_stats

    # Validate input via Pydantic
    args = GetReasonStatsInput(**arguments)

    # Build params dict for CSTP
    params: dict[str, Any] = {
        "minReviewed": args.min_reviewed,
    }
    if args.filters:
        filters: dict[str, Any] = {}
        if args.filters.category:
            filters["category"] = args.filters.category
        if args.filters.stakes:
            filters["stakes"] = args.filters.stakes
        if args.filters.project:
            filters["project"] = args.filters.project
        if filters:
            params["filters"] = filters

    # Create request and fetch
    request = GetReasonStatsRequest.from_dict(params)
    response = await get_reason_stats(request)

    # Format response
    result = response.to_dict()

    # F023 Phase 2: Track stats lookup for auto-deliberation
    from .cstp.deliberation_tracker import track_stats

    track_stats(
        key=get_mcp_tracker_key(),
        total_decisions=result.get("totalDecisions", 0),
        reason_type_count=len(result.get("byReasonType", [])),
        diversity=result.get("diversity", {}).get("avgTypesPerDecision"),
    )

    return [
        TextContent(
            type="text",
            text=json.dumps(result, indent=2, default=str),
        )
    ]


async def _handle_update_decision_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle update_decision tool call."""
    from .cstp.decision_service import update_decision

    input_data = UpdateDecisionInput(**arguments)

    updates: dict[str, Any] = {}
    if input_data.decision is not None:
        updates["decision"] = input_data.decision
    if input_data.confidence is not None:
        updates["confidence"] = input_data.confidence
    if input_data.context is not None:
        updates["context"] = input_data.context
    if input_data.tags is not None:
        updates["tags"] = input_data.tags
    if input_data.pattern is not None:
        updates["pattern"] = input_data.pattern

    if not updates:
        return [TextContent(type="text", text=json.dumps({"error": "No fields to update"}))]

    from .cstp.access import require_decision_owner

    await require_decision_owner(input_data.id, get_caller_agent_id())
    result = await update_decision(input_data.id, updates)

    return [TextContent(type="text", text=json.dumps(result))]


async def _handle_record_thought_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle record_thought tool call (F028)."""
    from .cstp.deliberation_tracker import track_reasoning

    input_data = RecordThoughtInput(**arguments)
    agent_id = get_caller_agent_id()

    if input_data.decision_id:
        # Try post-decision append first (decision already exists)
        from .cstp.access import require_decision_owner
        from .cstp.decision_service import append_thought, find_decision

        found = await find_decision(input_data.decision_id)
        if found:
            await require_decision_owner(input_data.decision_id, agent_id)
            result = await append_thought(input_data.decision_id, input_data.text)
            if not result.get("success"):
                return [TextContent(type="text", text=json.dumps({"error": result.get("error", "Unknown error")}))]
            return [TextContent(type="text", text=json.dumps({
                "success": True,
                "mode": "post-decision",
                "decision_id": input_data.decision_id,
                "step_number": result["step_number"],
            }))]
        # Decision not found — fall through to pre-decision tracker
        # with decision_id scoping (decision will be recorded later)

    # Pre-decision: accumulate in tracker with composite key
    from .cstp.deliberation_tracker import build_tracker_key, principal_key

    tracker_key = build_tracker_key(
        agent_id=input_data.agent_id,
        decision_id=input_data.decision_id,
        transport_key=principal_key(agent_id, "mcp-session"),
        principal=agent_id,
    )
    track_reasoning(tracker_key, input_data.text)
    return [TextContent(type="text", text=json.dumps({
        "success": True,
        "mode": "pre-decision",
        "tracker_key": tracker_key,
    }))]


async def _handle_pre_action_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle pre_action tool call (F046)."""
    from .cstp.models import PreActionRequest
    from .cstp.preaction_service import pre_action

    # Validate input via Pydantic
    args = PreActionInput(**arguments)

    # Build params dict for CSTP
    params: dict[str, Any] = {
        "action": {
            "description": args.action.description,
            "stakes": args.action.stakes,
        },
    }
    if args.action.category:
        params["action"]["category"] = args.action.category
    if args.action.confidence is not None:
        params["action"]["confidence"] = args.action.confidence
    if args.action.context:
        params["action"]["context"] = args.action.context
    if args.options:
        params["options"] = {
            "queryLimit": args.options.query_limit,
            "autoRecord": args.options.auto_record,
        }
    if args.reasons:
        params["reasons"] = [
            {"type": r.type, "text": r.text} for r in args.reasons
        ]
    if args.tags:
        params["tags"] = args.tags
    if args.pattern:
        params["pattern"] = args.pattern

    from .cstp.deliberation_tracker import principal_key

    request = PreActionRequest.from_params(params)
    agent_id = get_caller_agent_id()
    response = await pre_action(
        request,
        agent_id=agent_id,
        tracker_key=principal_key(agent_id, f"mcp:{args.agent_id or 'mcp-client'}"),
    )

    return [
        TextContent(
            type="text",
            text=json.dumps(response.to_dict(), indent=2, default=str),
        )
    ]


async def _handle_get_session_context_mcp(
    arguments: dict[str, Any],
) -> list[TextContent]:
    """Handle get_session_context tool call (F047)."""
    from .cstp.models import SessionContextRequest
    from .cstp.session_context_service import get_session_context

    # Validate input via Pydantic
    args = GetSessionContextInput(**arguments)

    # Build params dict for CSTP
    params: dict[str, Any] = {
        "decisionsLimit": args.decisions_limit,
        "readyLimit": args.ready_limit,
        "format": args.format,
    }
    if args.task_description:
        params["taskDescription"] = args.task_description
    if args.include:
        params["include"] = args.include

    request = SessionContextRequest.from_params(params)
    response = await get_session_context(request, agent_id=get_caller_agent_id())

    return [
        TextContent(
            type="text",
            text=json.dumps(response.to_dict(), indent=2, default=str),
        )
    ]


async def _handle_ready_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle ready tool call (F044)."""
    from .cstp.models import ReadyRequest
    from .cstp.ready_service import get_ready_actions

    args = ReadyInput(**arguments)

    params: dict[str, Any] = {
        "minPriority": args.min_priority,
        "limit": args.limit,
    }
    if args.action_types:
        params["actionTypes"] = args.action_types
    if args.category:
        params["category"] = args.category

    request = ReadyRequest.from_params(params)
    response = await get_ready_actions(request, agent_id=get_caller_agent_id())

    return [
        TextContent(
            type="text",
            text=json.dumps(response.to_dict(), indent=2, default=str),
        )
    ]


async def _handle_link_decisions_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle link_decisions tool call (F045)."""
    from .cstp.graph_service import link_decisions

    args = LinkDecisionsInput(**arguments)

    response = await link_decisions(
        source_id=args.source_id,
        target_id=args.target_id,
        edge_type=args.edge_type,
        weight=args.weight,
        context=args.context,
        agent_id=get_caller_agent_id(),
    )
    return [
        TextContent(
            type="text",
            text=json.dumps(response.to_dict(), indent=2, default=str),
        )
    ]


async def _handle_get_graph_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle get_graph tool call (F045)."""
    from .cstp.graph_service import get_graph

    args = GetGraphInput(**arguments)

    response = await get_graph(
        node_id=args.node_id,
        depth=args.depth,
        edge_types=list(args.edge_types) if args.edge_types else None,
        direction=args.direction,
    )
    return [
        TextContent(
            type="text",
            text=json.dumps(response.to_dict(), indent=2, default=str),
        )
    ]


async def _handle_get_neighbors_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle get_neighbors tool call (F045)."""
    from .cstp.graph_service import get_neighbors

    args = GetNeighborsInput(**arguments)

    response = await get_neighbors(
        node_id=args.node_id,
        direction=args.direction,
        edge_type=args.edge_type,
        limit=args.limit,
    )
    return [
        TextContent(
            type="text",
            text=json.dumps(response.to_dict(), indent=2, default=str),
        )
    ]


async def _handle_get_circuit_state_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle get_circuit_state tool call (F030)."""
    from .cstp.circuit_breaker_service import get_circuit_breaker_manager

    args = GetCircuitStateInput(**arguments)

    mgr = await get_circuit_breaker_manager()
    state = await mgr.get_state(args.scope)
    if state is None:
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"No breaker found for scope: {args.scope}"}),
        )]

    return [TextContent(
        type="text",
        text=json.dumps(state, indent=2, default=str),
    )]


async def _handle_list_breakers_mcp(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle list_breakers tool call (F030)."""
    from .cstp.circuit_breaker_service import get_circuit_breaker_manager

    # Validate input (no required fields, but validates structure)
    ListBreakersInput(**arguments)

    mgr = await get_circuit_breaker_manager()
    breakers = await mgr.list_breakers()
    return [TextContent(
        type="text",
        text=json.dumps({"breakers": breakers, "total": len(breakers)}, indent=2, default=str),
    )]


async def run_stdio() -> None:
    """Run the MCP server with stdio transport.

    Owns the decision store lifecycle. The HTTP server gets this from the FastAPI
    lifespan hook, but this entrypoint (`cstp-mcp`) never enters it, so without
    the setup below SQLiteDecisionStore would serve every request with `_conn is
    None`. Since store writes are authoritative, that failure would propagate to
    the caller — and record_decision would delete the YAML file it had just
    written, on the assumption the write never landed.

    The graph store needs the same lifespan setup: without it the persisted
    edges are never loaded, and get_graph/get_neighbors report saved decisions
    as "Node not found".
    """
    logger.info("Starting CSTP MCP server (stdio transport)")

    from .cstp.storage.factory import get_decision_store, mark_initialized

    store = get_decision_store()
    await store.initialize()
    mark_initialized()
    logger.info("Decision store initialized (backend=%s)", type(store).__name__)

    from .cstp.graph_service import initialize_graph_from_decisions
    from .cstp.graphdb.factory import (
        get_graph_store,
        mark_initialized as mark_graph_initialized,
        set_graph_store,
    )

    try:
        graph_store = get_graph_store()
        await graph_store.initialize()
        await initialize_graph_from_decisions()
        mark_graph_initialized()
        logger.info("Graph store initialized")
    except Exception:
        logger.warning("Graph store initialization failed", exc_info=True)
        set_graph_store(None)

    # Tool calls on this transport carry no HTTP request; they act as the
    # local user (LOCAL_AGENT_ID) instead of being refused as unauthenticated.
    # There are no tokens here, as with auth disabled, so that user is an admin
    # and can still change decisions recorded under older or other names.
    from .auth import AuthManager, set_auth_manager
    from .config import AuthConfig, Config

    set_auth_manager(AuthManager(Config(auth=AuthConfig(enabled=False))))
    global _stdio_transport
    _stdio_transport = True
    try:
        async with stdio_server() as (read_stream, write_stream):
            await mcp_app.run(
                read_stream,
                write_stream,
                mcp_app.create_initialization_options(),
            )
    finally:
        _stdio_transport = False
        await store.close()


def main() -> None:
    """Entry point for the MCP server."""
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
