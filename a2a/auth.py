"""Bearer token authentication for CSTP endpoints."""

from typing import Annotated

from fastapi import Header, HTTPException, status

from .config import Config


class AuthManager:
    """Manages bearer token authentication.

    Attributes:
        config: Server configuration with auth settings.
    """

    def __init__(self, config: Config) -> None:
        """Initialize auth manager.

        Args:
            config: Server configuration.
        """
        self._config = config

    def verify_token(self, authorization: str) -> str:
        """Verify bearer token and return agent ID.

        Args:
            authorization: Authorization header value.

        Returns:
            Agent ID if token is valid.

        Raises:
            HTTPException: If token is missing or invalid.
        """
        if not self._config.auth.enabled:
            return "anonymous"

        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization header required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization scheme, expected Bearer",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = authorization[7:]  # Remove "Bearer " prefix
        agent_id = self._config.auth.validate_token(token)

        if not agent_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return agent_id

    def is_admin(self, agent_id: str) -> bool:
        """Return whether an authenticated agent has the admin role.

        Args:
            agent_id: Agent ID returned by `verify_token`.

        Returns:
            True if the agent is listed in `auth.admins` or auth is disabled.
        """
        return self._config.auth.is_admin(agent_id)


# Global auth manager instance (set by server on startup)
_auth_manager: AuthManager | None = None


def set_auth_manager(manager: AuthManager) -> None:
    """Set the global auth manager instance.

    Args:
        manager: Auth manager to use.
    """
    global _auth_manager
    _auth_manager = manager


def get_auth_manager() -> AuthManager:
    """Get the global auth manager instance.

    Returns:
        The auth manager.

    Raises:
        RuntimeError: If auth manager not initialized.
    """
    if _auth_manager is None:
        raise RuntimeError("Auth manager not initialized")
    return _auth_manager


async def verify_bearer_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """FastAPI dependency for bearer token verification.

    Args:
        authorization: Authorization header from request.

    Returns:
        Agent ID if authenticated.

    Raises:
        HTTPException: If authentication fails.
    """
    manager = get_auth_manager()
    return manager.verify_token(authorization or "")
