# CSTP API Reference

The **Cognition State Transfer Protocol** (CSTP) is a JSON-RPC 2.0 API exposed over HTTP. All method calls use `POST /cstp` with a bearer token in the `Authorization` header.

---

## Protocol Basics

### Request Format

```json
{
  "jsonrpc": "2.0",
  "method": "cstp.<methodName>",
  "params": { ... },
  "id": "unique-request-id"
}
```

### Response Format — Success

```json
{
  "jsonrpc": "2.0",
  "result": { ... },
  "id": "unique-request-id"
}
```

### Response Format — Error

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32003,
    "message": "Query failed: ..."
  },
  "id": "unique-request-id"
}
```

### Authentication

All requests must include a bearer token:

```
Authorization: Bearer <agent>:<token>
```

The agent name is extracted from the token pair and included in audit trails.

### Identity and Permissions

The token's agent is the caller's identity on both `/cstp` and `/mcp`. An `agentId` /
`agent_id` argument never changes it: it only labels a deliberation bucket inside the
caller's own namespace, so subagents sharing one token stay apart and no other token can
add thoughts to those buckets or consume them.

- **Only your own decisions.** `updateDecision`, `reviewDecision`, `setPreserve`,
  `linkEvidence`, and `recordThought` appending to a recorded decision require the decision
  to have been recorded by the caller.
- **Admin only.** `resetCircuit`, `reindex`, `attributeOutcomes`, `ingestEvidence` and
  `exportEvidenceBundle` require an agent listed in `auth.admins` (`CSTP_ADMIN_AGENTS`; see
  [Configuration](./configuration.md#admin-role)). Admins may also change any agent's
  decisions.
- **`debugTracker`** shows an agent only its own sessions; admins see every session.

A denied call returns error `-32009`; an MCP tool returns `{"error": "permission_denied"}`.

### Error Codes

| Code | Name | Description |
|------|------|-------------|
| -32700 | Parse Error | Malformed JSON |
| -32600 | Invalid Request | Missing required fields |
| -32601 | Method Not Found | Unknown CSTP method |
| -32602 | Invalid Params | Parameter validation failed |
| -32603 | Internal Error | Unexpected server error. The message is `Internal error (ref <id>)`; the detail is in the server log under that reference |
| -32002 | Rate Limited | Too many requests |
| -32003 | Query Failed | Error during decision query |
| -32004 | Guardrail Eval Failed | Error evaluating guardrails |
| -32005 | Record Failed | Error recording decision |
| -32006 | Review Failed | Error reviewing decision |
| -32007 | Decision Not Found | Decision ID not found |
| -32008 | Attribution Failed | Error attributing outcomes |
| -32009 | Permission Denied | The caller's agent may not perform this operation (see above) |

---

## Non-Authenticated Endpoints

### `GET /health`

Health check with uptime tracking.

**Response:**

```json
{
  "status": "ok",
  "version": "0.10.0",
  "agent": "cognition-engines",
  "uptime_seconds": 3600,
  "timestamp": "2026-02-07T12:00:00Z"
}
```

### `GET /.well-known/agent.json`

A2A agent discovery card.

**Response:**

```json
{
  "name": "cognition-engines",
  "description": "Decision Intelligence Service",
  "version": "0.10.0",
  "url": "http://localhost:9991",
  "capabilities": ["queryDecisions", "checkGuardrails", "recordDecision", "reviewDecision", "getCalibration", "getDecision", "getReasonStats"],
  "protocol": "cstp",
  "protocolVersion": "0.10.0"
}
```

---

## CSTP Methods

### `cstp.queryDecisions` — Search Decision Memory

Search for semantically similar decisions using vector similarity, keyword matching, or hybrid search.

**Parameters:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | ✅ | — | Natural language search query |
| `limit` | int | ❌ | 10 | Maximum results |
| `include_reasons` | bool | ❌ | false | Include decision reasons in results |
| `retrieval_mode` | string | ❌ | `"semantic"` | `"semantic"`, `"keyword"`, or `"hybrid"` |
| `bridge_side` | string | ❌ | `"both"` | `"structure"`, `"function"`, or `"both"` |
| `hybrid_weight` | float | ❌ | 0.7 | Semantic weight in hybrid mode (0.0–1.0) |
| `filters` | object | ❌ | `{}` | Filtering criteria (see below) |

**Filter object:**

| Field | Type | Description |
|-------|------|-------------|
| `category` | string | Filter by decision category |
| `min_confidence` | float | Minimum confidence (0.0–1.0) |
| `max_confidence` | float | Maximum confidence (0.0–1.0) |
| `stakes` | string[] | Filter by stakes level (`"low"`, `"medium"`, `"high"`, `"critical"`) |
| `status` | string[] | Filter by status (`"pending"`, `"reviewed"`) |
| `project` | string | Filter by project name |
| `feature` | string | Filter by feature name |
| `pr` | int | Filter by PR number |
| `has_outcome` | bool | Only reviewed (true) or pending (false) |
| `date_after` | string | ISO 8601 minimum date |
| `date_before` | string | ISO 8601 maximum date |

**Example request:**

```json
{
  "jsonrpc": "2.0",
  "method": "cstp.queryDecisions",
  "params": {
    "query": "database selection for agent memory storage",
    "limit": 5,
    "retrieval_mode": "hybrid",
    "include_reasons": true,
    "filters": {
      "category": "architecture",
      "min_confidence": 0.6
    }
  },
  "id": "q-001"
}
```

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "decisions": [
      {
        "id": "2026-01-15-decision-a1b2c3",
        "title": "Use ChromaDB for semantic memory",
        "category": "architecture",
        "confidence": 0.85,
        "stakes": "high",
        "status": "reviewed",
        "outcome": "success",
        "date": "2026-01-15",
        "distance": 0.234,
        "reasons": ["Lightweight vector DB", "HTTP API", "Good Python support"]
      }
    ],
    "total": 1,
    "query": "database selection for agent memory storage",
    "query_time_ms": 142,
    "agent": "cognition-engines",
    "retrieval_mode": "hybrid",
    "scores": {
      "2026-01-15-decision-a1b2c3": {
        "semantic": 0.892,
        "keyword": 0.654,
        "combined": 0.821
      }
    }
  },
  "id": "q-001"
}
```

---

### `cstp.checkGuardrails` — Evaluate Policy Rules

Check a proposed action against all active guardrails.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | object | ✅ | Action context |
| `action.description` | string | ✅ | What the agent intends to do (max 10,000 characters) |
| `action.category` | string | ❌ | Decision category |
| `action.stakes` | string | ❌ | Stakes level (default: `"medium"`) |
| `action.confidence` | float | ❌ | Agent's confidence (0.0–1.0) |
| `action.context` | object | ❌ | Arbitrary context fields for condition matching |
| `agent` | object | ❌ | Agent identity |
| `agent.id` | string | ❌ | Agent identifier |
| `agent.url` | string | ❌ | Agent callback URL |

**Example request:**

```json
{
  "jsonrpc": "2.0",
  "method": "cstp.checkGuardrails",
  "params": {
    "action": {
      "description": "Deploy new ML pipeline to production",
      "category": "deployment",
      "stakes": "high",
      "confidence": 0.75,
      "context": {
        "affects_production": true,
        "code_review_completed": false
      }
    }
  },
  "id": "g-001"
}
```

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "allowed": false,
    "violations": [
      {
        "guardrail_id": "no-production-without-review",
        "name": "Production changes require code review",
        "message": "Production changes require completed code review",
        "severity": "block",
        "suggestion": "Complete code review before proceeding"
      }
    ],
    "warnings": [],
    "evaluated": 3,
    "evaluated_at": "2026-02-07T12:00:00Z",
    "agent": "cognition-engines"
  },
  "id": "g-001"
}
```

---

### `cstp.listGuardrails` — List Active Guardrails

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `scope` | string | ❌ | Filter by scope/project |

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "guardrails": [
      {
        "id": "no-production-without-review",
        "description": "Production changes require code review",
        "action": "block",
        "scope": null,
        "conditions": 1,
        "requirements": 1
      }
    ],
    "total": 3
  },
  "id": "lg-001"
}
```

---

### `cstp.recordDecision` — Record a Decision

Record a new decision with full metadata, reasoning trace, and optional guardrail pre-check.

**Parameters:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `decision` | string | ✅ | — | Decision text / title (max 10,000 characters) |
| `confidence` | float | ✅ | — | Confidence level (0.0–1.0) |
| `category` | string | ✅ | — | Category (e.g., `"architecture"`, `"trading"`) |
| `stakes` | string | ❌ | `"medium"` | `"low"`, `"medium"`, `"high"`, `"critical"` |
| `context` | string | ❌ | — | Free-text context (max 50,000 characters) |
| `reasons` | array | ❌ | `[]` | List of reason objects |
| `reasons[].type` | string | — | — | Reason type: `"technical"`, `"risk"`, `"constraint"`, etc. |
| `reasons[].text` | string | — | — | Reason text |
| `reasons[].strength` | float | — | — | Weight (0.0–1.0) |
| `alternatives_considered` | array | ❌ | `[]` | List of rejected alternatives |
| `review_in` | string | ❌ | `"7d"` | Review cadence: `"24h"`, `"3d"`, `"7d"`, `"30d"` |
| `project` | object | ❌ | — | Project context |
| `project.name` | string | — | — | Project name |
| `project.feature` | string | — | — | Feature name |
| `project.pr` | int | — | — | Pull request number |
| `project.files` | array | — | — | Affected files |
| `reasoning_trace` | array | ❌ | — | Step-by-step reasoning |
| `bridge` | object | ❌ | — | Bridge definition (structure + function) |
| `bridge.structure` | string | ❌ | — | What the decision looks like (pattern) |
| `bridge.function` | string | ❌ | — | What problem it solves (purpose) |
| `bridge.tolerance` | array | ❌ | — | Features that don't matter |
| `bridge.enforcement` | array | ❌ | — | Features that must be present |
| `bridge.prevention` | array | ❌ | — | Features that must be absent |
| `pre_decision_protocol` | object | ❌ | — | Track whether query + guardrail check were performed |

**Example request:**

```json
{
  "jsonrpc": "2.0",
  "method": "cstp.recordDecision",
  "params": {
    "decision": "Use PostgreSQL for persistent state storage",
    "confidence": 0.82,
    "category": "architecture",
    "stakes": "high",
    "bridge": {
        "structure": "PostgreSQL with row-level security",
        "function": "secure multi-tenant data storage"
    },
    "context": "Evaluating database options for agent state persistence",
    "reasons": [
      {"type": "technical", "text": "ACID compliance for critical state", "strength": 0.9},
      {"type": "risk", "text": "Team has PostgreSQL experience", "strength": 0.7}
    ],
    "alternatives_considered": ["SQLite", "Redis", "MongoDB"],
    "project": {
      "name": "CryptoTrader",
      "feature": "state-persistence",
      "pr": 42
    }
  },
  "id": "r-001"
}
```

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "success": true,
    "decision_id": "2026-02-07-decision-a1b2c3d4",
    "path": "decisions/2026/02/2026-02-07-decision-a1b2c3d4.yaml",
    "indexed": true,
    "guardrails_checked": false,
    "bridge_auto": false,
    "deliberation_auto": true,
    "deliberation_inputs_count": 3,
    "related_count": 5,
    "message": "Decision recorded and indexed"
  },
  "id": "r-001"
}
```

---

### `cstp.reviewDecision` — Record Outcome

Record the actual outcome of a previously recorded decision. Only the agent that recorded
it, or an admin, may review it.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `decision_id` | string | ✅ | Decision ID (full or prefix) |
| `outcome` | string | ✅ | `"success"`, `"failure"`, `"partial"`, `"abandoned"` |
| `actual_result` | string | ✅ | Description of what actually happened |
| `lessons` | string | ❌ | Lessons learned |

**Example request:**

```json
{
  "jsonrpc": "2.0",
  "method": "cstp.reviewDecision",
  "params": {
    "decision_id": "2026-02-07-decision-a1b2c3d4",
    "outcome": "success",
    "actual_result": "PostgreSQL has been stable for 30 days with zero data loss",
    "lessons": "Connection pooling was needed sooner than expected"
  },
  "id": "rv-001"
}
```

---

### `cstp.getDecision` — Get Decision Details

Retrieve full decision record including bridge definition and deliberation trace.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | ✅ | Decision ID |

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "id": "2026-02-07-decision-a1b2c3d4",
    "title": "Use PostgreSQL",
    "deliberation": {
      "inputs": [{"type": "query", "content": "db options"}],
      "steps": [{"step": 1, "thought": "PostgreSQL meets ACID reqs"}]
    },
    "bridge": {
      "structure": "PostgreSQL",
      "function": "persistence"
    }
  },
  "id": "gd-001"
}
```

---

### `cstp.updateDecision` - Update Existing Decision

Update fields on an existing decision. Use after recording to finalize with actual outcomes.
Only the agent that recorded it, or an admin, may update it.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | ✅ | Decision ID |
| `updates` | object | ✅ | Fields to update |

**Allowed update fields:** `decision`, `confidence`, `context`, `tags`, `pattern`, `reasons`, `bridge`

> **Note:** `deliberation` is intentionally excluded - it's append-only via `recordThought`.

**Example request:**

```json
{
  "jsonrpc": "2.0",
  "method": "cstp.updateDecision",
  "params": {
    "id": "dec_abc123",
    "updates": {
      "decision": "Used Redis with connection pooling",
      "confidence": 0.90,
      "context": "Deployed successfully. Latency improved 5x.",
      "tags": ["caching", "redis", "infrastructure"]
    }
  },
  "id": 1
}
```

**Response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "success": true,
    "id": "dec_abc123",
    "applied": ["decision", "confidence", "context", "tags"],
    "indexed": true
  },
  "id": 1
}
```

---

### `cstp.recordThought` - Capture Reasoning Steps

Record chain-of-thought reasoning. Pre-decision mode accumulates in the tracker; post-decision mode appends to an existing decision's trace.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | ✅ | Reasoning text, at most 10,000 characters |
| `decision_id` | string | ❌ | If set, appends to existing decision (post-decision mode); the caller must have recorded it |

**Pre-decision example (no decision_id):**

```json
{
  "jsonrpc": "2.0",
  "method": "cstp.recordThought",
  "params": {
    "text": "Considering Redis vs Memcached - Redis has persistence"
  },
  "id": 1
}
```

**Response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "success": true,
    "mode": "pre-decision",
    "agent_id": "emerson"
  },
  "id": 1
}
```

**Post-decision example (with decision_id):**

```json
{
  "jsonrpc": "2.0",
  "method": "cstp.recordThought",
  "params": {
    "text": "Redis chosen - persistence and pub/sub clinched it",
    "decision_id": "dec_abc123"
  },
  "id": 1
}
```

**Response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "success": true,
    "mode": "post-decision",
    "decision_id": "dec_abc123",
    "step_number": 4
  },
  "id": 1
}
```

---

### `cstp.getReasonStats` - Reason Analytics

Analyze success rates and diversity of decision reasons.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `min_reviewed` | int | ❌ | Minimum number of reviewed decisions (default 10) |

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "success_rates": {"technical": 0.9, "intuition": 0.6},
    "diversity_score": 0.75,
    "dominant_type": "technical"
  },
  "id": "grs-001"
}
```

---

### `cstp.getCalibration` — Calibration Statistics

Compute confidence calibration metrics for reviewed decisions.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `agent` | string | ❌ | Filter by recording agent |
| `category` | string | ❌ | Filter by decision category |
| `stakes` | string | ❌ | Filter by stakes level |
| `project` | string | ❌ | Filter by project |
| `feature` | string | ❌ | Filter by feature |
| `window` | string | ❌ | Time window: `"30d"`, `"60d"`, `"90d"` |

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "total_decisions": 47,
    "reviewed_decisions": 32,
    "overall": {
      "brier_score": 0.142,
      "accuracy": 0.78,
      "calibration_gap": 0.05,
      "interpretation": "Good calibration"
    },
    "buckets": [
      {"range": "0.0-0.2", "decisions": 2, "predicted": 0.15, "actual": 0.0, "brier": 0.023},
      {"range": "0.2-0.4", "decisions": 5, "predicted": 0.32, "actual": 0.4, "brier": 0.051},
      {"range": "0.4-0.6", "decisions": 8, "predicted": 0.52, "actual": 0.5, "brier": 0.098},
      {"range": "0.6-0.8", "decisions": 10, "predicted": 0.72, "actual": 0.7, "brier": 0.089},
      {"range": "0.8-1.0", "decisions": 7, "predicted": 0.88, "actual": 0.86, "brier": 0.021}
    ],
    "confidence_stats": {
      "mean": 0.64,
      "std_dev": 0.21,
      "min": 0.12,
      "max": 0.95,
      "habituation_detected": false
    },
    "recommendations": [
      {"type": "improvement", "message": "Consider more decisions in 0.0-0.2 range", "severity": "low"}
    ]
  },
  "id": "c-001"
}
```

---

### `cstp.attributeOutcomes` — Automatic Outcome Attribution

Automatically attribute outcomes to decisions based on PR stability (age). Admin only: it
writes outcomes onto every matching decision, whichever agent recorded it.

**Parameters:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project` | string | ✅ | — | Project name |
| `since` | string | ❌ | `"30d"` | Look-back window |
| `stability_days` | int | ❌ | 7 | Days before PR is considered stable |
| `dry_run` | bool | ❌ | false | Preview only, don't modify files |

---

### `cstp.checkDrift` — Calibration Drift Detection

Compare recent calibration against historical baseline to detect degradation.

**Parameters:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `category` | string | ❌ | — | Filter by category |
| `project` | string | ❌ | — | Filter by project |
| `brier_threshold` | float | ❌ | 0.05 | Brier score change threshold for alerts |
| `accuracy_threshold` | float | ❌ | 0.1 | Accuracy change threshold for alerts |

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "drift_detected": true,
    "alerts": [
      {
        "type": "brier_degradation",
        "recent_value": 0.22,
        "historical_value": 0.14,
        "change_pct": 57.1,
        "severity": "warning"
      }
    ],
    "recent_period": "30d",
    "historical_period": "90d+",
    "recommendations": ["Review recent high-stakes decisions for confidence accuracy"]
  },
  "id": "d-001"
}
```

---

### `cstp.reindex` — Full Reindex

Delete and rebuild the ChromaDB collection from YAML files. Admin only.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `decisions_path` | string | ❌ | Override decisions directory |

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "success": true,
    "decisions_indexed": 47,
    "errors": 0,
    "duration_ms": 12340
  },
  "id": "ri-001"
}
```

---

## Provenance & Control Evidence (F055)

Five methods that produce audit-grade evidence for regulated decisions. **JSON-RPC only — these
have no MCP tool equivalents.**

Evidence is stored in its own SQLite WAL database, separate from the decision store, at the path
given by `PROVENANCE_DB` (default `~/.cstp/provenance.db`). The path is server-configured and
cannot be overridden per request. The same holds for the bundle output directory
(`PROVENANCE_BUNDLE_DIR`, default `~/.cstp/bundles`) and the control-mapping rules, which always
load from `a2a/cstp/provenance/mappings/`.

### The Two-Class Evidence Model

Every evidence record carries an `evidence_class` that is never nullable and never defaulted:

| Class | Source | Trust |
|-------|--------|-------|
| `observed` | Third-party events from a system of record the agent does not control — GitHub PR opens, commits, reviews, approvals, merges | Hash-chained and tamper-evident |
| `attested` | First-party CSTP decision records linked to observed events | Self-reported; an auditor discounts these |

Coverage is reported **separately per class**. There is no blended `coverage_pct` in the API
response, the JSON bundle, or the PDF — merging the two would weaken the strong half.

---

### `cstp.ingestEvidence` — Ingest Observed Events

Append third-party events to the hash chain. All ingested events receive
`evidence_class: "observed"`; this is not overridable. A batch is ingested atomically.

Admin only: control mappings treat observed events as coming from a system the decision's
agent does not control, such as a human PR approval, so an agent must not be able to write
them. Each payload is stamped with `ingested_by`, the agent that sent it.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | ✅ | Ingest origin, e.g. `"github"` |
| `events` | array | ✅ | Event objects with `event_type`, `ts`, and a `payload` object |

**Example request:**

```json
{
  "jsonrpc": "2.0",
  "method": "cstp.ingestEvidence",
  "params": {
    "source": "github",
    "events": [
      {
        "event_type": "pr_opened",
        "ts": "2026-07-01T10:00:00Z",
        "payload": {
          "pr_number": 42,
          "title": "Add risk scorer v2",
          "is_agent_authored": true,
          "agent_type": "Claude",
          "approval_count": 0
        }
      },
      {
        "event_type": "pr_review_approved",
        "ts": "2026-07-01T14:00:00Z",
        "payload": { "pr_number": 42, "reviewer": "alice", "actor_is_human": true }
      }
    ]
  },
  "id": "ie-001"
}
```

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "ingested": 2,
    "head_hash": "65b58d83a0b5be919...",
    "first_seq": 1
  },
  "id": "ie-001"
}
```

---

### `cstp.linkEvidence` — Correlate a Decision to Observed Events

Links an existing CSTP decision to observed event sequence numbers, creating one
`cstp_decision_linked` record with `evidence_class: "attested"`. The decision must exist
and have been recorded by the caller (admins may link any decision).

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `decision_id` | string | ✅ | Existing CSTP decision ID |
| `event_seqs` | array | ✅ | Observed event `seq` values. Nonexistent, duplicate, and attested-class seqs are rejected |
| `stakes` | string | ❌ | Stakes recorded on the attestation |
| `has_outcome` | boolean | ❌ | Whether the decision has a reviewed outcome |

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": { "linked": 3, "decision_id": "dec-7e2f9a", "attested_seq": 4 },
  "id": "le-001"
}
```

---

### `cstp.mapControls` — Map Evidence to Control Frameworks

Runs the YAML rules engine over all stored evidence. Rules only match events of their own
evidence class.

**Parameters:** none.

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "coverage": {
      "observed": { "total_events": 50, "mapped_events": 41, "unmapped_events": 9, "coverage_pct": 82.0 },
      "attested": { "total_events": 3, "mapped_events": 3, "unmapped_events": 0, "coverage_pct": 100.0 }
    },
    "mappings": [
      {
        "seq": 1, "event_type": "pr_opened", "pr_number": 42,
        "rule_id": "SR11-7-MV1-agent-pr-opened",
        "framework": "SR 11-7", "stage": "Model Development",
        "function_id": "MV-1", "control_name": "Agent-Authored Change Documentation",
        "evidence_class": "observed", "type": "mapping"
      }
    ],
    "insufficient_evidence": [
      {
        "seq": 10, "pr_number": 3, "framework": "SR 11-7",
        "function_id": "MV-3", "control_name": "Independent Human Review and Approval",
        "evidence_class": "observed", "type": "INSUFFICIENT_EVIDENCE",
        "reason": "INSUFFICIENT EVIDENCE: Agent-authored PR was merged without any human approval..."
      }
    ],
    "insufficient_evidence_count": 2,
    "unmapped": [],
    "attested_only_controls": ["MV-3"]
  },
  "id": "mc-001"
}
```

Rules live in `a2a/cstp/provenance/mappings/`:

| File | Framework | Evidence class |
|------|-----------|----------------|
| `sr11-7.yaml` | SR 11-7 (Federal Reserve MRM) | observed |
| `nist-ai-rmf.yaml` | NIST AI RMF 1.0 | observed |
| `cstp-attested.yaml` | CSTP Attested | attested |

Mappings are hand-written on purpose — an ML-inferred control mapping is itself an audit
liability. Where evidence does not honestly support a control, the engine emits
`INSUFFICIENT_EVIDENCE` with a plain-English reason instead of stretching the mapping. Controls
requiring human review (`MV-3`, `MEASURE-2.5`) fail closed unless `actor_is_human` is true;
`CM-1` / `MANAGE-1.1` require a nonzero human approval count.

---

### `cstp.exportEvidenceBundle` — Export the Auditor Bundle

Admin only: it writes bundle files on the server and records the chain head that
`cstp.verifyEvidenceChain` later checks against.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `format` | string | ❌ | `"json"` (default) or `"pdf"`. Validated before any output directory is created |

Bundles are written to the server's `PROVENANCE_BUNDLE_DIR`; the response's `json_path` /
`pdf_path` say where. An `output_dir` or `mappings_dir` param is ignored.

PDF export requires the optional `pdf` extra (`pip install -e ".[pdf]"`, included in `[all]`).
Without `reportlab` installed, a `format: "pdf"` request raises. Bundle filenames include a UUID
and microsecond timestamp, so concurrent exports cannot collide.

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "json_path": "/var/bundles/2026-Q3/bundle_20260809T173649Z.json",
    "chain_head_hash": "65b58d83a0b5be919...",
    "chain_intact": true,
    "coverage": { "observed": {}, "attested": {} },
    "insufficient_evidence_count": 2,
    "attested_only_controls": ["MV-3"]
  },
  "id": "eb-001"
}
```

The bundle persists `chain_head_hash` at generation time, which is what makes tail truncation
detectable later.

---

### `cstp.verifyEvidenceChain` — Verify Chain Integrity

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `expected_head_hash` | string | ❌ | Detects tail truncation. Defaults to the head hash stored by the most recent bundle export |

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "intact": true,
    "broken_at_seq": null,
    "head_hash": "65b58d83a0b5be919...",
    "expected_hash": "65b58d83a0b5be919...",
    "tail_check": true
  },
  "id": "vc-001"
}
```

If no hash is supplied and no bundle has ever been generated, tail-truncation detection is
inactive and `tail_check` is `false` — internal tampering is still detected.

::: warning Chain format version 2
The hash preimage is the canonical JSON of `{seq, ts, event_type, evidence_class, payload,
prev_hash}`. Stores written under format version 1 (newline-delimited preimage) fail
`verify_chain` and must be re-ingested.
:::

See the [F055 spec](/specs/f055-decision-provenance) for the full evidence model and hash chain
design.

---

## Methods Not Yet Documented Here

The dispatcher registers 34 methods. Beyond those above, the following are live but not yet
covered in this reference — see the linked specs for parameters and responses:

| Method | Feature |
|--------|---------|
| `cstp.preAction` | [F046 Pre-Action Hook](/specs/f046-pre-action-hook) |
| `cstp.getSessionContext` | [F047 Session Context](/specs/f047-session-context) |
| `cstp.ready` | [F044 Agent Work Discovery](/specs/f044-agent-work-discovery) |
| `cstp.linkDecisions`, `cstp.getGraph`, `cstp.getNeighbors` | [F045 Graph Storage](/specs/f045-graph-storage-layer) |
| `cstp.compact`, `cstp.getCompacted`, `cstp.setPreserve`, `cstp.getWisdom` | [F041 Memory Compaction](/specs/f041-memory-compaction) |
| `cstp.listDecisions`, `cstp.getStats` | F050 Structured Storage |
| `cstp.listBreakers`, `cstp.getCircuitState`, `cstp.resetCircuit` | [F030 Circuit Breakers](/specs/f030-circuit-breaker-guardrails) |
| `cstp.debugTracker` | Live deliberation tracker inspection |

Most of these are also exposed as MCP tools — see the tool table below.

---

## MCP Interface (Model Context Protocol)

Since v0.9.0, CSTP exposes decision intelligence capabilities as **MCP tools** for native integration with any MCP-compliant agent. The MCP layer is a thin bridge — each tool maps 1:1 to an existing CSTP service method with zero code duplication.

### Transports

| Transport | Endpoint | Use Case |
|-----------|----------|----------|
| **Streamable HTTP** | `POST`/`GET` `http://host:9991/mcp` | Remote access from any network-reachable MCP client |
| **stdio** | `python -m a2a.mcp_server` | Local access or Docker exec |

### Connecting

```bash
# Claude Code — add as remote MCP server
claude mcp add --transport http cstp-decisions http://your-server:9991/mcp

# Claude Desktop — add to claude_desktop_config.json (stdio via Docker)
{
  "mcpServers": {
    "cstp": {
      "command": "docker",
      "args": ["exec", "-i", "cstp", "python", "-m", "a2a.mcp_server"]
    }
  }
}

# Local development (stdio)
python -m a2a.mcp_server
```

### MCP Tools

#### `query_decisions`

Search similar past decisions using semantic search, keyword matching, or hybrid retrieval.

**Input Schema:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | ✅ | — | Natural language query |
| `limit` | int | ❌ | 5 | Max results (1–50) |
| `retrieval_mode` | string | ❌ | `"hybrid"` | `"semantic"`, `"keyword"`, or `"hybrid"` |
| `filters` | object | ❌ | — | `category`, `stakes`, `project`, `has_outcome` |

**Maps to:** `cstp.queryDecisions`

---

#### `check_action`

Validate an intended action against safety guardrails and policies.

**Input Schema:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `description` | string | ✅ | — | Action you intend to take (max 10,000 characters) |
| `category` | string | ❌ | — | Action category |
| `stakes` | string | ❌ | `"medium"` | `"low"`, `"medium"`, `"high"`, `"critical"` |
| `confidence` | float | ❌ | — | Your confidence (0.0–1.0) |

**Maps to:** `cstp.checkGuardrails`

---

#### `log_decision`

> ⚠️ **Last resort.** Prefer `pre_action` with `auto_record: true` for the standard flow.

Record a decision to the immutable decision log manually. Use only when `pre_action` wasn't called (legacy clients, spontaneous decisions, no prior context).

**Input Schema:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `decision` | string | ✅ | — | What you decided (state the choice; max 10,000 characters) |
| `confidence` | float | ✅ | — | Confidence level (0.0–1.0) |
| `category` | string | ✅ | — | `"architecture"`, `"process"`, `"integration"`, `"tooling"`, `"security"` |
| `stakes` | string | ❌ | `"medium"` | Stakes level |
| `context` | string | ❌ | — | Situation context (max 50,000 characters) |
| `reasons` | array | ❌ | — | `[{"type": "analysis", "text": "..."}]` — types: authority, analogy, analysis, pattern, intuition |
| `tags` | array | ❌ | — | Tags for categorization |
| `project` | string | ❌ | — | Project in owner/repo format |
| `feature` | string | ❌ | — | Feature or epic name |
| `pr` | int | ❌ | — | Pull request number |
| `agent_id` | string | ❌ | — | Agent identifier for multi-agent deliberation isolation |
| `decision_id` | string | ❌ | — | Decision ID to scope deliberation consumption |

**Maps to:** `cstp.recordDecision`

---

#### `review_outcome`

Record the outcome of a past decision for calibration.

**Input Schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | ✅ | Decision ID (8-char hex) |
| `outcome` | string | ✅ | `"success"`, `"partial"`, `"failure"`, `"abandoned"` |
| `actual_result` | string | ❌ | What actually happened |
| `lessons` | string | ❌ | Lessons learned |
| `notes` | string | ❌ | Additional review notes |

**Maps to:** `cstp.reviewDecision`

---

#### `get_stats`

Get calibration statistics to assess decision-making quality.

**Input Schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `category` | string | ❌ | Filter by category |
| `project` | string | ❌ | Filter by project (owner/repo) |
| `window` | string | ❌ | `"30d"`, `"60d"`, `"90d"`, or `"all"` |

**Maps to:** `cstp.getCalibration`

---

#### `link_decisions`

Create typed edges between decisions in the knowledge graph.

**Input Schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_id` | string | ✅ | Source decision ID |
| `target_id` | string | ✅ | Target decision ID |
| `edge_type` | string | ✅ | `"relates_to"`, `"supersedes"`, `"depends_on"` |
| `weight` | float | ❌ | Edge weight (0.0-1.0, default 1.0) |
| `context` | string | ❌ | Why these decisions are linked |

**Maps to:** `cstp.linkDecisions`

---

#### `get_graph`

Query subgraph around a decision with configurable depth.

**Input Schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `decision_id` | string | ✅ | Center decision ID |
| `depth` | int | ❌ | Traversal depth (default 1) |
| `edge_types` | string[] | ❌ | Filter by edge type |

**Maps to:** `cstp.getGraph`

---

#### `get_neighbors`

Lightweight query for direct connections of a decision.

**Input Schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `decision_id` | string | ✅ | Decision ID to query |
| `edge_type` | string | ❌ | Filter by edge type |

Returns neighbor IDs with edge types and weights. Faster than `get_graph` when you only need immediate connections.

**Maps to:** `cstp.getNeighbors`

---

#### Debug: `cstp.debugTracker`

Inspect live deliberation tracker state. Available via JSON-RPC only (not as MCP tool).

```bash
curl -X POST http://localhost:8100/cstp \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"cstp.debugTracker","params":{},"id":1}'
```

Returns active sessions with composite tracker keys, input counts, thought text, source, and age in seconds. Useful for debugging multi-agent deliberation isolation. Keys are namespaced by the token's agent (`emerson|agent:planner`); an agent sees only its own sessions, admins see all.

---

### Error Handling

MCP tool errors are returned as `TextContent` with JSON:

```json
{
  "error": "validation_error",
  "message": "1 validation error for QueryDecisionsInput..."
}
```

Error types:
- `validation_error` — Invalid input (Pydantic validation failure)
- `permission_denied` — The token's agent may not perform this operation
- `internal_error` — Unexpected server error; the message carries a reference to the server log

---

## cURL Quick Reference

```bash
# Query decisions
curl -X POST http://localhost:9991/cstp \
  -H "Authorization: Bearer myagent:mytoken" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"cstp.queryDecisions","params":{"query":"authentication approach"},"id":"1"}'

# Check guardrails
curl -X POST http://localhost:9991/cstp \
  -H "Authorization: Bearer myagent:mytoken" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"cstp.checkGuardrails","params":{"action":{"description":"Deploy to prod","stakes":"high","context":{"affects_production":true}}},"id":"2"}'

# Record decision
curl -X POST http://localhost:9991/cstp \
  -H "Authorization: Bearer myagent:mytoken" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"cstp.recordDecision","params":{"decision":"Use Redis for caching","confidence":0.8,"category":"architecture"},"id":"3"}'

# Health check (no auth)
curl http://localhost:9991/health
```
