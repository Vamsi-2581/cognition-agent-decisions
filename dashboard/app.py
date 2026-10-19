"""CSTP Dashboard Flask application."""
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from os import environ
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, Response, flash, redirect, render_template, request, url_for
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError

from auth import requires_auth
from config import config, enforce_security_config
from cstp_client import CSTPClient, CSTPError

# Fail closed at import, not in main(): gunicorn imports `app:app` without ever
# calling main(), so a guard placed there does not run in any deployment we ship.
enforce_security_config()

# Initialize Flask app
app = Flask(__name__)
app.secret_key = config.secret_key

# Enable CSRF protection
csrf = CSRFProtect(app)

# Initialize CSTP client (module-level, reused across requests for connection pooling)
cstp = CSTPClient(config.cstp_url, config.cstp_token)

# Auth decorator bound to config
auth = requires_auth(config)


@app.errorhandler(CSRFError)
def handle_csrf_error(e: CSRFError) -> tuple[str, int]:
    """Handle CSRF validation errors."""
    flash("Session expired. Please try again.", "error")
    return redirect(url_for("decisions")), 400


@app.route("/health")
@csrf.exempt  # Health check doesn't need CSRF
def health() -> Response:
    """Health check endpoint (no auth required).
    
    Returns 200 OK if CSTP server is reachable, 503 otherwise.
    """
    if cstp.health_check():
        return Response("OK", status=200)
    return Response("CSTP unavailable", status=503)


@app.route("/")
@auth
def index() -> str:
    """Overview dashboard with aggregated stats and date filtering."""
    # Parse date filter
    period = request.args.get("period", "today")
    custom_date = request.args.get("date")
    tz_name = request.args.get("tz") or request.cookies.get("tz") or "America/New_York"

    try:
        user_tz = ZoneInfo(tz_name)
    except (KeyError, ValueError):
        user_tz = ZoneInfo("America/New_York")

    # Calculate "now" and "today" in the user's timezone
    now_utc = datetime.now(UTC)
    now_local = now_utc.astimezone(user_tz)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "custom" and custom_date:
        try:
            # Parse as local date, convert to UTC
            local_dt = datetime.strptime(custom_date, "%Y-%m-%d").replace(tzinfo=user_tz)
            date_from = local_dt.astimezone(UTC)
            date_to = (local_dt + timedelta(days=1)).astimezone(UTC)
        except ValueError:
            date_from = today_local.astimezone(UTC)
            date_to = (today_local + timedelta(days=1)).astimezone(UTC)
    elif period == "week":
        week_start = today_local - timedelta(days=today_local.weekday())
        date_from = week_start.astimezone(UTC)
        date_to = now_utc + timedelta(seconds=1)
    elif period == "month":
        month_start = today_local.replace(day=1)
        date_from = month_start.astimezone(UTC)
        date_to = now_utc + timedelta(seconds=1)
    elif period == "all":
        date_from = None
        date_to = None
    else:
        # Default: today in user's timezone
        period = "today"
        date_from = today_local.astimezone(UTC)
        date_to = (today_local + timedelta(days=1)).astimezone(UTC)

    # Convert datetime to ISO strings for server-side filtering
    date_from_str: str | None = None
    date_to_str: str | None = None
    if date_from is not None:
        date_from_str = date_from.strftime("%Y-%m-%dT%H:%M:%S")
    if date_to is not None:
        date_to_str = date_to.strftime("%Y-%m-%dT%H:%M:%S")

    # Fetch calibration stats (always all-time)
    stats = cstp.get_calibration()

    # Fetch server-side aggregated stats (with date filter)
    try:
        stats_data = cstp.get_stats(
            date_from=date_from_str, date_to=date_to_str,
        )
    except CSTPError:
        stats_data = {}

    # Fetch recent decisions for the activity list (server-side filtered)
    try:
        decisions_list, _ = cstp.list_decisions(
            limit=50, date_from=date_from_str, date_to=date_to_str,
        )
    except CSTPError:
        decisions_list = []

    # Extract aggregated breakdowns from server stats (normalized to existing shapes)
    cat_counts: dict[str, int] = stats_data.get("byCategory", {})
    stakes_counts: dict[str, int] = stats_data.get("byStakes", {})
    outcome_counts: dict[str, int] = stats_data.get("byStatus", {})
    top_tags: list[tuple[str, int]] = [
        (t["tag"], t["count"]) for t in stats_data.get("topTags", [])
    ]
    total_filtered = stats_data.get("total", len(decisions_list))

    # Fetch all-time total (no date filter) for comparison
    # Skip when already showing all-time (date_from/date_to both None)
    if date_from is None and date_to is None:
        total_all = total_filtered
    else:
        try:
            all_time_stats = cstp.get_stats()
            total_all = all_time_stats.get("total", total_filtered)
        except CSTPError:
            total_all = total_filtered

    return render_template(
        "overview.html",
        stats=stats,
        decisions_list=decisions_list,
        total=total_filtered,
        total_all=total_all,
        cat_counts=cat_counts,
        stakes_counts=stakes_counts,
        outcome_counts=outcome_counts,
        top_tags=top_tags,
        period=period,
        custom_date=custom_date or today_local.strftime("%Y-%m-%d"),
        today_str=today_local.strftime("%Y-%m-%d"),
        tz_name=tz_name,
    )


def _map_sort(sort: str | None) -> tuple[str, str]:
    """Map dashboard sort parameter to (column, order) for cstp.listDecisions."""
    match sort:
        case "confidence":
            return ("confidence", "desc")
        case "-confidence":
            return ("confidence", "asc")
        case "category":
            return ("category", "asc")
        case "-date":
            return ("created_at", "asc")
        case _:
            return ("created_at", "desc")


def _get_decisions(
    page: int = 1,
    per_page: int = 20,
    category: str | None = None,
    status: str | None = None,
    stakes: str | None = None,
    search: str | None = None,
    sort: str | None = None,
) -> tuple[list, int, int, int]:
    """Shared logic for server-side filtered, sorted, paginated decisions.

    Uses cstp.listDecisions for structured queries and cstp.queryDecisions
    (via search_decisions) for semantic search.

    Returns:
        (page_decisions, total, page, total_pages)
    """
    sort_col, order = _map_sort(sort)
    offset = (page - 1) * per_page

    try:
        if search:
            # Semantic search via queryDecisions (no server-side pagination).
            # Results are returned in relevance order — sort param is intentionally
            # not applied here to preserve semantic ranking.
            all_results, total = cstp.search_decisions(
                query=search, limit=200, category=category,
            )
            # Client-side filtering for fields not supported by queryDecisions
            if stakes:
                all_results = [d for d in all_results if d.stakes == stakes]
            if status:
                all_results = [
                    d for d in all_results
                    if (d.outcome is not None) == (status == "reviewed")
                ]
            total = len(all_results)
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)
            start = (page - 1) * per_page
            page_decisions = all_results[start : start + per_page]
        else:
            # Server-side filtering, sorting, and pagination
            page_decisions, total = cstp.list_decisions(
                limit=per_page,
                offset=offset,
                category=category,
                stakes=stakes,
                status=status,
                sort=sort_col,
                order=order,
            )
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)
    except CSTPError:
        return [], 0, page, 1

    return page_decisions, total, page, total_pages


@app.route("/decisions")
@auth
def decisions() -> str:
    """List all decisions with pagination and filters."""
    page = request.args.get("page", 1, type=int)
    category = request.args.get("category") or None
    status = request.args.get("status") or None
    stakes = request.args.get("stakes") or None
    search = request.args.get("search") or None
    sort = request.args.get("sort") or None

    decision_list, total, page, total_pages = _get_decisions(
        page=page, category=category, status=status,
        stakes=stakes, search=search, sort=sort,
    )

    return render_template(
        "decisions.html",
        decisions=decision_list,
        page=page,
        total_pages=total_pages,
        total=total,
        category=category,
        status=status,
        stakes=stakes,
        search=search,
        sort=sort,
    )


@app.route("/decisions/partial")
@auth
def decisions_partial() -> str:
    """Return just the table rows for HTMX partial swap."""
    page = request.args.get("page", 1, type=int)
    category = request.args.get("category") or None
    status = request.args.get("status") or None
    stakes = request.args.get("stakes") or None
    search = request.args.get("search") or None
    sort = request.args.get("sort") or None

    decision_list, total, page, total_pages = _get_decisions(
        page=page, category=category, status=status,
        stakes=stakes, search=search, sort=sort,
    )

    return render_template(
        "decisions_partial.html",
        decisions=decision_list,
        page=page,
        total_pages=total_pages,
        total=total,
        category=category,
        status=status,
        stakes=stakes,
        search=search,
        sort=sort,
    )


@app.route("/decisions/<decision_id>")
@auth
def decision_detail(decision_id: str) -> str | Response:
    """View single decision details.

    Args:
        decision_id: Decision ID (full or prefix)
    """
    try:
        decision = cstp.get_decision(decision_id)
        if not decision:
            flash("Decision not found", "error")
            return redirect(url_for("decisions"))
    except CSTPError as e:
        flash(f"Error loading decision: {e}", "error")
        return redirect(url_for("decisions"))

    # Fetch graph neighbors (error-isolated — never break the page)
    graph_neighbors: list = []
    try:
        graph_neighbors = cstp.get_neighbors(decision_id)
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to fetch graph neighbors for %s", decision_id, exc_info=True,
        )

    return render_template(
        "decision.html",
        decision=decision,
        graph_neighbors=graph_neighbors,
    )


@app.route("/decisions/<decision_id>/review", methods=["GET", "POST"])
@auth
def review(decision_id: str) -> str | Response:
    """Review decision outcome.
    
    GET: Show review form
    POST: Submit review (CSRF protected)
    
    Args:
        decision_id: Decision ID to review
    """
    try:
        decision = cstp.get_decision(decision_id)
        if not decision:
            flash("Decision not found", "error")
            return redirect(url_for("decisions"))
    except CSTPError as e:
        flash(f"Error loading decision: {e}", "error")
        return redirect(url_for("decisions"))
    
    if request.method == "POST":
        outcome = request.form.get("outcome", "")
        actual_result = request.form.get("actual_result", "")
        lessons = request.form.get("lessons") or None
        
        if not outcome:
            flash("Outcome is required", "error")
        elif not actual_result:
            flash("Result description is required", "error")
        else:
            try:
                success = cstp.review_decision(
                    decision_id,
                    outcome,
                    actual_result,
                    lessons,
                )
                if success:
                    flash("Outcome recorded successfully!", "success")
                    return redirect(url_for("decision_detail", decision_id=decision_id))
                else:
                    flash("Failed to record outcome", "error")
            except CSTPError as e:
                flash(f"Error: {e}", "error")
    
    return render_template("review.html", decision=decision)


def parse_tracker_key(key: str) -> dict[str, str | None]:
    """Parse composite tracker key into display components."""
    result: dict[str, str | None] = {
        "agent_id": None, "decision_id": None,
        "transport": None, "transport_id": None, "raw": key,
    }
    # The server namespaces keys under the authenticated agent: "{agent}|{key}".
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
    elif ":" in key:
        transport, _, transport_id = key.partition(":")
        result["transport"] = transport
        result["transport_id"] = transport_id
    return result


def _transform_tracker_sessions(tracker_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Transform raw debugTracker response into template-friendly dicts."""
    sessions = []
    detail = tracker_data.get("detail", {})
    for key in tracker_data.get("sessions", []):
        session_detail = detail.get(key, {})
        parsed = parse_tracker_key(key)
        inputs = []
        for inp in session_detail.get("inputs", []):
            age = inp.get("ageSeconds", 0)
            inputs.append({
                **inp,
                "age_display": _format_age(age),
                "age_class": _age_freshness_class(age),
            })
        raw_inputs = session_detail.get("inputs", [])
        sessions.append({
            "key": key, "parsed": parsed,
            "input_count": session_detail.get("inputCount", 0), "inputs": inputs,
            "freshness": _session_freshness_class(raw_inputs),
        })
    return sessions


def _format_age(seconds: int) -> str:
    """Format age in seconds to human-readable string."""
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _age_freshness_class(seconds: int) -> str:
    """Return CSS class based on age freshness.

    Thresholds per F049 spec:
    - active (green): < 60s
    - stale (yellow): 60-300s
    - orphaned (red): > 300s (5min)
    """
    if seconds < 60:
        return "age--fresh"
    if seconds < 300:
        return "age--stale"
    return "age--orphaned"


def _session_freshness_class(inputs: list[dict[str, Any]]) -> str:
    """Return session-level CSS class based on newest input age."""
    if not inputs:
        return "age--orphaned"
    min_age = min(inp.get("ageSeconds", 0) for inp in inputs)
    return _age_freshness_class(min_age)


def _transform_consumed_sessions(consumed_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Transform consumed session records into template-friendly dicts."""
    sessions = []
    for item in consumed_data:
        parsed = parse_tracker_key(item.get("key", ""))
        age = item.get("consumedAt", 0)
        sessions.append({
            "key": item.get("key", ""),
            "parsed": parsed,
            "input_count": item.get("inputCount", 0),
            "agent_id": item.get("agentId"),
            "decision_id": item.get("decisionId"),
            "status": item.get("status", "consumed"),
            "age_display": _format_age(age),
            "age_class": _age_freshness_class(age),
            "inputs_summary": item.get("inputsSummary", []),
        })
    return sessions


@app.route("/deliberation")
@auth
def deliberation() -> str:
    """Live deliberation tracker viewer."""
    filter_key = request.args.get("key") or None
    try:
        tracker_data = cstp.debug_tracker(key=filter_key, include_consumed=True)
    except CSTPError as e:
        flash(f"Error loading tracker: {e}", "error")
        tracker_data = {"sessions": [], "sessionCount": 0, "detail": {}}
    sessions = _transform_tracker_sessions(tracker_data)
    consumed = _transform_consumed_sessions(tracker_data.get("consumed", []))
    return render_template(
        "deliberation.html",
        sessions=sessions,
        consumed=consumed,
        session_count=tracker_data.get("sessionCount", 0),
        filter_key=filter_key,
    )


@app.route("/deliberation/partial")
@auth
def deliberation_partial() -> str:
    """Partial template for HTMX auto-refresh."""
    filter_key = request.args.get("key") or None
    try:
        tracker_data = cstp.debug_tracker(key=filter_key, include_consumed=True)
    except CSTPError:
        tracker_data = {"sessions": [], "sessionCount": 0, "detail": {}}
    sessions = _transform_tracker_sessions(tracker_data)
    consumed = _transform_consumed_sessions(tracker_data.get("consumed", []))
    return render_template(
        "deliberation_partial.html",
        sessions=sessions,
        consumed=consumed,
        session_count=tracker_data.get("sessionCount", 0),
        filter_key=filter_key,
    )


def _format_cooldown(ms: int | None) -> str:
    """Format cooldown remaining milliseconds to human-readable string."""
    if ms is None or ms <= 0:
        return ""
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    if mins:
        return f"{hours}h {mins}m"
    return f"{hours}h"


def _format_window(ms: int) -> str:
    """Format window milliseconds to human-readable string."""
    seconds = ms // 1000
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours = seconds // 3600
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def _breaker_state_class(state: str) -> str:
    """Return CSS modifier class for breaker state."""
    return {
        "closed": "breaker--closed",
        "open": "breaker--open",
        "half_open": "breaker--half-open",
    }.get(state, "")


def _enrich_breakers(breaker_list: list[dict]) -> None:
    """Add display helpers to breaker dicts for templates."""
    for b in breaker_list:
        b["state_class"] = _breaker_state_class(b.get("state", ""))
        b["state_label"] = b.get("state", "").upper().replace("_", " ")
        b["cooldown_display"] = _format_cooldown(b.get("cooldown_remaining_ms"))
        b["window_display"] = _format_window(b.get("window_ms", 0))
        b["cooldown_period"] = _format_window(b.get("cooldown_ms", 0))
        threshold = b.get("failure_threshold", 1)
        count = b.get("failure_count", 0)
        b["progress_pct"] = min(100, int((count / max(threshold, 1)) * 100))


@app.route("/breakers")
@auth
def breakers() -> str:
    """Circuit breaker status page."""
    try:
        breaker_list = cstp.list_breakers()
    except CSTPError as e:
        flash(f"Error loading breakers: {e}", "error")
        breaker_list = []

    _enrich_breakers(breaker_list)
    open_count = sum(1 for b in breaker_list if b.get("state") != "closed")

    return render_template(
        "breakers.html",
        breakers=breaker_list,
        open_count=open_count,
        total_count=len(breaker_list),
    )


@app.route("/breakers/partial")
@auth
def breakers_partial() -> str:
    """Partial template for HTMX auto-refresh of breaker table."""
    try:
        breaker_list = cstp.list_breakers()
    except CSTPError:
        breaker_list = []

    _enrich_breakers(breaker_list)
    open_count = sum(1 for b in breaker_list if b.get("state") != "closed")

    return render_template(
        "breakers_partial.html",
        breakers=breaker_list,
        open_count=open_count,
        total_count=len(breaker_list),
    )


@app.route("/breakers/reset", methods=["POST"])
@auth
def breakers_reset() -> Response:
    """Reset an OPEN circuit breaker (admin action)."""
    scope = request.form.get("scope", "")
    probe_first = request.form.get("probe_first") == "true"

    if not scope:
        flash("Scope is required", "error")
        return redirect(url_for("breakers"))

    try:
        result = cstp.reset_circuit(scope, probe_first=probe_first)
        prev = result.get("previous_state", "?")
        new = result.get("new_state", "?")
        flash(f"Reset {scope}: {prev} \u2192 {new}", "success")
    except CSTPError as e:
        flash(f"Reset failed: {e}", "error")

    return redirect(url_for("breakers"))


@app.route("/calibration")
@auth
def calibration() -> str:
    """Calibration dashboard showing accuracy metrics.
    
    Query params:
        project: Optional project filter
        window: Time window (30d, 60d, 90d)
    """
    project = request.args.get("project") or None
    window = request.args.get("window") or None
    
    stats = None
    drift = None
    
    try:
        stats = cstp.get_calibration(project=project, window=window)
    except CSTPError as e:
        flash(f"Error loading calibration: {e}", "error")
    
    # Check for drift (only if not filtering by window)
    if not window:
        with contextlib.suppress(CSTPError):
            drift = cstp.check_drift(project=project)
    
    return render_template(
        "calibration.html",
        stats=stats,
        project=project,
        window=window,
        drift=drift,
    )


def main() -> None:
    """Run development server."""
    errors = config.validate()
    if errors:
        for error in errors:
            print(f"Config error: {error}")
        return

    # Werkzeug's debugger is an RCE vector on any reachable interface, so it is
    # opt-in rather than the default even on this dev-only path.
    debug = environ.get("DASHBOARD_DEBUG", "").lower() in {"1", "true", "yes"}
    host = environ.get("DASHBOARD_HOST", "127.0.0.1")
    app.run(host=host, port=config.dashboard_port, debug=debug)


if __name__ == "__main__":
    main()
