"""F046: Pre-action hook service.

Composes query, guardrails, calibration, and optional record into a single call.
Designed to be the one call an agent makes before any significant decision.
"""

import asyncio
import logging
import time
from typing import Any

from .calibration_service import GetCalibrationRequest, get_calibration
from .decision_service import RecordDecisionRequest, record_decision
from .guardrails_service import evaluate_guardrails, log_guardrail_check
from .models import (
    CalibrationContext,
    DecisionSummary,
    GuardrailViolation,
    PatternSummary,
    PreActionRequest,
    PreActionResponse,
)
from .query_service import query_decisions

logger = logging.getLogger("cstp.preaction")


async def pre_action(
    request: PreActionRequest,
    agent_id: str,
    tracker_key: str | None = None,
) -> PreActionResponse:
    """Execute the pre-action hook.

    Steps:
    1. Query similar past decisions (semantic search)
    2. Evaluate guardrails against proposed action
    3. Fetch calibration for this category
    4. Extract patterns from matched decisions
    5. Optionally record the decision if allowed

    Args:
        request: Parsed PreActionRequest.
        agent_id: Authenticated agent ID.
        tracker_key: Deliberation tracker key (e.g. "rpc:agent" or "mcp:client").
            Defaults to ``f"rpc:{agent_id}"`` for backward compatibility.

    Returns:
        PreActionResponse with allowed flag, decisions, guardrails, calibration.
    """
    if tracker_key is None:
        tracker_key = f"rpc:{agent_id}"
    start_time = time.time()

    action = request.action
    options = request.options

    # --- Build inputs for concurrent calls ---

    # Guardrail evaluation context
    guardrail_context: dict[str, Any] = {
        "category": action.category,
        "stakes": action.stakes,
        "confidence": action.confidence,
    }
    if action.context:
        guardrail_context.update(action.context)

    # Calibration request scoped to this category
    cal_request = GetCalibrationRequest(category=action.category)

    # --- Run query, guardrails, calibration concurrently ---
    query_result, guardrail_result, calibration_result = await asyncio.gather(
        query_decisions(
            query=action.description,
            n_results=options.query_limit,
            category=action.category,
        ),
        evaluate_guardrails(guardrail_context),
        get_calibration(cal_request),
        return_exceptions=True,
    )

    # --- Process query results ---
    relevant_decisions: list[DecisionSummary] = []
    if isinstance(query_result, Exception):
        logger.warning("Query failed in pre_action: %s", query_result)
    elif query_result.error:
        logger.warning("Query returned error: %s", query_result.error)
    else:
        for r in query_result.results:
            relevant_decisions.append(DecisionSummary(
                id=r.id,
                title=r.title,
                category=r.category,
                confidence=r.confidence,
                stakes=r.stakes,
                status=r.status,
                outcome=r.outcome,
                date=r.date,
                distance=r.distance,
                reasons=r.reasons,
                tags=r.tags,
                pattern=r.pattern,
                lessons=r.lessons,
                bridge=r.bridge,
            ))

    # F023/F049: Track query for deliberation (issue #159)
    if not isinstance(query_result, Exception) and not query_result.error:
        from .deliberation_tracker import track_query

        track_query(
            key=tracker_key,
            query=action.description,
            result_count=len(query_result.results),
            top_ids=[r.id for r in query_result.results[:5]],
            retrieval_mode="hybrid",
            top_results=[
                {"id": r.id, "summary": r.title[:80], "distance": r.distance}
                for r in query_result.results[:5]
            ],
        )

    # F041 P2: Always annotate with compaction level in pre_action
    if relevant_decisions:
        try:
            from .compaction_service import determine_compaction_level

            for d in relevant_decisions:
                decision_dict: dict[str, Any] = {
                    "date": d.date,
                    "status": d.status,
                }
                d.compaction_level = determine_compaction_level(decision_dict)
        except Exception:
            logger.debug("Failed to annotate compaction levels", exc_info=True)

    # --- Process guardrail results ---
    allowed = True
    block_reasons: list[str] = []
    guardrail_violations: list[GuardrailViolation] = []

    if isinstance(guardrail_result, Exception):
        logger.warning("Guardrail evaluation failed: %s", guardrail_result)
        # Fail open: allow if guardrails error
    else:
        allowed = guardrail_result.allowed
        for v in guardrail_result.violations:
            guardrail_violations.append(GuardrailViolation(
                guardrail_id=v.guardrail_id,
                name=v.name,
                message=v.message,
                severity=v.severity,
                suggestion=v.suggestion,
            ))
            block_reasons.append(v.message)
        for w in guardrail_result.warnings:
            guardrail_violations.append(GuardrailViolation(
                guardrail_id=w.guardrail_id,
                name=w.name,
                message=w.message,
                severity=w.severity,
                suggestion=w.suggestion,
            ))

        # Audit log
        log_guardrail_check(
            requesting_agent=agent_id,
            action_description=action.description,
            allowed=guardrail_result.allowed,
            violations=guardrail_result.violations,
            evaluated=guardrail_result.evaluated,
        )

        # F023/F049: Track guardrail check for deliberation (issue #159)
        from .deliberation_tracker import track_guardrail

        track_guardrail(
            key=tracker_key,
            description=action.description,
            allowed=guardrail_result.allowed,
            violation_count=len(guardrail_result.violations),
        )

    # --- Process calibration results ---
    calibration_context = CalibrationContext()
    if isinstance(calibration_result, Exception):
        logger.warning("Calibration failed: %s", calibration_result)
    elif calibration_result.overall:
        cal = calibration_result.overall
        calibration_context = CalibrationContext(
            brier_score=cal.brier_score,
            accuracy=cal.accuracy,
            calibration_gap=cal.calibration_gap,
            interpretation=cal.interpretation,
            reviewed_decisions=cal.reviewed_decisions,
        )

    # --- Extract patterns from matched decisions ---
    patterns_summary: list[PatternSummary] = []
    if options.include_patterns:
        pattern_groups: dict[str, list[str]] = {}
        for d in relevant_decisions:
            if d.pattern:
                if d.pattern not in pattern_groups:
                    pattern_groups[d.pattern] = []
                pattern_groups[d.pattern].append(d.id)
        for pat, ids in pattern_groups.items():
            patterns_summary.append(PatternSummary(
                pattern=pat,
                count=len(ids),
                example_ids=ids[:3],
            ))

    # --- Optionally record the decision ---
    decision_id: str | None = None
    if allowed and options.auto_record:
        record_req = RecordDecisionRequest.from_dict(
            {
                "decision": action.description,
                "confidence": action.confidence or 0.5,
                "category": action.category or "process",
                "stakes": action.stakes,
                "reasons": request.reasons,
                "tags": request.tags,
                "pattern": request.pattern,
            },
            agent_id=agent_id,
        )
        try:
            # Apply dispatcher hooks before recording (issue #120):
            # Must match the sequence in dispatcher._handle_record_decision

            # F025: Extract related decisions BEFORE consuming tracker
            from .deliberation_tracker import (
                auto_attach_deliberation,
                extract_related_from_tracker,
            )

            if not record_req.related_to:
                related_raw = extract_related_from_tracker(tracker_key)
                if related_raw:
                    from .decision_service import RelatedDecision

                    record_req.related_to = [
                        RelatedDecision.from_dict(r) for r in related_raw
                    ]

            # F023 Phase 2: Auto-attach deliberation from tracked inputs
            record_req.deliberation, _auto_captured = auto_attach_deliberation(
                key=tracker_key,
                deliberation=record_req.deliberation,
            )

            # Enforce the text caps before bridge extraction, which runs regexes
            # over the raw text; reasons and pattern arrive here unchecked from
            # cstp.preAction.
            errors = record_req.length_errors()
            if errors:
                raise ValueError(f"Validation failed: {'; '.join(errors)}")

            # F027 P2: Smart bridge extraction
            from .bridge_hook import maybe_smart_extract_bridge

            await maybe_smart_extract_bridge(record_req)

            record_result = await record_decision(record_req)
            if record_result.success:
                decision_id = record_result.id

                # F045 follow-up: Auto-link decision in graph (issue #157)
                from .graph_service import safe_auto_link

                related_dicts = (
                    [r.to_dict() for r in record_req.related_to]
                    if record_req.related_to
                    else []
                )
                await safe_auto_link(
                    response_id=decision_id,
                    category=record_req.category,
                    stakes=record_req.stakes,
                    confidence=record_req.confidence,
                    tags=list(record_req.tags),
                    pattern=record_req.pattern,
                    related_to=related_dicts,
                    summary=str(record_req.decision)[:120],
                )
        except Exception as e:
            logger.warning("Auto-record failed in pre_action: %s", e)

    query_time_ms = int((time.time() - start_time) * 1000)

    return PreActionResponse(
        allowed=allowed,
        decision_id=decision_id,
        relevant_decisions=relevant_decisions,
        guardrail_results=guardrail_violations,
        calibration_context=calibration_context,
        patterns_summary=patterns_summary,
        block_reasons=block_reasons,
        query_time_ms=query_time_ms,
    )
