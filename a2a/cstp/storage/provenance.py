"""
Append-only event store with SHA-256 hash chain for F055 Decision Provenance.

Every event carries an evidence_class (observed | attested) which is included in
the hash preimage, making it tamper-evident. The two classes must never be blended:

  observed  — third-party events from a system the subject does not control
               (GitHub PR opens, commits, reviews, approvals, merges)
  attested  — first-party CSTP records correlated via cstp.linkEvidence

Hash preimage (UTF-8, canonical JSON of an ordered dict):
    {"evidence_class":..., "event_type":..., "payload":..., "prev_hash":..., "seq":..., "ts":...}

All six fields are JSON-encoded, so any embedded delimiters in string values are
escaped — the preimage is injective and cannot be forged by swapping field values.

Chain format version: 2 (format version 1 used a newline-delimited string preimage).

Note: `source` is intentionally excluded from the hash preimage. It identifies the
ingest origin but does not affect tamper-evidence.

BREAKING: version 2 changes the hash preimage. Any store written with version 1
will fail verify_chain. F055 is unreleased, so this is acceptable.
"""

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Optional

DEFAULT_DB_PATH = os.environ.get("PROVENANCE_DB", "provenance.db")


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit or roll back like ``with conn:``, then close it.

    ``with sqlite3.connect(...) as conn`` only manages the transaction: the
    connection stays open until garbage collection, holding a file handle (and,
    on Windows, a lock on the evidence DB) for an unbounded time.
    """
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


CHAIN_FORMAT_VERSION = 2

EVIDENCE_CLASS_OBSERVED = "observed"
EVIDENCE_CLASS_ATTESTED = "attested"
_VALID_CLASSES = frozenset({EVIDENCE_CLASS_OBSERVED, EVIDENCE_CLASS_ATTESTED})

CREATE_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    source         TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    evidence_class TEXT NOT NULL CHECK(evidence_class IN ('observed', 'attested')),
    payload_json   TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    hash           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bundle_metadata (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    head_seq     INTEGER NOT NULL DEFAULT 0,
    head_hash    TEXT NOT NULL,
    format       TEXT NOT NULL
);
"""


def canonical_json(obj: dict) -> str:
    """Canonical JSON: sorted keys, no whitespace, UTF-8-safe."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(
    seq: int,
    ts: str,
    event_type: str,
    evidence_class: str,
    payload: dict,
    prev_hash: str,
) -> str:
    """Compute SHA-256 hash for an event record.

    Preimage: canonical JSON of the six-field ordered dict. JSON-encoding each
    field makes the preimage injective — no two distinct (seq, ts, event_type,
    evidence_class, payload, prev_hash) tuples produce the same byte string.
    """
    preimage = canonical_json(
        {
            "seq": seq,
            "ts": ts,
            "event_type": event_type,
            "evidence_class": evidence_class,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create tables if they do not exist. Safe to call multiple times."""
    with _connect(db_path) as conn:
        conn.executescript(CREATE_SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO schema_metadata (key, value) VALUES (?, ?)",
            ("chain_format_version", str(CHAIN_FORMAT_VERSION)),
        )
        conn.commit()


def append_event(
    source: str,
    event_type: str,
    evidence_class: str,
    payload: dict,
    ts: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[int, str]:
    """Append a single event to the chain. Returns (seq, hash).

    Uses BEGIN IMMEDIATE to prevent concurrent-append races: only one writer
    can read the head and insert the next record at a time. The seq is inserted
    explicitly (not via AUTOINCREMENT) so the hash preimage matches the stored row.
    """
    if evidence_class not in _VALID_CLASSES:
        raise ValueError(
            f"evidence_class must be 'observed' or 'attested', got: {evidence_class!r}"
        )
    if ts is None:
        ts = datetime.now(UTC).isoformat()

    conn = sqlite3.connect(db_path, timeout=30)
    conn.isolation_level = None  # manual transaction control
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["hash"] if row else ""
        next_seq = (row["seq"] + 1) if row else 1

        h = compute_hash(next_seq, ts, event_type, evidence_class, payload, prev_hash)

        conn.execute(
            "INSERT INTO events "
            "(seq, ts, source, event_type, evidence_class, payload_json, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (next_seq, ts, source, event_type, evidence_class, canonical_json(payload), prev_hash, h),
        )
        conn.execute("COMMIT")
        return next_seq, h
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def append_events_batch(
    events: list[tuple[str, str, str, dict, str | None]],
    db_path: str = DEFAULT_DB_PATH,
) -> list[tuple[int, str]]:
    """Atomically append multiple events. All succeed or none are committed.

    events — list of (source, event_type, evidence_class, payload, ts) tuples.
    Returns list of (seq, hash) pairs in insertion order.
    """
    for _, _, evidence_class, _, _ in events:
        if evidence_class not in _VALID_CLASSES:
            raise ValueError(
                f"evidence_class must be 'observed' or 'attested', got: {evidence_class!r}"
            )

    now = datetime.now(UTC).isoformat()
    results: list[tuple[int, str]] = []

    conn = sqlite3.connect(db_path, timeout=30)
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["hash"] if row else ""
        next_seq = (row["seq"] + 1) if row else 1

        for source, event_type, evidence_class, payload, ts in events:
            if ts is None:
                ts = now
            h = compute_hash(next_seq, ts, event_type, evidence_class, payload, prev_hash)
            conn.execute(
                "INSERT INTO events "
                "(seq, ts, source, event_type, evidence_class, payload_json, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    next_seq, ts, source, event_type, evidence_class,
                    canonical_json(payload), prev_hash, h,
                ),
            )
            results.append((next_seq, h))
            prev_hash = h
            next_seq += 1

        conn.execute("COMMIT")
        return results
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_events(
    db_path: str = DEFAULT_DB_PATH,
    evidence_class: str | None = None,
) -> list[dict]:
    """Return events in seq order as dicts. Optionally filter by evidence_class."""
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if evidence_class is not None:
            rows = conn.execute(
                "SELECT seq, ts, source, event_type, evidence_class, payload_json, prev_hash, hash "
                "FROM events WHERE evidence_class = ? ORDER BY seq",
                (evidence_class,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT seq, ts, source, event_type, evidence_class, payload_json, prev_hash, hash "
                "FROM events ORDER BY seq"
            ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        d["payload"] = json.loads(d["payload_json"])
        result.append(d)
    return result


def get_head(db_path: str = DEFAULT_DB_PATH) -> Optional[tuple[int, str]]:
    """Return (seq, hash) of the most recent event, or None if the store is empty."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    return (row[0], row[1]) if row else None


def get_head_hash(db_path: str = DEFAULT_DB_PATH) -> Optional[str]:
    """Return the hash of the most recent event, or None if the store is empty."""
    head = get_head(db_path)
    return head[1] if head else None


def get_seq_for_hash(event_hash: str, db_path: str = DEFAULT_DB_PATH) -> Optional[int]:
    """Return the seq of the event with the given hash, or None if not found."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT seq FROM events WHERE hash = ?", (event_hash,)).fetchone()
    return row[0] if row else None


def validate_observed_seqs(event_seqs: list[int], db_path: str = DEFAULT_DB_PATH) -> None:
    """Validate that all seqs exist, have evidence_class='observed', and have no duplicates.

    Raises ValueError if any check fails. Empty list is valid (no-op).
    """
    if not event_seqs:
        return

    seen: set[int] = set()
    for seq in event_seqs:
        if seq in seen:
            raise ValueError(
                f"event_seqs contains duplicate sequence number: {seq}"
            )
        seen.add(seq)

    placeholders = ",".join("?" * len(event_seqs))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT seq, evidence_class FROM events WHERE seq IN ({placeholders})",
            event_seqs,
        ).fetchall()

    found: dict[int, str] = {row[0]: row[1] for row in rows}
    missing = set(event_seqs) - set(found)
    if missing:
        raise ValueError(
            f"event_seqs references nonexistent sequences: {sorted(missing)}"
        )

    non_observed = [seq for seq, ec in found.items() if ec != EVIDENCE_CLASS_OBSERVED]
    if non_observed:
        raise ValueError(
            f"event_seqs must only reference observed events; "
            f"sequences with wrong evidence_class: {sorted(non_observed)}"
        )


def verify_chain(
    db_path: str = DEFAULT_DB_PATH,
    expected_head_hash: Optional[str] = None,
) -> Optional[int]:
    """Walk the entire chain and verify each event's hash.

    Returns the seq of the first broken record, or None if the chain is intact.
    Returns 0 if the chain is internally intact but its head hash does not match
    expected_head_hash — indicating tail truncation (deleted events at the end).
    Callers that omit expected_head_hash see behaviour identical to pre-P1 code.
    """
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT seq, ts, event_type, evidence_class, payload_json, prev_hash, hash "
            "FROM events ORDER BY seq"
        ).fetchall()

    prev_hash = ""
    actual_head_hash = None
    for row in rows:
        payload = json.loads(row["payload_json"])
        expected = compute_hash(
            row["seq"], row["ts"], row["event_type"],
            row["evidence_class"], payload, prev_hash,
        )
        if expected != row["hash"]:
            return row["seq"]
        if row["prev_hash"] != prev_hash:
            return row["seq"]
        prev_hash = row["hash"]
        actual_head_hash = row["hash"]

    if expected_head_hash is not None and actual_head_hash != expected_head_hash:
        return 0  # Tail truncated: chain internally valid but head doesn't match

    return None


def verify_at_checkpoint(
    db_path: str,
    checkpoint_seq: int,
    checkpoint_hash: str,
) -> Optional[int]:
    """Verify all events up to and including checkpoint_seq.

    Allows the chain to extend beyond checkpoint_seq — valid growth after a
    bundle export does not trigger a false positive.

    Returns:
        None  — all hashes valid and the hash at checkpoint_seq matches checkpoint_hash
        0     — checkpoint_seq is absent from the chain (tail truncated past or at it)
        seq   — hash or prev_hash mismatch at seq (record tampered)
    """
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT seq, ts, event_type, evidence_class, payload_json, prev_hash, hash "
            "FROM events WHERE seq <= ? ORDER BY seq",
            (checkpoint_seq,),
        ).fetchall()

    found_seqs = {row["seq"] for row in rows}
    if checkpoint_seq not in found_seqs:
        return 0  # checkpoint record was deleted

    prev_hash = ""
    for row in rows:
        payload = json.loads(row["payload_json"])
        expected = compute_hash(
            row["seq"], row["ts"], row["event_type"],
            row["evidence_class"], payload, prev_hash,
        )
        if expected != row["hash"]:
            return row["seq"]
        if row["prev_hash"] != prev_hash:
            return row["seq"]
        if row["seq"] == checkpoint_seq and row["hash"] != checkpoint_hash:
            return 0  # hash at checkpoint doesn't match stored checkpoint
        prev_hash = row["hash"]

    return None


def event_count(db_path: str = DEFAULT_DB_PATH) -> int:
    """Return total number of events in the store."""
    with _connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def store_bundle_checkpoint(
    head_seq: int,
    head_hash: str,
    fmt: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Persist the trusted checkpoint (seq + hash) at bundle generation time."""
    generated_at = datetime.now(UTC).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO bundle_metadata (generated_at, head_seq, head_hash, format) "
            "VALUES (?, ?, ?, ?)",
            (generated_at, head_seq, head_hash, fmt),
        )
        conn.commit()


def get_last_bundle_checkpoint(
    db_path: str = DEFAULT_DB_PATH,
) -> Optional[tuple[int, str]]:
    """Return (head_seq, head_hash) of the most recent trusted bundle checkpoint, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT head_seq, head_hash FROM bundle_metadata ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    seq, h = row[0], row[1]
    if seq == 0:
        # Legacy record without seq — treat as no trusted checkpoint
        return None
    return (seq, h)


def store_bundle_head_hash(
    head_hash: str,
    fmt: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Compatibility wrapper: persist head hash + current head seq as checkpoint."""
    head = get_head(db_path)
    head_seq = head[0] if head else 0
    store_bundle_checkpoint(head_seq, head_hash, fmt, db_path)


def get_last_bundle_head_hash(db_path: str = DEFAULT_DB_PATH) -> Optional[str]:
    """Compatibility wrapper: return the head hash from the most recent bundle, or None."""
    checkpoint = get_last_bundle_checkpoint(db_path)
    return checkpoint[1] if checkpoint else None
