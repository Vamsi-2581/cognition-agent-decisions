# F054: CEL Expression Guardrails

**Status:** Shipped (on `main`, unreleased — targets v0.16.0)
**Priority:** P1 — Fixes MCP guardrail context gap, enables flexible rules
**Dependencies:** None (replaces existing guardrail evaluator)
**Origin:** Acteon Action Gateway (CEL for rule evaluation), Nous 004.1

## Summary

Replace the JSONB condition matching in `checkGuardrails` with Google's Common Expression Language (CEL). This makes guardrails composable, readable, and able to access any field in the action context — including the `context` dict that MCP clients currently cannot pass.

## Problem

### Current: Rigid JSONB Matching
```json
{"stakes": "high", "confidence_lt": 0.5}
```
- Only 4 hardcoded condition keys recognized
- Unknown keys silently dropped by `ActionContext.from_dict()`
- MCP `CheckActionInput` schema has no `context` field
- Category-specific guardrails (e.g., `require-architecture-review`) always block through MCP because there's no way to pass `architecture_review=true`

### Proposed: CEL Expressions
```cel
action.stakes == 'high' && action.confidence < 0.5
```
```cel
action.category == 'architecture' && !action.context.architecture_review
```
```cel
size(action.tags) == 0 && action.stakes in ['high', 'critical']
```
- Any field accessible via dot notation
- Custom fields via `action.context.*`
- Boolean logic, comparisons, list/map functions
- Sandboxed — no side effects, no I/O

## Changes

### 1. Guardrail Storage

Guardrail `condition` column (JSONB) supports three formats:

| Format | Example | Notes |
|--------|---------|-------|
| CEL string | `"action.stakes == 'high'"` | Preferred |
| Dict with `cel` key | `{"cel": "action.stakes == 'high'"}` | Alternative |
| Legacy JSONB | `{"stakes": "high", "confidence_lt": 0.5}` | Auto-converted to CEL |

### 2. Evaluation Engine

New `CelGuardrailEvaluator` class:
- Compiles CEL expressions once, caches programs
- Builds activation context from action parameters
- Evaluates all active guardrails, returns blocked/warned
- Fails open on eval errors (log + skip, don't block) — reversed, see D1

### 3. CEL Activation Context

```python
{
    "action": {
        "description": "...",
        "stakes": "high",
        "confidence": 0.85,
        "category": "architecture",
        "tags": ["deployment", "infrastructure"],
        "reason_count": 2,
        "pattern": "...",
        "quality_score": 0.8,
        "has_pattern": true,
        "has_tags": true,
        "context": {
            "architecture_review": true,
            "code_review": true,
            # ... any custom key-value pairs
        }
    }
}
```

### 4. MCP Schema Update

Add `context` field to `CheckActionInput`:
```python
class CheckActionInput(BaseModel):
    description: str
    stakes: str = "medium"
    confidence: float = 0.8
    category: str | None = None
    context: dict | None = None  # NEW — arbitrary key-value pairs for CEL
```

Update `_build_guardrails_params` in `mcp_server.py` to forward `context`.

### 5. Migration of Existing Guardrails

Auto-convert legacy JSONB to CEL at evaluation time:

| JSONB Key | CEL Expression |
|-----------|---------------|
| `"stakes": "high"` | `action.stakes == 'high'` |
| `"confidence_lt": 0.5` | `action.confidence < 0.5` |
| `"reason_count_lt": 1` | `action.reason_count < 1` |
| `"quality_lt": 0.5` | `action.quality_score < 0.5` |
| `"category": "tooling"` | `action.category == 'tooling'` |

Existing guardrails continue to work without manual migration.

## Example Guardrails (CEL)

### Current guardrails rewritten
```cel
# no-high-stakes-low-confidence
action.stakes == 'high' && action.confidence < 0.5

# no-trading-strategy-without-backtest
action.category == 'tooling' && action.description.contains('trading') && !action.context.backtest_completed

# require-code-review-tooling
action.category == 'tooling' && !action.context.code_review

# require-architecture-review
action.category == 'architecture' && !action.context.architecture_review

# low-quality-recording
action.quality_score < 0.5

# require-deliberation (check for reasoning)
action.reason_count < 1 && action.stakes in ['medium', 'high', 'critical']
```

### New guardrails enabled by CEL
```cel
# Block high-stakes decisions at night (context.hour set by caller)
action.stakes == 'critical' && has(action.context.hour) && action.context.hour >= 22

# Require 2+ reasons for high-stakes
action.stakes == 'high' && action.reason_count < 2

# Block decisions without tags AND pattern
!action.has_tags && !action.has_pattern && action.stakes != 'low'

# Category-specific confidence floors
action.category == 'security' && action.confidence < 0.7
```

## Dependency

```
cel-python >= 0.4, < 1.0
```

Pure Python, maintained by Cloud Custodian (Google-backed). ~10KB, minimal transitive deps.

## Files Changed

| File | Change |
|------|--------|
| `a2a/cstp/guardrails_service.py` | New `CelGuardrailEvaluator`, replace `_matches()` |
| `a2a/cstp/models.py` | Add `context` field to `ActionContext` |
| `a2a/mcp_schemas.py` | Add `context` field to `CheckActionInput` |
| `a2a/mcp_server.py` | Forward `context` in `_build_guardrails_params` |
| `pyproject.toml` | Add `cel-python` dependency |
| `tests/test_f054_cel_guardrails.py` | New CEL tests + verify legacy compat |

## As Shipped

Two details differ from the plan above and are worth knowing when authoring rules:

- **`condition:` disables the flat format.** When a `condition` key is present — string, `{"cel": …}`,
  or legacy dict — the guardrail is evaluated only through CEL, and sibling `condition_*` /
  `requires_*` keys are ignored. The nested `requires:` dict is still parsed.
- **`cel-python` is a core dependency, not an extra.** If it is missing, the import fails softly and
  every CEL guardrail is skipped rather than erroring at startup.

See the [guardrails authoring guide](/reference/guardrails-authoring#cel-expressions) for the full
activation field reference.

## Backward Compatibility

- **Full backward compatibility** — legacy JSONB conditions auto-convert to CEL
- **No database migration needed** — existing condition values work as-is
- **MCP clients without `context`** — work exactly as before (context defaults to empty dict)
- **Dashboard** — guardrail display unchanged (condition shown as-is)

## Testing

| Test | What |
|------|------|
| Legacy JSONB still works | Auto-conversion produces correct CEL |
| CEL string condition | Direct expression evaluation |
| CEL dict condition | `{"cel": "..."}` format |
| Context access via CEL | `action.context.custom_field` works |
| MCP context forwarding | `CheckActionInput.context` reaches evaluator |
| Invalid CEL fails open | Bad syntax → no block, warning logged |
| Program caching | Same expression compiled once |
| Complex expressions | AND/OR/NOT/in/contains/size |
| Null handling | Missing fields don't crash |

## Design Decisions

### D1: Fail open
Invalid expressions don't block. Better to miss a guardrail than brick all decisions. Errors logged for admin to fix.

**Reversed.** Failing open let a broken block rule allow everything, and hid that three shipped
rules never evaluated: the record-phase fields were missing outside the record step, and
`quality_score: "< 0.5"` became a string equality. Errors now apply the rule. The activation
defaults every record-phase field, and dict conditions guard context fields with `has()`, so a
well-formed rule does not error on input the caller left out.

### D2: `action` namespace (not `decision`)
CE uses "action" terminology in guardrails. Nous uses "decision". Each project uses its own convention. CE expressions use `action.*`, Nous uses `decision.*`.

### D3: No CEL in hot path
CEL evaluation is only in `checkGuardrails`, not in `queryDecisions` or `recordDecision`. The hot path stays fast.

### D4: Context dict is the escape hatch
Instead of adding fields to `ActionContext` for every new guardrail need, `context` is an open map. CEL makes it usable without schema changes.

## Impact

- **Fixes MCP guardrail gap** — MCP clients can now pass `context` dict
- **Enables user-defined guardrails** — no code changes needed for new conditions
- **Dashboard potential** — CEL expressions could be edited in the dashboard UI
- **Shared pattern with Nous** — both projects use CEL for guardrails
