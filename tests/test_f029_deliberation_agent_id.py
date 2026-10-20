"""Tests for F029/F129: agent_id and decision_id in recordThought.

Verifies that:
1. Backward compatibility -- no params = same behavior
2. agent_id only -- thoughts keyed by agent:{agent_id}
3. decision_id only -- thoughts keyed by decision:{decision_id}
4. Both agent_id + decision_id -- composite key agent:{agent_id}:decision:{decision_id}
5. Multi-agent isolation -- Agent A and Agent B thoughts don't cross
6. auto_attach_deliberation priority -- resolve_tracker_keys ordering
7. Edge cases -- empty strings, None, mixed usage
8. build_tracker_key and resolve_tracker_keys unit tests
9. Dispatcher integration -- recordThought with agent_id param
"""

from __future__ import annotations

import time

import pytest

from a2a.cstp.deliberation_tracker import (
    DeliberationTracker,
    TrackedInput,
    auto_attach_deliberation,
    build_tracker_key,
    get_tracker,
    reset_tracker,
    resolve_tracker_keys,
    track_reasoning,
)
from a2a.cstp.dispatcher import CstpDispatcher, register_methods
from a2a.cstp.dispatcher import build_tracker_key as dispatcher_build_key
from a2a.cstp.models import RecordThoughtParams
from a2a.models.jsonrpc import JsonRpcRequest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tracker():
    """Ensure a fresh tracker for every test."""
    reset_tracker()
    yield
    reset_tracker()


@pytest.fixture
def tracker() -> DeliberationTracker:
    """Return a fresh tracker instance."""
    return get_tracker()


@pytest.fixture
def dispatcher() -> CstpDispatcher:
    d = CstpDispatcher()
    register_methods(d)
    return d


# ===========================================================================
# 1. build_tracker_key (tracker module)
# ===========================================================================


class TestBuildTrackerKey:
    """Unit tests for build_tracker_key in deliberation_tracker."""

    def test_both_agent_and_decision(self) -> None:
        key = build_tracker_key(agent_id="planner", decision_id="d1", transport_key="rpc:x")
        assert key == "agent:planner:decision:d1"

    def test_agent_only(self) -> None:
        key = build_tracker_key(agent_id="planner", transport_key="rpc:x")
        assert key == "agent:planner"

    def test_decision_only(self) -> None:
        key = build_tracker_key(decision_id="d1", transport_key="rpc:x")
        assert key == "decision:d1"

    def test_transport_fallback(self) -> None:
        key = build_tracker_key(transport_key="rpc:agent-001")
        assert key == "rpc:agent-001"

    def test_no_keys_raises(self) -> None:
        with pytest.raises(ValueError, match="required"):
            build_tracker_key()

    def test_empty_string_agent_id_falls_back(self) -> None:
        """Empty string agent_id is falsy, should fall back."""
        key = build_tracker_key(agent_id="", transport_key="rpc:x")
        assert key == "rpc:x"

    def test_none_agent_id_falls_back(self) -> None:
        key = build_tracker_key(agent_id=None, transport_key="rpc:x")
        assert key == "rpc:x"

    def test_empty_decision_id_falls_back(self) -> None:
        key = build_tracker_key(decision_id="", transport_key="rpc:x")
        assert key == "rpc:x"


# ===========================================================================
# 2. Dispatcher build_tracker_key
# ===========================================================================


class TestDispatcherBuildTrackerKey:
    """Unit tests for build_tracker_key in dispatcher module."""

    def test_with_agent_id(self) -> None:
        key = dispatcher_build_key("transport-auth", agent_id="planner-01")
        assert key == "transport-auth|agent:planner-01"

    def test_with_decision_id(self) -> None:
        key = dispatcher_build_key("transport-auth", decision_id="d1")
        assert key == "transport-auth|decision:d1"

    def test_with_both(self) -> None:
        key = dispatcher_build_key("transport-auth", agent_id="a1", decision_id="d1")
        assert key == "transport-auth|agent:a1:decision:d1"

    def test_fallback_to_transport(self) -> None:
        key = dispatcher_build_key("transport-auth")
        assert key == "rpc:transport-auth"


# ===========================================================================
# 3. resolve_tracker_keys
# ===========================================================================


class TestResolveTrackerKeys:
    """Unit tests for resolve_tracker_keys priority ordering."""

    def test_all_params(self) -> None:
        keys = resolve_tracker_keys(agent_id="a1", decision_id="d1", transport_key="rpc:x")
        assert keys == [
            "agent:a1:decision:d1",
            "decision:d1",
            "agent:a1",
            "rpc:x",
        ]

    def test_agent_only(self) -> None:
        keys = resolve_tracker_keys(agent_id="a1", transport_key="rpc:x")
        assert keys == ["agent:a1", "rpc:x"]

    def test_decision_only(self) -> None:
        keys = resolve_tracker_keys(decision_id="d1", transport_key="rpc:x")
        assert keys == ["decision:d1", "rpc:x"]

    def test_transport_only(self) -> None:
        keys = resolve_tracker_keys(transport_key="rpc:x")
        assert keys == ["rpc:x"]

    def test_no_duplicates(self) -> None:
        """Deduplication preserves order."""
        keys = resolve_tracker_keys(agent_id="a1", decision_id="d1", transport_key="rpc:x")
        assert len(keys) == len(set(keys))

    def test_empty_list_when_nothing(self) -> None:
        keys = resolve_tracker_keys()
        assert keys == []


# ===========================================================================
# 4. RecordThoughtParams model
# ===========================================================================


class TestRecordThoughtParams:
    """Tests for the Pydantic model parsing."""

    def test_minimal(self) -> None:
        p = RecordThoughtParams.from_params({"text": "hello"})
        assert p.text == "hello"
        assert p.agent_id is None
        assert p.decision_id is None

    def test_snake_case(self) -> None:
        p = RecordThoughtParams.from_params({
            "text": "t",
            "agent_id": "a1",
            "decision_id": "d1",
        })
        assert p.agent_id == "a1"
        assert p.decision_id == "d1"

    def test_camel_case(self) -> None:
        p = RecordThoughtParams.from_params({
            "text": "t",
            "agentId": "a1",
            "decisionId": "d1",
        })
        assert p.agent_id == "a1"
        assert p.decision_id == "d1"

    def test_missing_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            RecordThoughtParams.from_params({"agent_id": "a1"})

    def test_empty_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            RecordThoughtParams.from_params({"text": ""})


# ===========================================================================
# 5. Backward compatibility -- no agent_id / decision_id
# ===========================================================================


class TestBackwardCompatibility:
    """recordThought without agent_id/decision_id works exactly as before."""

    def test_track_reasoning_uses_transport_key(self, tracker: DeliberationTracker) -> None:
        """Without agent_id, thoughts use the transport-derived key."""
        track_reasoning("rpc:agent-001", "Thinking about option A")

        inputs = tracker.get_inputs("rpc:agent-001")
        assert len(inputs) == 1
        assert inputs[0].text == "Thinking about option A"
        assert inputs[0].type == "reasoning"

    def test_consume_by_transport_key(self, tracker: DeliberationTracker) -> None:
        """Consuming by transport key returns deliberation."""
        track_reasoning("rpc:agent-001", "Step 1")
        track_reasoning("rpc:agent-001", "Step 2")

        delib = tracker.consume("rpc:agent-001")
        assert delib is not None
        assert len(delib.inputs) == 2
        assert len(delib.steps) == 2

    def test_auto_attach_uses_transport_key(self) -> None:
        """auto_attach_deliberation works with transport key only."""
        track_reasoning("rpc:agent-001", "My reasoning")

        delib, auto_captured = auto_attach_deliberation(
            key="rpc:agent-001",
            deliberation=None,
        )
        assert auto_captured is True
        assert delib is not None
        assert len(delib.inputs) == 1

    @pytest.mark.asyncio
    async def test_dispatcher_no_new_params(self, dispatcher: CstpDispatcher) -> None:
        """Dispatcher handles recordThought without agent_id/decision_id."""
        request = JsonRpcRequest(
            jsonrpc="2.0",
            method="cstp.recordThought",
            params={"text": "Just a thought"},
            id="1",
        )
        response = await dispatcher.dispatch(request, agent_id="agent-001")
        assert response.error is None
        assert response.result["success"] is True
        assert response.result["mode"] == "pre-decision"


# ===========================================================================
# 6. agent_id only -- keyed by agent:{agent_id}
# ===========================================================================


class TestAgentIdOnly:
    """recordThought with agent_id creates agent-scoped tracker key."""

    def test_track_with_agent_id(self, tracker: DeliberationTracker) -> None:
        """Thoughts with agent_id are stored under agent:{agent_id} key."""
        track_reasoning("rpc:fallback", "Planning phase", agent_id="planner-01")

        inputs = tracker.get_inputs("agent:planner-01")
        assert len(inputs) == 1
        assert inputs[0].text == "Planning phase"

    def test_track_agent_id_isolation(self, tracker: DeliberationTracker) -> None:
        """agent_id key doesn't pollute transport key."""
        track_reasoning("rpc:agent-001", "Agent thought", agent_id="planner-01")
        track_reasoning("rpc:agent-001", "Transport thought")

        agent_inputs = tracker.get_inputs("agent:planner-01")
        transport_inputs = tracker.get_inputs("rpc:agent-001")

        assert len(agent_inputs) == 1
        assert agent_inputs[0].text == "Agent thought"
        assert len(transport_inputs) == 1
        assert transport_inputs[0].text == "Transport thought"

    @pytest.mark.asyncio
    async def test_dispatcher_with_agent_id(self, dispatcher: CstpDispatcher) -> None:
        """Dispatcher passes agent_id to tracker and builds composite key."""
        request = JsonRpcRequest(
            jsonrpc="2.0",
            method="cstp.recordThought",
            params={"text": "Agent thought", "agent_id": "planner-01"},
            id="1",
        )
        response = await dispatcher.dispatch(request, agent_id="rpc-auth-id")
        assert response.error is None
        assert response.result["success"] is True
        assert response.result["mode"] == "pre-decision"
        assert response.result["tracker_key"] == "rpc-auth-id|agent:planner-01"

        # Verify thought was tracked under agent key
        tracker = get_tracker()
        inputs = tracker.get_inputs("rpc-auth-id|agent:planner-01")
        assert len(inputs) == 1

    @pytest.mark.asyncio
    async def test_dispatcher_camel_case_agent_id(self, dispatcher: CstpDispatcher) -> None:
        """Dispatcher accepts agentId (camelCase) param."""
        request = JsonRpcRequest(
            jsonrpc="2.0",
            method="cstp.recordThought",
            params={"text": "Camel test", "agentId": "architect-02"},
            id="1",
        )
        response = await dispatcher.dispatch(request, agent_id="auth-id")
        assert response.error is None
        assert response.result["tracker_key"] == "auth-id|agent:architect-02"


# ===========================================================================
# 7. decision_id only -- keyed by decision:{decision_id}
# ===========================================================================


class TestDecisionIdOnly:
    """track_reasoning with decision_id uses decision-scoped key."""

    def test_track_with_decision_id_key(self, tracker: DeliberationTracker) -> None:
        """Thoughts keyed by decision:{decision_id}."""
        track_reasoning("rpc:fallback", "Deciding on architecture", decision_id="abc12345")

        inputs = tracker.get_inputs("decision:abc12345")
        assert len(inputs) == 1
        assert inputs[0].text == "Deciding on architecture"

    def test_decision_id_isolation(self, tracker: DeliberationTracker) -> None:
        """decision_id key is separate from transport key."""
        track_reasoning("rpc:agent-001", "Decision thought", decision_id="abc12345")
        track_reasoning("rpc:agent-001", "Transport thought")

        decision_inputs = tracker.get_inputs("decision:abc12345")
        transport_inputs = tracker.get_inputs("rpc:agent-001")

        assert len(decision_inputs) == 1
        assert decision_inputs[0].text == "Decision thought"
        assert len(transport_inputs) == 1
        assert transport_inputs[0].text == "Transport thought"


# ===========================================================================
# 8. Both agent_id + decision_id -- composite key
# ===========================================================================


class TestCompositeKey:
    """Both agent_id and decision_id create composite key."""

    def test_composite_key_format(self, tracker: DeliberationTracker) -> None:
        """Composite key is agent:{agent_id}:decision:{decision_id}."""
        track_reasoning(
            "rpc:fallback", "Composite thought",
            agent_id="planner-01", decision_id="abc12345",
        )

        inputs = tracker.get_inputs("agent:planner-01:decision:abc12345")
        assert len(inputs) == 1
        assert inputs[0].text == "Composite thought"

    def test_composite_key_isolation(self, tracker: DeliberationTracker) -> None:
        """Composite key is separate from agent-only and decision-only keys."""
        track_reasoning("rpc:f", "Composite", agent_id="planner-01", decision_id="abc12345")
        track_reasoning("rpc:f", "Agent only", agent_id="planner-01")
        track_reasoning("rpc:f", "Decision only", decision_id="abc12345")

        composite = tracker.get_inputs("agent:planner-01:decision:abc12345")
        agent_only = tracker.get_inputs("agent:planner-01")
        decision_only = tracker.get_inputs("decision:abc12345")

        assert len(composite) == 1
        assert composite[0].text == "Composite"
        assert len(agent_only) == 1
        assert agent_only[0].text == "Agent only"
        assert len(decision_only) == 1
        assert decision_only[0].text == "Decision only"


# ===========================================================================
# 9. Multi-agent isolation
# ===========================================================================


class TestMultiAgentIsolation:
    """Agent A and Agent B thoughts don't cross-contaminate."""

    def test_two_agents_isolated(self, tracker: DeliberationTracker) -> None:
        """Different agents have separate thought streams."""
        track_reasoning("rpc:shared", "A's thought 1", agent_id="agent-A")
        track_reasoning("rpc:shared", "A's thought 2", agent_id="agent-A")
        track_reasoning("rpc:shared", "B's thought 1", agent_id="agent-B")
        track_reasoning("rpc:shared", "B's thought 2", agent_id="agent-B")
        track_reasoning("rpc:shared", "B's thought 3", agent_id="agent-B")

        a_inputs = tracker.get_inputs("agent:agent-A")
        b_inputs = tracker.get_inputs("agent:agent-B")

        assert len(a_inputs) == 2
        assert len(b_inputs) == 3
        assert all("A's" in i.text for i in a_inputs)
        assert all("B's" in i.text for i in b_inputs)

    def test_consume_one_agent_leaves_other(self, tracker: DeliberationTracker) -> None:
        """Consuming one agent's thoughts doesn't affect the other."""
        track_reasoning("rpc:shared", "A's thought", agent_id="agent-A")
        track_reasoning("rpc:shared", "B's thought", agent_id="agent-B")

        delib_a = tracker.consume("agent:agent-A")
        assert delib_a is not None
        assert len(delib_a.inputs) == 1

        # B's thoughts are still there
        b_inputs = tracker.get_inputs("agent:agent-B")
        assert len(b_inputs) == 1
        assert b_inputs[0].text == "B's thought"

    def test_three_agents_with_composite_keys(self, tracker: DeliberationTracker) -> None:
        """Multiple agents with same decision_id are isolated by composite key."""
        track_reasoning("rpc:f", "A on d1", agent_id="A", decision_id="d1")
        track_reasoning("rpc:f", "B on d1", agent_id="B", decision_id="d1")
        track_reasoning("rpc:f", "C on d1", agent_id="C", decision_id="d1")

        a = tracker.get_inputs("agent:A:decision:d1")
        b = tracker.get_inputs("agent:B:decision:d1")
        c = tracker.get_inputs("agent:C:decision:d1")

        assert len(a) == 1 and a[0].text == "A on d1"
        assert len(b) == 1 and b[0].text == "B on d1"
        assert len(c) == 1 and c[0].text == "C on d1"

    @pytest.mark.asyncio
    async def test_dispatcher_two_agents_isolated(self, dispatcher: CstpDispatcher) -> None:
        """Two agents using dispatcher don't interfere."""
        for text, aid in [("A thinking", "agent-A"), ("B thinking", "agent-B")]:
            req = JsonRpcRequest(
                jsonrpc="2.0",
                method="cstp.recordThought",
                params={"text": text, "agentId": aid},
                id="1",
            )
            resp = await dispatcher.dispatch(req, agent_id="shared-auth")
            assert resp.error is None

        tracker = get_tracker()
        assert len(tracker.get_inputs("shared-auth|agent:agent-A")) == 1
        assert len(tracker.get_inputs("shared-auth|agent:agent-B")) == 1
        assert tracker.get_inputs("shared-auth|agent:agent-A")[0].text == "A thinking"
        assert tracker.get_inputs("shared-auth|agent:agent-B")[0].text == "B thinking"


# ===========================================================================
# 10. auto_attach_deliberation priority
# ===========================================================================


class TestAutoAttachPriority:
    """auto_attach_deliberation with agent_id/decision_id uses priority matching."""

    def test_decision_id_key_consumed_first(self) -> None:
        """When both decision and agent keys exist, decision_id is consumed first."""
        track_reasoning("rpc:t", "Decision-scoped thought", decision_id="d1")
        track_reasoning("rpc:t", "Agent-scoped thought", agent_id="a1")
        track_reasoning("rpc:t", "Transport thought")

        # Consume with all params -- gets all tracked thoughts via priority keys
        delib, captured = auto_attach_deliberation(
            key="rpc:t",
            deliberation=None,
            agent_id="a1",
            decision_id="d1",
        )
        assert captured is True
        assert delib is not None
        # Should have consumed from all matching keys
        texts = [i.text for i in delib.inputs]
        assert "Decision-scoped thought" in texts

    def test_agent_key_consumed(self) -> None:
        """When agent key exists, it is consumed."""
        track_reasoning("rpc:t", "Agent thought", agent_id="a1")
        track_reasoning("rpc:t", "Transport thought")

        delib, captured = auto_attach_deliberation(
            key="rpc:t",
            deliberation=None,
            agent_id="a1",
        )
        assert captured is True
        assert delib is not None
        texts = [i.text for i in delib.inputs]
        assert "Agent thought" in texts

    def test_transport_fallback(self) -> None:
        """When only transport key exists, it is used."""
        track_reasoning("rpc:transport1", "Transport thought")

        delib, captured = auto_attach_deliberation("rpc:transport1", None)
        assert captured is True
        assert delib is not None
        assert delib.inputs[0].text == "Transport thought"

    def test_no_keys_match(self) -> None:
        """When no keys match, returns None."""
        delib, captured = auto_attach_deliberation("rpc:nonexistent", None)
        assert captured is False
        assert delib is None

    def test_merge_with_explicit_deliberation(self) -> None:
        """Tracked thoughts merge into explicit deliberation."""
        from a2a.cstp.decision_service import (
            Deliberation,
            DeliberationInput,
            DeliberationStep,
        )

        track_reasoning("rpc:t", "Auto-tracked thought", agent_id="a1")

        explicit = Deliberation(
            inputs=[DeliberationInput(id="manual-1", text="Manual input")],
            steps=[DeliberationStep(step=1, thought="Manual step", inputs_used=["manual-1"])],
        )

        delib, captured = auto_attach_deliberation(
            key="rpc:t",
            deliberation=explicit,
            agent_id="a1",
        )
        assert captured is True
        assert delib is not None
        texts = [i.text for i in delib.inputs]
        assert "Manual input" in texts
        assert "Auto-tracked thought" in texts

    def test_consumes_all_matching_keys(self) -> None:
        """auto_attach_deliberation consumes from all priority keys."""
        track_reasoning("rpc:t", "Composite", agent_id="a1", decision_id="d1")
        track_reasoning("rpc:t", "Decision only", decision_id="d1")
        track_reasoning("rpc:t", "Agent only", agent_id="a1")
        track_reasoning("rpc:t", "Transport only")

        delib, captured = auto_attach_deliberation(
            key="rpc:t",
            deliberation=None,
            agent_id="a1",
            decision_id="d1",
        )
        assert captured is True
        assert delib is not None
        texts = [i.text for i in delib.inputs]
        assert "Composite" in texts
        assert "Decision only" in texts
        assert "Agent only" in texts
        assert "Transport only" in texts


# ===========================================================================
# 11. Edge cases
# ===========================================================================


class TestEdgeCases:
    """Edge cases: empty strings, None, mixed usage."""

    def test_empty_agent_id_uses_transport_key(self, tracker: DeliberationTracker) -> None:
        """Empty string agent_id should fall back to transport key."""
        track_reasoning("rpc:agent-001", "Fallback thought", agent_id="")
        inputs = tracker.get_inputs("rpc:agent-001")
        assert len(inputs) == 1

    def test_none_params_use_transport_key(self, tracker: DeliberationTracker) -> None:
        """None agent_id and decision_id use transport key."""
        track_reasoning("rpc:agent-001", "Default thought", agent_id=None, decision_id=None)
        inputs = tracker.get_inputs("rpc:agent-001")
        assert len(inputs) == 1

    def test_multiple_thoughts_same_key(self, tracker: DeliberationTracker) -> None:
        """Multiple thoughts accumulate under the same key."""
        for i in range(5):
            track_reasoning("rpc:f", f"Thought {i}", agent_id="multi")

        inputs = tracker.get_inputs("agent:multi")
        assert len(inputs) == 5

    def test_consume_clears_inputs(self, tracker: DeliberationTracker) -> None:
        """After consume, get_inputs returns empty."""
        track_reasoning("rpc:f", "Will be consumed", agent_id="clear-test")
        delib = tracker.consume("agent:clear-test")
        assert delib is not None

        inputs = tracker.get_inputs("agent:clear-test")
        assert len(inputs) == 0

    def test_double_consume_returns_none(self, tracker: DeliberationTracker) -> None:
        """Second consume returns None."""
        track_reasoning("rpc:f", "One thought", agent_id="double")
        first = tracker.consume("agent:double")
        assert first is not None

        second = tracker.consume("agent:double")
        assert second is None

    @pytest.mark.asyncio
    async def test_dispatcher_missing_text_error(self, dispatcher: CstpDispatcher) -> None:
        """Dispatcher rejects recordThought without text."""
        request = JsonRpcRequest(
            jsonrpc="2.0",
            method="cstp.recordThought",
            params={"agent_id": "test"},
            id="1",
        )
        response = await dispatcher.dispatch(request, agent_id="agent-001")
        assert response.error is not None
        assert "text" in response.error.message.lower()

    @pytest.mark.asyncio
    async def test_dispatcher_empty_text_error(self, dispatcher: CstpDispatcher) -> None:
        """Dispatcher rejects recordThought with empty text."""
        request = JsonRpcRequest(
            jsonrpc="2.0",
            method="cstp.recordThought",
            params={"text": "", "agent_id": "test"},
            id="1",
        )
        response = await dispatcher.dispatch(request, agent_id="agent-001")
        assert response.error is not None
        assert "text" in response.error.message.lower()

    def test_session_count_reflects_keys(self, tracker: DeliberationTracker) -> None:
        """session_count reflects distinct keys."""
        track_reasoning("rpc:f", "A", agent_id="a")
        track_reasoning("rpc:f", "B", agent_id="b")
        track_reasoning("rpc:f", "D1", decision_id="d1")

        assert tracker.session_count == 3

    def test_debug_sessions_shows_agent_keys(self, tracker: DeliberationTracker) -> None:
        """debug_sessions reveals agent-scoped keys."""
        track_reasoning("rpc:f", "Debug thought", agent_id="debug-test")

        debug = tracker.debug_sessions("agent:debug-test")
        assert debug["sessionCount"] == 1
        assert "agent:debug-test" in debug["sessions"]
        assert debug["detail"]["agent:debug-test"]["inputCount"] == 1

    def test_raw_data_includes_agent_id(self, tracker: DeliberationTracker) -> None:
        """track_reasoning stores agent_id in raw_data."""
        track_reasoning("rpc:f", "Test", agent_id="raw-test")
        inputs = tracker.get_inputs("agent:raw-test")
        assert inputs[0].raw_data.get("agent_id") == "raw-test"

    def test_raw_data_includes_decision_id(self, tracker: DeliberationTracker) -> None:
        """track_reasoning stores decision_id in raw_data."""
        track_reasoning("rpc:f", "Test", decision_id="d-raw")
        inputs = tracker.get_inputs("decision:d-raw")
        assert inputs[0].raw_data.get("decision_id") == "d-raw"


# ===========================================================================
# 12. Integration: full recordThought -> recordDecision flow
# ===========================================================================


class TestRecordThoughtToDecisionFlow:
    """End-to-end: thoughts tracked, then attached on recordDecision."""

    def test_thoughts_build_deliberation(self, tracker: DeliberationTracker) -> None:
        """Multiple thoughts produce a valid Deliberation object."""
        track_reasoning("rpc:f", "Step 1: analyze options", agent_id="flow-test")
        track_reasoning("rpc:f", "Step 2: compare tradeoffs", agent_id="flow-test")
        track_reasoning("rpc:f", "Step 3: decide on approach", agent_id="flow-test")

        delib = tracker.consume("agent:flow-test")
        assert delib is not None
        assert len(delib.inputs) == 3
        assert len(delib.steps) == 3

        # Steps are ordered
        for i, step in enumerate(delib.steps, start=1):
            assert step.step == i
            assert step.type == "reasoning"

    def test_deliberation_duration_calculated(self, tracker: DeliberationTracker) -> None:
        """Duration is calculated from first to last thought timestamp."""
        tracker.track(
            "agent:duration-test",
            TrackedInput(
                id="r-1",
                type="reasoning",
                text="First",
                source="cstp:recordThought",
                timestamp=time.time() - 2.0,
                raw_data={"text": "First"},
            ),
        )
        tracker.track(
            "agent:duration-test",
            TrackedInput(
                id="r-2",
                type="reasoning",
                text="Last",
                source="cstp:recordThought",
                timestamp=time.time(),
                raw_data={"text": "Last"},
            ),
        )

        delib = tracker.consume("agent:duration-test")
        assert delib is not None
        assert delib.total_duration_ms is not None
        # Should be roughly 2000ms
        assert delib.total_duration_ms >= 1500
        assert delib.total_duration_ms <= 3000

    def test_auto_attach_merges_agent_and_transport(self) -> None:
        """auto_attach_deliberation merges both agent-scoped and transport thoughts."""
        track_reasoning("rpc:auth-id", "Agent thought", agent_id="my-agent")
        track_reasoning("rpc:auth-id", "Transport thought")

        delib, captured = auto_attach_deliberation(
            key="rpc:auth-id",
            deliberation=None,
            agent_id="my-agent",
        )
        assert captured is True
        assert delib is not None
        texts = [i.text for i in delib.inputs]
        assert "Agent thought" in texts
        assert "Transport thought" in texts


# ===========================================================================
# 13. extract_related_from_tracker with multi-key
# ===========================================================================


class TestExtractRelatedMultiKey:
    """extract_related_from_tracker collects from multiple composite keys."""

    def test_extract_related_with_agent_id(self, tracker: DeliberationTracker) -> None:
        """Peek at multiple keys, collect related decisions from all."""
        from a2a.cstp.deliberation_tracker import (
            extract_related_from_tracker,
            track_query,
        )

        # Track a query under agent-scoped key
        track_query(
            "agent:dev",
            query="architecture patterns",
            result_count=2,
            top_ids=["d1", "d2"],
            retrieval_mode="hybrid",
            top_results=[
                {"id": "d1", "summary": "From agent key", "distance": 0.1},
                {"id": "d2", "summary": "Also agent key", "distance": 0.2},
            ],
        )
        # Track a query under transport key
        track_query(
            "rpc:shared",
            query="process decisions",
            result_count=1,
            top_ids=["d3"],
            retrieval_mode="hybrid",
            top_results=[
                {"id": "d3", "summary": "From transport", "distance": 0.15},
            ],
        )

        related = extract_related_from_tracker(
            "rpc:shared",
            agent_id="dev",
        )
        ids = [r["id"] for r in related]
        assert "d1" in ids
        assert "d2" in ids
        assert "d3" in ids
        # Sorted by distance
        assert related[0]["distance"] <= related[-1]["distance"]

    def test_extract_related_backward_compat(self, tracker: DeliberationTracker) -> None:
        """Without agent_id, only transport key is checked."""
        from a2a.cstp.deliberation_tracker import (
            extract_related_from_tracker,
            track_query,
        )

        track_query(
            "rpc:only-transport",
            query="test",
            result_count=1,
            top_ids=["d1"],
            retrieval_mode="hybrid",
            top_results=[
                {"id": "d1", "summary": "Transport result", "distance": 0.1},
            ],
        )

        related = extract_related_from_tracker("rpc:only-transport")
        assert len(related) == 1
        assert related[0]["id"] == "d1"

    def test_extract_related_deduplicates(self, tracker: DeliberationTracker) -> None:
        """Same decision ID in two keys is deduplicated (best distance wins)."""
        from a2a.cstp.deliberation_tracker import (
            extract_related_from_tracker,
            track_query,
        )

        track_query(
            "agent:dev",
            query="q1",
            result_count=1,
            top_ids=["d1"],
            retrieval_mode="hybrid",
            top_results=[
                {"id": "d1", "summary": "Agent version", "distance": 0.3},
            ],
        )
        track_query(
            "rpc:shared",
            query="q2",
            result_count=1,
            top_ids=["d1"],
            retrieval_mode="hybrid",
            top_results=[
                {"id": "d1", "summary": "Transport version", "distance": 0.1},
            ],
        )

        related = extract_related_from_tracker("rpc:shared", agent_id="dev")
        assert len(related) == 1
        assert related[0]["id"] == "d1"
        # Best (lowest) distance wins
        assert related[0]["distance"] == 0.1
