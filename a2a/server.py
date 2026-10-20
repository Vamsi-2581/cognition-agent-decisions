"""CSTP HTTP Server.

FastAPI-based server exposing CSTP methods via JSON-RPC 2.0
and MCP tools via Streamable HTTP transport.
"""

import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import AuthManager, get_auth_manager, set_auth_manager, verify_bearer_token
from .config import Config
from .cstp import CstpDispatcher, get_dispatcher, register_methods
from .models import AgentCapabilities, AgentCard, HealthResponse
from .models.jsonrpc import (
    INVALID_REQUEST,
    PARSE_ERROR,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager.

    Initializes auth manager, dispatcher, and MCP session manager on startup.
    Stores state in app.state instead of global variables.
    """
    # Use monotonic time for uptime (not affected by system clock changes)
    app.state.start_time = time.monotonic()

    # Load configuration (use provided or load from file)
    if not hasattr(app.state, "config") or app.state.config is None:
        config_path = Path("config/server.yaml")
        app.state.config = Config.from_yaml(config_path)

    # Initialize auth manager
    auth_manager = AuthManager(app.state.config)
    set_auth_manager(auth_manager)
    app.state.auth_manager = auth_manager

    # Initialize deliberation tracker with config values
    from .cstp.deliberation_tracker import get_tracker

    get_tracker(
        input_ttl=app.state.config.tracker.input_ttl_seconds,
        session_ttl=app.state.config.tracker.session_ttl_seconds,
        consumed_history_size=app.state.config.tracker.consumed_history_size,
        max_sessions_per_agent=app.state.config.tracker.max_sessions_per_agent,
        max_inputs_per_session=app.state.config.tracker.max_inputs_per_session,
    )

    # F050: Initialize decision store
    try:
        from .cstp.storage.factory import (
            get_decision_store,
            mark_initialized as mark_store_initialized,
            set_decision_store,
        )

        # Pass the loaded StorageConfig so a `--config` YAML that names a backend
        # or db_path is honoured; env still wins over both.
        decision_store = get_decision_store(getattr(app.state.config, "storage", None))
        await decision_store.initialize()
        mark_store_initialized()
        app.state.decision_store = decision_store
        logger.info("Decision store initialized (backend=%s)", type(decision_store).__name__)

        # Auto-migrate YAML to SQLite if store is empty
        from .cstp.storage.sqlite import SQLiteDecisionStore

        if isinstance(decision_store, SQLiteDecisionStore):
            from .cstp.storage.migrate import auto_migrate_if_incomplete

            migrated = await auto_migrate_if_incomplete(decision_store)
            if migrated > 0:
                logger.info("Auto-migrated %d decisions from YAML to SQLite", migrated)
    except Exception:
        logger.warning("Decision store initialization failed", exc_info=True)
        set_decision_store(None)
        app.state.decision_store = None

    # Initialize dispatcher with methods
    dispatcher = get_dispatcher()
    register_methods(dispatcher)
    app.state.dispatcher = dispatcher

    # F045: Initialize graph store and load existing related_to edges
    try:
        from .cstp.graph_service import initialize_graph_from_decisions
        from .cstp.graphdb.factory import get_graph_store, mark_initialized, set_graph_store

        graph_store = get_graph_store()
        await graph_store.initialize()
        await initialize_graph_from_decisions()
        mark_initialized()
        app.state.graph_store = graph_store
        logger.info("Graph store initialized")
    except Exception:
        logger.warning("Graph store initialization failed", exc_info=True)
        set_graph_store(None)
        app.state.graph_store = None

    # F041 P2: Auto-compact on startup (read-only level calculation)
    try:
        from .cstp.compaction_service import run_compaction
        from .cstp.models import CompactRequest

        compact_result = await run_compaction(CompactRequest())
        levels = compact_result.levels
        logger.info(
            "Startup compaction: %d decisions "
            "(full=%d, summary=%d, digest=%d, wisdom=%d)",
            compact_result.compacted,
            levels.full, levels.summary, levels.digest, levels.wisdom,
        )
    except Exception:
        logger.warning("Startup compaction failed", exc_info=True)

    # Initialize MCP Streamable HTTP session manager
    try:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        from .mcp_server import mcp_app

        session_manager = StreamableHTTPSessionManager(
            app=mcp_app,
            json_response=True,
        )
        app.state.mcp_session_manager = session_manager

        async with session_manager.run():
            logger.info("MCP Streamable HTTP transport available at /mcp")
            yield
    except ImportError:
        logger.warning("MCP SDK not installed — Streamable HTTP transport disabled")
        app.state.mcp_session_manager = None
        yield

    # Cleanup
    # F050: Close decision store
    if getattr(app.state, "decision_store", None):
        try:
            await app.state.decision_store.close()
            logger.info("Decision store closed")
        except Exception:
            logger.warning("Decision store close failed", exc_info=True)


def create_app(config: Config | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: Optional configuration (uses default if not provided).

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(
        title="CSTP Server",
        description="Cognition State Transfer Protocol - Decision Intelligence API",
        version="0.7.0",
        lifespan=lifespan,
    )

    # Store config in app.state for lifespan to access
    if config:
        app.state.config = config

    # Configure CORS. Credentials are only allowed alongside an explicit origin
    # allow-list: with allow_origins=["*"] Starlette reflects the request's Origin
    # header, so pairing the two would mean "any origin, with credentials".
    cors_origins = config.server.cors_origins if config else []
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials="*" not in cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Register routes
    _register_routes(app)

    # Mount MCP Streamable HTTP transport at /mcp
    _mount_mcp(app)

    return app


def _reject_constant(name: str) -> float:
    """`json.loads` hook that refuses the NaN / Infinity / -Infinity literals."""
    raise ValueError(f"{name} is not valid JSON")


def _parse_finite_float(text: str) -> float:
    """`json.loads` hook that refuses number literals overflowing to infinity."""
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"{text} is out of range")
    return number


def _mount_mcp(app: FastAPI) -> None:
    """Mount the MCP Streamable HTTP handler at /mcp.

    Falls back gracefully if the MCP SDK is not installed.
    """
    from starlette.types import Receive, Scope, Send

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI handler that delegates to the MCP session manager.

        A raw mount bypasses the `verify_bearer_token` dependency that guards
        /cstp, so the same token check runs here, before anything reaches the
        MCP tools. Without it anyone who could reach the port could read and
        write decisions. The verified agent goes into the request state, where
        the tools read their identity (see `mcp_server.get_caller_agent_id`).
        """
        from fastapi import HTTPException
        from starlette.requests import HTTPConnection
        from starlette.responses import JSONResponse as StarletteJSON

        connection = HTTPConnection(scope)
        try:
            connection.state.agent_id = get_auth_manager().verify_token(
                connection.headers.get("authorization", "")
            )
        except HTTPException as exc:
            await StarletteJSON(
                {"error": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )(scope, receive, send)
            return

        session_manager = getattr(app.state, "mcp_session_manager", None)
        if session_manager is None:
            # MCP not available — return 503
            response = StarletteJSON(
                {"error": "MCP transport not available"},
                status_code=503,
            )
            await response(scope, receive, send)
            return

        await session_manager.handle_request(scope, receive, send)

    # Mount as raw ASGI app (not a FastAPI route — MCP handles its own dispatch)
    app.mount("/mcp", handle_mcp)


def _register_routes(app: FastAPI) -> None:
    """Register all API routes.

    Args:
        app: FastAPI application.
    """

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        """Health check endpoint."""
        start_time = getattr(request.app.state, "start_time", 0.0)
        uptime = time.monotonic() - start_time if start_time else 0.0
        response = HealthResponse(
            status="healthy",
            version="0.7.0",
            uptime_seconds=uptime,
            timestamp=datetime.now(UTC),
        )
        return JSONResponse(content=response.to_dict())

    @app.get("/.well-known/agent.json")
    async def agent_card(request: Request) -> JSONResponse:
        """Agent Card endpoint for A2A discovery."""
        config: Config = request.app.state.config
        card = AgentCard(
            name=config.agent.name,
            description=config.agent.description,
            version=config.agent.version,
            url=config.agent.url,
            capabilities=AgentCapabilities(),
            contact=config.agent.contact,
        )
        return JSONResponse(content=card.to_dict())

    @app.post("/cstp")
    async def cstp_endpoint(
        request: Request,
        agent_id: str = Depends(verify_bearer_token),
    ) -> JSONResponse:
        """JSON-RPC 2.0 endpoint for CSTP methods.

        Args:
            request: FastAPI request object.
            agent_id: Authenticated agent ID from bearer token.

        Returns:
            JSON-RPC response.
        """
        # Parse request body. NaN and Infinity are not JSON, but json.loads
        # accepts them, and a literal like 1e999 overflows to infinity; stored
        # on a decision or graph edge, such a value makes every later response
        # that includes it fail to serialize, for every agent.
        try:
            body = json.loads(
                await request.body(),
                parse_constant=_reject_constant,
                parse_float=_parse_finite_float,
            )
        except Exception:
            error_response = JsonRpcResponse.failure(
                None,
                JsonRpcError(code=PARSE_ERROR, message="Invalid JSON"),
            )
            return JSONResponse(content=error_response.to_dict())

        # Validate basic structure
        if not isinstance(body, dict):
            error_response = JsonRpcResponse.failure(
                None,
                JsonRpcError(code=INVALID_REQUEST, message="Request must be an object"),
            )
            return JSONResponse(content=error_response.to_dict())

        # Build request object
        rpc_request = JsonRpcRequest(
            method=body.get("method", ""),
            params=body.get("params", {}),
            id=body.get("id"),
            jsonrpc=body.get("jsonrpc", ""),
        )

        # Dispatch to handler
        dispatcher: CstpDispatcher = request.app.state.dispatcher
        response = await dispatcher.dispatch(rpc_request, agent_id)

        return JSONResponse(content=response.to_dict())


def run_server(
    host: str = "0.0.0.0",
    port: int = 8100,
    config_path: str | None = None,
) -> None:
    """Run the CSTP server.

    Args:
        host: Bind address.
        port: Bind port.
        config_path: Path to configuration file.
    """
    import uvicorn

    # Load configuration (env takes precedence, then yaml, then defaults)
    if config_path:
        config = Config.from_yaml(Path(config_path))
    else:
        # Try to load from environment variables
        config = Config.from_env()

    # Override with CLI arguments if provided
    config.server.host = host
    config.server.port = port

    # DeliberationTracker state lives in a process-local dict, so a client's
    # preAction → recordThought → ready sequence must land on the same process.
    # Refuse to start multi-worker rather than silently losing session state on
    # whichever requests get load-balanced to the wrong worker.
    workers = int(os.getenv("CSTP_WORKERS", "1"))
    if workers > 1:
        msg = (
            f"CSTP_WORKERS={workers} is not supported: DeliberationTracker keeps "
            "session state in process memory, so deliberation would be split across "
            "workers and silently lost. Run a single worker until the tracker is "
            "backed by shared storage."
        )
        raise SystemExit(msg)

    app = create_app(config)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(description="CSTP Server")
    parser.add_argument(
        "--host",
        default=os.getenv("CSTP_HOST", "0.0.0.0"),
        help="Bind address (default: $CSTP_HOST or 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("CSTP_PORT", "8100")),
        help="Bind port (default: $CSTP_PORT or 8100)",
    )
    parser.add_argument(
        "--config",
        help="Path to YAML config file (overrides env vars)",
    )

    args = parser.parse_args()
    run_server(host=args.host, port=args.port, config_path=args.config)
