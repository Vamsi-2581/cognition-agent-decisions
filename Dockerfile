# syntax=docker/dockerfile:1

# =============================================================================
# CSTP Server Dockerfile
# Multi-stage build for production deployment
# =============================================================================

FROM python:3.11-slim AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# --- Builder stage ---
FROM base AS builder

# Install build dependencies
RUN pip install --no-cache-dir uv

# Copy dependency files first (for layer caching)
COPY pyproject.toml ./
COPY README.md ./

# Install dependencies (a2a + mcp)
RUN uv pip install --system ".[a2a,mcp]"

# --- Runtime stage ---
FROM base AS runtime

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY a2a/ ./a2a/
COPY src/ ./src/
COPY config/ ./config/
COPY pyproject.toml ./

# Create non-root user for security. /app/decisions is the docker-compose volume
# mount point: creating it here means a fresh named volume copies its appuser
# ownership instead of mounting root-owned and unwritable.
RUN useradd --create-home --shell /bin/bash appuser && \
    mkdir -p /app/decisions && \
    chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8100

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8100/health || exit 1

# Default command (JSON-RPC server)
CMD ["python", "-m", "a2a.server", "--host", "0.0.0.0", "--port", "8100"]

# Alternative: MCP server (stdio transport)
# CMD ["python", "-m", "a2a.mcp_server"]
