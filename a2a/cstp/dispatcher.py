"""JSON-RPC method dispatcher for CSTP.

Routes incoming JSON-RPC requests to appropriate method handlers.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from ..models.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PERMISSION_DENIED,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
)
from .access import (
    PermissionDeniedError,
    is_admin,
    require_admin,
    require_decision_owner,
)
from .attribution_service import (
    AttributeOutcomesRequest,
    attribute_outcomes,
)
from .bm25_index import get_cached_index, merge_results
from .calibration_service import (
    GetCalibrationRequest,
    get_calibration,
)
from .decision_service import (
    GetDecisionRequest,
    RecordDecisionRequest,
    ReviewDecisionRequest,
    get_decision,
    record_decision,
    review_decision,
)
from .drift_service import (
    CheckDriftRequest,
    check_drift,
)
from .reason_stats_service import (
    GetReasonStatsRequest,
    get_reason_stats,
)
from .guardrails_service import evaluate_guardrails, log_guardrail_check, list_guardrails
from .models import (
    CheckGuardrailsRequest,
    CheckGuardrailsResponse,
    DecisionSummary,
    GetCircuitStateRequest,
    GetCircuitStateResponse,
    GetStatsRequest,
    GetStatsResponse,
    GuardrailViolation,
    ListDecisionsRequest,
    ListDecisionsResponse,
    PreActionRequest,
    QueryDecisionsRequest,
    QueryDecisionsResponse,
    RecordThoughtParams,
    ResetCircuitRequest,
    ResetCircuitResponse,
    SessionContextRequest,
)
from .preaction_service import pre_action
from .provenance_service import (
    export_evidence_bundle,
    ingest_evidence,
    link_evidence,
    map_controls,
    verify_evidence_chain,
)
from .query_service import query_decisions, load_all_decisions
from .reindex_service import reindex_decisions
from .session_context_service import get_session_context

logger = logging.getLogger("cstp.dispatcher")


def _extract_bridge(d: dict[str, Any]) -> dict[str, str] | None:
    """Extract bridge structure/function from a decision dict (F169)."""
    bridge = d.get("bridge")
    if not bridge or not isinstance(bridge, dict):
        return None
    result: dict[str, str] = {}
    if bridge.get("structure"):
        result["structure"] = bridge["structure"]
    if bridge.get("function"):
        result["function"] = bridge["function"]
    return result or None


# Type alias for method handlers
MethodHandler = Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]]

# Custom error codes
QUERY_FAILED = -32003
RATE_LIMITED = -32002
GUARDRAIL_EVAL_FAILED = -32004
RECORD_FAILED = -32005
ATTRIBUTION_FAILED = -32008
REVIEW_FAILED = -32006
DECISION_NOT_FOUND = -32007


def build_tracker_key(
    transport_agent_id: str,
    agent_id: str | None = None,
    decision_id: str | None = None,
) -> str:
    """Build a composite tracker key for deliberation tracking (F129).

    Delegates to deliberation_tracker.build_tracker_key with the
    transport-derived fallback key. Composite keys are namespaced under the
    authenticated agent, so `agent_id` is only a label within its buckets.

    Priority: most-specific composite key first.
    - agent_id + decision_id → "{transport_agent_id}|agent:{agent_id}:decision:{decision_id}"
    - agent_id only → "{transport_agent_id}|agent:{agent_id}"
    - decision_id only → "{transport_agent_id}|decision:{decision_id}"
    - neither → "rpc:{transport_agent_id}" (backward-compatible fallback)
    """
    from .deliberation_tracker import build_tracker_key as _tracker_build_key

    return _tracker_build_key(
        agent_id=agent_id,
        decision_id=decision_id,
        transport_key=f"rpc:{transport_agent_id}",
        principal=transport_agent_id,
    )


class CstpDispatcher:
    """Dispatches JSON-RPC requests to method handlers.

    Attributes:
        methods: Registry of method name -> handler.
    """

    def __init__(self) -> None:
        """Initialize empty dispatcher."""
        self._methods: dict[str, MethodHandler] = {}

    def register(self, method: str, handler: MethodHandler) -> None:
        """Register a method handler.

        Args:
            method: Method name (e.g., "cstp.queryDecisions").
            handler: Async function to handle the method.
        """
        self._methods[method] = handler

    async def dispatch(
        self,
        request: JsonRpcRequest,
        agent_id: str,
    ) -> JsonRpcResponse:
        """Dispatch a JSON-RPC request to the appropriate handler.

        Args:
            request: Validated JSON-RPC request.
            agent_id: Authenticated agent ID.

        Returns:
            JSON-RPC response with result or error.
        """
        # Validate request format
        validation_error = request.validate()
        if validation_error:
            return JsonRpcResponse.failure(request.id, validation_error)

        # Find handler
        handler = self._methods.get(request.method)
        if not handler:
            return JsonRpcResponse.failure(
                request.id,
                JsonRpcError(
                    code=METHOD_NOT_FOUND,
                    message=f"Method not found: {request.method}",
                    data={"method": request.method, "available": list(self._methods.keys())},
                ),
            )

        # Execute handler
        try:
            result = await handler(request.params, agent_id)
            return JsonRpcResponse.success(request.id, result)
        except ValueError as e:
            return JsonRpcResponse.failure(
                request.id,
                JsonRpcError(
                    code=INVALID_PARAMS,
                    message=str(e),
                ),
            )
        except PermissionDeniedError as e:
            return JsonRpcResponse.failure(
                request.id,
                JsonRpcError(code=PERMISSION_DENIED, message=str(e)),
            )
        except Exception:
            # Unexpected errors can carry paths, SQL or backend detail, so the
            # caller gets a reference to the server log instead of the message.
            ref = uuid.uuid4().hex[:12]
            logger.exception("%s failed (ref %s)", request.method, ref)
            return JsonRpcResponse.failure(
                request.id,
                JsonRpcError(
                    code=INTERNAL_ERROR,
                    message=f"Internal error (ref {ref})",
                ),
            )


# Global dispatcher instance
_dispatcher: CstpDispatcher | None = None


def get_dispatcher() -> CstpDispatcher:
    """Get the global dispatcher instance.

    Returns:
        The CSTP dispatcher.
    """
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = CstpDispatcher()
    return _dispatcher


def _annotate_compaction_levels(result: QueryDecisionsResponse) -> None:
    """Annotate DecisionSummary items with compaction level (F041 P2).

    Determines compaction level from each decision's date and status,
    sets compaction_level on each summary, and filters out wisdom-level
    decisions (those are aggregated via cstp.getWisdom instead).

    Mutates result in place.
    """
    from .compaction_service import determine_compaction_level

    annotated: list[DecisionSummary] = []
    for d in result.decisions:
        # Build a minimal decision dict for determine_compaction_level
        decision_dict: dict[str, Any] = {
            "date": d.date,
            "status": d.status,
        }
        level = determine_compaction_level(decision_dict)
        d.compaction_level = level
        # Exclude wisdom-level from individual results
        if level != "wisdom":
            annotated.append(d)
    result.decisions = annotated
    result.total = len(annotated)


async def _handle_query_decisions(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.queryDecisions method.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Query results as dict.
    """
    import time
    start_time = time.time()

    # Parse request
    request = QueryDecisionsRequest.from_params(params)

    # F017: Handle different retrieval modes
    scores: dict[str, dict[str, float]] = {}

    # Handle empty query - list all decisions (for dashboard)
    if not request.query.strip():
        all_decisions = await load_all_decisions(
            category=request.filters.category,
            project=request.filters.project,
        )
        # Sort by date descending, limit results
        all_decisions.sort(
            key=lambda d: d.get("created_at", ""),
            reverse=True,
        )
        all_decisions = all_decisions[:request.limit]

        decisions = []
        for d in all_decisions:
            doc_id = d.get("id", "")
            decisions.append(
                DecisionSummary(
                    id=doc_id[:8] if len(doc_id) > 8 else doc_id,
                    title=d.get("summary", d.get("decision", "Untitled"))[:50],
                    category=d.get("category", ""),
                    confidence=d.get("confidence"),
                    stakes=d.get("stakes"),
                    status=d.get("status", ""),
                    outcome=d.get("outcome"),
                    date=d.get("created_at", "")[:10],
                    distance=0.0,
                    reasons=d.get("reasons") if request.include_reasons else None,
                    tags=d.get("tags"),
                    pattern=d.get("pattern"),
                    lessons=d.get("lessons"),
                    actual_result=(
                        d.get("actual_result") if request.include_detail else None
                    ),
                    bridge=_extract_bridge(d),
                )
            )

        query_time_ms = int((time.time() - start_time) * 1000)

        result = QueryDecisionsResponse(
            decisions=decisions,
            total=len(decisions),
            query=request.query,
            query_time_ms=query_time_ms,
            agent="cognition-engines",
            retrieval_mode="list",
            scores={},
        )

        # F041 P2: Annotate with compaction levels and filter wisdom
        if request.compacted:
            _annotate_compaction_levels(result)

        # F023 Phase 2: Track query for auto-deliberation
        from .deliberation_tracker import track_query

        track_query(
            key=f"rpc:{agent_id}",
            query=request.query,
            result_count=result.total,
            top_ids=[d.id for d in result.decisions[:5]],
            retrieval_mode="list",
            top_results=[
                {"id": d.id, "summary": d.title[:100], "distance": d.distance}
                for d in result.decisions[:5]
            ],
        )

        return result.to_dict()

    if request.retrieval_mode == "keyword":
        # Keyword-only search via BM25
        all_decisions = await load_all_decisions(
            category=request.filters.category,
            project=request.filters.project,
        )
        # Use cached index for performance
        cache_key = f"kw:{request.filters.category}:{request.filters.project}"
        bm25_index = get_cached_index(all_decisions, cache_key)
        keyword_results = bm25_index.search(request.query, request.limit)

        # Build decision map for quick lookup
        decision_map = {d["id"]: d for d in all_decisions}

        decisions = []
        for doc_id, score in keyword_results:
            d = decision_map.get(doc_id, {})
            decisions.append(
                DecisionSummary(
                    id=doc_id[:8] if len(doc_id) > 8 else doc_id,
                    title=d.get("summary", d.get("decision", "Untitled"))[:50],
                    category=d.get("category", ""),
                    confidence=d.get("confidence"),
                    stakes=d.get("stakes"),
                    status=d.get("status", ""),
                    outcome=d.get("outcome"),
                    date=d.get("created_at", "")[:10],
                    distance=round(1.0 - score / 10.0, 4),  # Approximate distance
                    reasons=d.get("reasons") if request.include_reasons else None,
                    tags=d.get("tags"),
                    pattern=d.get("pattern"),
                    lessons=d.get("lessons"),
                    actual_result=(
                        d.get("actual_result") if request.include_detail else None
                    ),
                    bridge=_extract_bridge(d),
                )
            )
            scores[doc_id[:8] if len(doc_id) > 8 else doc_id] = {
                "semantic": 0.0,
                "keyword": round(score, 4),
                "combined": round(score, 4),
            }

        query_time_ms = int((time.time() - start_time) * 1000)

    elif request.retrieval_mode == "hybrid":
        # Hybrid: combine semantic + keyword
        # First, get semantic results
        response = await query_decisions(
            query=request.effective_query,
            n_results=request.limit * 2,  # Get more for merging
            category=request.filters.category,
            min_confidence=request.filters.min_confidence if request.filters.min_confidence > 0 else None,
            max_confidence=request.filters.max_confidence if request.filters.max_confidence < 1 else None,
            stakes=request.filters.stakes,
            status_filter=request.filters.status,
            project=request.filters.project,
            feature=request.filters.feature,
            pr=request.filters.pr,
            has_outcome=request.filters.has_outcome,
            tags=request.filters.tags,
        )

        if response.error:
            raise RuntimeError(response.error)

        # Convert semantic results to (id, score) format
        # Distance is inverse of similarity, so convert
        semantic_results = [
            (r.id, 1.0 - r.distance) for r in response.results
        ]

        # Get keyword results
        all_decisions = await load_all_decisions(
            category=request.filters.category,
            project=request.filters.project,
        )
        # Use cached index for performance
        cache_key = f"hybrid:{request.filters.category}:{request.filters.project}"
        bm25_index = get_cached_index(all_decisions, cache_key)
        keyword_results = bm25_index.search(request.query, request.limit * 2)

        # Merge results
        merged = merge_results(
            semantic_results,
            keyword_results,
            semantic_weight=request.hybrid_weight,
            top_k=request.limit,
        )

        # Build response from merged results
        decision_map = {d["id"][:8]: d for d in all_decisions}
        decision_map.update({d["id"]: d for d in all_decisions})

        # Also map from semantic results
        semantic_map = {r.id: r for r in response.results}

        decisions = []
        for doc_id, score_dict in merged:
            # Try semantic result first
            if doc_id in semantic_map:
                r = semantic_map[doc_id]
                decisions.append(
                    DecisionSummary(
                        id=r.id,
                        title=r.title,
                        category=r.category,
                        confidence=r.confidence,
                        stakes=r.stakes,
                        status=r.status,
                        outcome=r.outcome,
                        date=r.date,
                        distance=round(1.0 - score_dict["combined"], 4),
                        reasons=r.reasons if request.include_reasons else None,
                        tags=r.tags,
                        pattern=r.pattern,
                        lessons=r.lessons,
                        actual_result=(
                            r.actual_result if request.include_detail else None
                        ),
                        bridge=r.bridge,
                    )
                )
            elif doc_id in decision_map:
                d = decision_map[doc_id]
                decisions.append(
                    DecisionSummary(
                        id=doc_id[:8] if len(doc_id) > 8 else doc_id,
                        title=d.get("summary", d.get("decision", "Untitled"))[:50],
                        category=d.get("category", ""),
                        confidence=d.get("confidence"),
                        stakes=d.get("stakes"),
                        status=d.get("status", ""),
                        outcome=d.get("outcome"),
                        date=d.get("created_at", "")[:10],
                        distance=round(1.0 - score_dict["combined"], 4),
                        reasons=d.get("reasons") if request.include_reasons else None,
                        tags=d.get("tags"),
                        pattern=d.get("pattern"),
                        lessons=d.get("lessons"),
                        actual_result=(
                            d.get("actual_result") if request.include_detail else None
                        ),
                        bridge=_extract_bridge(d),
                    )
                )
            scores[doc_id] = score_dict

        query_time_ms = int((time.time() - start_time) * 1000)

    else:
        # Default: semantic-only search
        response = await query_decisions(
            query=request.effective_query,
            n_results=request.limit,
            category=request.filters.category,
            min_confidence=request.filters.min_confidence if request.filters.min_confidence > 0 else None,
            max_confidence=request.filters.max_confidence if request.filters.max_confidence < 1 else None,
            stakes=request.filters.stakes,
            status_filter=request.filters.status,
            project=request.filters.project,
            feature=request.filters.feature,
            pr=request.filters.pr,
            has_outcome=request.filters.has_outcome,
            tags=request.filters.tags,
        )

        if response.error:
            raise RuntimeError(response.error)

        decisions = [
            DecisionSummary(
                id=r.id,
                title=r.title,
                category=r.category,
                confidence=r.confidence,
                stakes=r.stakes,
                status=r.status,
                outcome=r.outcome,
                date=r.date,
                distance=r.distance,
                reasons=r.reasons if request.include_reasons else None,
                tags=r.tags,
                pattern=r.pattern,
                lessons=r.lessons,
                actual_result=r.actual_result if request.include_detail else None,
                bridge=r.bridge,
            )
            for r in response.results
        ]
        query_time_ms = response.query_time_ms

    result = QueryDecisionsResponse(
        decisions=decisions,
        total=len(decisions),
        query=request.query,
        query_time_ms=query_time_ms,
        agent="cognition-engines",
        retrieval_mode=request.retrieval_mode,
        scores=scores if scores else {},
    )

    # F041 P2: Annotate with compaction levels and filter wisdom
    if request.compacted:
        _annotate_compaction_levels(result)

    # F023 Phase 2: Track query for auto-deliberation
    from .deliberation_tracker import track_query

    track_query(
        key=f"rpc:{agent_id}",
        query=request.query,
        result_count=result.total,
        top_ids=[d.id for d in result.decisions[:5]],
        retrieval_mode=request.retrieval_mode,
        top_results=[
            {"id": d.id, "summary": d.title[:100], "distance": d.distance}
            for d in result.decisions[:5]
        ],
    )

    return result.to_dict()


async def _handle_check_guardrails(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.checkGuardrails method.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Guardrail check results as dict.
    """
    # Parse request
    request = CheckGuardrailsRequest.from_params(params)

    # Evaluate guardrails
    eval_result = await evaluate_guardrails(request.action.guardrail_context(agent_id))

    # Audit log
    log_guardrail_check(
        requesting_agent=agent_id,
        action_description=request.action.description,
        allowed=eval_result.allowed,
        violations=eval_result.violations,
        evaluated=eval_result.evaluated,
    )

    # Map to response format
    violations = [
        GuardrailViolation(
            guardrail_id=v.guardrail_id,
            name=v.name,
            message=v.message,
            severity=v.severity,
            suggestion=v.suggestion,
        )
        for v in eval_result.violations
    ]

    warnings = [
        GuardrailViolation(
            guardrail_id=w.guardrail_id,
            name=w.name,
            message=w.message,
            severity=w.severity,
            suggestion=w.suggestion,
        )
        for w in eval_result.warnings
    ]

    # F030: Enrich circuit breaker violations with F030-specific fields
    try:
        from .circuit_breaker_service import get_circuit_breaker_manager

        mgr = await get_circuit_breaker_manager()
        if mgr.is_initialized:
            for v in violations:
                if v.guardrail_id.startswith("circuit_breaker:"):
                    scope = v.guardrail_id[len("circuit_breaker:"):]
                    state_info = await mgr.get_state(scope)
                    if state_info:
                        v.type = "circuit_breaker"
                        v.state = state_info["state"]
                        threshold = state_info.get("failure_threshold", 1)
                        v.failure_rate = (
                            state_info["failure_count"] / max(threshold, 1)
                        )
                        if state_info.get("cooldown_remaining_ms") is not None:
                            from datetime import timedelta
                            reset_dt = (
                                datetime.now(UTC)
                                + timedelta(
                                    milliseconds=state_info["cooldown_remaining_ms"],
                                )
                            )
                            v.reset_at = reset_dt.isoformat()
    except Exception:
        logger.debug(
            "Circuit breaker enrichment failed", exc_info=True,
        )

    result = CheckGuardrailsResponse(
        allowed=eval_result.allowed,
        violations=violations,
        warnings=warnings,
        evaluated=eval_result.evaluated,
        evaluated_at=datetime.now(UTC),
        agent="cognition-engines",
    )

    # F023 Phase 2: Track guardrail check for auto-deliberation
    from .deliberation_tracker import track_guardrail

    track_guardrail(
        key=f"rpc:{agent_id}",
        description=request.action.description,
        allowed=eval_result.allowed,
        violation_count=len(eval_result.violations),
    )

    return result.to_dict()


async def _handle_list_guardrails(params: dict[str, Any], _agent_id: str) -> dict[str, Any]:
    """Handle cstp.listGuardrails method.

    Args:
        params: JSON-RPC params.
        _agent_id: Authenticated agent ID (unused).

    Returns:
        List of active guardrails.
    """
    scope = params.get("scope")
    guardrails = list_guardrails(scope=scope)

    return {
        "guardrails": guardrails,
        "count": len(guardrails),
        "agent": "cognition-engines"
    }


async def _handle_pre_action(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.preAction method (F046).

    Combines query + guardrails + calibration + optional record in one call.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Pre-action results as dict.
    """
    request = PreActionRequest.from_params(params)
    response = await pre_action(request, agent_id, tracker_key=f"rpc:{agent_id}")
    return response.to_dict()


async def _handle_get_session_context(
    params: dict[str, Any], agent_id: str,
) -> dict[str, Any]:
    """Handle cstp.getSessionContext method (F047).

    Returns full cognitive context for session start.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Session context as dict.
    """
    request = SessionContextRequest.from_params(params)
    response = await get_session_context(request, agent_id)
    return response.to_dict()


async def _handle_ready(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.ready method (F044).

    Returns prioritized cognitive actions (reviews, drift, stale decisions).

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Ready response with prioritized actions.
    """
    from .models import ReadyRequest
    from .ready_service import get_ready_actions

    request = ReadyRequest.from_params(params)
    response = await get_ready_actions(request, agent_id=agent_id)
    return response.to_dict()


async def _handle_link_decisions(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.linkDecisions method (F045 P1).

    Creates a typed edge between two decisions.

    Args:
        params: {"sourceId": "...", "targetId": "...", "edgeType": "...", ...}
        agent_id: Authenticated agent ID.

    Returns:
        Link response with the created edge.
    """
    from .graph_service import link_decisions
    from .models import LinkDecisionsRequest

    request = LinkDecisionsRequest.from_params(params)
    errors = request.validate()
    if errors:
        raise ValueError("; ".join(errors))

    response = await link_decisions(
        source_id=request.source_id,
        target_id=request.target_id,
        edge_type=request.edge_type,
        weight=request.weight,
        context=request.context,
        agent_id=agent_id,
    )
    return response.to_dict()


async def _handle_get_graph(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.getGraph method (F045 P1).

    Returns subgraph around a decision node.

    Args:
        params: {"nodeId": "...", "depth": 1, "edgeTypes": [...], "direction": "both"}
        agent_id: Authenticated agent ID.

    Returns:
        Subgraph with nodes and edges.
    """
    from .graph_service import get_graph
    from .models import GetGraphRequest

    request = GetGraphRequest.from_params(params)
    errors = request.validate()
    if errors:
        raise ValueError("; ".join(errors))

    response = await get_graph(
        node_id=request.node_id,
        depth=request.depth,
        edge_types=request.edge_types,
        direction=request.direction,
    )
    return response.to_dict()


async def _handle_get_neighbors(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.getNeighbors method (F045 follow-up).

    Returns immediate neighbors of a decision node.

    Args:
        params: {"nodeId": "...", "direction": "both", "edgeType": "...", "limit": 20}
        agent_id: Authenticated agent ID.

    Returns:
        Neighbor list with connecting edges.
    """
    from .graph_service import get_neighbors
    from .models import GetNeighborsRequest

    request = GetNeighborsRequest.from_params(params)
    errors = request.validate()
    if errors:
        raise ValueError("; ".join(errors))

    response = await get_neighbors(
        node_id=request.node_id,
        direction=request.direction,
        edge_type=request.edge_type,
        limit=request.limit,
    )
    return response.to_dict()


async def _handle_compact(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.compact method (F041 P1).

    Runs compaction cycle — recalculates levels for all decisions.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Compaction results with level counts.
    """
    from .compaction_service import run_compaction
    from .models import CompactRequest

    request = CompactRequest.from_params(params)
    response = await run_compaction(request)
    return response.to_dict()


async def _handle_get_compacted(
    params: dict[str, Any], agent_id: str,
) -> dict[str, Any]:
    """Handle cstp.getCompacted method (F041 P1).

    Returns decisions shaped at their appropriate compaction level.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Compacted decisions with level metadata.
    """
    from .compaction_service import get_compacted_decisions
    from .models import GetCompactedRequest

    request = GetCompactedRequest.from_params(params)
    response = await get_compacted_decisions(request)
    return response.to_dict()


async def _handle_set_preserve(
    params: dict[str, Any], agent_id: str,
) -> dict[str, Any]:
    """Handle cstp.setPreserve method (F041 P1).

    Marks a decision as never-compact (or removes the mark).

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Preserve status result.
    """
    from .compaction_service import set_preserve
    from .models import SetPreserveRequest

    request = SetPreserveRequest.from_params(params)
    errors = request.validate()
    if errors:
        raise ValueError("; ".join(errors))

    await require_decision_owner(request.decision_id, agent_id)
    response = await set_preserve(request)
    return response.to_dict()


async def _handle_get_wisdom(
    params: dict[str, Any], agent_id: str,
) -> dict[str, Any]:
    """Handle cstp.getWisdom method (F041 P1).

    Returns category-level distilled principles from old decisions.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Wisdom entries with principles and statistics.
    """
    from .compaction_service import get_wisdom
    from .models import GetWisdomRequest

    request = GetWisdomRequest.from_params(params)
    response = await get_wisdom(request)
    return response.to_dict()


async def _handle_list_decisions(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.listDecisions method (F050).

    Server-side filtered, sorted, paginated list of decisions.

    Args:
        params: JSON-RPC params with filter/sort/pagination fields.
        agent_id: Authenticated agent ID.

    Returns:
        Paginated list of decisions with total count.
    """
    from .storage import ListQuery
    from .storage.factory import get_decision_store

    request = ListDecisionsRequest.from_params(params)
    store = get_decision_store()
    query = ListQuery(
        limit=request.limit,
        offset=request.offset,
        category=request.category,
        stakes=request.stakes,
        status=request.status,
        agent=request.agent,
        tags=request.tags,
        project=request.project,
        date_from=request.date_from,
        date_to=request.date_to,
        search=request.search,
        sort=request.sort,
        order=request.order,
    )
    result = await store.list(query)
    response = ListDecisionsResponse(
        decisions=result.decisions,
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )
    return response.to_dict()


async def _handle_get_stats(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.getStats method (F050).

    Server-side aggregated statistics over decisions.

    Args:
        params: JSON-RPC params with optional date range and project.
        agent_id: Authenticated agent ID.

    Returns:
        Aggregated decision statistics.
    """
    from .storage import StatsQuery
    from .storage.factory import get_decision_store

    request = GetStatsRequest.from_params(params)
    store = get_decision_store()
    query = StatsQuery(
        date_from=request.date_from,
        date_to=request.date_to,
        project=request.project,
    )
    result = await store.stats(query)
    response = GetStatsResponse(
        total=result.total,
        by_category=result.by_category,
        by_stakes=result.by_stakes,
        by_status=result.by_status,
        by_agent=result.by_agent,
        by_day=result.by_day,
        top_tags=result.top_tags,
        recent_activity=result.recent_activity,
    )
    return response.to_dict()


async def _handle_list_breakers(
    params: dict[str, Any], agent_id: str,
) -> dict[str, Any]:
    """Handle cstp.listBreakers method (F030).

    Returns all circuit breakers with their current state.

    Args:
        params: JSON-RPC params (unused).
        agent_id: Authenticated agent ID.

    Returns:
        Dict with 'breakers' list.
    """
    from .circuit_breaker_service import get_circuit_breaker_manager

    mgr = await get_circuit_breaker_manager()
    breakers = await mgr.list_breakers()
    return {"breakers": breakers}


async def _handle_get_circuit_state(
    params: dict[str, Any], agent_id: str,
) -> dict[str, Any]:
    """Handle cstp.getCircuitState method (F030).

    Returns the current state of a circuit breaker by scope.

    Args:
        params: JSON-RPC params with 'scope' field.
        agent_id: Authenticated agent ID.

    Returns:
        Circuit breaker state as dict.
    """
    from .circuit_breaker_service import get_circuit_breaker_manager

    request = GetCircuitStateRequest.from_params(params)
    mgr = await get_circuit_breaker_manager()
    state = await mgr.get_state(request.scope)
    if state is None:
        raise ValueError(
            f"No circuit breaker found for scope: {request.scope}"
        )

    response = GetCircuitStateResponse(
        scope=state["scope"],
        state=state["state"],
        failure_count=state["failure_count"],
        failure_threshold=state["failure_threshold"],
        window_ms=state["window_ms"],
        cooldown_ms=state["cooldown_ms"],
        cooldown_remaining_ms=state.get("cooldown_remaining_ms"),
        opened_at=(
            str(state["opened_at"]) if state.get("opened_at") else None
        ),
    )
    return response.to_dict()


async def _handle_reset_circuit(
    params: dict[str, Any], agent_id: str,
) -> dict[str, Any]:
    """Handle cstp.resetCircuit method (F030).

    Manually resets an OPEN circuit breaker.

    Args:
        params: JSON-RPC params with 'scope' and optional
            'probeFirst' fields.
        agent_id: Authenticated agent ID.

    Returns:
        Reset result with previous and new state.
    """
    from .circuit_breaker_service import get_circuit_breaker_manager

    require_admin(agent_id, "cstp.resetCircuit")
    request = ResetCircuitRequest.from_params(params)
    mgr = await get_circuit_breaker_manager()
    result = await mgr.reset(
        request.scope, probe_first=request.probe_first,
    )

    if "error" in result:
        raise ValueError(result["error"])

    response = ResetCircuitResponse(
        scope=result["scope"],
        previous_state=result["previous_state"],
        new_state=result["new_state"],
        message=(
            f"Circuit breaker {request.scope} reset: "
            f"{result['previous_state']} -> {result['new_state']}"
        ),
    )
    return response.to_dict()


# ── F055: Decision Provenance handlers ───────────────────────────────────────


async def _handle_ingest_evidence(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.ingestEvidence method (F055).

    Admin only: ingested events are "observed" evidence, which control
    mappings trust as coming from a system the decision's agent does not
    control (e.g. a human PR approval).
    """
    require_admin(agent_id, "cstp.ingestEvidence")
    return await ingest_evidence(params, agent_id)


async def _handle_link_evidence(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.linkEvidence method (F055)."""
    return await link_evidence(params, agent_id)


async def _handle_map_controls(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.mapControls method (F055)."""
    return await map_controls(params, agent_id)


async def _handle_export_evidence_bundle(
    params: dict[str, Any], agent_id: str
) -> dict[str, Any]:
    """Handle cstp.exportEvidenceBundle method (F055).

    Admin only: it writes bundle files on the server and records the chain
    head that verifyEvidenceChain later checks against.
    """
    require_admin(agent_id, "cstp.exportEvidenceBundle")
    return await export_evidence_bundle(params, agent_id)


async def _handle_verify_evidence_chain(
    params: dict[str, Any], agent_id: str
) -> dict[str, Any]:
    """Handle cstp.verifyEvidenceChain method (F055)."""
    return await verify_evidence_chain(params, agent_id)


def register_methods(dispatcher: CstpDispatcher) -> None:
    """Register all CSTP method handlers.

    Args:
        dispatcher: Dispatcher to register methods on.
    """
    dispatcher.register("cstp.queryDecisions", _handle_query_decisions)
    dispatcher.register("cstp.checkGuardrails", _handle_check_guardrails)
    dispatcher.register("cstp.listGuardrails", _handle_list_guardrails)
    dispatcher.register("cstp.recordDecision", _handle_record_decision)
    dispatcher.register("cstp.updateDecision", _handle_update_decision)
    dispatcher.register("cstp.recordThought", _handle_record_thought)
    dispatcher.register("cstp.getDecision", _handle_get_decision)

    dispatcher.register("cstp.reviewDecision", _handle_review_decision)
    dispatcher.register("cstp.getCalibration", _handle_get_calibration)
    dispatcher.register("cstp.attributeOutcomes", _handle_attribute_outcomes)
    dispatcher.register("cstp.checkDrift", _handle_check_drift)
    dispatcher.register("cstp.reindex", _handle_reindex)
    dispatcher.register("cstp.getReasonStats", _handle_get_reason_stats)

    # F046/F047: Agentic loop integration
    dispatcher.register("cstp.preAction", _handle_pre_action)
    dispatcher.register("cstp.getSessionContext", _handle_get_session_context)

    # F044: Agent Work Discovery
    dispatcher.register("cstp.ready", _handle_ready)

    # F045: Decision Graph Storage Layer
    dispatcher.register("cstp.linkDecisions", _handle_link_decisions)
    dispatcher.register("cstp.getGraph", _handle_get_graph)
    dispatcher.register("cstp.getNeighbors", _handle_get_neighbors)

    # F041: Memory Compaction
    dispatcher.register("cstp.compact", _handle_compact)
    dispatcher.register("cstp.getCompacted", _handle_get_compacted)
    dispatcher.register("cstp.setPreserve", _handle_set_preserve)
    dispatcher.register("cstp.getWisdom", _handle_get_wisdom)

    # F050: Structured Storage Layer
    dispatcher.register("cstp.listDecisions", _handle_list_decisions)
    dispatcher.register("cstp.getStats", _handle_get_stats)

    # F030: Circuit Breaker
    dispatcher.register("cstp.listBreakers", _handle_list_breakers)
    dispatcher.register(
        "cstp.getCircuitState", _handle_get_circuit_state,
    )
    dispatcher.register(
        "cstp.resetCircuit", _handle_reset_circuit,
    )

    # F126: Debug Tracker
    dispatcher.register("cstp.debugTracker", _handle_debug_tracker)

    # F055: Decision Provenance & Control Evidence
    dispatcher.register("cstp.ingestEvidence", _handle_ingest_evidence)
    dispatcher.register("cstp.linkEvidence", _handle_link_evidence)
    dispatcher.register("cstp.mapControls", _handle_map_controls)
    dispatcher.register("cstp.exportEvidenceBundle", _handle_export_evidence_bundle)
    dispatcher.register("cstp.verifyEvidenceChain", _handle_verify_evidence_chain)


async def _handle_debug_tracker(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.debugTracker method (F126).

    Read-only peek at deliberation tracker state for debugging. Sessions hold
    agents' in-flight reasoning, so an agent sees only its own; admins see all.

    Args:
        params: JSON-RPC params with optional 'key' field.
        agent_id: Authenticated agent ID.

    Returns:
        Tracker debug info with sessions, counts, and input details.
    """
    from .deliberation_tracker import debug_tracker
    from .models import DebugTrackerRequest, DebugTrackerResponse

    params = params or {}
    request = DebugTrackerRequest.from_params(params)
    raw = debug_tracker(
        key=request.key,
        include_consumed=request.include_consumed,
        owner=None if is_admin(agent_id) else agent_id,
    )
    response = DebugTrackerResponse.from_raw(raw)
    return response.to_dict()


async def _handle_update_decision(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.updateDecision method (F027 backfill).

    Updates specific fields on an existing decision.

    Args:
        params: {"id": "abc123", "updates": {"tags": [...], "pattern": "..."}}
        agent_id: Authenticated agent ID.

    Returns:
        Update result with success status.
    """
    from .decision_service import update_decision

    decision_id = params.get("id") or params.get("decision_id", "")
    if not decision_id:
        raise ValueError("Missing required parameter: id")

    updates = params.get("updates", {})
    if not updates:
        raise ValueError("Missing required parameter: updates")

    await require_decision_owner(decision_id, agent_id)
    return await update_decision(decision_id, updates)


async def _handle_record_thought(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.recordThought method (F028 + F129).

    Records a reasoning/chain-of-thought step in the deliberation tracker.
    Three modes:
    - Post-decision (legacy): bare "id" param without F129 scoping params
      — appends thought to existing decision's deliberation trace.
    - Pre-decision with F129 scoping: agent_id and/or decision_id provided
      — thought accumulates under composite tracker key.
    - Pre-decision (legacy): no params — uses transport-derived key.

    F129: agent_id and decision_id are scoping keys for multi-agent
    deliberation isolation, NOT triggers for post-decision append.

    Args:
        params: {"text": "...", "agentId": "optional", "decisionId": "optional"}
        agent_id: Authenticated agent ID (from transport/auth).

    Returns:
        Acknowledgment with tracked input ID.
    """
    from .deliberation_tracker import track_reasoning

    request = RecordThoughtParams.from_params(params)

    # Post-decision append: only when bare "id" param is used WITHOUT
    # F129 scoping params (backward-compatible legacy path)
    legacy_decision_id = params.get("id")
    if legacy_decision_id and not request.agent_id and not request.decision_id:
        from .decision_service import append_thought

        await require_decision_owner(legacy_decision_id, agent_id)
        result = await append_thought(legacy_decision_id, request.text)
        if not result.get("success"):
            raise ValueError(result.get("error", "Unknown error"))
        return {
            "success": True,
            "mode": "post-decision",
            "decision_id": legacy_decision_id,
            "step_number": result["step_number"],
        }

    # Pre-decision: accumulate in tracker using composite key
    tracker_key = build_tracker_key(
        agent_id, agent_id=request.agent_id, decision_id=request.decision_id,
    )
    track_reasoning(
        tracker_key, request.text,
        agent_id=request.agent_id, decision_id=request.decision_id,
        principal=agent_id,
    )
    return {
        "success": True,
        "mode": "pre-decision",
        "tracker_key": tracker_key,
        "agent_id": request.agent_id or agent_id,
    }


async def _handle_get_decision(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.getDecision method.

    Retrieves full decision details by ID, including context, reasons,
    and all metadata stored in the YAML file.

    Args:
        params: JSON-RPC params with 'id' field.
        agent_id: Authenticated agent ID.

    Returns:
        Full decision data as dict.

    Raises:
        ValueError: If ID is missing or invalid.
    """
    request = GetDecisionRequest.from_dict(params)
    response = await get_decision(request)

    if not response.found:
        raise ValueError(response.error or f"Decision not found: {request.decision_id}")

    # F023 Phase 2: Track decision lookup for auto-deliberation
    from .deliberation_tracker import track_lookup

    dec = response.decision or {}
    track_lookup(
        key=f"rpc:{agent_id}",
        decision_id=request.decision_id,
        title=dec.get("summary", dec.get("decision", ""))[:50],
    )

    return response.to_dict()


async def _handle_reindex(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.reindex method.

    Reindexes all decisions with fresh embeddings.
    This will delete and recreate the ChromaDB collection.

    Args:
        params: JSON-RPC params (unused).
        agent_id: Authenticated agent ID.

    Returns:
        Reindex result as dict.
    """
    require_admin(agent_id, "cstp.reindex")
    result = await reindex_decisions()
    return result.to_dict()


async def _handle_record_decision(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.recordDecision method.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Record result as dict.

    Raises:
        ValueError: If validation fails.
        RuntimeError: If recording fails.
    """
    # Parse and validate request
    request = RecordDecisionRequest.from_dict(params, agent_id=agent_id)

    # F129: Build composite tracker key from client-provided agent_id/decision_id
    client_agent_id = params.get("agentId") or params.get("agent_id")
    client_decision_id = params.get("decisionId") or params.get("decision_id")
    tracker_key = build_tracker_key(
        agent_id, agent_id=client_agent_id, decision_id=client_decision_id,
    )

    # F025: Extract related decisions BEFORE consuming tracker
    from .deliberation_tracker import extract_related_from_tracker

    if not request.related_to:
        related_raw = extract_related_from_tracker(
            tracker_key, agent_id=client_agent_id, decision_id=client_decision_id,
            principal=agent_id,
        )
        if related_raw:
            from .decision_service import RelatedDecision

            request.related_to = [
                RelatedDecision.from_dict(r) for r in related_raw
            ]

    # F023 Phase 2: Auto-attach deliberation from tracked inputs
    from .deliberation_tracker import auto_attach_deliberation

    request.deliberation, auto_captured = auto_attach_deliberation(
        key=tracker_key,
        deliberation=request.deliberation,
        agent_id=client_agent_id,
        decision_id=client_decision_id,
        principal=agent_id,
    )

    # Validate before bridge extraction, which runs regexes over the raw text.
    errors = request.validate()
    if errors:
        raise ValueError(f"Validation failed: {'; '.join(errors)}")

    # F027 P2: Smart bridge extraction (replaces F024 Phase 3)
    from .bridge_hook import maybe_smart_extract_bridge

    bridge_auto, bridge_method = await maybe_smart_extract_bridge(request)

    # Record the decision
    response = await record_decision(request)

    # F149: Backfill consumed history with decision_id
    if response.success and response.id:
        from .deliberation_tracker import get_tracker
        get_tracker().backfill_consumed(tracker_key, response.id)

    # F045 follow-up: Auto-link decision in graph
    auto_linked = 0
    if response.success and response.id:
        from .graph_service import safe_auto_link

        related_dicts = (
            [r.to_dict() for r in request.related_to] if request.related_to else []
        )
        auto_linked = await safe_auto_link(
            response_id=response.id,
            category=request.category,
            stakes=request.stakes,
            confidence=request.confidence,
            tags=list(request.tags),
            pattern=request.pattern,
            related_to=related_dicts,
            summary=str(request.decision)[:120],
        )

    if not response.success:
        raise RuntimeError(response.error or "Failed to record decision")

    # Add auto-deliberation info to response only if auto-capture happened
    result = response.to_dict()
    if auto_linked > 0:
        result["graph_edges_created"] = auto_linked
    if auto_captured and request.deliberation:
        result["deliberation_auto"] = True
        result["deliberation_inputs_count"] = len(request.deliberation.inputs)

    # F026: Run guardrails against record context (supports deliberation checks)
    from .guardrails_service import evaluate_record_guardrails
    record_warnings = await evaluate_record_guardrails(request)
    if record_warnings:
        result["guardrail_warnings"] = record_warnings

    if bridge_auto and request.bridge:
        result["bridge_auto"] = True
        result["bridge_method"] = bridge_method

    if request.related_to:
        result["related_count"] = len(request.related_to)

    return result


async def _handle_review_decision(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.reviewDecision method.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID (reviewer).

    Returns:
        Review result as dict.

    Raises:
        ValueError: If validation fails.
        RuntimeError: If review fails.
    """
    # Parse and validate request
    request = ReviewDecisionRequest.from_dict(params, reviewer_id=agent_id)

    errors = request.validate()
    if errors:
        raise ValueError(f"Validation failed: {'; '.join(errors)}")

    await require_decision_owner(request.id, agent_id)

    # Review the decision
    response = await review_decision(request)

    if not response.success:
        raise RuntimeError(response.error or "Failed to review decision")

    result = response.to_dict()

    # F041 P2: Annotate with compaction level after review
    try:
        from .compaction_service import determine_compaction_level
        from .decision_service import find_decision

        found = await find_decision(request.id)
        if found:
            _, decision_data = found
            level = determine_compaction_level(decision_data)
            result["compactionLevel"] = level
    except Exception:
        logger.debug(
            "Failed to annotate compaction level for %s",
            request.id, exc_info=True,
        )

    # F030: Record outcome for circuit breaker tracking
    try:
        from .circuit_breaker_service import get_circuit_breaker_manager
        from .decision_service import find_decision as _find_decision

        mgr = await get_circuit_breaker_manager()
        if mgr.is_initialized:
            found_dec = await _find_decision(request.id)
            if found_dec:
                _, dec_data = found_dec
                cb_context: dict[str, Any] = {
                    "category": dec_data.get("category"),
                    "stakes": dec_data.get("stakes"),
                    # The outcome counts against the agent that recorded the
                    # decision, not whoever reviews it (an admin, the dashboard).
                    "agent_id": dec_data.get("recorded_by"),
                    "tags": dec_data.get("tags", []),
                }
                await mgr.record_outcome(
                    cb_context, request.outcome,
                )
    except Exception:
        logger.debug(
            "Circuit breaker outcome recording failed for %s",
            request.id, exc_info=True,
        )

    return result


async def _handle_get_calibration(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.getCalibration method.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Calibration statistics as dict.
    """
    request = GetCalibrationRequest.from_dict(params)
    response = await get_calibration(request)
    return response.to_dict()


async def _handle_attribute_outcomes(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.attributeOutcomes method.

    Args:
        params: JSON-RPC params.
        agent_id: Authenticated agent ID.

    Returns:
        Attribution results as dict.

    Raises:
        ValueError: If validation fails.
    """
    # Writes outcomes onto every matching decision, whoever recorded it.
    require_admin(agent_id, "cstp.attributeOutcomes")
    request = AttributeOutcomesRequest.from_dict(params)
    response = await attribute_outcomes(request)
    return response.to_dict()


async def _handle_check_drift(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.checkDrift method.

    Compares recent (30d) vs historical (90d+) calibration to detect drift.

    Args:
        params: JSON-RPC params with thresholds and filters.
        agent_id: Authenticated agent ID.

    Returns:
        Drift check results as dict.
    """
    request = CheckDriftRequest.from_dict(params)
    response = await check_drift(request)
    return response.to_dict()


async def _handle_get_reason_stats(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Handle cstp.getReasonStats method.

    Analyzes which reason types correlate with better outcomes.
    Implements Minsky Ch 18 parallel bundle analysis.

    Args:
        params: JSON-RPC params with optional filters.
        agent_id: Authenticated agent ID.

    Returns:
        Reason-type calibration statistics as dict.
    """
    request = GetReasonStatsRequest.from_dict(params)
    response = await get_reason_stats(request)

    # F023 Phase 2: Track stats lookup for auto-deliberation
    from .deliberation_tracker import track_stats

    result = response.to_dict()
    track_stats(
        key=f"rpc:{agent_id}",
        total_decisions=result.get("totalDecisions", 0),
        reason_type_count=len(result.get("byReasonType", [])),
        diversity=result.get("diversity", {}).get("avgTypesPerDecision"),
    )

    return result
