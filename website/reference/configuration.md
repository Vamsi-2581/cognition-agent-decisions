# Configuration Guide

Cognition Engines supports configuration through three sources (in order of precedence):

1. **Environment variables** (highest priority)
2. **YAML configuration file** (`config/server.yaml`)
3. **Built-in defaults** (lowest priority)

---

## Environment Variables

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `GEMINI_API_KEY` | Google Gemini API key for text embeddings | `AIza...` |
| `CSTP_AUTH_TOKENS` | Comma-separated `agent:token` pairs | `emerson:abc123,claude:xyz789` |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `CSTP_ADMIN_AGENTS` | empty | Comma-separated agents with the [admin role](#admin-role) |
| `CSTP_TRACKER_MAX_SESSIONS_PER_AGENT` | `200` | Open deliberation sessions one agent may hold; the least recently active is dropped past it |
| `CSTP_TRACKER_MAX_INPUTS_PER_SESSION` | `100` | Inputs one deliberation session keeps; the oldest is dropped past it |

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `CSTP_HOST` | `0.0.0.0` | Server bind address |
| `CSTP_PORT` | `9991` | Server port |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### ChromaDB

| Variable | Default | Description |
|----------|---------|-------------|
| `CHROMA_URL` | `http://chromadb:8000` | ChromaDB server URL |
| `CHROMA_TOKEN` | — | ChromaDB auth token (optional) |
| `CHROMA_COLLECTION` | `decisions_gemini` | Collection name |

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `DECISIONS_PATH` | `decisions` | Directory for decision YAML files |
| `GUARDRAILS_PATHS` | `guardrails` | Colon-separated guardrail directories |
| `SECRETS_PATHS` | — | Directories to search for secret files |

### Agent Card

| Variable | Default | Description |
|----------|---------|-------------|
| `CSTP_AGENT_NAME` | `cognition-engines` | Agent name in discovery card |
| `CSTP_AGENT_DESCRIPTION` | `Decision Intelligence Service` | Agent description |
| `CSTP_AGENT_VERSION` | `0.9.0` | Reported version |
| `CSTP_AGENT_URL` | — | Public URL for agent card |
| `CSTP_AGENT_CONTACT` | — | Contact email |

---

## YAML Configuration File

**Location:** `config/server.yaml`

```yaml
# Server settings
host: 0.0.0.0
port: 9991

# CORS (comma-separated or list)
cors_origins:
  - "*"

# Agent identity (shown in /.well-known/agent.json)
agent:
  name: cognition-engines
  description: Decision Intelligence Service - Semantic search and guardrail evaluation
  version: 0.9.0
  url: https://your-domain.com
  contact: admin@your-domain.com

# Authentication
auth:
  enabled: true
  tokens:
    - agent: emerson
      token: your-secret-token-here
    - agent: claude
      token: another-secret-token
```

### Variable Substitution in YAML

YAML values support `${ENV_VAR}` expansion:

```yaml
auth:
  tokens:
    - agent: emerson
      token: ${EMERSON_TOKEN}
```

---

## Authentication Configuration

### Token Format

Tokens are `agent:secret` pairs. The agent name is:

- Extracted from the `Authorization: Bearer agent:secret` header
- Logged in audit trails
- Used for per-agent calibration filtering

### Via Environment

```bash
# Single agent
CSTP_AUTH_TOKENS=myagent:mysecrettoken

# Multiple agents
CSTP_AUTH_TOKENS=emerson:abc123,claude:xyz789,autogen:secret99
```

### Via YAML

```yaml
auth:
  enabled: true
  tokens:
    - agent: emerson
      token: abc123
    - agent: claude
      token: xyz789
```

### Admin Role

Every agent may read the shared decision store, but may change only the decisions it
recorded. Agents listed as admins may also change any agent's decisions, see every
deliberation session in `cstp.debugTracker`, and run the operator methods that affect all
agents: `cstp.resetCircuit`, `cstp.reindex`, `cstp.attributeOutcomes`, `cstp.ingestEvidence`
and `cstp.exportEvidenceBundle`.

```yaml
auth:
  tokens:
    - agent: emerson
      token: ${EMERSON_TOKEN}
    - agent: dashboard
      token: ${DASHBOARD_TOKEN}
  admins: [dashboard]
```

or `CSTP_ADMIN_AGENTS=dashboard`. The dashboard's `CSTP_TOKEN` needs an admin agent for its
breaker Reset button and Deliberation page. Give agents that act on their own a separate,
non-admin token: an admin token can reset a tripped breaker or ingest the "observed"
evidence that control mappings treat as independent, such as human PR approvals.

With auth disabled every caller is an admin.

### Disabling Auth

For development or testing:

```yaml
auth:
  enabled: false
```

> **Warning:** Never disable auth in production.

### MCP Authentication

The MCP Streamable HTTP transport at `/mcp` checks the same bearer token as the JSON-RPC `/cstp` endpoint, so one `CSTP_AUTH_TOKENS` configuration covers both. A request without a valid `Authorization: Bearer` header gets `401` before it reaches any MCP tool. Tools act as the token's agent; their `agent_id` argument only labels deliberation buckets within it.

The stdio transport (`python -m a2a.mcp_server`) does not use bearer tokens — it relies on the process-level security of the hosting environment (e.g., Docker container isolation). Its tools act as the agent `mcp-client` with the admin role, as with auth disabled: the local user already has the database.

---

## Storage Configuration

### Decision Storage

Decisions are stored as YAML files in a hierarchical directory:

```
decisions/
├── 2026/
│   ├── 01/
│   │   ├── 2026-01-15-decision-a1b2c3d4.yaml
│   │   └── 2026-01-20-decision-e5f6g7h8.yaml
│   └── 02/
│       └── 2026-02-07-decision-i9j0k1l2.yaml
```

**File naming:** `YYYY-MM-DD-decision-<8-char-id>.yaml`

### Guardrail Storage

Guardrails are loaded from YAML files in configured directories:

```
guardrails/
├── cornerstone.yaml           # Core rules (always loaded)
├── project-specific.yaml      # Your project rules
└── templates/
    ├── financial.yaml         # Template for financial projects
    └── production-safety.yaml # Template for production deploys
```

Multiple guardrail directories can be configured:

```bash
GUARDRAILS_PATHS=/app/guardrails:/app/custom-guardrails:/shared/company-guardrails
```

---

## CORS Configuration

**Default: deny.** `cors_origins` is empty out of the box, and the CORS middleware is
only installed when it is non-empty. The CSTP API is bearer-token authenticated and has
no browser client of its own, so no origin needs access unless you add one.

Configure allowed origins explicitly:

```yaml
cors_origins:
  - "http://localhost:3000"
  - "http://localhost:5001"
  - "https://your-domain.com"
```

::: warning Avoid `"*"`
Starlette's `CORSMiddleware` reflects the caller's `Origin` header when origins is `"*"`,
which combined with credentialed requests means "any origin, with credentials." The server
therefore refuses to send credentials when `"*"` is present — `allow_credentials` is
enabled only for an explicit allow-list.
:::

---

## Secrets Management

The system searches for secret files in `SECRETS_PATHS`:

```bash
SECRETS_PATHS=/run/secrets:/app/secrets
```

This is used primarily for loading `GEMINI_API_KEY` from files (useful in Docker secrets or Kubernetes secrets):

```bash
# Create a secret file
echo "AIza..." > /run/secrets/gemini_api_key
```

The `load_gemini_key()` function searches for files named `gemini_api_key` or `GEMINI_API_KEY` in these paths.

---

## Docker Compose Configuration

The `docker-compose.yml` file manages all configuration via environment variables:

```yaml
services:
  cstp-server:
    environment:
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - CSTP_AUTH_TOKENS=${CSTP_AUTH_TOKENS}
      - CHROMA_URL=http://chromadb:8000
      - DECISIONS_PATH=/app/decisions
      - GUARDRAILS_PATHS=/app/guardrails
      - LOG_LEVEL=INFO
```

Create a `.env` file in the project root:

```env
GEMINI_API_KEY=AIza...
CSTP_AUTH_TOKENS=myagent:mysecrettoken
```

Docker Compose automatically loads variables from `.env`.
