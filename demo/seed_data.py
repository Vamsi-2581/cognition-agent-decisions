#!/usr/bin/env python3
"""Generate realistic seed data for the Cognition Engines demo.

Creates a SQLite database with ~50 decisions across categories,
with varying confidence, outcomes, and intentional miscalibration
to make the dashboard interesting from the start.
"""

import ast
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The server's SQLite store. Its schema is read from the source rather than
# imported: importing `a2a` pulls in FastAPI, and this script runs before
# anything is installed (see demo/README.md).
STORE_MODULE = Path(__file__).resolve().parent.parent / "a2a" / "cstp" / "storage" / "sqlite.py"

# Realistic demo decisions organized by category
DECISIONS = [
    # Architecture decisions
    {
        "decision": "Use Redis for session caching instead of Memcached",
        "category": "architecture",
        "stakes": "medium",
        "confidence": 0.85,
        "context": "Need distributed caching for session data. Redis supports persistence and pub/sub, Memcached is simpler but memory-only.",
        "reasons": [
            {"type": "analysis", "text": "Redis persistence prevents cache cold starts after restarts"},
            {"type": "empirical", "text": "Benchmarks show similar throughput for our workload size"},
        ],
        "tags": ["caching", "infrastructure", "redis"],
        "pattern": "Choose richer tool when the extra features have near-term use",
        "outcome": "success",
        "result": "Redis caching deployed. Persistence saved us during a restart incident.",
        "days_ago": 30,
    },
    {
        "decision": "Migrate from REST to gRPC for inter-service communication",
        "category": "architecture",
        "stakes": "high",
        "confidence": 0.75,
        "context": "Service-to-service latency is growing. gRPC offers binary protocol, streaming, and code generation.",
        "reasons": [
            {"type": "analysis", "text": "P99 latency will drop ~40% based on protobuf vs JSON benchmarks"},
            {"type": "constraint", "text": "Team needs training on Protocol Buffers and gRPC patterns"},
        ],
        "tags": ["grpc", "api", "performance", "migration"],
        "pattern": "Accept short-term complexity for long-term performance gains",
        "outcome": "partial",
        "result": "Migration completed for 3 of 8 services. Latency improved but debugging is harder.",
        "days_ago": 25,
    },
    {
        "decision": "Adopt event sourcing for the order management domain",
        "category": "architecture",
        "stakes": "high",
        "confidence": 0.70,
        "context": "Order state changes need full audit trail. Event sourcing provides natural audit log and temporal queries.",
        "reasons": [
            {"type": "pattern", "text": "Event sourcing is proven for financial/order domains"},
            {"type": "analysis", "text": "CQRS read models can optimize for different query patterns"},
        ],
        "tags": ["event-sourcing", "cqrs", "orders", "audit"],
        "pattern": "Use event sourcing when audit trail is a core requirement, not an afterthought",
        "outcome": "success",
        "result": "Event store handles 10K events/sec. Audit trail eliminated compliance gaps.",
        "days_ago": 22,
    },
    {
        "decision": "Use SQLite for embedded analytics instead of PostgreSQL",
        "category": "architecture",
        "stakes": "medium",
        "confidence": 0.90,
        "context": "Analytics module runs on edge devices with limited resources. Need embedded DB.",
        "reasons": [
            {"type": "constraint", "text": "Edge devices have 512MB RAM, can't run PostgreSQL"},
            {"type": "empirical", "text": "SQLite WAL mode handles concurrent reads well for our scale"},
        ],
        "tags": ["sqlite", "edge", "analytics", "embedded"],
        "pattern": "Match database choice to deployment constraints, not feature wishlist",
        "outcome": "success",
        "result": "SQLite deployed to 200+ edge devices. WAL mode handles the read concurrency.",
        "days_ago": 18,
    },
    {
        "decision": "Implement circuit breaker pattern for external API calls",
        "category": "architecture",
        "stakes": "medium",
        "confidence": 0.92,
        "context": "Third-party payment API has periodic outages causing cascading failures.",
        "reasons": [
            {"type": "pattern", "text": "Circuit breaker prevents cascade failures from flaky dependencies"},
            {"type": "empirical", "text": "Last outage took down checkout for 45 minutes"},
        ],
        "tags": ["circuit-breaker", "resilience", "payments", "reliability"],
        "pattern": "Add circuit breakers before the second outage, not after the third",
        "outcome": "success",
        "result": "Circuit breaker tripped 3 times in first month, each time preventing cascade.",
        "days_ago": 15,
    },
    {
        "decision": "Use WebSocket instead of polling for real-time dashboard updates",
        "category": "architecture",
        "stakes": "low",
        "confidence": 0.88,
        "context": "Dashboard currently polls every 5 seconds. Users want instant updates.",
        "reasons": [
            {"type": "analysis", "text": "WebSocket eliminates polling overhead and reduces server load"},
            {"type": "constraint", "text": "Load balancer already supports WebSocket upgrade"},
        ],
        "tags": ["websocket", "realtime", "dashboard", "performance"],
        "pattern": "Switch from polling to push when latency expectations decrease",
        "outcome": None,
        "days_ago": 5,
    },
    {
        "decision": "Adopt trunk-based development with feature flags",
        "category": "architecture",
        "stakes": "medium",
        "confidence": 0.80,
        "context": "Long-lived feature branches causing painful merges. Team of 8 developers.",
        "reasons": [
            {"type": "authority", "text": "Google and Facebook both use trunk-based development at scale"},
            {"type": "analysis", "text": "Feature flags decouple deployment from release, reducing merge conflicts"},
        ],
        "tags": ["git", "branching", "feature-flags", "process"],
        "pattern": "Reduce branch lifetime to reduce integration pain",
        "outcome": "success",
        "result": "Merge conflicts dropped 70%. Deploy frequency increased from weekly to daily.",
        "days_ago": 20,
    },

    # Tooling decisions
    {
        "decision": "Replace Jenkins with GitHub Actions for CI/CD",
        "category": "tooling",
        "stakes": "medium",
        "confidence": 0.88,
        "context": "Jenkins server requires maintenance. GitHub Actions integrates natively with our repos.",
        "reasons": [
            {"type": "analysis", "text": "GitHub Actions eliminates Jenkins server maintenance overhead"},
            {"type": "empirical", "text": "Action marketplace has pre-built steps for our stack"},
        ],
        "tags": ["ci-cd", "github-actions", "jenkins", "automation"],
        "pattern": "Prefer managed services over self-hosted when maintenance cost exceeds control benefit",
        "outcome": "success",
        "result": "CI/CD pipeline migrated in 2 weeks. Build times improved 30%.",
        "days_ago": 28,
    },
    {
        "decision": "Use Ruff instead of flake8 + isort + black for Python linting",
        "category": "tooling",
        "stakes": "low",
        "confidence": 0.95,
        "context": "Multiple Python linting tools with overlapping configs. Ruff replaces all three.",
        "reasons": [
            {"type": "empirical", "text": "Ruff is 10-100x faster than flake8 and handles formatting too"},
            {"type": "analysis", "text": "Single config file replaces three separate configs"},
        ],
        "tags": ["python", "linting", "developer-experience"],
        "pattern": "Consolidate tools when a single replacement covers all use cases",
        "outcome": "success",
        "result": "Lint time dropped from 12s to 0.3s. One pyproject.toml section instead of three config files.",
        "days_ago": 24,
    },
    {
        "decision": "Adopt uv for Python package management",
        "category": "tooling",
        "stakes": "low",
        "confidence": 0.82,
        "context": "pip + virtualenv workflow is slow. uv promises faster installs and better resolution.",
        "reasons": [
            {"type": "empirical", "text": "uv resolves and installs 10-100x faster than pip"},
            {"type": "constraint", "text": "Still needs pip compatibility for some legacy packages"},
        ],
        "tags": ["python", "package-management", "developer-experience"],
        "pattern": "Adopt faster tooling early when backward compatibility is maintained",
        "outcome": "success",
        "result": "CI install step dropped from 90s to 8s. All packages compatible.",
        "days_ago": 21,
    },
    {
        "decision": "Use Terraform instead of CloudFormation for AWS infrastructure",
        "category": "tooling",
        "stakes": "medium",
        "confidence": 0.78,
        "context": "CloudFormation only works with AWS. Planning multi-cloud expansion to GCP.",
        "reasons": [
            {"type": "analysis", "text": "Terraform supports multi-cloud with same language and workflow"},
            {"type": "constraint", "text": "Team already knows HCL from a previous project"},
        ],
        "tags": ["terraform", "infrastructure-as-code", "multi-cloud"],
        "pattern": "Choose multi-cloud tooling before multi-cloud need becomes urgent",
        "outcome": "success",
        "result": "GCP expansion completed 3 months later. Terraform modules reused 60% of AWS configs.",
        "days_ago": 27,
    },
    {
        "decision": "Switch from Datadog to Grafana+Prometheus for observability",
        "category": "tooling",
        "stakes": "medium",
        "confidence": 0.72,
        "context": "Datadog costs $8K/month. Self-hosted Grafana+Prometheus would be ~$500/month in compute.",
        "reasons": [
            {"type": "analysis", "text": "Cost reduction of ~$7.5K/month justifies migration effort"},
            {"type": "constraint", "text": "Team needs training on PromQL and Grafana dashboard creation"},
        ],
        "tags": ["observability", "monitoring", "cost-reduction"],
        "pattern": "Migrate from SaaS to self-hosted when cost exceeds value and team can maintain",
        "outcome": "partial",
        "result": "Metrics migrated successfully. Log aggregation still on Datadog - Loki adoption stalled.",
        "days_ago": 19,
    },

    # Process decisions
    {
        "decision": "Implement mandatory PR reviews with 2 approvers",
        "category": "process",
        "stakes": "medium",
        "confidence": 0.90,
        "context": "Two production incidents traced to unreviewed code changes.",
        "reasons": [
            {"type": "empirical", "text": "Both incidents would have been caught by basic review"},
            {"type": "pattern", "text": "Two reviewers catch different types of issues"},
        ],
        "tags": ["code-review", "quality", "process", "safety"],
        "pattern": "Add review gates after the second incident, not the third",
        "outcome": "success",
        "result": "Zero unreviewed-code incidents in 3 months. Review turnaround < 4 hours.",
        "days_ago": 26,
    },
    {
        "decision": "Run weekly architecture decision records (ADR) reviews",
        "category": "process",
        "stakes": "low",
        "confidence": 0.75,
        "context": "Architecture decisions are made in Slack threads and forgotten. Need institutional memory.",
        "reasons": [
            {"type": "pattern", "text": "ADRs create searchable decision history"},
            {"type": "analysis", "text": "Weekly cadence prevents backlog without being burdensome"},
        ],
        "tags": ["adr", "documentation", "knowledge-management"],
        "pattern": "Institutionalize decision recording before organizational memory is needed",
        "outcome": "success",
        "result": "30 ADRs in 3 months. Referenced 5 times to resolve 'why did we do this?' questions.",
        "days_ago": 23,
    },
    {
        "decision": "Adopt blameless post-mortems for all P1 incidents",
        "category": "process",
        "stakes": "medium",
        "confidence": 0.85,
        "context": "Engineers afraid to report incidents due to blame culture. Issues getting hidden.",
        "reasons": [
            {"type": "authority", "text": "Google SRE handbook recommends blameless post-mortems"},
            {"type": "analysis", "text": "Blame culture reduces incident reporting, increasing actual risk"},
        ],
        "tags": ["incidents", "post-mortem", "culture", "safety"],
        "pattern": "Remove blame to increase transparency and reduce hidden failures",
        "outcome": "success",
        "result": "Incident reporting up 300%. Mean time to detection dropped 40%.",
        "days_ago": 17,
    },
    {
        "decision": "Implement on-call rotation with 1-week shifts",
        "category": "process",
        "stakes": "medium",
        "confidence": 0.80,
        "context": "Two senior engineers handling all pages. Unsustainable and creates single points of failure.",
        "reasons": [
            {"type": "constraint", "text": "Need at least 5 engineers in rotation for sustainable coverage"},
            {"type": "analysis", "text": "1-week shifts balance context retention with burnout prevention"},
        ],
        "tags": ["on-call", "reliability", "team-health"],
        "pattern": "Distribute operational burden before key people burn out",
        "outcome": "partial",
        "result": "Rotation implemented with 6 engineers. Two still escalated to seniors 60% of the time.",
        "days_ago": 14,
    },
    {
        "decision": "Set up automated dependency updates with Renovate",
        "category": "process",
        "stakes": "low",
        "confidence": 0.88,
        "context": "Dependencies 6+ months out of date. Manual updates are tedious and forgotten.",
        "reasons": [
            {"type": "empirical", "text": "Automated PRs with CI checks catch breaking updates early"},
            {"type": "analysis", "text": "Small, frequent updates are safer than big-bang upgrades"},
        ],
        "tags": ["dependencies", "automation", "security"],
        "pattern": "Automate maintenance tasks that humans consistently forget",
        "outcome": "success",
        "result": "200+ dependency PRs auto-merged in 3 months. Zero breaking upgrades.",
        "days_ago": 16,
    },

    # Security decisions
    {
        "decision": "Implement API rate limiting with token bucket algorithm",
        "category": "security",
        "stakes": "high",
        "confidence": 0.90,
        "context": "No rate limiting on public API. Vulnerable to DDoS and credential stuffing.",
        "reasons": [
            {"type": "constraint", "text": "Must handle burst traffic without blocking legitimate users"},
            {"type": "analysis", "text": "Token bucket allows bursts while enforcing average rate"},
        ],
        "tags": ["rate-limiting", "api-security", "ddos", "reliability"],
        "pattern": "Implement rate limiting before the first abuse incident",
        "outcome": "success",
        "result": "Blocked 3 credential stuffing attempts in first week. Zero false positives.",
        "days_ago": 29,
    },
    {
        "decision": "Migrate secrets from environment variables to HashiCorp Vault",
        "category": "security",
        "stakes": "high",
        "confidence": 0.82,
        "context": "Secrets in .env files and CI variables. No rotation, no audit trail.",
        "reasons": [
            {"type": "analysis", "text": "Vault provides rotation, audit logging, and dynamic secrets"},
            {"type": "pattern", "text": "Centralized secret management scales better than distributed .env files"},
        ],
        "tags": ["secrets", "vault", "security", "compliance"],
        "pattern": "Centralize secret management before the first leak",
        "outcome": "success",
        "result": "All production secrets in Vault. Automatic rotation for database credentials.",
        "days_ago": 13,
    },
    {
        "decision": "Add Content Security Policy headers to all web responses",
        "category": "security",
        "stakes": "medium",
        "confidence": 0.85,
        "context": "Penetration test flagged missing CSP headers. XSS risk on user-generated content pages.",
        "reasons": [
            {"type": "empirical", "text": "Pen test found 2 potential XSS vectors"},
            {"type": "analysis", "text": "CSP headers block inline scripts and unauthorized resource loading"},
        ],
        "tags": ["csp", "xss", "web-security", "headers"],
        "pattern": "Add defense-in-depth headers even when no active exploit exists",
        "outcome": "success",
        "result": "CSP deployed. Blocked 1 reflected XSS attempt from a third-party script.",
        "days_ago": 11,
    },
    {
        "decision": "Implement JWT refresh token rotation",
        "category": "security",
        "stakes": "high",
        "confidence": 0.78,
        "context": "Long-lived access tokens (24h) create large window for token theft exploitation.",
        "reasons": [
            {"type": "analysis", "text": "Short access tokens (15min) + refresh rotation limits theft window"},
            {"type": "constraint", "text": "Must not disrupt active user sessions during rotation"},
        ],
        "tags": ["jwt", "authentication", "token-security"],
        "pattern": "Reduce credential lifetime to reduce theft impact",
        "outcome": "failure",
        "result": "Rotation caused logout storms when Redis went down. Rolled back to 1h access tokens.",
        "days_ago": 10,
    },

    # Integration decisions
    {
        "decision": "Use webhook-based integration instead of polling for Stripe events",
        "category": "integration",
        "stakes": "medium",
        "confidence": 0.92,
        "context": "Polling Stripe API every 30s for payment status. Wastes API quota and adds latency.",
        "reasons": [
            {"type": "analysis", "text": "Webhooks provide instant notification vs 30s polling delay"},
            {"type": "constraint", "text": "Must handle webhook replay and out-of-order delivery"},
        ],
        "tags": ["stripe", "webhooks", "payments", "integration"],
        "pattern": "Prefer push over poll when the upstream supports it",
        "outcome": "success",
        "result": "Payment status updates now instant. API calls reduced 95%.",
        "days_ago": 12,
    },
    {
        "decision": "Build custom ETL pipeline instead of using Fivetran",
        "category": "integration",
        "stakes": "medium",
        "confidence": 0.65,
        "context": "Fivetran costs $2K/month for 5 connectors. Custom pipeline would take ~3 weeks to build.",
        "reasons": [
            {"type": "analysis", "text": "Custom pipeline saves $24K/year after initial build investment"},
            {"type": "constraint", "text": "Need to maintain connectors ourselves going forward"},
        ],
        "tags": ["etl", "data-pipeline", "cost-reduction", "build-vs-buy"],
        "pattern": "Build custom when maintenance cost is predictable and savings are significant",
        "outcome": "failure",
        "result": "Custom pipeline took 8 weeks, not 3. Maintenance burden higher than expected. Migrating back to Fivetran.",
        "days_ago": 8,
    },
    {
        "decision": "Adopt OpenTelemetry for distributed tracing",
        "category": "integration",
        "stakes": "medium",
        "confidence": 0.85,
        "context": "Jaeger traces only cover 2 of 8 services. Need vendor-neutral instrumentation.",
        "reasons": [
            {"type": "pattern", "text": "OpenTelemetry is the industry standard for vendor-neutral observability"},
            {"type": "analysis", "text": "Auto-instrumentation covers HTTP, gRPC, and database calls"},
        ],
        "tags": ["opentelemetry", "tracing", "observability"],
        "pattern": "Adopt standards-based tooling for cross-cutting concerns",
        "outcome": "success",
        "result": "All 8 services instrumented. End-to-end traces reveal bottlenecks we couldn't see before.",
        "days_ago": 9,
    },
    {
        "decision": "Use API gateway for external partner integrations",
        "category": "integration",
        "stakes": "medium",
        "confidence": 0.88,
        "context": "Partners connecting directly to internal services. No central auth, rate limiting, or versioning.",
        "reasons": [
            {"type": "analysis", "text": "Gateway centralizes auth, rate limiting, and API versioning"},
            {"type": "constraint", "text": "Must not add >5ms latency to existing partner calls"},
        ],
        "tags": ["api-gateway", "partners", "security", "integration"],
        "pattern": "Add an API gateway before the third external integration",
        "outcome": "success",
        "result": "Gateway handling 500K partner requests/day. Auth centralized, <2ms added latency.",
        "days_ago": 7,
    },

    # Some unreviewed recent decisions to show pending work
    {
        "decision": "Evaluate LLM-based code review as complement to human review",
        "category": "process",
        "stakes": "medium",
        "confidence": 0.70,
        "context": "Review backlog growing. LLM reviewers could catch obvious issues before human review.",
        "reasons": [
            {"type": "analysis", "text": "LLM catches formatting, naming, and simple logic issues reliably"},
            {"type": "constraint", "text": "Cannot replace human review for architecture and security decisions"},
        ],
        "tags": ["llm", "code-review", "automation", "process"],
        "pattern": "Use AI for triage, not for judgment",
        "outcome": None,
        "days_ago": 3,
    },
    {
        "decision": "Implement feature flag cleanup automation",
        "category": "tooling",
        "stakes": "low",
        "confidence": 0.85,
        "context": "47 stale feature flags in codebase. Manual cleanup is tedious.",
        "reasons": [
            {"type": "empirical", "text": "Stale flags increase code complexity and confuse new developers"},
            {"type": "analysis", "text": "Automated detection + PR creation removes human bottleneck"},
        ],
        "tags": ["feature-flags", "automation", "code-health"],
        "pattern": "Automate cleanup of temporary artifacts that accumulate",
        "outcome": None,
        "days_ago": 2,
    },
    {
        "decision": "Add structured logging with correlation IDs across all services",
        "category": "architecture",
        "stakes": "medium",
        "confidence": 0.90,
        "context": "Debugging cross-service issues requires manual log correlation. Need request tracing.",
        "reasons": [
            {"type": "pattern", "text": "Correlation IDs are essential for distributed system observability"},
            {"type": "analysis", "text": "Structured logging enables machine parsing and alerting"},
        ],
        "tags": ["logging", "observability", "distributed-systems"],
        "pattern": "Add correlation before debugging becomes the bottleneck",
        "outcome": None,
        "days_ago": 1,
    },
]

# Graph edges between related decisions
GRAPH_EDGES = [
    # Infrastructure cluster
    ("redis-caching", "circuit-breaker", "relates_to"),
    ("sqlite-edge", "redis-caching", "relates_to"),
    # Observability cluster
    ("datadog-to-grafana", "opentelemetry", "relates_to"),
    ("structured-logging", "opentelemetry", "relates_to"),
    ("websocket-dashboard", "datadog-to-grafana", "relates_to"),
    # Security cluster
    ("rate-limiting", "csp-headers", "relates_to"),
    ("vault-secrets", "jwt-rotation", "relates_to"),
    ("rate-limiting", "api-gateway", "relates_to"),
    # Process cluster
    ("pr-reviews", "adr-reviews", "relates_to"),
    ("pr-reviews", "llm-code-review", "relates_to"),
    ("trunk-based", "pr-reviews", "depends_on"),
    ("blameless-postmortems", "on-call-rotation", "relates_to"),
    # Build vs buy
    ("custom-etl", "datadog-to-grafana", "relates_to"),
    ("github-actions", "renovate", "relates_to"),
]


def load_store_schema() -> str:
    """Return SCHEMA_SQL from the server's SQLite store module.

    A hand-copied schema drifted (no created_at column), and the server then
    failed to open the seeded database. Reading the literal keeps the two equal.
    """
    tree = ast.parse(STORE_MODULE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SCHEMA_SQL" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(f"SCHEMA_SQL not found in {STORE_MODULE}")


def create_database(db_path: str) -> None:
    """Create a SQLite database with seed data, plus graph_edges.jsonl beside it."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(load_store_schema())

    now = datetime.now(timezone.utc)
    decision_ids = {}  # slug -> id for graph edges

    for d in DECISIONS:
        # Same format as decision_service.generate_decision_id; the graph keys
        # nodes by these 8 characters.
        decision_id = uuid.uuid4().hex[:8]
        created_at = (now - timedelta(days=d["days_ago"])).isoformat()

        # Create a slug for graph edge mapping
        for key, edge_slug in [
            ("Redis", "redis-caching"),
            ("gRPC", "grpc-migration"),
            ("event sourcing", "event-sourcing"),
            ("SQLite", "sqlite-edge"),
            ("circuit breaker", "circuit-breaker"),
            ("WebSocket", "websocket-dashboard"),
            ("trunk-based", "trunk-based"),
            ("Jenkins", "github-actions"),
            ("Ruff", "ruff-linting"),
            ("uv for Python", "uv-adoption"),
            ("Terraform", "terraform"),
            ("Datadog", "datadog-to-grafana"),
            ("PR reviews", "pr-reviews"),
            ("ADR", "adr-reviews"),
            ("blameless", "blameless-postmortems"),
            ("on-call", "on-call-rotation"),
            ("Renovate", "renovate"),
            ("rate limiting", "rate-limiting"),
            ("Vault", "vault-secrets"),
            ("Content Security Policy", "csp-headers"),
            ("JWT", "jwt-rotation"),
            ("Stripe", "stripe-webhooks"),
            ("ETL", "custom-etl"),
            ("OpenTelemetry", "opentelemetry"),
            ("API gateway", "api-gateway"),
            ("LLM-based code", "llm-code-review"),
            ("feature flag cleanup", "feature-flag-cleanup"),
            ("structured logging", "structured-logging"),
        ]:
            if key.lower() in d["decision"].lower():
                decision_ids[edge_slug] = decision_id
                break

        reviewed = d.get("outcome") is not None

        conn.execute(
            """INSERT INTO decisions
               (id, decision, confidence, category, stakes, status, context,
                recorded_by, pattern, outcome, outcome_result, reviewed_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                d["decision"],
                d["confidence"],
                d["category"],
                d["stakes"],
                "reviewed" if reviewed else "pending",
                d.get("context"),
                "demo-agent",
                d.get("pattern"),
                d.get("outcome"),
                d.get("result"),
                created_at if reviewed else None,
                created_at,
                created_at,
            ),
        )

        for tag in d.get("tags", []):
            conn.execute(
                "INSERT OR IGNORE INTO decision_tags (decision_id, tag) VALUES (?, ?)",
                (decision_id, tag),
            )

        for reason in d.get("reasons", []):
            conn.execute(
                "INSERT INTO decision_reasons (decision_id, type, text) VALUES (?, ?, ?)",
                (decision_id, reason["type"], reason["text"]),
            )

    conn.commit()
    conn.close()

    # Generate graph edges in the server's JSONL format (graphdb/persistence.py)
    graph_edges = []
    for source_slug, target_slug, edge_type in GRAPH_EDGES:
        source_id = decision_ids.get(source_slug)
        target_id = decision_ids.get(target_slug)
        if source_id and target_id:
            graph_edges.append({
                "source_id": source_id,
                "target_id": target_id,
                "edge_type": edge_type,
                "weight": 1.0,
                "created_at": now.isoformat(),
                "created_by": "seed-data",
                "context": None,
            })

    # Write graph edges
    graph_path = Path(db_path).parent / "graph_edges.jsonl"
    with open(graph_path, "w", encoding="utf-8") as f:
        for edge in graph_edges:
            f.write(json.dumps(edge) + "\n")

    # Print summary
    print(f"Created {db_path}")
    print(f"  Decisions: {len(DECISIONS)}")
    print(f"  Reviewed: {sum(1 for d in DECISIONS if d.get('outcome'))}")
    print(f"  Graph edges: {len(graph_edges)}")
    print(f"  Categories: {', '.join(sorted(set(d['category'] for d in DECISIONS)))}")


if __name__ == "__main__":
    import sys
    # Script-relative path so it works from any directory
    default_path = Path(__file__).parent / "seed-data" / "decisions.db"
    output = sys.argv[1] if len(sys.argv) > 1 else str(default_path)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    # Remove existing to start fresh
    Path(output).unlink(missing_ok=True)
    create_database(output)
