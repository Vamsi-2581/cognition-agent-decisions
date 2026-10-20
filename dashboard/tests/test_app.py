"""Tests for Flask routes."""
from datetime import UTC, datetime
from html.parser import HTMLParser
from unittest.mock import MagicMock

from flask.testing import FlaskClient

from dashboard.app import _map_sort
from dashboard.models import Decision, GraphNeighbor, Reason


def test_health_check_ok(client: FlaskClient, mock_cstp: MagicMock) -> None:
    """Test health endpoint returns 200 when CSTP is healthy."""
    mock_cstp.health_check = MagicMock(return_value=True)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.data == b"OK"


def test_health_check_fail(client: FlaskClient, mock_cstp: MagicMock) -> None:
    """Test health endpoint returns 503 when CSTP is down."""
    mock_cstp.health_check = MagicMock(return_value=False)
    response = client.get("/health")
    assert response.status_code == 503


def test_index_renders_overview(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test root renders overview dashboard."""
    response = client.get("/", headers=auth_headers)
    assert response.status_code == 200


def test_decisions_requires_auth(client: FlaskClient) -> None:
    """Test decisions page requires authentication."""
    response = client.get("/decisions")
    assert response.status_code == 401


def test_decisions_list(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions list renders."""
    mock_cstp.list_decisions = MagicMock(return_value=([], 0))
    response = client.get("/decisions", headers=auth_headers)
    assert response.status_code == 200
    assert b"Decisions" in response.data


def test_calibration_page(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test calibration page renders with stats."""
    response = client.get("/calibration", headers=auth_headers)
    assert response.status_code == 200
    assert b"Calibration" in response.data
    assert b"90%" in response.data  # accuracy from mock


# --- Issue #150: decision_detail route with graph_neighbors ---


def _make_decision(
    summary: str = "Short summary",
    agent_id: str | None = None,
    reasons: list[Reason] | None = None,
) -> Decision:
    """Helper to create a test Decision."""
    return Decision(
        id="abcd1234",
        summary=summary,
        category="architecture",
        stakes="medium",
        confidence=0.85,
        created_at=datetime(2026, 2, 16, 12, 0, 0, tzinfo=UTC),
        agent_id=agent_id,
        reasons=reasons or [],
    )


def test_decision_detail_with_graph_neighbors(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decision_detail route passes graph_neighbors to template."""
    decision = _make_decision()
    mock_cstp.get_decision = MagicMock(return_value=decision)
    mock_cstp.get_neighbors = MagicMock(return_value=[
        GraphNeighbor(
            id="neigh001",
            summary="Use cursor pagination for lists",
            category="process",
            edge_type="relates_to",
            weight=0.7,
            direction="outgoing",
        ),
    ])

    response = client.get("/decisions/abcd1234", headers=auth_headers)

    assert response.status_code == 200
    assert b"Graph Neighbors" in response.data
    assert b"neigh001" in response.data
    assert b"relates to" in response.data  # edge_type rendered with replace('_', ' ')
    assert b"Use cursor pagination" in response.data  # summary text shown


def test_decision_detail_graph_neighbors_error_isolation(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decision_detail still renders when get_neighbors raises."""
    decision = _make_decision()
    mock_cstp.get_decision = MagicMock(return_value=decision)
    mock_cstp.get_neighbors = MagicMock(side_effect=Exception("Network error"))

    response = client.get("/decisions/abcd1234", headers=auth_headers)

    assert response.status_code == 200
    # Page renders without graph neighbors section
    assert b"Graph Neighbors" not in response.data


def test_decision_detail_no_neighbors(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decision_detail with empty graph_neighbors."""
    decision = _make_decision()
    mock_cstp.get_decision = MagicMock(return_value=decision)
    mock_cstp.get_neighbors = MagicMock(return_value=[])

    response = client.get("/decisions/abcd1234", headers=auth_headers)

    assert response.status_code == 200
    assert b"Graph Neighbors" not in response.data


# --- Issue #150: Full decision text card rendering ---


def test_decision_detail_shows_full_text_long_summary(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test full decision text card is shown when summary > 80 chars."""
    long_summary = "A" * 100  # 100 chars > 80 threshold
    decision = _make_decision(summary=long_summary)
    mock_cstp.get_decision = MagicMock(return_value=decision)
    mock_cstp.get_neighbors = MagicMock(return_value=[])

    response = client.get("/decisions/abcd1234", headers=auth_headers)

    assert response.status_code == 200
    assert b"decision-full-text" in response.data


def test_decision_detail_hides_full_text_short_summary(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test full decision text card is hidden when summary <= 80 chars."""
    short_summary = "B" * 60  # 60 chars <= 80 threshold
    decision = _make_decision(summary=short_summary)
    mock_cstp.get_decision = MagicMock(return_value=decision)
    mock_cstp.get_neighbors = MagicMock(return_value=[])

    response = client.get("/decisions/abcd1234", headers=auth_headers)

    assert response.status_code == 200
    assert b"decision-full-text" not in response.data


# --- Issue #150: Reason strength bars rendering ---


def test_decision_detail_shows_reason_strength_bars(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test reason strength bars render in template."""
    decision = _make_decision(reasons=[
        Reason(type="analysis", text="Strong analysis", strength=0.95),
        Reason(type="pattern", text="Weak pattern", strength=0.3),
    ])
    mock_cstp.get_decision = MagicMock(return_value=decision)
    mock_cstp.get_neighbors = MagicMock(return_value=[])

    response = client.get("/decisions/abcd1234", headers=auth_headers)

    assert response.status_code == 200
    assert b"reason-strength" in response.data
    assert b"95%" in response.data  # high strength percentage
    assert b"30%" in response.data  # low strength percentage
    # Check color coding: high gets --high, low gets --low
    assert b"confidence-bar-fill--high" in response.data
    assert b"confidence-bar-fill--low" in response.data


# --- Issue #150: recorded_by shown as Agent badge ---


def test_decision_detail_shows_agent_from_recorded_by(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test agent badge renders when agent_id is set (from recorded_by)."""
    decision = _make_decision(agent_id="test-agent")
    mock_cstp.get_decision = MagicMock(return_value=decision)
    mock_cstp.get_neighbors = MagicMock(return_value=[])

    response = client.get("/decisions/abcd1234", headers=auth_headers)

    assert response.status_code == 200
    assert b"test-agent" in response.data
    assert b"Agent" in response.data


# --- Issue #150: Graph neighbor direction arrows ---


def test_decision_detail_graph_neighbor_directions(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test graph neighbor cards show correct direction arrows."""
    decision = _make_decision()
    mock_cstp.get_decision = MagicMock(return_value=decision)
    mock_cstp.get_neighbors = MagicMock(return_value=[
        GraphNeighbor(
            id="out00001",
            edge_type="depends_on",
            weight=0.8,
            direction="outgoing",
        ),
        GraphNeighbor(
            id="inc00001",
            edge_type="supersedes",
            weight=0.6,
            direction="incoming",
        ),
    ])

    response = client.get("/decisions/abcd1234", headers=auth_headers)

    assert response.status_code == 200
    html = response.data.decode()
    # Outgoing arrow: right arrow entity &#8594;
    assert "&#8594;" in html
    # Incoming arrow: left arrow entity &#8592;
    assert "&#8592;" in html
    # Edge type badges
    assert "depends on" in html
    assert "supersedes" in html


# --- Issue #177: _map_sort helper ---


def test_map_sort_default() -> None:
    """Test _map_sort returns default sort when None."""
    assert _map_sort(None) == ("created_at", "desc")


def test_map_sort_reverse_date() -> None:
    """Test _map_sort handles -date (oldest first)."""
    assert _map_sort("-date") == ("created_at", "asc")


def test_map_sort_confidence() -> None:
    """Test _map_sort handles confidence and -confidence."""
    assert _map_sort("confidence") == ("confidence", "desc")
    assert _map_sort("-confidence") == ("confidence", "asc")


def test_map_sort_category() -> None:
    """Test _map_sort handles category sort."""
    assert _map_sort("category") == ("category", "asc")


def test_map_sort_unknown() -> None:
    """Test _map_sort falls back to default for unknown values."""
    assert _map_sort("unknown") == ("created_at", "desc")


# --- Issue #177: Overview route uses get_stats ---


def test_overview_calls_get_stats(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test overview page calls get_stats for server-side aggregation."""
    response = client.get("/", headers=auth_headers)

    assert response.status_code == 200
    mock_cstp.get_stats.assert_called()


def test_overview_calls_get_calibration(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test overview page still calls get_calibration for calibration stats."""
    response = client.get("/", headers=auth_headers)

    assert response.status_code == 200
    mock_cstp.get_calibration.assert_called_once()


def test_overview_calls_list_decisions_small_batch(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test overview fetches small batch of decisions (not 500)."""
    response = client.get("/", headers=auth_headers)

    assert response.status_code == 200
    mock_cstp.list_decisions.assert_called()
    # Check the limit is reasonable (50 or less, not 500)
    call_kwargs = mock_cstp.list_decisions.call_args
    if call_kwargs[1]:
        limit = call_kwargs[1].get("limit")
    else:
        limit = call_kwargs[0][0] if call_kwargs[0] else None
    # The call should use limit=50 based on implementation
    assert limit is not None
    assert limit <= 50


def test_overview_period_all(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test overview with period=all skips redundant all-time get_stats call."""
    response = client.get("/?period=all", headers=auth_headers)

    assert response.status_code == 200
    # period=all means date_from/date_to are both None, so the filtered call
    # already returns all-time data — no second call needed
    assert mock_cstp.get_stats.call_count == 1


def test_overview_period_today_calls_get_stats_twice(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test overview with period=today calls get_stats twice (filtered + all-time)."""
    response = client.get("/?period=today", headers=auth_headers)

    assert response.status_code == 200
    # First call is date-filtered, second is all-time for the total comparison
    assert mock_cstp.get_stats.call_count == 2


def test_overview_handles_stats_error(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test overview handles get_stats error gracefully."""
    # Import from the same path app.py uses (cstp_client, not dashboard.cstp_client)
    from cstp_client import CSTPError
    mock_cstp.get_stats = MagicMock(side_effect=CSTPError("stats error"))

    response = client.get("/", headers=auth_headers)

    assert response.status_code == 200  # Should still render


def test_overview_handles_list_decisions_error(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test overview handles list_decisions error gracefully."""
    from cstp_client import CSTPError
    mock_cstp.list_decisions = MagicMock(side_effect=CSTPError("list error"))

    response = client.get("/", headers=auth_headers)

    assert response.status_code == 200  # Should still render


# --- Issue #177: Decisions route server-side filtering ---


def _make_decisions_list(count: int = 3) -> list[Decision]:
    """Helper to create a list of test decisions."""
    return [
        Decision(
            id=f"dec{i:05d}",
            summary=f"Decision {i}",
            category="architecture" if i % 2 == 0 else "process",
            stakes="medium" if i % 2 == 0 else "high",
            confidence=0.5 + i * 0.1,
            created_at=datetime(2026, 2, 18, 12, 0, i, tzinfo=UTC),
            outcome="success" if i == 0 else None,
        )
        for i in range(count)
    ]


def test_decisions_browse_uses_list_decisions(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions page without search uses list_decisions (server-side)."""
    mock_cstp.list_decisions = MagicMock(return_value=(_make_decisions_list(), 3))

    response = client.get("/decisions", headers=auth_headers)

    assert response.status_code == 200
    mock_cstp.list_decisions.assert_called()
    mock_cstp.search_decisions.assert_not_called()


def test_decisions_search_uses_search_decisions(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions page with search uses search_decisions (semantic)."""
    mock_cstp.search_decisions = MagicMock(
        return_value=(_make_decisions_list(1), 1)
    )

    response = client.get("/decisions?search=architecture", headers=auth_headers)

    assert response.status_code == 200
    mock_cstp.search_decisions.assert_called_once()
    # search_decisions should get the query
    call_kwargs = mock_cstp.search_decisions.call_args
    assert call_kwargs[1].get("query") == "architecture" or call_kwargs[0][0] == "architecture"


def test_decisions_passes_category_filter(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions page passes category filter to list_decisions."""
    mock_cstp.list_decisions = MagicMock(return_value=([], 0))

    response = client.get("/decisions?category=architecture", headers=auth_headers)

    assert response.status_code == 200
    call_kwargs = mock_cstp.list_decisions.call_args
    assert call_kwargs[1].get("category") == "architecture"


def test_decisions_passes_stakes_filter(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions page passes stakes filter to list_decisions."""
    mock_cstp.list_decisions = MagicMock(return_value=([], 0))

    response = client.get("/decisions?stakes=high", headers=auth_headers)

    assert response.status_code == 200
    call_kwargs = mock_cstp.list_decisions.call_args
    assert call_kwargs[1].get("stakes") == "high"


def test_decisions_passes_status_filter(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions page passes status filter to list_decisions."""
    mock_cstp.list_decisions = MagicMock(return_value=([], 0))

    response = client.get("/decisions?status=reviewed", headers=auth_headers)

    assert response.status_code == 200
    call_kwargs = mock_cstp.list_decisions.call_args
    assert call_kwargs[1].get("status") == "reviewed"


def test_decisions_passes_sort_params(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions page translates sort param to server-side columns."""
    mock_cstp.list_decisions = MagicMock(return_value=([], 0))

    response = client.get("/decisions?sort=confidence", headers=auth_headers)

    assert response.status_code == 200
    call_kwargs = mock_cstp.list_decisions.call_args
    assert call_kwargs[1].get("sort") == "confidence"
    assert call_kwargs[1].get("order") == "desc"


def test_decisions_pagination_offset(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions page calculates correct offset for pagination."""
    mock_cstp.list_decisions = MagicMock(return_value=([], 40))

    response = client.get("/decisions?page=3", headers=auth_headers)

    assert response.status_code == 200
    call_kwargs = mock_cstp.list_decisions.call_args
    # Page 3 with per_page=20 means offset=40
    assert call_kwargs[1].get("offset") == 40


def test_decisions_handles_cstp_error(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions page handles CSTPError gracefully."""
    from cstp_client import CSTPError
    mock_cstp.list_decisions = MagicMock(side_effect=CSTPError("connection error"))

    response = client.get("/decisions", headers=auth_headers)

    assert response.status_code == 200  # Should still render with empty results


# --- Issue #177: decisions_partial HTMX route ---


def test_decisions_partial_renders(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions_partial returns partial template for HTMX."""
    mock_cstp.list_decisions = MagicMock(return_value=(_make_decisions_list(), 3))

    response = client.get("/decisions/partial", headers=auth_headers)

    assert response.status_code == 200


def test_decisions_partial_passes_filters(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions_partial passes all filter params."""
    mock_cstp.list_decisions = MagicMock(return_value=([], 0))

    response = client.get(
        "/decisions/partial?category=process&stakes=high&status=pending&sort=-date",
        headers=auth_headers,
    )

    assert response.status_code == 200
    call_kwargs = mock_cstp.list_decisions.call_args
    assert call_kwargs[1].get("category") == "process"
    assert call_kwargs[1].get("stakes") == "high"
    assert call_kwargs[1].get("status") == "pending"


def test_decisions_partial_search_branch(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test decisions_partial with search uses search_decisions."""
    mock_cstp.search_decisions = MagicMock(return_value=([], 0))

    response = client.get(
        "/decisions/partial?search=test+query", headers=auth_headers,
    )

    assert response.status_code == 200
    mock_cstp.search_decisions.assert_called_once()


# --- Issue #177: Search with client-side filtering fallback ---


def test_search_with_stakes_filter(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test search path applies client-side stakes filtering."""
    decisions = _make_decisions_list(4)
    decisions[0].stakes = "high"
    decisions[1].stakes = "low"
    decisions[2].stakes = "high"
    decisions[3].stakes = "medium"
    mock_cstp.search_decisions = MagicMock(return_value=(decisions, 4))

    response = client.get(
        "/decisions?search=test&stakes=high", headers=auth_headers,
    )

    assert response.status_code == 200


def test_search_with_status_filter(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Test search path applies client-side status filtering."""
    decisions = _make_decisions_list(3)
    decisions[0].outcome = "success"  # reviewed
    decisions[1].outcome = None  # pending
    decisions[2].outcome = "partial"  # reviewed
    mock_cstp.search_decisions = MagicMock(return_value=(decisions, 3))

    response = client.get(
        "/decisions?search=test&status=reviewed", headers=auth_headers,
    )

    assert response.status_code == 200


def _collect_attrs(html: str) -> list[tuple[str, str]]:
    """Return every (attribute, unescaped value) pair in the document."""
    found: list[tuple[str, str]] = []

    class _Collector(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            found.extend((name, value or "") for name, value in attrs)

    _Collector().feed(html)
    return found


def test_deliberation_keys_never_reach_alpine_expressions(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Regression: stored XSS through a tracker key.

    Keys embed the caller's agent_id. The template escaped quotes but not
    backslashes before splicing the key into Alpine expressions, so an agent_id
    ending in a backslash closed the string and the rest ran as JS for whoever
    opened the page. Keys now travel as HTML-escaped data attributes.
    """
    evil = "agent:x\\');alert(document.domain);//"
    mock_cstp.debug_tracker = MagicMock(return_value={
        "sessions": [evil],
        "sessionCount": 1,
        "detail": {evil: {
            "inputCount": 1,
            "inputs": [{"type": "thought", "text": "hi", "ageSeconds": 5}],
        }},
        "consumed": [{"key": evil, "inputCount": 1, "status": "consumed", "consumedAt": 5}],
    })

    response = client.get("/deliberation/partial", headers=auth_headers)

    assert response.status_code == 200
    attrs = _collect_attrs(response.get_data(as_text=True))
    alpine = [value for name, value in attrs if name.startswith(("@", ":", "x-"))]
    assert alpine, "expected Alpine bindings in the partial"
    assert not any("alert" in value for value in alpine)
    data_keys = sorted(value for name, value in attrs if name == "data-key")
    assert data_keys == sorted([evil, f"consumed:{evil}"])


def test_breaker_reset_button_handler_is_intact(
    client: FlaskClient, auth_headers: dict[str, str], mock_cstp: MagicMock
) -> None:
    """Regression: `openReset({{ b.scope|tojson }})` sat in a double-quoted
    attribute, so the JSON's own quotes ended it and the handler parsed as
    `$store.breakers.openReset(` - the Reset button did nothing."""
    scope = "tag:O'Malley \"x\""
    mock_cstp.list_breakers = MagicMock(return_value=[{
        "scope": scope, "state": "open", "failure_count": 3, "failure_threshold": 3,
        "window_ms": 60_000, "cooldown_ms": 60_000, "cooldown_remaining_ms": 1_000,
    }])

    response = client.get("/breakers/partial", headers=auth_headers)

    assert response.status_code == 200
    attrs = dict(_collect_attrs(response.get_data(as_text=True)))
    assert attrs["@click"] == "$store.breakers.openReset($el.dataset.scope)"
    assert attrs["data-scope"] == scope
