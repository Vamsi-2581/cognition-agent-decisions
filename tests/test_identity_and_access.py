"""Identity comes from the bearer token; agent_id arguments are only labels.

The security review ran these as attacks with two tokens, "alice" and "bob":
bob recorded decisions as alice over MCP, rewrote and reviewed her decisions,
reset a breaker that was blocking her, read her in-flight reasoning through
debugTracker, and planted thoughts that her next decision picked up.
"""

import importlib.util
import json
from typing import Any

import pytest

from a2a.auth import AuthManager, set_auth_manager
from a2a.config import AuthConfig, AuthToken, Config, _parse_names
from a2a.cstp.deliberation_tracker import (
    DeliberationTracker,
    TrackedInput,
    key_owner,
    reset_tracker,
)
from a2a.cstp.dispatcher import CstpDispatcher, register_methods
from a2a.cstp.models import MAX_TEXT_CHARS
from a2a.cstp.storage.factory import get_decision_store
from a2a.models.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    PERMISSION_DENIED,
    JsonRpcRequest,
)

MCP_INSTALLED = importlib.util.find_spec("mcp") is not None

TOKENS = {"alice": "alice-token", "bob": "bob-token", "ops": "ops-token"}


def _config() -> Config:
    return Config(
        auth=AuthConfig(
            enabled=True,
            tokens=[AuthToken(agent=a, token=t) for a, t in TOKENS.items()],
            admins=["ops"],
        )
    )


@pytest.fixture(autouse=True)
def _environment(tmp_path, monkeypatch):
    """Tokens for alice, bob and the admin ops; decisions and evidence in tmp."""
    monkeypatch.setattr("a2a.cstp.decision_service.DECISIONS_PATH", str(tmp_path / "decisions"))
    monkeypatch.setattr(
        "a2a.cstp.provenance_service.DEFAULT_PROVENANCE_DB", str(tmp_path / "provenance.db")
    )
    set_auth_manager(AuthManager(_config()))
    reset_tracker()
    yield
    reset_tracker()


@pytest.fixture
def dispatcher() -> CstpDispatcher:
    d = CstpDispatcher()
    register_methods(d)
    return d


async def _call(
    dispatcher: CstpDispatcher, agent_id: str, method: str, params: dict[str, Any]
) -> Any:
    request = JsonRpcRequest(jsonrpc="2.0", method=method, params=params, id="1")
    return await dispatcher.dispatch(request, agent_id)


async def _record(dispatcher: CstpDispatcher, agent_id: str, **params: Any) -> str:
    response = await _call(dispatcher, agent_id, "cstp.recordDecision", {
        "decision": "Use SQLite for the cache",
        "confidence": 0.8,
        "category": "architecture",
        **params,
    })
    assert response.error is None, response.error
    return str(response.result["id"])


async def _stored(decision_id: str) -> dict[str, Any]:
    data = await get_decision_store().get(decision_id)
    assert data is not None
    return data


class TestDecisionOwnership:
    async def test_agent_id_param_does_not_set_recorded_by(self, dispatcher) -> None:
        decision_id = await _record(dispatcher, "bob", agentId="alice")

        assert (await _stored(decision_id))["recorded_by"] == "bob"

    async def test_update_by_another_agent_is_denied(self, dispatcher) -> None:
        decision_id = await _record(dispatcher, "alice")

        response = await _call(dispatcher, "bob", "cstp.updateDecision", {
            "id": decision_id, "updates": {"decision": "Rewritten by bob", "confidence": 0.01},
        })

        assert response.error.code == PERMISSION_DENIED
        stored = await _stored(decision_id)
        assert stored["decision"] == "Use SQLite for the cache"
        assert stored["confidence"] == 0.8

    async def test_owner_and_admin_may_update(self, dispatcher) -> None:
        decision_id = await _record(dispatcher, "alice")

        for agent_id in ("alice", "ops"):
            response = await _call(dispatcher, agent_id, "cstp.updateDecision", {
                "id": decision_id, "updates": {"pattern": f"set by {agent_id}"},
            })
            assert response.error is None, response.error

        assert (await _stored(decision_id))["pattern"] == "set by ops"

    async def test_review_by_another_agent_is_denied(self, dispatcher) -> None:
        decision_id = await _record(dispatcher, "alice")

        response = await _call(dispatcher, "bob", "cstp.reviewDecision", {
            "id": decision_id, "outcome": "failure",
        })

        assert response.error.code == PERMISSION_DENIED
        assert (await _stored(decision_id)).get("outcome") is None

    async def test_set_preserve_by_another_agent_is_denied(self, dispatcher) -> None:
        decision_id = await _record(dispatcher, "alice")

        response = await _call(dispatcher, "bob", "cstp.setPreserve", {
            "decisionId": decision_id, "preserve": True,
        })

        assert response.error.code == PERMISSION_DENIED

    async def test_appending_a_thought_to_another_agents_decision_is_denied(
        self, dispatcher
    ) -> None:
        decision_id = await _record(dispatcher, "alice")

        response = await _call(dispatcher, "bob", "cstp.recordThought", {
            "id": decision_id, "text": "INJECTED BY BOB",
        })

        assert response.error.code == PERMISSION_DENIED
        assert "INJECTED" not in json.dumps(await _stored(decision_id), default=str)


class TestAdminOperations:
    ADMIN_METHODS = [
        "cstp.resetCircuit",
        "cstp.reindex",
        "cstp.attributeOutcomes",
        "cstp.ingestEvidence",
        "cstp.exportEvidenceBundle",
    ]

    @pytest.mark.parametrize("method", ADMIN_METHODS)
    async def test_agents_are_denied(self, dispatcher, method: str) -> None:
        response = await _call(dispatcher, "bob", method, {})
        assert response.error.code == PERMISSION_DENIED

    @pytest.mark.parametrize("method", ADMIN_METHODS)
    async def test_admins_get_past_the_check(self, dispatcher, method: str) -> None:
        # Empty params may still fail validation; only the role check matters here.
        response = await _call(dispatcher, "ops", method, {})
        assert response.error is None or response.error.code != PERMISSION_DENIED

    async def test_debug_tracker_shows_an_agent_only_its_own_sessions(self, dispatcher) -> None:
        await _call(dispatcher, "alice", "cstp.recordThought", {
            "text": "alice is weighing Redis", "agentId": "planner",
        })
        await _call(dispatcher, "bob", "cstp.recordThought", {
            "text": "bob thought", "agentId": "planner",
        })

        bob_view = (await _call(dispatcher, "bob", "cstp.debugTracker", {})).result
        admin_view = (await _call(dispatcher, "ops", "cstp.debugTracker", {})).result

        assert bob_view["sessions"] == ["bob|agent:planner"]
        assert "alice is weighing Redis" not in json.dumps(bob_view)
        assert sorted(admin_view["sessions"]) == ["alice|agent:planner", "bob|agent:planner"]

    async def test_debug_tracker_hides_a_named_session_of_another_agent(self, dispatcher) -> None:
        await _call(dispatcher, "alice", "cstp.recordThought", {
            "text": "alice is weighing Redis", "agentId": "planner",
        })

        response = await _call(dispatcher, "bob", "cstp.debugTracker", {
            "key": "alice|agent:planner",
        })

        assert response.result["sessions"] == []


class TestDeliberationIsolation:
    async def test_thought_under_another_agents_label_is_not_consumed(self, dispatcher) -> None:
        await _call(dispatcher, "bob", "cstp.recordThought", {
            "text": "INJECTED BY BOB", "agentId": "alice",
        })

        decision_id = await _record(dispatcher, "alice", agentId="alice")

        assert "INJECTED" not in json.dumps(await _stored(decision_id), default=str)

    async def test_thought_under_another_agents_decision_id_is_not_consumed(
        self, dispatcher
    ) -> None:
        await _call(dispatcher, "bob", "cstp.recordThought", {
            "text": "INJECTED BY BOB", "decisionId": "planned01",
        })

        decision_id = await _record(dispatcher, "alice", decisionId="planned01")

        assert "INJECTED" not in json.dumps(await _stored(decision_id), default=str)

    async def test_own_thoughts_are_still_attached(self, dispatcher) -> None:
        await _call(dispatcher, "alice", "cstp.recordThought", {
            "text": "Redis needs another service to run", "agentId": "planner",
        })

        decision_id = await _record(dispatcher, "alice", agentId="planner")

        assert "Redis needs another service" in json.dumps(
            await _stored(decision_id), default=str
        )

    async def test_thought_text_is_capped(self, dispatcher) -> None:
        response = await _call(dispatcher, "alice", "cstp.recordThought", {
            "text": "x" * (MAX_TEXT_CHARS + 1),
        })
        assert response.error.code == INVALID_PARAMS


HIGH_STAKES_LOW_CONFIDENCE = {
    "description": "Ship the migration tonight",
    "stakes": "high",
    "confidence": 0.3,
}


class TestGuardrailContext:
    """A caller's free-form context must not talk guardrails out of a block."""

    async def test_context_cannot_override_structured_fields(self, dispatcher) -> None:
        action = {**HIGH_STAKES_LOW_CONFIDENCE, "context": {"stakes": "low", "confidence": 0.9}}

        check = await _call(dispatcher, "bob", "cstp.checkGuardrails", {"action": action})
        pre = await _call(dispatcher, "bob", "cstp.preAction", {
            "action": action, "options": {"autoRecord": False},
        })

        assert check.result["allowed"] is False
        assert pre.result["allowed"] is False

    async def test_non_numeric_confidence_is_rejected(self, dispatcher) -> None:
        # Without auto-record, the old code let evaluation raise and allowed.
        response = await _call(dispatcher, "bob", "cstp.preAction", {
            "action": {**HIGH_STAKES_LOW_CONFIDENCE, "confidence": "x"},
            "options": {"autoRecord": False},
        })
        assert response.error.code == INVALID_PARAMS

    async def test_pre_action_blocks_when_guardrails_cannot_be_evaluated(
        self, dispatcher, monkeypatch
    ) -> None:
        async def broken(context: dict[str, Any]) -> Any:
            raise RuntimeError("evaluation failed")

        monkeypatch.setattr("a2a.cstp.preaction_service.evaluate_guardrails", broken)

        response = await _call(dispatcher, "bob", "cstp.preAction", {
            "action": {"description": "Pin pytest", "category": "tooling"},
        })

        assert response.result["allowed"] is False
        assert response.result["decisionId"] is None

    @pytest.mark.parametrize(
        ("method", "target"),
        [
            ("cstp.checkGuardrails", "a2a.cstp.dispatcher.evaluate_guardrails"),
            ("cstp.preAction", "a2a.cstp.preaction_service.evaluate_guardrails"),
        ],
    )
    async def test_agent_breakers_see_the_token_agent(
        self, dispatcher, monkeypatch, method: str, target: str
    ) -> None:
        from a2a.cstp.guardrails_service import EvaluationResult

        seen: list[dict[str, Any]] = []

        async def capture(context: dict[str, Any]) -> EvaluationResult:
            seen.append(context)
            return EvaluationResult(allowed=True, violations=[], warnings=[], evaluated=0)

        monkeypatch.setattr(target, capture)

        await _call(dispatcher, "bob", method, {
            "action": {"description": "Deploy", "context": {"agent_id": "alice"}},
            "options": {"autoRecord": False},
        })

        assert [c["agent_id"] for c in seen] == ["bob"]

    async def test_reviewed_outcome_counts_against_the_recording_agent(
        self, dispatcher, monkeypatch
    ) -> None:
        from types import SimpleNamespace

        outcomes: list[dict[str, Any]] = []

        async def record_outcome(context: dict[str, Any], outcome: str) -> None:
            outcomes.append(context)

        async def manager() -> Any:
            return SimpleNamespace(is_initialized=True, record_outcome=record_outcome)

        monkeypatch.setattr(
            "a2a.cstp.circuit_breaker_service.get_circuit_breaker_manager", manager
        )
        decision_id = await _record(dispatcher, "alice")

        await _call(dispatcher, "ops", "cstp.reviewDecision", {
            "id": decision_id, "outcome": "failure",
        })

        assert [c["agent_id"] for c in outcomes] == ["alice"]


class TestQueryLimits:
    async def test_query_text_is_capped(self, dispatcher) -> None:
        response = await _call(dispatcher, "bob", "cstp.queryDecisions", {
            "query": "a" * (MAX_TEXT_CHARS + 1),
        })
        assert response.error.code == INVALID_PARAMS

    def test_mcp_query_text_is_capped(self) -> None:
        from pydantic import ValidationError

        from a2a.mcp_schemas import QueryDecisionsInput

        with pytest.raises(ValidationError, match="at most"):
            QueryDecisionsInput(query="a" * (MAX_TEXT_CHARS + 1))


class TestInternalErrors:
    async def test_unexpected_error_detail_stays_in_the_log(self, caplog) -> None:
        async def broken(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
            raise RuntimeError("cannot open /srv/cstp/secret.db")

        dispatcher = CstpDispatcher()
        dispatcher.register("cstp.broken", broken)

        response = await _call(dispatcher, "alice", "cstp.broken", {})

        assert response.error.code == INTERNAL_ERROR
        assert "secret" not in json.dumps(response.error.to_dict())
        assert response.error.data is None
        assert "secret.db" in caplog.text


class TestTrackerBounds:
    @staticmethod
    def _input(text: str) -> TrackedInput:
        import time

        return TrackedInput(
            id=text, type="reasoning", text=text, source="test",
            timestamp=time.time(), raw_data={},
        )

    def test_sessions_are_capped_per_agent(self) -> None:
        tracker = DeliberationTracker(max_sessions_per_agent=3)
        tracker.track("alice|agent:a", self._input("alice"))
        for i in range(5):
            tracker.track(f"bob|agent:{i}", self._input(f"bob {i}"))

        sessions = tracker.debug_sessions()["sessions"]
        assert sorted(k for k in sessions if key_owner(k) == "bob") == [
            "bob|agent:2", "bob|agent:3", "bob|agent:4",
        ]
        # Bob filling his quota never evicts another agent's session.
        assert "alice|agent:a" in sessions
        evicted = tracker.debug_sessions(include_consumed=True)["consumed"]
        assert {r["key"] for r in evicted if r["status"] == "evicted"} == {
            "bob|agent:0", "bob|agent:1",
        }

    def test_inputs_are_capped_per_session(self) -> None:
        tracker = DeliberationTracker(max_inputs_per_session=2)
        for i in range(4):
            tracker.track("bob|agent:a", self._input(f"thought {i}"))

        assert [i.text for i in tracker.get_inputs("bob|agent:a")] == ["thought 2", "thought 3"]


class TestAdminConfig:
    def test_admins_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("CSTP_ADMIN_AGENTS", " ops , dashboard ,,")
        config = Config.from_env()
        assert config.auth.admins == ["ops", "dashboard"]
        assert config.auth.is_admin("dashboard")
        assert not config.auth.is_admin("bob")

    def test_admins_from_yaml(self, tmp_path) -> None:
        path = tmp_path / "server.yaml"
        path.write_text(
            "auth:\n  tokens:\n    - agent: ops\n      token: t\n  admins: [ops]\n",
            encoding="utf-8",
        )
        assert Config.from_yaml(path).auth.admins == ["ops"]

    def test_with_auth_disabled_everyone_is_admin(self) -> None:
        assert AuthConfig(enabled=False).is_admin("anonymous")

    def test_name_list_parsing(self) -> None:
        assert _parse_names("") == []

    def test_agent_name_with_namespace_separator_is_rejected(self) -> None:
        # Tracker keys are "{agent}|...": agent "a" would see agent "a|b"'s sessions.
        with pytest.raises(ValueError, match=r"\|"):
            AuthToken(agent="a|b", token="t")


@pytest.mark.skipif(not MCP_INSTALLED, reason="mcp SDK not installed")
class TestMcpIdentity:
    """Identity over the real MCP Streamable HTTP transport, end to end."""

    _HEADERS = {"Accept": "application/json, text/event-stream"}

    def _open_session(self, client: Any, token: str) -> dict[str, str]:
        headers = {**self._HEADERS, "Authorization": f"Bearer {token}"}
        response = client.post("/mcp/", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        })
        assert response.status_code == 200, response.text
        headers["mcp-session-id"] = response.headers["mcp-session-id"]
        client.post(
            "/mcp/", headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return headers

    def _tool(self, client: Any, headers: dict[str, str], name: str, arguments: dict) -> dict:
        response = client.post("/mcp/", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        assert response.status_code == 200, response.text
        return dict(json.loads(response.json()["result"]["content"][0]["text"]))

    @pytest.fixture
    def client(self) -> Any:
        from fastapi.testclient import TestClient

        from a2a.server import create_app

        with TestClient(create_app(_config())) as client:
            yield client

    async def test_agent_id_argument_does_not_set_recorded_by(self, client) -> None:
        log = {"decision": "Use SQLite", "confidence": 0.8, "category": "process",
               "agent_id": "alice"}

        alice = self._tool(client, self._open_session(client, TOKENS["alice"]),
                           "log_decision", log)
        bob = self._tool(client, self._open_session(client, TOKENS["bob"]),
                         "log_decision", log)

        assert (await _stored(alice["id"]))["recorded_by"] == "alice"
        assert (await _stored(bob["id"]))["recorded_by"] == "bob"

    async def test_another_tokens_session_id_does_not_carry_its_identity(self, client) -> None:
        alice_headers = self._open_session(client, TOKENS["alice"])
        decision = self._tool(client, alice_headers, "log_decision", {
            "decision": "Use SQLite", "confidence": 0.8, "category": "process",
        })

        # Bob replays alice's session id with his own token.
        hijack = {**alice_headers, "Authorization": f"Bearer {TOKENS['bob']}"}
        result = self._tool(client, hijack, "update_decision", {
            "id": decision["id"], "decision": "Rewritten by bob",
        })

        assert result["error"] == "permission_denied"
        assert (await _stored(decision["id"]))["decision"] == "Use SQLite"


@pytest.mark.skipif(not MCP_INSTALLED, reason="mcp SDK not installed")
class TestMcpCallerResolution:
    """get_caller_agent_id fails closed when a session call lacks a token."""

    @staticmethod
    def _in_request(request: Any) -> Any:
        from unittest.mock import MagicMock

        from mcp.server.lowlevel.server import request_ctx
        from mcp.shared.context import RequestContext

        return request_ctx.set(RequestContext(
            request_id=1, meta=None, session=MagicMock(), lifespan_context=None,
            request=request,
        ))

    def _resolve(self, request: Any) -> str:
        from mcp.server.lowlevel.server import request_ctx

        from a2a.mcp_server import get_caller_agent_id

        token = self._in_request(request)
        try:
            return get_caller_agent_id()
        finally:
            request_ctx.reset(token)

    def test_reads_the_agent_from_request_state(self) -> None:
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace(agent_id="bob"))
        assert self._resolve(request) == "bob"

    def test_request_without_agent_is_refused(self) -> None:
        from types import SimpleNamespace

        from a2a.cstp.access import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            self._resolve(SimpleNamespace(state=SimpleNamespace()))

    def test_session_call_without_request_is_refused_outside_stdio(self) -> None:
        from a2a.cstp.access import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            self._resolve(None)
