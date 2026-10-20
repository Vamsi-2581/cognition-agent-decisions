"""Recording must stay cheap whatever text a caller sends.

Bridge extraction runs regexes over decision, reason and context text
synchronously on the single-worker event loop. Its file-path pattern was
quadratic in the length of a path-like run (64 KB took 1.7 s), and no free-text
field had a length limit, so one request could stall every other caller.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from a2a.cstp.bridge_abstractors import _strip_specifics
from a2a.cstp.decision_service import Reason, RecordDecisionRequest
from a2a.cstp.dispatcher import _handle_record_decision
from a2a.cstp.models import (
    MAX_CONTEXT_CHARS,
    MAX_TEXT_CHARS,
    ActionContext,
    PreActionRequest,
)
from a2a.mcp_schemas import (
    CheckActionInput,
    LogDecisionInput,
    PreActionActionInput,
    PreActionInput,
    ReasonInput,
)

TOO_LONG = "x" * (MAX_TEXT_CHARS + 1)


class TestPathRegex:
    def test_long_path_like_run_is_linear(self) -> None:
        # The old pattern needed ~17 s here; the bound is loose for slow CI.
        started = time.perf_counter()
        _strip_specifics("a/" * 100_000)
        assert time.perf_counter() - started < 1.0

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("edit src/foo.py now", "edit a file now"),
            ("a/b.py and c/d/e.md", "a file and a file"),
            ("/etc/nginx/nginx.conf", "a file"),
            ("foo/bar", "foo/bar"),
        ],
    )
    def test_paths_still_abstracted(self, text: str, expected: str) -> None:
        assert _strip_specifics(text) == expected


def _request(**overrides: object) -> RecordDecisionRequest:
    fields: dict[str, object] = {
        "decision": "Use SQLite",
        "confidence": 0.8,
        "category": "architecture",
    }
    fields.update(overrides)
    return RecordDecisionRequest(**fields)  # type: ignore[arg-type]


class TestRecordDecisionRequestLimits:
    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"decision": TOO_LONG}, "decision"),
            ({"context": "c" * (MAX_CONTEXT_CHARS + 1)}, "context"),
            ({"pattern": TOO_LONG}, "pattern"),
            ({"reasons": [Reason(type="analysis", text=TOO_LONG)]}, "reasons[0].text"),
        ],
    )
    def test_over_long_field_is_rejected(self, overrides: dict, field: str) -> None:
        errors = _request(**overrides).validate()
        assert any(e.startswith(f"{field}:") for e in errors), errors

    def test_fields_at_the_limit_are_accepted(self) -> None:
        req = _request(
            decision="d" * MAX_TEXT_CHARS,
            context="c" * MAX_CONTEXT_CHARS,
            pattern="p" * MAX_TEXT_CHARS,
            reasons=[Reason(type="analysis", text="r" * MAX_TEXT_CHARS)],
        )
        assert req.validate() == []


class TestRecordDecisionRpc:
    async def test_rejected_before_bridge_extraction(self) -> None:
        bridge = AsyncMock(return_value=(False, None))
        record = AsyncMock()
        params = {
            "decision": "Use SQLite",
            "confidence": 0.8,
            "category": "architecture",
            "reasons": [{"type": "analysis", "text": "a/" * 50_000}],
        }

        with (
            patch("a2a.cstp.bridge_hook.maybe_smart_extract_bridge", bridge),
            patch("a2a.cstp.dispatcher.record_decision", record),
            pytest.raises(ValueError, match=r"reasons\[0\]\.text"),
        ):
            await _handle_record_decision(params, "agent-1")

        bridge.assert_not_awaited()
        record.assert_not_awaited()


class TestPreActionAutoRecord:
    async def _run(self, params: dict) -> tuple[object, AsyncMock, AsyncMock]:
        from a2a.cstp.preaction_service import pre_action

        bridge = AsyncMock(return_value=(False, None))
        record = AsyncMock(return_value=SimpleNamespace(success=True, id="new12345"))
        with (
            patch(
                "a2a.cstp.preaction_service.query_decisions",
                AsyncMock(return_value=SimpleNamespace(results=[], error=None)),
            ),
            patch(
                "a2a.cstp.preaction_service.evaluate_guardrails",
                AsyncMock(return_value=SimpleNamespace(
                    allowed=True, violations=[], warnings=[], evaluated=0,
                )),
            ),
            patch(
                "a2a.cstp.preaction_service.get_calibration",
                AsyncMock(return_value=SimpleNamespace(overall=None)),
            ),
            patch("a2a.cstp.preaction_service.log_guardrail_check"),
            patch("a2a.cstp.preaction_service.record_decision", record),
            patch("a2a.cstp.bridge_hook.maybe_smart_extract_bridge", bridge),
            patch("a2a.cstp.graph_service.safe_auto_link", AsyncMock(return_value=0)),
        ):
            resp = await pre_action(PreActionRequest.from_params(params), agent_id="agent-1")
        return resp, bridge, record

    async def test_over_long_reason_is_not_recorded(self) -> None:
        resp, bridge, record = await self._run({
            "action": {"description": "Use SQLite", "category": "architecture"},
            "reasons": [{"type": "analysis", "text": TOO_LONG}],
        })

        assert resp.decision_id is None  # type: ignore[attr-defined]
        bridge.assert_not_awaited()
        record.assert_not_awaited()

    async def test_only_the_caps_apply(self) -> None:
        # pre_action never validated category; enforcing the caps must not start
        # dropping decisions that use a custom one.
        resp, _, record = await self._run({
            "action": {"description": "Pin pytest", "category": "testing"},
        })

        assert resp.decision_id == "new12345"  # type: ignore[attr-defined]
        record.assert_awaited_once()


def test_action_description_is_capped() -> None:
    with pytest.raises(ValueError, match="action.description"):
        ActionContext.from_dict({"description": TOO_LONG})


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (LogDecisionInput, {"decision": TOO_LONG, "confidence": 0.5, "category": "process"}),
        (
            LogDecisionInput,
            {
                "decision": "ok",
                "confidence": 0.5,
                "category": "process",
                "context": "c" * (MAX_CONTEXT_CHARS + 1),
            },
        ),
        (ReasonInput, {"type": "analysis", "text": TOO_LONG}),
        (PreActionActionInput, {"description": TOO_LONG}),
        (PreActionInput, {"action": {"description": "ok"}, "pattern": TOO_LONG}),
        (CheckActionInput, {"description": TOO_LONG}),
    ],
)
def test_mcp_inputs_are_capped(model: type, fields: dict) -> None:
    with pytest.raises(ValidationError, match="at most"):
        model(**fields)
