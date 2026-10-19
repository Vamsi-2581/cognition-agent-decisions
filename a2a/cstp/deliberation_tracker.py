"""F023 Phase 2: Server-side deliberation auto-capture.

Tracks API calls (queries, guardrail checks, lookups) per agent/session
and auto-builds Deliberation objects when decisions are recorded.

Works for both JSON-RPC (keyed by agent_id) and MCP (keyed by session_id).
Zero client changes required.

All tracking operations are fail-open: errors are logged but never
propagate to the main API flow.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .decision_service import Deliberation, DeliberationInput, DeliberationStep

logger = logging.getLogger("cstp-deliberation")


@dataclass(slots=True)
class TrackedInput:
    """A single tracked input from an API call."""

    id: str
    type: str  # "query" | "guardrail" | "lookup" | "stats"
    text: str
    source: str  # "cstp:queryDecisions" etc.
    timestamp: float  # time.time()
    raw_data: dict[str, Any]


@dataclass
class TrackerSession:
    """Accumulated inputs for one agent/session."""

    inputs: list[TrackedInput] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)


@dataclass(slots=True)
class ConsumedRecord:
    """Record of a consumed (or expired) tracker session."""

    key: str
    consumed_at: float  # time.time()
    input_count: int
    agent_id: str | None
    decision_id: str | None  # backfilled after record_decision
    status: str  # "consumed" | "expired" | "evicted"
    inputs_summary: list[dict[str, str]]  # [{id, type, text}] - brief snapshot


def principal_key(principal: str, key: str) -> str:
    """Namespace a tracker key under the authenticated agent.

    Agent and decision labels come from request arguments, so two tokens can
    name the same bucket. The prefix keeps each token's buckets apart: one
    token cannot add thoughts to, or consume, another token's deliberation.
    """
    return f"{principal}|{key}"


def key_owner(key: str) -> str | None:
    """Return the authenticated agent a tracker key belongs to, if known.

    Keys are either namespaced with `principal_key` or are the JSON-RPC
    transport key `rpc:{agent}`, which only that agent's token produces.
    """
    owner, sep, _ = key.partition("|")
    if sep:
        return owner
    if key.startswith("rpc:"):
        return key.removeprefix("rpc:")
    return None


def _parse_key_components(key: str) -> dict[str, str | None]:
    """Extract agent_id/decision_id from composite key."""
    result: dict[str, str | None] = {"agent_id": None, "decision_id": None}
    _, sep, scoped = key.partition("|")
    if sep:
        key = scoped
    if key.startswith("agent:") and ":decision:" in key:
        parts = key.split(":decision:")
        result["agent_id"] = parts[0].removeprefix("agent:")
        result["decision_id"] = parts[1]
    elif key.startswith("agent:"):
        result["agent_id"] = key.removeprefix("agent:")
    elif key.startswith("decision:"):
        result["decision_id"] = key.removeprefix("decision:")
    return result


class DeliberationTracker:
    """Tracks API calls per agent/session for auto-deliberation capture.

    Thread-safe. Singleton instance shared across dispatcher and MCP server.
    """

    def __init__(
        self,
        input_ttl: int = 300,
        session_ttl: int = 1800,
        consumed_history_size: int = 50,
        max_sessions_per_agent: int = 200,
        max_inputs_per_session: int = 100,
    ) -> None:
        self._sessions: dict[str, TrackerSession] = {}
        self._input_ttl = input_ttl
        self._session_ttl = session_ttl
        self._consumed_history: deque[ConsumedRecord] = deque(maxlen=consumed_history_size)
        # Callers choose session keys, so without caps one agent could grow the
        # tracker without bound until the session TTL expired its entries.
        self._max_sessions_per_agent = max_sessions_per_agent
        self._max_inputs_per_session = max_inputs_per_session
        self._lock = threading.Lock()

    def track(self, key: str, tracked_input: TrackedInput) -> None:
        """Register an input for the given agent/session key."""
        with self._lock:
            # Probabilistic cleanup: ~2% of calls
            if random.random() < 0.02:
                self._cleanup_expired_locked()

            if key not in self._sessions:
                self._evict_for_new_session_locked(key)
                self._sessions[key] = TrackerSession()
            session = self._sessions[key]
            session.inputs.append(tracked_input)
            if len(session.inputs) > self._max_inputs_per_session:
                del session.inputs[0]
            session.last_activity = time.time()

    def _evict_for_new_session_locked(self, key: str) -> None:
        """Make room for a new session of the key's owner (must hold lock).

        Drops the owner's least recently active session when the owner is at
        its cap, so one agent filling the tracker never evicts another's.
        """
        owner = key_owner(key)
        owned = [k for k in self._sessions if key_owner(k) == owner]
        if len(owned) < self._max_sessions_per_agent:
            return
        oldest = min(owned, key=lambda k: self._sessions[k].last_activity)
        self._retire_locked(oldest, time.time(), status="evicted")

    def consume(self, key: str) -> Deliberation | None:
        """Build Deliberation from tracked inputs and clear them.

        Returns None if no inputs were tracked.
        Called during recordDecision.
        Filters out inputs older than TTL.

        Always records a ConsumedRecord to _consumed_history, even when
        all inputs are expired — so sessions never vanish silently.
        """
        with self._lock:
            session = self._sessions.pop(key, None)

            if not session or not session.inputs:
                return None

            # Filter expired inputs
            now = time.time()
            cutoff = now - self._input_ttl
            valid_inputs = [i for i in session.inputs if i.timestamp >= cutoff]

            parsed = _parse_key_components(key)

            if not valid_inputs:
                # All inputs expired — still record so it doesn't vanish
                self._consumed_history.append(ConsumedRecord(
                    key=key,
                    consumed_at=now,
                    input_count=0,
                    agent_id=parsed.get("agent_id"),
                    decision_id=None,
                    status="consumed",
                    inputs_summary=[
                        {"id": "-", "type": "info",
                         "text": "[all inputs expired at consume time]"},
                    ],
                ))
                return None

            # Record consumption history
            self._consumed_history.append(ConsumedRecord(
                key=key,
                consumed_at=now,
                input_count=len(valid_inputs),
                agent_id=parsed.get("agent_id"),
                decision_id=None,  # backfilled later
                status="consumed",
                inputs_summary=[
                    {"id": i.id, "type": i.type, "text": i.text[:80]}
                    for i in valid_inputs[:10]
                ],
            ))

        return self._build_deliberation(valid_inputs)

    def get_inputs(self, key: str) -> list[TrackedInput]:
        """Peek at current tracked inputs without consuming."""
        with self._lock:
            session = self._sessions.get(key)
            if not session:
                return []
            # Filter expired inputs
            cutoff = time.time() - self._input_ttl
            return [i for i in session.inputs if i.timestamp >= cutoff]

    def cleanup_expired(self) -> int:
        """Remove sessions older than TTL. Returns count removed."""
        with self._lock:
            return self._cleanup_expired_locked()

    def _cleanup_expired_locked(self) -> int:
        """Internal cleanup (must hold lock). Handles session-level TTL."""
        now = time.time()
        session_cutoff = now - self._session_ttl
        expired_keys: list[str] = []

        for k, s in self._sessions.items():
            if s.last_activity < session_cutoff:
                expired_keys.append(k)

        for k in expired_keys:
            self._retire_locked(k, now, status="expired")

        return len(expired_keys)

    def _retire_locked(self, key: str, now: float, status: str) -> None:
        """Drop a session unconsumed, keeping a history record (must hold lock)."""
        session = self._sessions.pop(key)
        parsed = _parse_key_components(key)
        valid_inputs = [
            i for i in session.inputs
            if i.timestamp >= (now - self._input_ttl)
        ]
        self._consumed_history.append(ConsumedRecord(
            key=key,
            consumed_at=now,
            input_count=len(valid_inputs),
            agent_id=parsed.get("agent_id"),
            decision_id=None,
            status=status,
            inputs_summary=[
                {"id": i.id, "type": i.type, "text": i.text[:80]}
                for i in valid_inputs[:10]
            ],
        ))

    def _build_deliberation(
        self, inputs: list[TrackedInput]
    ) -> Deliberation:
        """Convert tracked inputs into a Deliberation object.

        Auto-generates steps from the input sequence:
        - Each input becomes a step describing what was gathered
        - Steps reference which input IDs they used
        - Total duration spans first to last input
        """
        # Build DeliberationInput list
        delib_inputs = []
        for inp in inputs:
            delib_inputs.append(
                DeliberationInput(
                    id=inp.id,
                    text=inp.text,
                    source=inp.source,
                    timestamp=_format_timestamp(inp.timestamp),
                )
            )

        # Build steps from the input sequence
        steps = []
        for i, inp in enumerate(inputs, start=1):
            step_type = _input_type_to_step_type(inp.type)
            steps.append(
                DeliberationStep(
                    step=i,
                    thought=inp.text,
                    inputs_used=[inp.id],
                    timestamp=_format_timestamp(inp.timestamp),
                    type=step_type,
                    conclusion=False,
                )
            )

        # Calculate total duration
        total_duration_ms: int | None = None
        if len(inputs) >= 2:
            total_duration_ms = int(
                (inputs[-1].timestamp - inputs[0].timestamp) * 1000
            )

        return Deliberation(
            inputs=delib_inputs,
            steps=steps,
            total_duration_ms=total_duration_ms,
            convergence_point=None,
        )

    def debug_sessions(
        self,
        key: str | None = None,
        include_consumed: bool = False,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Peek at tracker state for debugging. Read-only, does not consume.

        Args:
            key: If provided, return detail for just that session.
                 If None, return all sessions.
            include_consumed: If True, include consumed/expired session history.
            owner: If provided, only sessions and history belonging to this
                authenticated agent are visible.

        Returns:
            Dict with sessions list, session_count, detail mapping, and
            optionally consumed history.
        """
        with self._lock:
            # Deterministic cleanup on read
            self._cleanup_expired_locked()

            now = time.time()
            cutoff = now - self._input_ttl

            if key is not None:
                session = self._sessions.get(key)
                if session is None or (owner is not None and key_owner(key) != owner):
                    result: dict[str, Any] = {
                        "sessions": [],
                        "sessionCount": 0,
                        "detail": {},
                    }
                    if include_consumed:
                        result["consumed"] = self._get_consumed_history_locked(owner=owner)
                    return result
                valid = [i for i in session.inputs if i.timestamp >= cutoff]
                inputs = [
                    {
                        "id": i.id,
                        "type": i.type,
                        "text": i.text,
                        "source": i.source,
                        "ageSeconds": int(now - i.timestamp),
                    }
                    for i in valid
                ]
                detail = {key: {"inputCount": len(inputs), "inputs": inputs}}
                result = {
                    "sessions": [key],
                    "sessionCount": 1,
                    "detail": detail,
                }
                if include_consumed:
                    result["consumed"] = self._get_consumed_history_locked(owner=owner)
                return result

            # All sessions
            sessions: list[str] = []
            detail: dict[str, Any] = {}
            for k, session in self._sessions.items():
                if owner is not None and key_owner(k) != owner:
                    continue
                sessions.append(k)
                valid = [i for i in session.inputs if i.timestamp >= cutoff]
                inputs = [
                    {
                        "id": i.id,
                        "type": i.type,
                        "text": i.text,
                        "source": i.source,
                        "ageSeconds": int(now - i.timestamp),
                    }
                    for i in valid
                ]
                detail[k] = {"inputCount": len(inputs), "inputs": inputs}

            result = {
                "sessions": sessions,
                "sessionCount": len(sessions),
                "detail": detail,
            }

            if include_consumed:
                result["consumed"] = self._get_consumed_history_locked(owner=owner)

            return result

    def backfill_consumed(self, key: str, decision_id: str) -> bool:
        """Backfill decision_id on the most recent ConsumedRecord matching key.

        Called from dispatcher after record_decision returns the decision ID.

        Returns True if a record was found and updated.
        """
        with self._lock:
            for record in reversed(self._consumed_history):
                if record.key == key and record.decision_id is None:
                    record.decision_id = decision_id
                    return True
        return False

    def get_consumed_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent consumed sessions for the dashboard.

        Returns newest-first list of consumed record dicts.
        """
        with self._lock:
            return self._get_consumed_history_locked(limit)

    def _get_consumed_history_locked(
        self, limit: int = 20, owner: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return consumed history (must hold lock or be called under lock)."""
        now = time.time()
        records = [
            r for r in reversed(self._consumed_history)
            if owner is None or key_owner(r.key) == owner
        ][:limit]
        return [
            {
                "key": r.key,
                "consumedAt": int(now - r.consumed_at),
                "inputCount": r.input_count,
                "agentId": r.agent_id,
                "decisionId": r.decision_id,
                "status": r.status,
                "inputsSummary": r.inputs_summary,
            }
            for r in records
        ]

    @property
    def session_count(self) -> int:
        """Number of active tracker sessions."""
        with self._lock:
            return len(self._sessions)


def _format_timestamp(ts: float) -> str:
    """Convert time.time() to ISO format string."""
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _input_type_to_step_type(input_type: str) -> str:
    """Map tracked input type to deliberation step type."""
    mapping = {
        "query": "analysis",
        "guardrail": "constraint",
        "lookup": "analysis",
        "stats": "empirical",
        "reasoning": "reasoning",
    }
    return mapping.get(input_type, "analysis")


# ---------------------------------------------------------------------------
# Composite key helpers (issue #129)
# ---------------------------------------------------------------------------


def build_tracker_key(
    agent_id: str | None = None,
    decision_id: str | None = None,
    transport_key: str | None = None,
    principal: str | None = None,
) -> str:
    """Build a composite tracker key from optional components.

    Priority:
    - agent_id + decision_id -> "agent:{agent_id}:decision:{decision_id}"
    - agent_id only -> "agent:{agent_id}"
    - decision_id only -> "decision:{decision_id}"
    - neither -> transport_key (e.g. "rpc:myagent" or "mcp:default")

    Args:
        agent_id: Optional explicit agent identifier.
        decision_id: Optional decision identifier.
        transport_key: Fallback transport-derived key, already bound to the
            authenticated agent by the transport.
        principal: Authenticated agent; namespaces the agent/decision keys
            (see `principal_key`). Every server call site passes it.

    Returns:
        Composite tracker key string.

    Raises:
        ValueError: If all three are None/empty.
    """
    if agent_id and decision_id:
        key = f"agent:{agent_id}:decision:{decision_id}"
    elif agent_id:
        key = f"agent:{agent_id}"
    elif decision_id:
        key = f"decision:{decision_id}"
    elif transport_key:
        return transport_key
    else:
        raise ValueError(
            "At least one of agent_id, decision_id, or transport_key is required"
        )
    return principal_key(principal, key) if principal else key


def resolve_tracker_keys(
    agent_id: str | None = None,
    decision_id: str | None = None,
    transport_key: str | None = None,
    principal: str | None = None,
) -> list[str]:
    """Return priority-ordered list of tracker keys to try for consumption.

    Matches most-specific first (exact composite), then decision-scoped,
    then agent-scoped, then transport fallback.
    Deduplicates while preserving order.

    Args:
        agent_id: Optional explicit agent identifier.
        decision_id: Optional decision identifier.
        transport_key: Fallback transport-derived key.
        principal: Authenticated agent; namespaces the agent/decision keys.

    Returns:
        List of keys to try, most-specific first.
    """
    scoped: list[str] = []

    # Most specific: exact composite
    if agent_id and decision_id:
        scoped.append(f"agent:{agent_id}:decision:{decision_id}")
    # Decision-scoped (any agent label of this principal)
    if decision_id:
        scoped.append(f"decision:{decision_id}")
    # Agent-scoped (any decision)
    if agent_id:
        scoped.append(f"agent:{agent_id}")

    keys = [principal_key(principal, k) for k in scoped] if principal else scoped
    # Transport fallback
    if transport_key:
        keys.append(transport_key)

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_tracker: DeliberationTracker | None = None
_tracker_lock = threading.Lock()


def get_tracker(
    input_ttl: int = 300,
    session_ttl: int = 1800,
    consumed_history_size: int = 50,
    max_sessions_per_agent: int = 200,
    max_inputs_per_session: int = 100,
) -> DeliberationTracker:
    """Get or create the global tracker instance."""
    global _tracker
    if _tracker is not None:
        return _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = DeliberationTracker(
                input_ttl=input_ttl,
                session_ttl=session_ttl,
                consumed_history_size=consumed_history_size,
                max_sessions_per_agent=max_sessions_per_agent,
                max_inputs_per_session=max_inputs_per_session,
            )
        return _tracker


def reset_tracker() -> None:
    """Reset the global tracker (for testing)."""
    global _tracker
    with _tracker_lock:
        _tracker = None


def debug_tracker(
    key: str | None = None,
    include_consumed: bool = False,
    owner: str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper for debugging tracker state.

    Args:
        key: Optional session key to inspect. None returns all sessions.
        include_consumed: If True, include consumed/expired session history.
        owner: If provided, limit the view to this authenticated agent.

    Returns:
        Dict with sessions, sessionCount, detail, and optionally consumed.
    """
    tracker = get_tracker()
    return tracker.debug_sessions(key, include_consumed=include_consumed, owner=owner)


# ---------------------------------------------------------------------------
# Convenience helpers — called from dispatcher / mcp_server
# ---------------------------------------------------------------------------


def track_query(
    key: str,
    query: str,
    result_count: int,
    top_ids: list[str],
    retrieval_mode: str,
    top_results: list[dict] | None = None,
) -> None:
    """Track a queryDecisions call. Fail-open."""
    try:
        tracker = get_tracker()
        tracker.track(
            key,
            TrackedInput(
                id=f"q-{uuid4().hex[:8]}",
                type="query",
                text=f"Queried '{query[:50]}': {result_count} results ({retrieval_mode})",
                source="cstp:queryDecisions",
                timestamp=time.time(),
                raw_data={
                    "query": query,
                    "result_count": result_count,
                    "top_ids": top_ids[:5],
                    "retrieval_mode": retrieval_mode,
                    "top_results": top_results[:5] if top_results else [],
                },
            ),
        )
    except Exception:
        logger.debug("Failed to track query", exc_info=True)


def track_guardrail(
    key: str,
    description: str,
    allowed: bool,
    violation_count: int,
) -> None:
    """Track a checkGuardrails call. Fail-open."""
    try:
        status = "allowed" if allowed else f"blocked ({violation_count} violations)"
        tracker = get_tracker()
        tracker.track(
            key,
            TrackedInput(
                id=f"g-{uuid4().hex[:8]}",
                type="guardrail",
                text=f"Checked '{description[:50]}': {status}",
                source="cstp:checkGuardrails",
                timestamp=time.time(),
                raw_data={
                    "description": description,
                    "allowed": allowed,
                    "violation_count": violation_count,
                },
            ),
        )
    except Exception:
        logger.debug("Failed to track guardrail", exc_info=True)


def track_lookup(
    key: str,
    decision_id: str,
    title: str,
) -> None:
    """Track a getDecision call. Fail-open."""
    try:
        tracker = get_tracker()
        tracker.track(
            key,
            TrackedInput(
                id=f"l-{uuid4().hex[:8]}",
                type="lookup",
                text=f"Retrieved decision {decision_id}: {title[:50]}",
                source="cstp:getDecision",
                timestamp=time.time(),
                raw_data={
                    "decision_id": decision_id,
                    "title": title,
                },
            ),
        )
    except Exception:
        logger.debug("Failed to track lookup", exc_info=True)


def track_stats(
    key: str,
    total_decisions: int,
    reason_type_count: int,
    diversity: float | None = None,
) -> None:
    """Track a getReasonStats call. Fail-open."""
    try:
        tracker = get_tracker()
        diversity_str = f", diversity={diversity:.2f}" if diversity else ""
        tracker.track(
            key,
            TrackedInput(
                id=f"s-{uuid4().hex[:8]}",
                type="stats",
                text=f"Reviewed reason stats: {reason_type_count} types, {total_decisions} decisions{diversity_str}",
                source="cstp:getReasonStats",
                timestamp=time.time(),
                raw_data={
                    "total_decisions": total_decisions,
                    "reason_type_count": reason_type_count,
                    "diversity": diversity,
                },
            ),
        )
    except Exception:
        logger.debug("Failed to track stats", exc_info=True)


def track_reasoning(
    key: str,
    text: str,
    decision_id: str | None = None,
    agent_id: str | None = None,
    principal: str | None = None,
) -> None:
    """Track a reasoning/chain-of-thought step. Fail-open.

    Captures the agent's internal reasoning process as a deliberation
    step. Can be used pre-decision (accumulated in tracker) or
    post-decision (appended to existing deliberation via update).

    When agent_id and/or decision_id are provided, a composite tracker
    key is built for multi-agent isolation (issue #129). The ``key``
    parameter serves as the transport fallback.

    Args:
        key: Transport-derived key (e.g. "rpc:myagent", "mcp:default").
        text: The reasoning/thought text.
        decision_id: Optional decision ID for scoping the thought.
        agent_id: Optional agent ID for multi-agent isolation.
        principal: Authenticated agent that namespaces the composite key.
    """
    try:
        tracker = get_tracker()
        storage_key = build_tracker_key(
            agent_id=agent_id,
            decision_id=decision_id,
            transport_key=key,
            principal=principal,
        )
        raw_data: dict[str, Any] = {"text": text}
        if decision_id:
            raw_data["decision_id"] = decision_id
        if agent_id:
            raw_data["agent_id"] = agent_id
        tracker.track(
            storage_key,
            TrackedInput(
                id=f"r-{uuid4().hex[:8]}",
                type="reasoning",
                text=text,
                source="cstp:recordThought",
                timestamp=time.time(),
                raw_data=raw_data,
            ),
        )
    except Exception:
        logger.debug("Failed to track reasoning", exc_info=True)


def auto_attach_deliberation(
    key: str,
    deliberation: Deliberation | None,
    agent_id: str | None = None,
    decision_id: str | None = None,
    principal: str | None = None,
) -> tuple[Deliberation | None, bool]:
    """Consume tracked inputs and attach/merge with deliberation. Fail-open.

    When agent_id/decision_id are provided, tries multiple composite keys
    in priority order (most-specific first) and merges all found inputs.

    Args:
        key: Transport-derived key (fallback).
        deliberation: Explicit deliberation from the client, or None.
        agent_id: Optional agent ID for multi-agent key resolution.
        decision_id: Optional decision ID for decision-scoped key resolution.
        principal: Authenticated agent that namespaces the composite keys.

    Returns:
        (deliberation, auto_captured) -- auto_captured is True only if
        tracked inputs were actually consumed and attached.

    - If no explicit deliberation: return auto-built from tracker
    - If explicit deliberation: merge tracked inputs + steps into it
    - If nothing tracked: return the explicit deliberation as-is (or None)
    """
    try:
        tracker = get_tracker()
        keys = resolve_tracker_keys(agent_id, decision_id, key, principal=principal)

        # Try consuming from each key in priority order, merge all found
        combined_delib: Deliberation | None = None
        for k in keys:
            auto_delib = tracker.consume(k)
            if auto_delib:
                if combined_delib is None:
                    combined_delib = auto_delib
                else:
                    # Merge into combined
                    existing_ids = {i.id for i in combined_delib.inputs}
                    for inp in auto_delib.inputs:
                        if inp.id not in existing_ids:
                            combined_delib.inputs.append(inp)
                    if auto_delib.steps:
                        max_step = max(
                            (s.step for s in combined_delib.steps), default=0
                        )
                        for step in auto_delib.steps:
                            step.step = max_step + step.step
                            combined_delib.steps.append(step)
        auto_delib = combined_delib
    except Exception:
        logger.debug("Failed to consume deliberation", exc_info=True)
        return deliberation, False

    if not auto_delib:
        # Nothing tracked — return whatever was passed in
        return deliberation, False

    if not deliberation or not deliberation.has_content():
        # No explicit deliberation — use auto-built
        return auto_delib, True

    # Merge: append tracked inputs AND steps to explicit deliberation
    existing_input_ids = {i.id for i in deliberation.inputs}
    for inp in auto_delib.inputs:
        if inp.id not in existing_input_ids:
            deliberation.inputs.append(inp)

    # Also merge auto-generated steps (append after existing steps)
    if auto_delib.steps:
        max_step = max((s.step for s in deliberation.steps), default=0)
        for step in auto_delib.steps:
            # Re-number to follow existing steps
            step.step = max_step + step.step
            deliberation.steps.append(step)

    return deliberation, True


def extract_related_from_tracker(
    key: str,
    agent_id: str | None = None,
    decision_id: str | None = None,
    principal: str | None = None,
) -> list[dict]:
    """Extract related decisions from tracked inputs BEFORE consumption.

    Call this before auto_attach_deliberation to capture top_results
    from query inputs while they're still in the tracker.

    When agent_id/decision_id are provided, checks multiple composite
    keys in priority order and collects from all matching sessions.

    Args:
        key: Transport-derived key (fallback).
        agent_id: Optional agent ID for multi-agent key resolution.
        decision_id: Optional decision ID for decision-scoped key resolution.
        principal: Authenticated agent that namespaces the composite keys.

    Returns:
        List of dicts with id, summary, distance - deduplicated and sorted.
    """
    try:
        tracker = get_tracker()
        keys = resolve_tracker_keys(agent_id, decision_id, key, principal=principal)

        with tracker._lock:
            seen: dict[str, dict] = {}
            cutoff = time.time() - tracker._input_ttl

            for k in keys:
                session = tracker._sessions.get(k)
                if not session:
                    continue

                for inp in session.inputs:
                    if inp.timestamp < cutoff:
                        continue
                    if inp.type != "query" or not inp.raw_data:
                        continue
                    top_results = inp.raw_data.get("top_results", [])
                    for r in top_results:
                        rid = r.get("id", "")
                        if not rid:
                            continue
                        dist = r.get("distance", 0.0)
                        if rid not in seen or dist < seen[rid]["distance"]:
                            seen[rid] = {
                                "id": rid,
                                "summary": r.get("summary", ""),
                                "distance": dist,
                            }

            # Sort by distance (closest first) and return
            return sorted(seen.values(), key=lambda x: x["distance"])
    except Exception:
        logger.debug("Failed to extract related decisions", exc_info=True)
        return []
