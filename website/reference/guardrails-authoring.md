# Guardrails Authoring Guide

This guide explains how to write custom guardrail rules to enforce decision-making policies in your organization.

---

## What Are Guardrails?

Guardrails are YAML-defined rules that are evaluated against a decision context **before** the decision is committed. They can:

- **Block** — Prevent the decision entirely
- **Warn** — Allow but flag the risk
- **Log** — Record for audit without blocking

---

## Guardrail Structure

```yaml
- id: unique-guardrail-id          # Required: unique identifier
  description: Human-readable text  # Required: what this rule does
  
  # Conditions — when does this rule apply?
  condition_<field>: <value>        # Field-based condition matching
  
  # Requirements — what must be true?
  requires_<field>: true            # Boolean requirement check
  
  # Scope — which projects does this apply to?
  scope: ProjectName                # Optional: restrict to specific projects
  
  # Action — what to do on violation
  action: block                     # block | warn | log
  
  # Message — what to tell the agent
  message: "Explanation of the violation"
```

---

## Condition Types

### Field Conditions

Match a specific field in the decision context:

```yaml
# Exact match
condition_category: architecture

# Comparison operators (prefix in string)
condition_confidence: "< 0.5"
condition_confidence: "> 0.8"
condition_confidence: "<= 0.3"
condition_confidence: ">= 0.9"
condition_confidence: "!= 0.5"

# Boolean match
condition_affects_production: true

# String match
condition_stakes: high
condition_decision_type: strategy_change
```

### Operator Reference

| Operator | Syntax | Example |
|----------|--------|---------|
| Equals | `field: value` | `condition_category: trading` |
| Not equals | `field: "!= value"` | `condition_stakes: "!= low"` |
| Less than | `field: "< value"` | `condition_confidence: "< 0.5"` |
| Greater than | `field: "> value"` | `condition_position_size_pct: "> 10"` |
| Less/equal | `field: "<= value"` | `condition_confidence: "<= 0.3"` |
| Greater/equal | `field: ">= value"` | `condition_confidence: ">= 0.9"` |

### CEL Expressions (F054) {#cel-expressions}

The `condition` key accepts a [CEL](https://github.com/google/cel-spec) expression. This is the
preferred format: it reaches any field, supports full boolean logic, and — unlike the flat
`condition_*` keys — can read the caller-supplied `context` dict.

```yaml
# As a plain string
- id: require-architecture-review
  description: Architecture decisions require an architecture review
  condition: "action.category == 'architecture' && !action.context.architecture_review"
  action: block
  message: "Architecture decisions require architecture_review: true"

# Or as an explicit dict
- id: no-high-stakes-low-confidence
  description: High-stakes decisions require minimum confidence
  condition:
    cel: "action.stakes == 'high' && action.confidence < 0.5"
  action: block
  message: "High-stakes decisions require ≥50% confidence"
```

#### Activation Fields

| Field | Type | Notes |
|-------|------|-------|
| `action.description` | string | Defaults to `""` |
| `action.stakes` | string | Defaults to `"medium"` |
| `action.confidence` | double | Coerced to `0.0` when absent or null |
| `action.category` | string | Defaults to `""` |
| `action.tags` | list | Defaults to `[]` |
| `action.reason_count` | int | Defaults to `0` |
| `action.pattern` | string | Defaults to `""` |
| `action.quality_score` | double | Defaults to `0.0` |
| `action.has_tags` | bool | Derived from `tags` |
| `action.has_pattern` | bool | Derived from `pattern` |
| `action.phase`, `action.scope`, `action.project` | string | Present when the caller supplies them |
| `action.deliberation_inputs_count`, `action.has_deliberation`, `action.has_reasoning` | — | Deliberation state |
| `action.context.*` | any | **Everything else the caller passed.** This is the escape hatch — no schema change needed for a new field |

Defaults are applied before evaluation, so comparisons never blow up on a missing field.

#### More Examples

```yaml
# Require 2+ reasons for high-stakes decisions
condition: "action.stakes == 'high' && action.reason_count < 2"

# Untagged, unpatterned, and not low-stakes
condition: "!action.has_tags && !action.has_pattern && action.stakes != 'low'"

# Category-specific confidence floor
condition: "action.category == 'security' && action.confidence < 0.7"

# Substring match on the description plus a caller-supplied flag
condition: "action.description.contains('trading') && !action.context.backtest_completed"

# Membership test
condition: "size(action.tags) == 0 && action.stakes in ['high', 'critical']"
```

#### Legacy Auto-Conversion

A `condition:` dict without a `cel` key is converted to CEL automatically at load time. Existing
guardrail files keep working with no migration:

| Legacy key | Generated CEL |
|------------|---------------|
| `stakes: high` | `action.stakes == 'high'` |
| `confidence_lt: 0.5` | `action.confidence < 0.5` |
| `reason_count_lt: 1` | `action.reason_count < 1` |
| `quality_lt: 0.5` | `action.quality_score < 0.5` |
| `category: tooling` | `action.category == 'tooling'` |
| `quality_score: "< 0.5"` | `action.quality_score < 0.5` |
| `deploy_env: production` | `has(action.context.deploy_env) && action.context.deploy_env == 'production'` |

Suffixes `_lt`, `_gt`, `_lte`, and `_gte` map to `<`, `>`, `<=`, and `>=`, as do string values
that start with a comparison (`"< 0.5"`). Keys that are not recognized activation fields resolve
to `action.context.<key>`, guarded with `has()` so a condition on a field the caller did not send
is simply unmet. Multiple keys are joined with `&&`.

::: warning A `condition:` key disables the flat format
When `condition:` is present, the guardrail is evaluated **only** through CEL — sibling
`condition_*` and `requires_*` keys are ignored. Use the nested `requires:` dict (which is always
parsed) or fold the requirement into the expression as `!action.context.<field>`.
:::

::: warning Fails closed
An expression that fails to compile, or raises at runtime, **applies**: a `block` rule blocks and
a `warn` rule warns, with the message "Guardrail <id> could not be evaluated, so it was applied".
Reading an `action.context.*` field the caller did not send is a runtime error, so guard optional
fields with `has()`:

```yaml
condition: "action.stakes == 'critical' && has(action.context.hour) && action.context.hour >= 22"
```

Leave the guard off when a missing field should count against the action, as in
`!action.context.backtest_completed` above. Check server logs for `CEL compile error` /
`CEL eval error` after editing. If `cel-python` is not installed, every CEL guardrail applies.
:::

CEL evaluation runs only in `cstp.checkGuardrails` (and the `pre_action` path that calls it). It is
not in the `queryDecisions` or `recordDecision` hot path. Programs are compiled once and cached per
expression string.

### V2 Conditions (Advanced)

For more complex scenarios, use the v2 structured condition format:

```yaml
conditions:
  - type: field
    field: stakes
    operator: "=="
    value: high

  - type: semantic
    query_field: description
    threshold: 0.85
    filter_outcome: failure
    filter_since_days: 30
    min_matches: 2

  - type: temporal
    field: category
    value: deployment
    window_hours: 24
    max_occurrences: 2

  - type: aggregate
    field: category
    value: trading
    metric: success_rate
    operator: "<"
    threshold: 0.5

  - type: compound
    operator: and   # or | or
    conditions:
      - type: field
        field: stakes
        operator: "=="
        value: critical
      - type: field
        field: confidence
        operator: "<"
        value: 0.7
```

---

## Requirements

Requirements are boolean checks — the named field must be `true` in the context:

```yaml
# Requires code review to be completed
requires_code_review: true

# Requires backtest to have run
requires_backtest_completed: true

# Requires risk assessment
requires_risk_assessed: true

# Requires human approval
requires_human_approval: true

# Custom requirements
requires_monitoring_configured: true
requires_rollback_plan: true
requires_ci_green: true
requires_audit_logged: true
```

If the corresponding field is missing or `false` in the context, the requirement fails.

---

## Scope

Restrict a guardrail to specific projects:

```yaml
# Single scope
scope: CryptoTrader

# The guardrail only applies when the context has:
# { "scope": "CryptoTrader" } or { "project": "CryptoTrader" }
```

---

## Actions

| Action | Behavior | Return |
|--------|----------|--------|
| `block` | Prevents the decision | `allowed: false` |
| `warn` | Allows but flags concern | `allowed: true` (with warnings) |
| `log` | Silently records evaluation | `allowed: true` |

---

## Examples

### Cornerstone Rules (Non-Negotiable)

```yaml
# guardrails/cornerstone.yaml

- id: no-production-without-review
  description: Production changes require code review
  condition_affects_production: true
  requires_code_review: true
  action: block
  message: Production changes require completed code review

- id: no-high-stakes-low-confidence
  description: High-stakes decisions need minimum confidence
  condition_stakes: high
  condition_confidence: "< 0.5"
  action: block
  message: High-stakes decisions require 50% confidence or more

- id: no-trading-strategy-without-backtest
  description: Trading strategy changes need backtesting
  scope: CryptoTrader
  condition_category: trading
  condition_decision_type: strategy_change
  requires_backtest_completed: true
  action: block
  message: Trading strategy changes require completed backtest
```

### Financial Template

```yaml
# guardrails/templates/financial.yaml

template:
  name: financial
  description: Guardrails for financial and trading decisions
  version: "1.0"

guardrails:
  - id: require-backtest
    description: Strategy changes require backtesting
    condition_category: trading
    condition_decision_type: strategy_change
    requires_backtest_completed: true
    action: block
    message: "Trading strategy changes require completed backtest"

  - id: require-risk-assessment
    description: Financial decisions need risk assessment
    condition_category: financial
    condition_stakes: high
    requires_risk_assessed: true
    action: block
    message: "High-stakes financial decisions require risk assessment"

  - id: limit-single-position
    description: Warn on large position sizes
    condition_category: trading
    condition_position_size_pct: "> 10"
    action: warn
    message: "Position size exceeds 10% of portfolio - review risk"

  - id: require-approval-large-amounts
    description: Large transactions need approval
    condition_amount_usd: "> 10000"
    requires_human_approval: true
    action: block
    message: "Transactions over $10k require human approval"

  - id: no-trading-during-volatility
    description: Pause new trades during high volatility
    condition_category: trading
    condition_market_volatility: high
    action: warn
    message: "High market volatility detected - consider pausing new positions"
```

### Production Safety Template

```yaml
# guardrails/templates/production-safety.yaml

template:
  name: production-safety
  description: Guardrails for production deployments
  version: "1.0"

guardrails:
  - id: require-code-review
    description: All production changes must be code reviewed
    condition_affects_production: true
    requires_code_review: true
    action: block
    message: "Production changes require completed code review"

  - id: require-tests-passing
    description: CI tests must pass before production deploy
    condition_affects_production: true
    requires_ci_green: true
    action: block
    message: "CI tests must pass before deploying to production"

  - id: require-rollback-plan
    description: Production deploys need rollback strategy
    condition_affects_production: true
    condition_change_type: deployment
    requires_rollback_plan: true
    action: warn
    message: "Consider documenting rollback plan before deploying"

  - id: no-friday-deploys
    description: Avoid production deploys on Fridays
    condition_affects_production: true
    condition_day_of_week: friday
    action: warn
    message: "Friday deploys are risky - consider waiting until Monday"
```

---

## Writing Your Own Guardrails

### Step 1: Create a YAML File

```bash
# Create in the guardrails directory
touch guardrails/my-project-rules.yaml
```

### Step 2: Define Rules

Think about:

1. **What decisions should be blocked?** → Use `action: block`
2. **What decisions should be flagged?** → Use `action: warn`
3. **What conditions trigger the rule?** → Use `condition_*` fields
4. **What requirements must be met?** → Use `requires_*` fields

### Step 3: Test Locally

```bash
# List guardrails to verify they load
python bin/cognition guardrails

# Test with a specific context
python bin/cognition check --category trading --stakes high --confidence 0.3
```

### Step 4: Deploy

Place your YAML file in a configured guardrail directory:

```bash
GUARDRAILS_PATHS=/app/guardrails:/app/my-custom-guardrails
```

---

## Audit Trail

Every guardrail evaluation is recorded in the audit trail:

**Output location:** `audit/YYYY-MM-DD-<decision_id>.json`

**Contents:**

```json
{
  "decision_id": "2026-02-07-decision-a1b2c3d4",
  "timestamp": "2026-02-07T12:00:00Z",
  "overall_allowed": false,
  "evaluations": [
    {
      "guardrail_id": "no-production-without-review",
      "matched": true,
      "passed": false,
      "action": "block",
      "message": "Production changes require completed code review"
    }
  ],
  "override": null
}
```

### Querying Audit Records

The `AuditLog` class provides methods for querying:

- `get_violations(since)` — All violations since a timestamp
- `get_statistics()` — Aggregate stats: total evaluations, block rate, most triggered rules
- `get_overrides()` — Decisions where violations were overridden

---

## Best Practices

1. **Start with cornerstone rules** — Block non-negotiable violations (safety, compliance)
2. **Use warnings for soft guidelines** — Don't block everything; let agents learn
3. **Scope to projects** — Use `scope:` to avoid overly broad rules
4. **Write clear messages** — Agents need to understand *why* they were blocked
5. **Review audit trails** — Monitor which rules trigger most often
6. **Version your templates** — Include `version:` for tracking changes
7. **Test before deploying** — Use the CLI to verify rules work as expected
