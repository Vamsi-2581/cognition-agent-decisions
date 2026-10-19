# F055: Decision Provenance & Control Evidence

**Status:** Shipped on `main`, unreleased (targets v0.16.0)
**Priority:** High
**Category:** Compliance & Auditability

## Problem

When an AI agent influences a regulated decision — approves a change, flags a transaction,
drafts an underwriting recommendation — nobody can hand a regulator a single artifact
proving *what* the agent reasoned, *who* reviewed it, and *which control framework* that
review satisfies. Evidence is scattered across PR comments, observability dashboards, and
CSTP decision logs built for engineers, not auditors.

Every audit becomes forensic reconstruction instead of a five-minute export.

## Solution

F055 makes CSTP able to prove *that* it acted and *which control that satisfies*.

CSTP already records *why* an agent decided something (see F007 `cstp.recordDecision`).
F055 adds:

1. An **observed evidence hash chain** that ingests third-party events (GitHub PR opens,
   reviews, approvals, merges) and makes them tamper-evident.
2. An **attested evidence layer** that links existing CSTP decisions to observed events.
3. A **YAML rules engine** that maps both evidence classes to SR 11-7 and NIST AI RMF controls.
4. A **bundle generator** that emits a JSON (and optionally PDF) artifact an auditor can
   receive in one API call.
5. A **chain verifier** that detects both internal tampering and tail truncation.

## The Two-Class Evidence Model

This is the intellectually load-bearing part of F055. All evidence falls into exactly one of
two classes and they must **never be blended**:

### Class OBSERVED

Third-party events from a system of record the subject does not control:
- GitHub PR opens, commits, code reviews, approvals, merges
- Ingested via `cstp.ingestEvidence`
- **Hash-chained and tamper-evident** — any modification to a stored record breaks the chain
- An auditor trusts observed evidence because the agent did not control the source

### Class ATTESTED

First-party CSTP records self-reported by the agent:
- Decision records (`cstp.recordDecision`) linked to observed events
- Linked via `cstp.linkEvidence`, stored as `cstp_decision_linked` events
- **Self-reported** — the agent wrote this; an auditor discounts it heavily
- Useful as corroboration, not as primary evidence

### Why the split matters

> "An auditor discounts self-attestation heavily. If you merge these into one number you
> *weaken* the strong half."

A single blended coverage percentage obscures whether controls are evidenced by tamper-proof
third-party records or by the agent's own claims. An 82% blended score where 70% is attested
is very different from an 82% score where 70% is observed.

### Enforcement

- Every evidence item in the schema carries an explicit `evidence_class` column
  (`"observed"` | `"attested"`). Not nullable. No default.
- Coverage is computed and reported **separately per class** via `observed.coverage_pct`
  and `attested.coverage_pct`. There is no blended `coverage_pct` anywhere in the API
  response, the JSON bundle, or the PDF.
- A control mapping satisfied *only* by attested evidence is flagged `attested_only: true`
  and rendered distinctly in the PDF (amber header, "ATTESTED-ONLY" status label).
- Tests enforce all three invariants (see `tests/test_f055_provenance_service.py`,
  class `TestPerClassCoverage` and `TestAttestedOnlyControls`).

## API

### `cstp.ingestEvidence`

Ingest observed events into the hash chain.

```json
{
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
          "approval_count": 0,
          "additions": 350,
          "deletions": 12,
          "changed_files": 5
        }
      },
      {
        "event_type": "pr_review_approved",
        "ts": "2026-07-01T14:00:00Z",
        "payload": { "pr_number": 42, "reviewer": "alice" }
      }
    ]
  }
}
```

Response:
```json
{
  "result": {
    "ingested": 2,
    "head_hash": "65b58d83a0b5be919...",
    "first_seq": 1
  }
}
```

All ingested events receive `evidence_class = "observed"`. This is not overridable.

### `cstp.linkEvidence`

Correlate an existing CSTP decision to observed events. Creates an attested evidence
record (`cstp_decision_linked`) in the provenance store.

```json
{
  "method": "cstp.linkEvidence",
  "params": {
    "decision_id": "dec-7e2f9a",
    "event_seqs": [1, 2, 3],
    "stakes": "high",
    "has_outcome": true
  }
}
```

Response:
```json
{
  "result": {
    "linked": 3,
    "decision_id": "dec-7e2f9a",
    "attested_seq": 4
  }
}
```

### `cstp.mapControls`

Run the YAML rules engine over all stored evidence. Returns per-class coverage —
**never a blended percentage**.

```json
{ "method": "cstp.mapControls", "params": {} }
```

Response:
```json
{
  "result": {
    "coverage": {
      "observed": {
        "total_events": 50,
        "mapped_events": 41,
        "unmapped_events": 9,
        "coverage_pct": 82.0
      },
      "attested": {
        "total_events": 3,
        "mapped_events": 3,
        "unmapped_events": 0,
        "coverage_pct": 100.0
      }
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
    "unmapped": [...],
    "attested_only_controls": ["MV-3"]
  }
}
```

### `cstp.exportEvidenceBundle`

Emit the full evidence bundle as JSON (and optionally PDF).

```json
{
  "method": "cstp.exportEvidenceBundle",
  "params": {
    "format": "json",
    "output_dir": "/var/bundles/2026-Q3"
  }
}
```

Response:
```json
{
  "result": {
    "json_path": "/var/bundles/2026-Q3/bundle_20260809T173649Z.json",
    "chain_head_hash": "65b58d83a0b5be919...",
    "chain_intact": true,
    "coverage": { "observed": {...}, "attested": {...} },
    "insufficient_evidence_count": 2,
    "attested_only_controls": ["MV-3"]
  }
}
```

The bundle persists `chain_head_hash` at generation time. Subsequent calls to
`cstp.verifyEvidenceChain` verify against that stored hash, enabling detection of
tail truncation (events deleted after the bundle was generated).

### `cstp.verifyEvidenceChain`

Verify hash chain integrity. Accepts `expected_head_hash` to detect tail truncation.

```json
{
  "method": "cstp.verifyEvidenceChain",
  "params": {
    "expected_head_hash": "65b58d83a0b5be919..."
  }
}
```

Response:
```json
{
  "result": {
    "intact": true,
    "broken_at_seq": null,
    "head_hash": "65b58d83a0b5be919...",
    "expected_hash": "65b58d83a0b5be919...",
    "tail_check": true
  }
}
```

If `expected_head_hash` is omitted, the hash from the most recent bundle generation is
used automatically. If no bundle has been generated, tail-truncation detection is inactive
and `tail_check` is `false`.

## Hash Chain

The observed evidence store uses a SHA-256 hash chain. **Chain format version 2.** The preimage
is the canonical JSON (sorted keys, no whitespace, UTF-8) of a six-field object:

```json
{"evidence_class":"…","event_type":"…","payload":{…},"prev_hash":"…","seq":1,"ts":"…"}
```

JSON-encoding every field makes the preimage injective: no two distinct
`(seq, ts, event_type, evidence_class, payload, prev_hash)` tuples produce the same byte
string, and embedded delimiters in string values cannot be used to forge a collision.

::: warning Breaking change
Format version 1 used a newline-delimited preimage
(`{seq}\n{ts}\n{event_type}\n{evidence_class}\n{canonical_json(payload)}\n{prev_hash}`),
which allowed collisions on embedded newlines. Any store written under version 1 will fail
`verify_chain` and must be re-ingested. F055 is unreleased, so no migration path is provided.
:::

Note that `evidence_class` is included in the preimage. Reclassifying an event from
`observed` to `attested` (or vice versa) breaks the chain at that record.

`source` is intentionally excluded — it identifies the ingest origin but does not affect
tamper-evidence.

Writes use `BEGIN IMMEDIATE` with an explicit `seq`, so concurrent writers are serialized and
cannot collide on a sequence number.

## Control Framework Mappings

Rules are defined in `a2a/cstp/provenance/mappings/`:

| File | Framework | Evidence class |
|------|-----------|----------------|
| `sr11-7.yaml` | SR 11-7 (Federal Reserve MRM) | observed |
| `nist-ai-rmf.yaml` | NIST AI RMF 1.0 | observed |
| `cstp-attested.yaml` | CSTP Attested | attested |

### Honest-mapping policy (from the PoC)

The mapping is hand-written YAML **on purpose** — an ML-inferred mapping is itself an audit
liability. Do not "improve" this with a model.

**P1 fixes preserved from PoC (commit `9643d21`):**
- `verify_chain()` accepts `expected_head_hash` so tail truncation is detectable
- MAP-1.1 is tagged `confidence: low` / `stretch: true` and renders as `[PARTIAL/CONTESTED]`
- MANAGE-1.1 only fires when `is_agent_authored: true` (human merges are a category error)

**P2 fix added in this port:**
- The JSON bundle persists `chain_head_hash` at generation time
- `cstp.verifyEvidenceChain` defaults to the stored head hash when no explicit hash is given
- This makes tail truncation detectable without the caller needing to remember the head hash

## Honesty

The original PoC produced 82.0% observed coverage with 2 genuine evidence gaps on PR #3
(agent-authored code merged by its own author with zero human approval). **That gap-finding
is the product.** Do not tune, stretch, or reclassify any mapping to make coverage numbers
look better.

The two-class split makes attested-only controls appear weaker — that is the correct and
desired outcome. An attested-only control should look weak to an auditor, because it is.

If a port surfaces a mapping that cannot be honestly justified, emit `INSUFFICIENT_EVIDENCE`
with a plain-English reason rather than stretching it.

## Storage

The provenance DB is a SQLite WAL file at `$PROVENANCE_DB` (default:
`~/.cstp/provenance.db`), separate from the decision store. Schema:

```sql
CREATE TABLE schema_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE events (
    seq            INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    source         TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    evidence_class TEXT NOT NULL CHECK(evidence_class IN ('observed', 'attested')),
    payload_json   TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    hash           TEXT NOT NULL
);

CREATE TABLE bundle_metadata (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    head_seq     INTEGER NOT NULL DEFAULT 0,
    head_hash    TEXT NOT NULL,
    format       TEXT NOT NULL
);
```

`seq` is assigned explicitly rather than via `AUTOINCREMENT` so it can be allocated inside the
`BEGIN IMMEDIATE` transaction that computes the chain hash.

The `db_path` RPC parameter described in earlier drafts was removed: the service always resolves
to the server-configured `PROVENANCE_DB`, so an authenticated caller cannot redirect reads or
writes to an arbitrary file. The `output_dir` and `mappings_dir` parameters were removed for the
same reason: bundles go to `PROVENANCE_BUNDLE_DIR`, and rules always load from the packaged
mappings, so a caller cannot write files elsewhere or swap in an empty rule set.

## Out of Scope

- Dashboard / web UI for provenance
- EU AI Act control mapping (stubbed — pending legal review; do not add ML inference)
- Multi-tenant billing or SOC 2
- Any ML-inferred control mapping
- Migrating the PoC repo

## Integration Points

- F007 (`cstp.recordDecision`): Decisions recorded here can be linked to observed events
  via `cstp.linkEvidence`, creating attested evidence entries
- F045 (Graph): Decision graph edges can be exported as observed events to trace reasoning chains
- F047 (Session Context): Session context can include provenance coverage summary
- F050 (Structured Storage): Provenance DB uses the same SQLite WAL pattern

## Phases

1. **P1 (this PR):** All five RPC methods, two-class evidence model, SR 11-7 + NIST AI RMF
   + CSTP-attested YAML rules, JSON bundle, chain verification with tail-truncation detection
2. **P2:** PDF bundle improvements — executive sign-off section, regulatory text citations,
   per-model evidence grouping
3. **P3:** EU AI Act Annex III mapping (requires legal review of each rule)
4. **P4:** Streaming ingest webhook for real-time GitHub event ingestion
