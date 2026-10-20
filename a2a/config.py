"""Server configuration management.

Loads configuration from YAML file and environment variables.
"""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class AuthToken:
    """Authentication token configuration.

    Attributes:
        agent: Agent identifier.
        token: Bearer token value.
    """

    agent: str
    token: str

    def __post_init__(self) -> None:
        # Deliberation tracker keys are namespaced "{agent}|...", so a "|" in a
        # name would let agent "a" read and consume agent "a|b"'s sessions.
        if "|" in self.agent:
            raise ValueError(f"Agent name may not contain '|': {self.agent!r}")


@dataclass(slots=True)
class AuthConfig:
    """Authentication configuration.

    Attributes:
        enabled: Whether authentication is required.
        tokens: List of valid tokens.
        admins: Agents allowed to run operator methods (resetCircuit, reindex,
            attributeOutcomes, ingestEvidence, exportEvidenceBundle), see every
            tracker session, and
            change decisions other agents recorded.
    """

    enabled: bool = True
    tokens: list[AuthToken] = field(default_factory=list)
    admins: list[str] = field(default_factory=list)

    def is_admin(self, agent_id: str) -> bool:
        """Return whether an authenticated agent has the admin role.

        With authentication disabled every caller is the same local user, so
        all of them are admins.
        """
        return not self.enabled or agent_id in self.admins

    def validate_token(self, token: str) -> str | None:
        """Validate a bearer token.

        Uses constant-time comparison to prevent timing attacks.

        Args:
            token: Bearer token to validate.

        Returns:
            Agent ID if valid, None if invalid.
        """
        for auth_token in self.tokens:
            if secrets.compare_digest(auth_token.token.encode(), token.encode()):
                return auth_token.agent
        return None


@dataclass(slots=True)
class AgentConfig:
    """Agent identity configuration.

    Attributes:
        name: Agent identifier.
        description: Human-readable description.
        version: Agent version string.
        url: Base URL for the agent's API.
        contact: Optional contact email.
    """

    name: str = "cognition-engines"
    description: str = "Decision intelligence for AI agents"
    version: str = "0.7.0"
    url: str = "http://localhost:8100"
    contact: str | None = None


@dataclass(slots=True)
class TrackerConfig:
    """Deliberation tracker configuration.

    Attributes:
        input_ttl_seconds: TTL for individual inputs within a session.
        session_ttl_seconds: TTL for entire tracker sessions (in seconds).
        consumed_history_size: Max consumed records to retain.
        max_sessions_per_agent: Open sessions one agent may hold; past it the
            agent's least recently active session is dropped.
        max_inputs_per_session: Inputs one session keeps; past it the oldest
            input is dropped.
    """

    input_ttl_seconds: int = 300
    session_ttl_seconds: int = 1800
    consumed_history_size: int = 50
    max_sessions_per_agent: int = 200
    max_inputs_per_session: int = 100


@dataclass(slots=True)
class StorageConfig:
    """Decision storage configuration.

    Attributes:
        backend: Storage backend (sqlite, yaml, memory). Defaults to sqlite —
            yaml has no WAL, no FTS5, and no concurrent-write protection, so it
            is opt-in for local single-user use only.
        db_path: Path to SQLite database file.
    """

    backend: str = "sqlite"
    db_path: str = "data/decisions.db"


@dataclass(slots=True)
class ServerConfig:
    """HTTP server configuration.

    Attributes:
        host: Bind address.
        port: Bind port.
        cors_origins: Allowed CORS origins.
    """

    host: str = "0.0.0.0"
    port: int = 8100
    # Deny by default. The API is bearer-token authenticated and has no browser
    # client of its own, so a wildcard buys nothing and ships an insecure default:
    # Starlette reflects the caller's Origin when allow_credentials is set, which
    # turns "*" into "any origin, with credentials". Operators who need browser
    # access allow-list their own origins explicitly.
    cors_origins: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Config:
    """Complete server configuration.

    Attributes:
        server: HTTP server settings.
        agent: Agent identity settings.
        auth: Authentication settings.
    """

    server: ServerConfig = field(default_factory=ServerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        """Load configuration from YAML file.

        Environment variables in format ${VAR_NAME} are expanded.

        Args:
            path: Path to YAML configuration file.

        Returns:
            Loaded configuration.
        """
        if not path.exists():
            return cls()

        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if not data:
            return cls()

        return cls._from_dict(data)

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables.

        Environment variables:
            CSTP_HOST: Server bind address
            CSTP_PORT: Server bind port
            CSTP_AUTH_TOKENS: Comma-separated agent:token pairs
            CSTP_ADMIN_AGENTS: Comma-separated agents with the admin role
            CSTP_AGENT_NAME: Agent name
            CSTP_AGENT_DESCRIPTION: Agent description
            CSTP_AGENT_VERSION: Agent version
            CSTP_AGENT_URL: Agent URL
            CSTP_AGENT_CONTACT: Agent contact email

        Returns:
            Configuration from environment.
        """
        return cls(
            server=ServerConfig(
                host=os.getenv("CSTP_HOST", "0.0.0.0"),
                port=int(os.getenv("CSTP_PORT", "8100")),
            ),
            agent=AgentConfig(
                name=os.getenv("CSTP_AGENT_NAME", "cognition-engines"),
                description=os.getenv(
                    "CSTP_AGENT_DESCRIPTION", "Decision intelligence for AI agents"
                ),
                version=os.getenv("CSTP_AGENT_VERSION", "0.7.0"),
                url=os.getenv("CSTP_AGENT_URL", "http://localhost:8100"),
                contact=os.getenv("CSTP_AGENT_CONTACT"),
            ),
            auth=AuthConfig(
                enabled=True,
                tokens=_parse_auth_tokens(os.getenv("CSTP_AUTH_TOKENS", "")),
                admins=_parse_names(os.getenv("CSTP_ADMIN_AGENTS", "")),
            ),
            tracker=TrackerConfig(
                input_ttl_seconds=int(os.getenv("CSTP_TRACKER_INPUT_TTL", "300")),
                session_ttl_seconds=int(os.getenv("CSTP_TRACKER_SESSION_TTL", "1800")),
                consumed_history_size=int(os.getenv("CSTP_TRACKER_HISTORY_SIZE", "50")),
                max_sessions_per_agent=int(
                    os.getenv("CSTP_TRACKER_MAX_SESSIONS_PER_AGENT", "200")
                ),
                max_inputs_per_session=int(
                    os.getenv("CSTP_TRACKER_MAX_INPUTS_PER_SESSION", "100")
                ),
            ),
            storage=StorageConfig(
                backend=os.getenv("CSTP_STORAGE", "sqlite"),
                db_path=os.getenv("CSTP_DB_PATH", "data/decisions.db"),
            ),
        )

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create config from dictionary."""
        config = cls()

        # Server config
        if "server" in data:
            srv = data["server"]
            config.server = ServerConfig(
                host=srv.get("host", config.server.host),
                port=srv.get("port", config.server.port),
                cors_origins=srv.get("cors_origins", config.server.cors_origins),
            )

        # Agent config
        if "agent" in data:
            agent = data["agent"]
            config.agent = AgentConfig(
                name=agent.get("name", config.agent.name),
                description=agent.get("description", config.agent.description),
                version=agent.get("version", config.agent.version),
                url=agent.get("url", config.agent.url),
                contact=agent.get("contact"),
            )

        # Auth config
        if "auth" in data:
            auth = data["auth"]
            tokens: list[AuthToken] = []
            for token_data in auth.get("tokens", []):
                token_value = token_data.get("token", "")
                # Expand environment variables
                if token_value.startswith("${") and token_value.endswith("}"):
                    env_var = token_value[2:-1]
                    token_value = os.environ.get(env_var, "")
                tokens.append(
                    AuthToken(
                        agent=token_data.get("agent", ""),
                        token=token_value,
                    )
                )
            config.auth = AuthConfig(
                enabled=auth.get("enabled", True),
                tokens=tokens,
                admins=[str(name) for name in auth.get("admins") or []],
            )

        # Storage config
        if "storage" in data:
            st = data["storage"]
            config.storage = StorageConfig(
                backend=st.get("backend", config.storage.backend),
                db_path=st.get("db_path", config.storage.db_path),
            )

        # Tracker config
        if "tracker" in data:
            tr = data["tracker"]
            # Accept session_ttl_seconds (preferred) or legacy session_ttl_minutes (* 60)
            if "session_ttl_seconds" in tr:
                session_ttl = tr["session_ttl_seconds"]
            elif "session_ttl_minutes" in tr:
                session_ttl = tr["session_ttl_minutes"] * 60
            else:
                session_ttl = 1800
            config.tracker = TrackerConfig(
                input_ttl_seconds=tr.get("input_ttl_seconds", 300),
                session_ttl_seconds=session_ttl,
                consumed_history_size=tr.get("consumed_history_size", 50),
                max_sessions_per_agent=tr.get("max_sessions_per_agent", 200),
                max_inputs_per_session=tr.get("max_inputs_per_session", 100),
            )

        return config


def _parse_auth_tokens(tokens_str: str) -> list[AuthToken]:
    """Parse CSTP_AUTH_TOKENS environment variable.

    Format: agent1:token1,agent2:token2

    Args:
        tokens_str: Comma-separated agent:token pairs.

    Returns:
        List of AuthToken objects.
    """
    tokens: list[AuthToken] = []
    if not tokens_str:
        return tokens

    for pair in tokens_str.split(","):
        pair = pair.strip()
        if ":" in pair:
            agent, token = pair.split(":", 1)
            if agent and token:
                tokens.append(AuthToken(agent=agent.strip(), token=token.strip()))

    return tokens


def _parse_names(names_str: str) -> list[str]:
    """Parse a comma-separated list of agent names, dropping blanks."""
    return [name.strip() for name in names_str.split(",") if name.strip()]
