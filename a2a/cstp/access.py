"""Who may do what: decision ownership and the admin role.

Identity always comes from the transport (the bearer token's agent on /cstp and
/mcp, a fixed local agent on stdio), never from request arguments. An agent may
change only the decisions it recorded; admins may change any decision and run
the operator methods that affect every agent.
"""

from typing import Any


class PermissionDeniedError(Exception):
    """The authenticated agent may not perform this operation."""


def is_admin(agent_id: str) -> bool:
    """Return whether the agent has the admin role.

    Fails closed: with no auth manager installed nobody is an admin.
    """
    from ..auth import get_auth_manager

    try:
        manager = get_auth_manager()
    except RuntimeError:
        return False
    return manager.is_admin(agent_id)


def require_admin(agent_id: str, operation: str) -> None:
    """Raise PermissionDeniedError unless the agent is an admin."""
    if not is_admin(agent_id):
        raise PermissionDeniedError(f"{operation} requires an admin token")


async def _find_recorded_decision(decision_id: str) -> dict[str, Any] | None:
    """Look a decision up by exact ID: the store first, then its YAML file."""
    from .decision_service import find_decision
    from .storage.factory import get_decision_store

    try:
        data = await get_decision_store().get(decision_id)
    except Exception:
        data = None
    if data:
        return data
    found = await find_decision(decision_id)
    return found[1] if found else None


async def require_decision_owner(
    decision_id: str,
    agent_id: str,
    *,
    must_exist: bool = False,
) -> None:
    """Raise PermissionDeniedError unless the agent recorded the decision.

    Admins pass for any decision. A missing decision passes unless
    `must_exist` is set, so the operation itself reports it as not found.

    Raises:
        PermissionDeniedError: The decision was recorded by another agent.
        ValueError: `must_exist` is set and the decision does not exist, or
            the ID is malformed.
    """
    if is_admin(agent_id):
        if must_exist and await _find_recorded_decision(decision_id) is None:
            raise ValueError(f"Decision {decision_id} not found")
        return
    data = await _find_recorded_decision(decision_id)
    if data is None:
        if must_exist:
            raise ValueError(f"Decision {decision_id} not found")
        return
    if data.get("recorded_by") != agent_id:
        raise PermissionDeniedError(
            f"Decision {decision_id} was recorded by another agent"
        )
