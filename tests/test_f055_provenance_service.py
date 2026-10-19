"""Tests for F055 Decision Provenance & Control Evidence.

Mirrors the PoC's 61 passing tests where applicable, plus integration tests
for the five new CSTP RPC methods.

Two-class evidence model is the non-negotiable design constraint:
  observed  — third-party events (hash-chained)
  attested  — first-party CSTP records (self-reported)
Coverage must be reported separately per class. Any blended metric is a bug.

Finding 3 — db_path removed from RPC params:
  Service functions no longer accept db_path via params. All service-layer
  tests inject the test DB via monkeypatch on DEFAULT_PROVENANCE_DB. Storage-layer
  tests (TestAppendAndGet, TestHashChainIntegrity, etc.) still pass db_path
  directly to the storage functions — that API is not changed.

Finding 8 — human actor signal required:
  Tests that previously used approval_count>0 without actor_is_human/human_approval_count
  have been updated deliberately: those events encoded the buggy behavior. Updated events
  now include actor_is_human=True and human_approval_count as appropriate.
"""

import builtins
import io
import json
import sqlite3
import threading
from contextlib import closing
import pytest
from pathlib import Path

import a2a.cstp.provenance_service as _svc

from a2a.cstp.storage.provenance import (
    EVIDENCE_CLASS_OBSERVED,
    append_event,
    canonical_json,
    compute_hash,
    event_count,
    get_events,
    get_head,
    get_head_hash,
    get_last_bundle_checkpoint,
    get_last_bundle_head_hash,
    init_db,
    store_bundle_head_hash,
    validate_observed_seqs,
    verify_at_checkpoint,
    verify_chain,
)
from a2a.cstp.provenance.mapping import (
    INSUFFICIENT_EVIDENCE_TAG,
    MAPPINGS_DIR,
    apply_rules,
    load_rules,
)
from a2a.cstp.provenance_service import (
    export_evidence_bundle,
    ingest_evidence,
    link_evidence,
    map_controls,
    verify_evidence_chain,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Low-level storage DB fixture. Does NOT patch the service default."""
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


@pytest.fixture
def db_with_observed(db, monkeypatch):
    """DB fixture with pre-loaded observed PR events.

    Also patches DEFAULT_PROVENANCE_DB so service-layer tests that take this
    fixture directly (without going through service_params_observed) correctly
    route to this temp DB.

    Events include actor_is_human and human_approval_count fields required by
    the finding-8 human-actor rules (updated deliberately from the prior
    buggy behavior).
    """
    events = [
        ("gh", "pr_opened", {"pr_number": 1, "is_agent_authored": True, "agent_type": "Claude",
                              "title": "Add risk scorer", "approval_count": 0}),
        ("gh", "pr_review_approved", {"pr_number": 1, "reviewer": "alice",
                                       "actor_is_human": True}),
        ("gh", "pr_merged", {"pr_number": 1, "is_agent_authored": True, "approval_count": 1,
                              "human_approval_count": 1, "merged_by": "alice"}),
        ("gh", "pr_opened", {"pr_number": 2, "is_agent_authored": True, "agent_type": "Claude",
                              "title": "Agent risk scorer v2", "approval_count": 0}),
        ("gh", "pr_merged", {"pr_number": 2, "is_agent_authored": True, "approval_count": 0,
                              "human_approval_count": 0, "merged_by": "claude-bot"}),
    ]
    for ts_idx, (source, etype, payload) in enumerate(events):
        append_event(
            source=source, event_type=etype, evidence_class=EVIDENCE_CLASS_OBSERVED,
            payload=payload, ts=f"2026-07-0{ts_idx + 1}T10:00:00Z", db_path=db,
        )
    monkeypatch.setattr(_svc, "DEFAULT_PROVENANCE_DB", db)
    return db


@pytest.fixture
def service_params(db, monkeypatch):
    """Service-layer params fixture. Patches DEFAULT_PROVENANCE_DB to test DB.

    Returns a dict that still includes db_path for any test code that reads the
    path directly (e.g. to call get_events). The service ignores db_path in params
    (finding 3 fix).
    """
    monkeypatch.setattr(_svc, "DEFAULT_PROVENANCE_DB", db)
    return {"db_path": db}


@pytest.fixture
def service_params_observed(db_with_observed, monkeypatch):
    """service_params variant with pre-loaded observed events."""
    monkeypatch.setattr(_svc, "DEFAULT_PROVENANCE_DB", db_with_observed)
    return {"db_path": db_with_observed}


@pytest.fixture(autouse=True)
def bundle_dir(tmp_path, monkeypatch):
    """Route every bundle export to the test's tmp dir instead of ~/.cstp/bundles.

    The service ignores output_dir in params, so DEFAULT_BUNDLE_DIR is the only
    way to choose where bundles land.
    """
    path = tmp_path / "bundles"
    monkeypatch.setattr(_svc, "DEFAULT_BUNDLE_DIR", str(path))
    return path


def _make_event(seq, event_type, payload, evidence_class="observed", ts="2026-07-01T00:00:00Z"):
    return {
        "seq": seq,
        "event_type": event_type,
        "ts": ts,
        "payload": payload,
        "source": "test",
        "evidence_class": evidence_class,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Store: canonical_json
# ══════════════════════════════════════════════════════════════════════════════

class TestCanonicalJson:
    def test_sorted_keys(self):
        result = canonical_json({"z": 1, "a": 2, "m": 3})
        assert result == '{"a":2,"m":3,"z":1}'

    def test_no_whitespace(self):
        result = canonical_json({"key": "val"})
        assert " " not in result

    def test_nested(self):
        result = canonical_json({"b": {"y": 2, "x": 1}, "a": 0})
        assert result == '{"a":0,"b":{"x":1,"y":2}}'

    def test_unicode_not_escaped(self):
        result = canonical_json({"k": "café"})
        assert "café" in result


# ══════════════════════════════════════════════════════════════════════════════
# Store: compute_hash
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeHash:
    def test_deterministic(self):
        h1 = compute_hash(1, "2026-01-01T00:00:00Z", "pr_opened", "observed", {"a": 1}, "")
        h2 = compute_hash(1, "2026-01-01T00:00:00Z", "pr_opened", "observed", {"a": 1}, "")
        assert h1 == h2

    def test_differs_on_seq(self):
        h1 = compute_hash(1, "ts", "et", "observed", {}, "")
        h2 = compute_hash(2, "ts", "et", "observed", {}, "")
        assert h1 != h2

    def test_differs_on_payload(self):
        h1 = compute_hash(1, "ts", "et", "observed", {"a": 1}, "")
        h2 = compute_hash(1, "ts", "et", "observed", {"a": 2}, "")
        assert h1 != h2

    def test_differs_on_evidence_class(self):
        """evidence_class is part of the hash preimage — changing it breaks the chain."""
        h1 = compute_hash(1, "ts", "et", "observed", {"a": 1}, "")
        h2 = compute_hash(1, "ts", "et", "attested", {"a": 1}, "")
        assert h1 != h2

    def test_differs_on_prev_hash(self):
        h1 = compute_hash(2, "ts", "et", "observed", {}, "abc")
        h2 = compute_hash(2, "ts", "et", "observed", {}, "def")
        assert h1 != h2

    def test_genesis_uses_empty_prev(self):
        h = compute_hash(1, "ts", "pr_opened", "observed", {}, "")
        assert isinstance(h, str) and len(h) == 64

    def test_payload_key_order_invariant(self):
        h1 = compute_hash(1, "ts", "et", "observed", {"z": 1, "a": 2}, "")
        h2 = compute_hash(1, "ts", "et", "observed", {"a": 2, "z": 1}, "")
        assert h1 == h2

    def test_hash_preimage_injective(self):
        """Finding 1: (ts='a\\nb', event_type='c') and (ts='a', event_type='b\\nc') must differ.

        The old newline-delimited preimage collapsed these two tuples to the same
        byte string. The canonical-JSON preimage encodes each field separately so
        no two distinct (seq, ts, event_type, ...) tuples share a preimage.
        FAILS before fix, PASSES after.
        """
        h1 = compute_hash(1, "a\nb", "c", "observed", {}, "")
        h2 = compute_hash(1, "a", "b\nc", "observed", {}, "")
        assert h1 != h2, (
            "Hash preimage must be injective — collisions allow hash chain forgery "
            "by swapping field values without changing the hash"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Store: append / get
# ══════════════════════════════════════════════════════════════════════════════

class TestAppendAndGet:
    def test_first_event_seq_is_one(self, db):
        seq, _ = append_event("src", "pr_opened", "observed", {"n": 1},
                               ts="2026-01-01T00:00:00Z", db_path=db)
        assert seq == 1

    def test_seq_increments(self, db):
        seq1, _ = append_event("src", "pr_opened", "observed", {"n": 1},
                                ts="2026-01-01T00:00:00Z", db_path=db)
        seq2, _ = append_event("src", "pr_merged", "observed", {"n": 1},
                                ts="2026-01-02T00:00:00Z", db_path=db)
        assert seq2 == seq1 + 1

    def test_get_events_order(self, db):
        for i in range(5):
            append_event("src", f"et_{i}", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        events = get_events(db)
        assert [e["seq"] for e in events] == list(range(1, 6))

    def test_payload_round_trips(self, db):
        payload = {"pr_number": 42, "title": "test PR", "is_agent": True}
        append_event("gh", "pr_opened", "observed", payload, ts="2026-01-01T00:00:00Z", db_path=db)
        events = get_events(db)
        assert events[0]["payload"] == payload

    def test_evidence_class_stored(self, db):
        append_event("src", "et", "observed", {}, ts="2026-01-01T00:00:00Z", db_path=db)
        append_event("src", "et", "attested", {}, ts="2026-01-02T00:00:00Z", db_path=db)
        events = get_events(db)
        assert events[0]["evidence_class"] == "observed"
        assert events[1]["evidence_class"] == "attested"

    def test_filter_by_evidence_class(self, db):
        append_event("src", "obs_event", "observed", {}, ts="2026-01-01T00:00:00Z", db_path=db)
        append_event("src", "att_event", "attested", {}, ts="2026-01-02T00:00:00Z", db_path=db)
        obs_events = get_events(db, evidence_class="observed")
        att_events = get_events(db, evidence_class="attested")
        assert len(obs_events) == 1 and obs_events[0]["event_type"] == "obs_event"
        assert len(att_events) == 1 and att_events[0]["event_type"] == "att_event"

    def test_event_count(self, db):
        for i in range(7):
            append_event("src", "evt", "observed", {"i": i},
                         ts="2026-01-01T00:00:00Z", db_path=db)
        assert event_count(db) == 7

    def test_invalid_evidence_class_rejected(self, db):
        with pytest.raises(ValueError, match="evidence_class"):
            append_event("src", "et", "unknown", {}, db_path=db)

    def test_concurrent_append_produces_valid_chain(self, db):
        """Finding 2: concurrent appenders must not break the chain.

        Before fix (no BEGIN IMMEDIATE): two threads could read the same head seq,
        compute the next hash with the same next_seq, then INSERT — one gets the
        right seq, the other has a hash that doesn't match its actual row.
        After fix (BEGIN IMMEDIATE + explicit seq): writes serialize, chain stays intact.
        FAILS (non-deterministically) before fix, PASSES reliably after.
        """
        errors: list[Exception] = []

        def appender(i: int) -> None:
            try:
                append_event(
                    "src", f"et_{i}", "observed", {"i": i},
                    ts=f"2026-{(i % 12) + 1:02d}-01T00:00:00Z", db_path=db,
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=appender, args=(i,)) for i in range(1, 11)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent append errors: {errors}"
        assert verify_chain(db) is None, "Concurrent appends left the chain broken"
        assert event_count(db) == 10, "Some events were lost under concurrent writes"


# ══════════════════════════════════════════════════════════════════════════════
# Store: hash chain integrity
# ══════════════════════════════════════════════════════════════════════════════

class TestHashChainIntegrity:
    def test_empty_chain_is_intact(self, db):
        assert verify_chain(db) is None

    def test_single_event_intact(self, db):
        append_event("src", "pr_opened", "observed", {"n": 1},
                     ts="2026-01-01T00:00:00Z", db_path=db)
        assert verify_chain(db) is None

    def test_multi_event_intact(self, db):
        for i in range(10):
            append_event("src", "pr_opened", "observed", {"i": i},
                         ts="2026-01-01T00:00:00Z", db_path=db)
        assert verify_chain(db) is None

    def test_tamper_detection_payload(self, db):
        for i in range(3):
            append_event("src", "pr_opened", "observed", {"i": i},
                         ts="2026-01-01T00:00:00Z", db_path=db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("UPDATE events SET payload_json = ? WHERE seq = 2", ('{"i":99}',))
            conn.commit()
        assert verify_chain(db) == 2

    def test_tamper_detection_first_record(self, db):
        for i in range(3):
            append_event("src", "pr_opened", "observed", {"i": i},
                         ts="2026-01-01T00:00:00Z", db_path=db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("UPDATE events SET payload_json = ? WHERE seq = 1",
                         ('{"tampered":true}',))
            conn.commit()
        assert verify_chain(db) == 1

    def test_tamper_detection_evidence_class(self, db):
        """Changing evidence_class must break the chain (it's in the hash preimage)."""
        append_event("src", "pr_opened", "observed", {"n": 1},
                     ts="2026-01-01T00:00:00Z", db_path=db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("UPDATE events SET evidence_class = 'attested' WHERE seq = 1")
            conn.commit()
        assert verify_chain(db) == 1

    def test_tail_truncation_invisible_without_expected_hash(self, db):
        """Deleting tail events is undetectable without expected_head_hash."""
        for i in range(5):
            append_event("src", "pr_opened", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        original_head = get_head_hash(db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("DELETE FROM events WHERE seq > 3")
            conn.commit()
        assert verify_chain(db) is None  # truncation invisible
        assert verify_chain(db, expected_head_hash=original_head) == 0  # detected

    def test_tail_truncation_detected_with_expected_hash(self, db):
        """With expected_head_hash, truncation returns sentinel 0."""
        for i in range(5):
            append_event("src", "pr_opened", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        original_head = get_head_hash(db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("DELETE FROM events WHERE seq >= 4")
            conn.commit()
        result = verify_chain(db, expected_head_hash=original_head)
        assert result == 0

    def test_correct_chain_passes_with_expected_hash(self, db):
        for i in range(3):
            append_event("src", "pr_opened", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        head = get_head_hash(db)
        assert verify_chain(db, expected_head_hash=head) is None

    def test_chain_determinism(self, tmp_path):
        """Same events appended to two fresh DBs must produce identical hashes."""
        events_data = [
            ("src", "pr_opened", "observed", {"pr_number": 1}, "2026-07-01T09:00:00Z"),
            ("src", "pr_review_approved", "observed", {"pr_number": 1}, "2026-07-01T12:00:00Z"),
            ("src", "pr_merged", "observed", {"pr_number": 1, "approval_count": 1},
             "2026-07-01T14:00:00Z"),
        ]
        db1 = str(tmp_path / "chain1.db")
        db2 = str(tmp_path / "chain2.db")
        init_db(db1)
        init_db(db2)
        hashes1, hashes2 = [], []
        for source, etype, ev_class, payload, ts in events_data:
            _, h1 = append_event(source, etype, ev_class, payload, ts=ts, db_path=db1)
            _, h2 = append_event(source, etype, ev_class, payload, ts=ts, db_path=db2)
            hashes1.append(h1)
            hashes2.append(h2)
        assert hashes1 == hashes2


# ══════════════════════════════════════════════════════════════════════════════
# Store: verify_at_checkpoint
# ══════════════════════════════════════════════════════════════════════════════

class TestVerifyAtCheckpoint:
    def test_intact_chain_passes_at_head(self, db):
        for i in range(5):
            append_event("src", "evt", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        head = get_head(db)
        assert head is not None
        assert verify_at_checkpoint(db, head[0], head[1]) is None

    def test_valid_extension_passes(self, db):
        """Finding 5+6: chain growth beyond checkpoint must NOT be flagged as broken."""
        for i in range(3):
            append_event("src", "evt", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        checkpoint = get_head(db)
        assert checkpoint is not None
        # Add two more events (valid extension)
        for i in range(3, 5):
            append_event("src", "evt", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        # Checkpoint at seq 3 must still pass even though chain has grown to seq 5
        assert verify_at_checkpoint(db, checkpoint[0], checkpoint[1]) is None

    def test_tail_truncation_detected(self, db):
        """Checkpoint record deleted — verify_at_checkpoint returns 0."""
        for i in range(5):
            append_event("src", "evt", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        checkpoint = get_head(db)
        assert checkpoint is not None
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("DELETE FROM events WHERE seq >= 4")
            conn.commit()
        assert verify_at_checkpoint(db, checkpoint[0], checkpoint[1]) == 0

    def test_tampered_record_detected(self, db):
        for i in range(5):
            append_event("src", "evt", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        head = get_head(db)
        assert head is not None
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("UPDATE events SET payload_json = '{\"x\":99}' WHERE seq = 3")
            conn.commit()
        assert verify_at_checkpoint(db, head[0], head[1]) == 3


# ══════════════════════════════════════════════════════════════════════════════
# Store: bundle metadata (P2 fix)
# ══════════════════════════════════════════════════════════════════════════════

class TestBundleMetadata:
    def test_store_and_retrieve_head_hash(self, db):
        append_event("src", "pr_opened", "observed", {}, ts="2026-01-01T00:00:00Z", db_path=db)
        head = get_head_hash(db)
        store_bundle_head_hash(head, "json", db)
        retrieved = get_last_bundle_head_hash(db)
        assert retrieved == head

    def test_returns_none_if_no_bundle(self, db):
        assert get_last_bundle_head_hash(db) is None

    def test_returns_most_recent(self, db):
        for i in range(3):
            append_event("src", "pr_opened", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        h1 = get_head_hash(db)
        store_bundle_head_hash(h1, "json", db)
        append_event("src", "pr_merged", "observed", {},
                     ts="2026-01-04T00:00:00Z", db_path=db)
        h2 = get_head_hash(db)
        store_bundle_head_hash(h2, "json", db)
        assert get_last_bundle_head_hash(db) == h2

    def test_checkpoint_stores_seq(self, db):
        """Checkpoint stores head_seq so verify_at_checkpoint can verify without head comparison."""
        for i in range(5):
            append_event("src", "evt", "observed", {"i": i},
                         ts=f"2026-01-0{i+1}T00:00:00Z", db_path=db)
        h = get_head_hash(db)
        store_bundle_head_hash(h, "json", db)
        cp = get_last_bundle_checkpoint(db)
        assert cp is not None
        assert cp == (5, h)


# ══════════════════════════════════════════════════════════════════════════════
# Store: validate_observed_seqs
# ══════════════════════════════════════════════════════════════════════════════

class TestValidateObservedSeqs:
    def test_empty_list_is_valid(self, db):
        validate_observed_seqs([], db)  # should not raise

    def test_valid_observed_seqs_pass(self, db):
        for _ in range(3):
            append_event("src", "evt", "observed", {}, ts="2026-01-01T00:00:00Z", db_path=db)
        validate_observed_seqs([1, 2, 3], db)  # should not raise

    def test_rejects_nonexistent_seq(self, db):
        append_event("src", "evt", "observed", {}, ts="2026-01-01T00:00:00Z", db_path=db)
        with pytest.raises(ValueError, match="nonexistent"):
            validate_observed_seqs([999], db)

    def test_rejects_attested_seq(self, db):
        append_event("src", "evt", "attested", {}, ts="2026-01-01T00:00:00Z", db_path=db)
        with pytest.raises(ValueError, match="observed"):
            validate_observed_seqs([1], db)

    def test_rejects_duplicate_seqs(self, db):
        append_event("src", "evt", "observed", {}, ts="2026-01-01T00:00:00Z", db_path=db)
        with pytest.raises(ValueError, match="duplicate"):
            validate_observed_seqs([1, 1], db)


# ══════════════════════════════════════════════════════════════════════════════
# Mapping: load rules
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadRules:
    def test_loads_both_observed_frameworks(self):
        rules = load_rules(MAPPINGS_DIR)
        frameworks = {r["_framework"] for r in rules}
        assert "SR 11-7" in frameworks
        assert "NIST AI RMF" in frameworks

    def test_loads_attested_framework(self):
        rules = load_rules(MAPPINGS_DIR)
        frameworks = {r["_framework"] for r in rules}
        assert "CSTP Attested" in frameworks

    def test_rules_preserve_non_ascii_under_non_utf8_default_encoding(self, monkeypatch):
        """Control text must decode as UTF-8 regardless of the platform default.

        The mapping YAML files contain em-dashes in control_name and reason text,
        and that text is copied verbatim into auditor-facing bundles. If load_rules
        relies on the ambient locale encoding, a cp1252 host (Windows) silently
        decodes those bytes as mojibake instead of raising — a corrupted regulatory
        citation in a compliance artifact.

        Simulates a cp1252 host by forcing that encoding on any text-mode open
        that does not name one, so the test fails on UTF-8 platforms too.
        """
        real_open = io.open

        def cp1252_default_open(
            file, mode="r", buffering=-1, encoding=None, *args, **kwargs
        ):
            # Path.open() resolves an unspecified encoding to the sentinel
            # "locale" before delegating, so both spellings mean "ambient default".
            if "b" not in mode and encoding in (None, "locale"):
                encoding = "cp1252"
            return real_open(file, mode, buffering, encoding, *args, **kwargs)

        monkeypatch.setattr(io, "open", cp1252_default_open)
        monkeypatch.setattr(builtins, "open", cp1252_default_open)

        rules = load_rules(MAPPINGS_DIR)

        assert rules, "no rules loaded"
        # U+2014 mis-decoded through cp1252 surfaces as this three-char sequence.
        mojibake = [r for r in rules if "â€" in str(r)]
        assert not mojibake, (
            f"{len(mojibake)} of {len(rules)} rules decoded as mojibake — "
            "load_rules is using the platform default encoding"
        )
        assert any("—" in str(r) for r in rules), (
            "expected an em-dash somewhere in the shipped control text"
        )

    def test_rules_have_required_fields(self):
        rules = load_rules(MAPPINGS_DIR)
        for rule in rules:
            assert "id" in rule, f"Missing 'id' in rule {rule}"
            assert "stage" in rule, f"Missing 'stage' in rule {rule}"
            assert "control_name" in rule, f"Missing 'control_name' in rule {rule}"
            assert "match" in rule, f"Missing 'match' in rule {rule}"
            assert "event_type" in rule["match"], f"Missing match.event_type in rule {rule}"

    def test_rules_carry_evidence_class(self):
        """Finding 7: each rule must carry _evidence_class from its YAML file."""
        rules = load_rules(MAPPINGS_DIR)
        for rule in rules:
            assert "_evidence_class" in rule, f"Rule {rule.get('id')} missing _evidence_class"
        observed_frameworks = {r["_framework"] for r in rules if r["_evidence_class"] == "observed"}
        attested_frameworks = {r["_framework"] for r in rules if r["_evidence_class"] == "attested"}
        assert "SR 11-7" in observed_frameworks
        assert "NIST AI RMF" in observed_frameworks
        assert "CSTP Attested" in attested_frameworks

    def test_ie_rules_have_reason(self):
        rules = load_rules(MAPPINGS_DIR)
        for rule in rules:
            if rule.get("insufficient_evidence"):
                assert rule.get("reason"), f"IE rule {rule['id']} missing reason"

    def test_map11_is_stretch(self):
        """MAP-1.1 must be tagged stretch=True per P1 fix."""
        rules = load_rules(MAPPINGS_DIR)
        map11 = [r for r in rules if r.get("function_id") == "MAP-1.1"]
        assert map11, "MAP-1.1 rule not found"
        assert map11[0].get("stretch") is True
        assert map11[0].get("confidence") == "low"

    def test_manage11_requires_agent_authored(self):
        """MANAGE-1.1 must only fire on agent-authored merges (P1 fix)."""
        rules = load_rules(MAPPINGS_DIR)
        manage11 = [r for r in rules if r.get("function_id") == "MANAGE-1.1"]
        assert manage11, "MANAGE-1.1 rule not found"
        conditions = manage11[0]["match"].get("conditions", [])
        has_agent_condition = any(
            c.get("field") == "is_agent_authored" and c.get("equals") is True
            for c in conditions
        )
        assert has_agent_condition, "MANAGE-1.1 must require is_agent_authored=true"

    def test_mv3_human_approval_requires_actor_is_human(self):
        """Finding 8: SR11-7 MV-3 human-approval rule must require actor_is_human."""
        rules = load_rules(MAPPINGS_DIR)
        mv3_approval = [r for r in rules if r.get("id") == "SR11-7-MV3-human-approval"]
        assert mv3_approval, "SR11-7-MV3-human-approval rule not found"
        conditions = mv3_approval[0]["match"].get("conditions", [])
        has_human_condition = any(
            c.get("field") == "actor_is_human" and c.get("equals") is True
            for c in conditions
        )
        assert has_human_condition, "MV-3 human-approval must require actor_is_human=true"

    def test_cm1_requires_human_approval_count(self):
        """Finding 8: CM-1 approved-agent-merge must require human_approval_count>0."""
        rules = load_rules(MAPPINGS_DIR)
        cm1 = [r for r in rules if r.get("id") == "SR11-7-CM1-approved-agent-merge"]
        assert cm1, "SR11-7-CM1-approved-agent-merge rule not found"
        conditions = cm1[0]["match"].get("conditions", [])
        has_human_count = any(
            c.get("field") == "human_approval_count" and c.get("gt", -1) >= 0
            for c in conditions
        )
        assert has_human_count, "CM-1 must require human_approval_count>0"


# ══════════════════════════════════════════════════════════════════════════════
# Mapping: apply rules
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyRules:
    def test_agent_pr_opens_maps_sr117(self):
        rules = load_rules(MAPPINGS_DIR)
        events = [_make_event(1, "pr_opened",
                               {"pr_number": 1, "is_agent_authored": True})]
        mr = apply_rules(events, rules)
        sr_maps = [m for m in mr.mappings if "SR 11-7" in m["framework"]]
        assert len(sr_maps) >= 1

    def test_human_pr_opens_maps_sr117(self):
        rules = load_rules(MAPPINGS_DIR)
        events = [_make_event(1, "pr_opened",
                               {"pr_number": 1, "is_agent_authored": False})]
        mr = apply_rules(events, rules)
        assert mr.observed.mapped_events == 1

    def test_approval_maps_to_mv3_when_actor_is_human(self):
        """Finding 8 (updated): approval must have actor_is_human=True to map to MV-3."""
        rules = load_rules(MAPPINGS_DIR)
        events = [_make_event(1, "pr_review_approved",
                               {"pr_number": 1, "reviewer": "bob", "actor_is_human": True})]
        mr = apply_rules(events, rules)
        mv3 = [m for m in mr.mappings if m.get("function_id") == "MV-3"]
        assert len(mv3) >= 1

    def test_agent_merge_with_human_approval_maps_cm1(self):
        """Finding 8 (updated): merge must have human_approval_count>0 to map to CM-1."""
        rules = load_rules(MAPPINGS_DIR)
        events = [_make_event(1, "pr_merged",
                               {"pr_number": 1, "is_agent_authored": True,
                                "approval_count": 2, "human_approval_count": 2})]
        mr = apply_rules(events, rules)
        cm = [m for m in mr.mappings if "CM" in m.get("function_id", "")]
        assert len(cm) >= 1

    def test_unknown_event_type_is_unmapped(self):
        rules = load_rules(MAPPINGS_DIR)
        events = [_make_event(1, "some_future_event_type", {"pr_number": 1})]
        mr = apply_rules(events, rules)
        assert mr.observed.unmapped_events == 1
        assert mr.observed.mapped_events == 0

    def test_evidence_class_propagated_to_mappings(self):
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "pr_opened", {"pr_number": 1, "is_agent_authored": True},
                        evidence_class="observed"),
        ]
        mr = apply_rules(events, rules)
        for m in mr.mappings:
            if m.get("seq") == 1:
                assert m["evidence_class"] == "observed"

    def test_attested_events_mapped_separately(self):
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "pr_opened", {"pr_number": 1, "is_agent_authored": True},
                        evidence_class="observed"),
            _make_event(2, "cstp_decision_linked",
                        {"decision_id": "abc", "event_seqs": [1], "has_outcome": True},
                        evidence_class="attested"),
        ]
        mr = apply_rules(events, rules)
        assert mr.observed.total_events == 1
        assert mr.attested.total_events == 1

    def test_bot_approval_does_not_satisfy_mv3(self):
        """Finding 8 regression: bot approval (no actor_is_human) must not map to MV-3.

        FAILS before fix (MV-3 fires unconditionally on pr_review_approved).
        PASSES after fix (actor_is_human must be True).
        """
        rules = load_rules(MAPPINGS_DIR)
        events = [_make_event(1, "pr_review_approved",
                               {"pr_number": 1, "reviewer": "dependabot[bot]"})]
        mr = apply_rules(events, rules)
        mv3_maps = [m for m in mr.mappings if m.get("function_id") == "MV-3"]
        assert len(mv3_maps) == 0, (
            "Bot approval (no actor_is_human field) must NOT satisfy MV-3 — "
            "a bot approving an agent's output is not independent human review"
        )

    def test_no_human_approval_count_no_cm1_credit(self):
        """Finding 8 regression: approval_count>0 without human_approval_count must not map CM-1.

        FAILS before fix (CM-1 fires on approval_count>0).
        PASSES after fix (human_approval_count>0 required).
        """
        rules = load_rules(MAPPINGS_DIR)
        events = [_make_event(1, "pr_merged",
                               {"pr_number": 1, "is_agent_authored": True, "approval_count": 1})]
        mr = apply_rules(events, rules)
        cm1 = [m for m in mr.mappings if m.get("function_id") == "CM-1"]
        assert len(cm1) == 0, (
            "approval_count>0 without human_approval_count must not satisfy CM-1 — "
            "bot approvals should not award change management credit"
        )

    def test_observed_event_does_not_match_attested_rules(self):
        """Finding 7 regression: observed events must not match attested-class rules.

        FAILS before fix (cstp-attested.yaml rules matched any event with the right
        event_type regardless of evidence_class).
        PASSES after fix (class isolation enforced in _rule_matches).
        """
        rules = load_rules(MAPPINGS_DIR)
        # Observed event of type cstp_decision_linked (evidence_class="observed")
        events = [_make_event(1, "cstp_decision_linked",
                               {"decision_id": "x", "event_seqs": [], "has_outcome": True},
                               evidence_class="observed")]
        mr = apply_rules(events, rules)
        attested_maps = [m for m in mr.mappings if "CSTP" in m.get("framework", "")]
        assert len(attested_maps) == 0, (
            "Observed events must never match attested-class rules — "
            "doing so awards high-trust observed credit for self-reported data"
        )

    def test_attested_event_does_not_match_observed_rules(self):
        """Finding 7 regression: attested events must not match observed-class rules.

        FAILS before fix. PASSES after fix.
        """
        rules = load_rules(MAPPINGS_DIR)
        # Attested event of type pr_opened — must not match SR 11-7 or NIST observed rules
        events = [_make_event(1, "pr_opened",
                               {"pr_number": 1, "is_agent_authored": True},
                               evidence_class="attested")]
        mr = apply_rules(events, rules)
        sr_maps = [m for m in mr.mappings if "SR 11-7" in m.get("framework", "")]
        nist_maps = [m for m in mr.mappings if "NIST" in m.get("framework", "")]
        assert len(sr_maps) == 0, "Attested events must not match SR 11-7 observed rules"
        assert len(nist_maps) == 0, "Attested events must not match NIST AI RMF observed rules"


# ══════════════════════════════════════════════════════════════════════════════
# Mapping: two-class coverage (THE NON-NEGOTIABLE CONSTRAINT)
# ══════════════════════════════════════════════════════════════════════════════

class TestPerClassCoverage:
    def test_observed_coverage_computed_separately(self):
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "pr_opened", {"pr_number": 1, "is_agent_authored": True},
                        evidence_class="observed"),
            _make_event(2, "pr_review_approved", {"pr_number": 1, "actor_is_human": True},
                        evidence_class="observed"),
        ]
        mr = apply_rules(events, rules)
        assert mr.observed.total_events == 2
        assert mr.observed.coverage_pct > 0

    def test_attested_coverage_computed_separately(self):
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "cstp_decision_linked",
                        {"decision_id": "x", "event_seqs": [], "has_outcome": False},
                        evidence_class="attested"),
        ]
        mr = apply_rules(events, rules)
        assert mr.attested.total_events == 1
        assert mr.attested.coverage_pct > 0

    def test_mapping_result_has_no_blended_coverage(self):
        """MappingResult must NOT expose a single blended coverage_pct.

        This test fails if a blended coverage number is ever added to MappingResult.
        A blended metric weakens the strong observed class by mixing it with weaker
        attested evidence — the most important design invariant of F055.
        """
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "pr_opened", {"pr_number": 1, "is_agent_authored": True},
                        evidence_class="observed"),
            _make_event(2, "cstp_decision_linked",
                        {"decision_id": "y", "event_seqs": [1], "has_outcome": True},
                        evidence_class="attested"),
        ]
        mr = apply_rules(events, rules)
        # Must have separate per-class coverage
        assert hasattr(mr, "observed") and hasattr(mr.observed, "coverage_pct")
        assert hasattr(mr, "attested") and hasattr(mr.attested, "coverage_pct")
        # Must NOT have a single blended coverage_pct on MappingResult itself
        assert not hasattr(mr, "coverage_pct"), (
            "MappingResult must not expose a blended coverage_pct. "
            "Separate observed.coverage_pct and attested.coverage_pct are required."
        )

    @pytest.mark.asyncio
    async def test_rpc_response_has_no_blended_coverage(self, tmp_path, monkeypatch):
        """cstp.mapControls response must not contain a top-level coverage_pct."""
        db_path = str(tmp_path / "no_blend.db")
        init_db(db_path)
        append_event("gh", "pr_opened", "observed",
                     {"pr_number": 1, "is_agent_authored": True},
                     ts="2026-01-01T00:00:00Z", db_path=db_path)
        monkeypatch.setattr(_svc, "DEFAULT_PROVENANCE_DB", db_path)
        result = await map_controls({}, "test-agent")
        assert "coverage_pct" not in result, (
            "cstp.mapControls must not return a blended coverage_pct. "
            "Only coverage.observed.coverage_pct and coverage.attested.coverage_pct are allowed."
        )
        assert "coverage" in result
        assert "observed" in result["coverage"]
        assert "attested" in result["coverage"]

    @pytest.mark.asyncio
    async def test_bundle_has_no_blended_coverage(self, tmp_path, monkeypatch):
        """exportEvidenceBundle JSON bundle must not contain a top-level coverage_pct."""
        db_path = str(tmp_path / "bundle_blend.db")
        init_db(db_path)
        append_event("gh", "pr_opened", "observed",
                     {"pr_number": 1, "is_agent_authored": True},
                     ts="2026-01-01T00:00:00Z", db_path=db_path)
        monkeypatch.setattr(_svc, "DEFAULT_PROVENANCE_DB", db_path)
        result = await export_evidence_bundle({"format": "json"}, "test-agent")
        # The service-level result must not have blended coverage
        assert "coverage_pct" not in result, (
            "exportEvidenceBundle must not return a blended coverage_pct."
        )
        # The JSON bundle file also must not have a top-level coverage_pct
        json_path = result.get("json_path")
        assert json_path, "json_path missing from result"
        bundle = json.loads(Path(json_path).read_text(encoding="utf-8"))
        assert "coverage_pct" not in bundle, (
            "JSON bundle must not contain a top-level blended coverage_pct."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Mapping: attested-only controls
# ══════════════════════════════════════════════════════════════════════════════

class TestAttestedOnlyControls:
    def test_attested_only_control_flagged(self):
        """Controls with ONLY attested evidence must be in attested_only_controls."""
        rules = load_rules(MAPPINGS_DIR)
        # Only attested event, no observed
        events = [
            _make_event(1, "cstp_decision_linked",
                        {"decision_id": "x", "event_seqs": [], "has_outcome": False,
                         "stakes": "high"},
                        evidence_class="attested"),
        ]
        mr = apply_rules(events, rules)
        # Should have some attested-only controls (no observed evidence for these)
        assert len(mr.attested_only_controls) >= 1

    def test_attested_only_not_flagged_when_observed_covers_same(self):
        """If observed evidence also covers a control, it must not be attested_only."""
        rules = load_rules(MAPPINGS_DIR)
        events = [
            # Observed event covers MV-1
            _make_event(1, "pr_opened", {"pr_number": 1, "is_agent_authored": True},
                        evidence_class="observed"),
            # Attested event would also cover MV-1 (via CSTP-SR117-MV1-decision-record)
            _make_event(2, "cstp_decision_linked",
                        {"decision_id": "x", "event_seqs": [1], "has_outcome": False},
                        evidence_class="attested"),
        ]
        mr = apply_rules(events, rules)
        # MV-1 must NOT be attested_only because observed event covers it
        assert "MV-1" not in mr.attested_only_controls

    def test_attested_only_mappings_tagged_in_list(self):
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "cstp_decision_linked",
                        {"decision_id": "y", "event_seqs": [], "has_outcome": True},
                        evidence_class="attested"),
        ]
        mr = apply_rules(events, rules)
        for m in mr.mappings:
            if m.get("function_id") in mr.attested_only_controls:
                assert m.get("attested_only") is True


# ══════════════════════════════════════════════════════════════════════════════
# Mapping: INSUFFICIENT EVIDENCE
# ══════════════════════════════════════════════════════════════════════════════

class TestInsufficientEvidence:
    def test_agent_merged_no_approval_fires_ie(self):
        """Agent-authored PR merged with no human approval must raise INSUFFICIENT EVIDENCE."""
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "pr_opened", {"pr_number": 3, "is_agent_authored": True}),
            _make_event(2, "pr_merged", {"pr_number": 3, "is_agent_authored": True,
                                          "approval_count": 0, "human_approval_count": 0}),
        ]
        mr = apply_rules(events, rules)
        ie = [i for i in mr.insufficient_evidence if i["type"] == INSUFFICIENT_EVIDENCE_TAG]
        assert len(ie) >= 1

    def test_ie_has_reason_text(self):
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "pr_merged", {"pr_number": 3, "is_agent_authored": True,
                                          "approval_count": 0, "human_approval_count": 0}),
        ]
        mr = apply_rules(events, rules)
        for ie in mr.insufficient_evidence:
            assert ie["reason"] and len(ie["reason"]) > 20

    def test_agent_merged_with_human_approval_no_mv3_ie(self):
        """Finding 8 (updated): merge with human_approval_count>0 must not fire MV-3 IE."""
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "pr_merged", {"pr_number": 2, "is_agent_authored": True,
                                          "approval_count": 1, "human_approval_count": 1}),
        ]
        mr = apply_rules(events, rules)
        mv3_ie = [i for i in mr.insufficient_evidence
                   if i.get("pr_number") == 2 and "MV-3" in i.get("function_id", "")]
        assert len(mv3_ie) == 0

    def test_manage11_does_not_fire_on_human_merge(self):
        """MANAGE-1.1 must NOT fire on human-authored merges (P1 fix)."""
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "pr_merged", {"pr_number": 5, "is_agent_authored": False,
                                          "approval_count": 2, "human_approval_count": 2}),
        ]
        mr = apply_rules(events, rules)
        manage11 = [m for m in mr.mappings
                     if m.get("function_id") == "MANAGE-1.1" and m.get("pr_number") == 5]
        assert len(manage11) == 0, "MANAGE-1.1 must not fire on human-authored merges"

    def test_bot_approval_only_fires_ie(self):
        """Finding 8: approvals without human_approval_count>0 must fire bot-only IE."""
        rules = load_rules(MAPPINGS_DIR)
        events = [
            _make_event(1, "pr_merged", {"pr_number": 7, "is_agent_authored": True,
                                          "approval_count": 1}),  # no human_approval_count
        ]
        mr = apply_rules(events, rules)
        bot_ie = [i for i in mr.insufficient_evidence
                   if i.get("pr_number") == 7 and "bot" in i.get("reason", "").lower()]
        assert len(bot_ie) >= 1, (
            "Agent PR merged with approval_count>0 but no human_approval_count "
            "must surface as INSUFFICIENT EVIDENCE (bot-only approval)"
        )


# ══════════════════════════════════════════════════════════════════════════════
# RPC: cstp.ingestEvidence
# ══════════════════════════════════════════════════════════════════════════════

class TestIngestEvidence:
    @pytest.mark.asyncio
    async def test_ingest_single_event(self, service_params):
        params = {
            **service_params,
            "events": [{"event_type": "pr_opened", "payload": {"pr_number": 1},
                        "ts": "2026-01-01T00:00:00Z"}],
        }
        result = await ingest_evidence(params, "agent-1")
        assert result["ingested"] == 1
        assert result["head_hash"] is not None
        assert result["first_seq"] == 1

    @pytest.mark.asyncio
    async def test_ingest_multiple_events(self, service_params):
        params = {
            **service_params,
            "events": [
                {"event_type": "pr_opened", "payload": {}, "ts": "2026-01-01T00:00:00Z"},
                {"event_type": "pr_merged", "payload": {}, "ts": "2026-01-02T00:00:00Z"},
            ],
        }
        result = await ingest_evidence(params, "agent-1")
        assert result["ingested"] == 2

    @pytest.mark.asyncio
    async def test_all_ingested_events_are_observed(self, service_params):
        params = {
            **service_params,
            "events": [{"event_type": "pr_opened", "payload": {}, "ts": "2026-01-01T00:00:00Z"}],
        }
        await ingest_evidence(params, "agent-1")
        events = get_events(service_params["db_path"])
        for e in events:
            assert e["evidence_class"] == "observed"

    @pytest.mark.asyncio
    async def test_ingest_requires_events(self, service_params):
        params = {**service_params, "events": []}
        with pytest.raises(ValueError, match="events"):
            await ingest_evidence(params, "agent-1")

    @pytest.mark.asyncio
    async def test_ingest_requires_event_type(self, service_params):
        params = {**service_params, "events": [{"payload": {}}]}
        with pytest.raises(ValueError, match="event_type"):
            await ingest_evidence(params, "agent-1")

    @pytest.mark.asyncio
    async def test_ingest_rejects_non_dict_payload(self, service_params):
        """Finding 10: non-dict payload must be rejected before persisting."""
        params = {
            **service_params,
            "events": [{"event_type": "pr_opened", "payload": ["not", "a", "dict"]}],
        }
        with pytest.raises(ValueError, match="dict"):
            await ingest_evidence(params, "agent-1")
        # Verify nothing was persisted
        assert event_count(service_params["db_path"]) == 0

    @pytest.mark.asyncio
    async def test_ingest_batch_is_atomic(self, service_params):
        """Finding 11: if any event is invalid, no events in the batch are committed."""
        params = {
            **service_params,
            "events": [
                {"event_type": "pr_opened", "payload": {}, "ts": "2026-01-01T00:00:00Z"},
                {"event_type": "pr_merged", "payload": "not_a_dict", "ts": "2026-01-02T00:00:00Z"},
            ],
        }
        with pytest.raises(ValueError, match="dict"):
            await ingest_evidence(params, "agent-1")
        # Both events must be absent — atomicity requirement
        assert event_count(service_params["db_path"]) == 0, (
            "Finding 11: batch ingest must be atomic — partial commit on error is a bug"
        )

    @pytest.mark.asyncio
    async def test_ingest_unsupported_format_rejected(self, service_params, bundle_dir):
        """Finding 13: unsupported format must raise ValueError before creating any files."""
        params = {
            **service_params,
            "format": "jsn",  # typo
        }
        with pytest.raises(ValueError, match="Unsupported bundle format"):
            await export_evidence_bundle(params, "agent-1")
        assert not bundle_dir.exists(), (
            "Finding 13: output_dir must not be created for invalid format"
        )


# ══════════════════════════════════════════════════════════════════════════════
# RPC: cstp.linkEvidence
# ══════════════════════════════════════════════════════════════════════════════

class TestLinkEvidence:
    @pytest.mark.asyncio
    async def test_link_creates_attested_event(self, db_with_observed, service_params_observed):
        params = {
            **service_params_observed,
            "decision_id": "dec-abc123",
            "event_seqs": [1, 2],
            "has_outcome": True,
        }
        result = await link_evidence(params, "agent-1")
        assert result["linked"] == 2
        assert result["decision_id"] == "dec-abc123"
        assert result["attested_seq"] is not None

        # Verify the attested event is in the store
        att_events = get_events(db_with_observed, evidence_class="attested")
        assert len(att_events) == 1
        assert att_events[0]["event_type"] == "cstp_decision_linked"
        assert att_events[0]["payload"]["decision_id"] == "dec-abc123"

    @pytest.mark.asyncio
    async def test_link_requires_decision_id(self, service_params_observed):
        params = {**service_params_observed, "event_seqs": [1]}
        with pytest.raises(ValueError, match="decision_id"):
            await link_evidence(params, "agent-1")

    @pytest.mark.asyncio
    async def test_link_attested_event_has_correct_class(self, service_params):
        params = {**service_params, "decision_id": "d1", "event_seqs": []}
        await link_evidence(params, "agent-1")
        events = get_events(service_params["db_path"])
        assert all(
            e["evidence_class"] in ("observed", "attested") for e in events
        )
        att = [e for e in events if e["evidence_class"] == "attested"]
        assert len(att) == 1

    @pytest.mark.asyncio
    async def test_link_rejects_nonexistent_seq(self, service_params_observed):
        """Finding 4 regression: linking a nonexistent seq must fail.

        FAILS before fix (link_evidence appended attested record regardless).
        PASSES after fix (validate_observed_seqs rejects missing seqs).
        """
        params = {
            **service_params_observed,
            "decision_id": "dec-xyz",
            "event_seqs": [999],  # seq 999 does not exist
        }
        with pytest.raises(ValueError, match="nonexistent"):
            await link_evidence(params, "agent-1")

    @pytest.mark.asyncio
    async def test_link_rejects_attested_seq(self, db_with_observed, service_params_observed):
        """Finding 4 regression: linking an attested seq must fail.

        FAILS before fix. PASSES after fix.
        """
        # First, create an attested event (the seq we'll try to reuse)
        att_result = await link_evidence(
            {**service_params_observed, "decision_id": "dec-1", "event_seqs": [1]},
            "agent-1",
        )
        attested_seq = att_result["attested_seq"]

        # Now try to link to that attested seq — must be rejected
        with pytest.raises(ValueError, match="observed"):
            await link_evidence(
                {**service_params_observed, "decision_id": "dec-2",
                 "event_seqs": [attested_seq]},
                "agent-1",
            )

    @pytest.mark.asyncio
    async def test_link_rejects_duplicate_seqs(self, service_params_observed):
        """Finding 4 regression: duplicate seqs in event_seqs must fail.

        FAILS before fix. PASSES after fix.
        """
        params = {
            **service_params_observed,
            "decision_id": "dec-xyz",
            "event_seqs": [1, 1, 2],  # seq 1 duplicated
        }
        with pytest.raises(ValueError, match="duplicate"):
            await link_evidence(params, "agent-1")


# ══════════════════════════════════════════════════════════════════════════════
# RPC: cstp.mapControls
# ══════════════════════════════════════════════════════════════════════════════

class TestMapControls:
    @pytest.mark.asyncio
    async def test_returns_per_class_coverage(self, service_params_observed):
        result = await map_controls(service_params_observed, "agent-1")
        assert "coverage" in result
        assert "observed" in result["coverage"]
        assert "attested" in result["coverage"]

    @pytest.mark.asyncio
    async def test_no_blended_coverage_in_result(self, service_params_observed):
        """cstp.mapControls must not return a top-level coverage_pct."""
        result = await map_controls(service_params_observed, "agent-1")
        assert "coverage_pct" not in result, (
            "mapControls must not return a blended coverage_pct"
        )

    @pytest.mark.asyncio
    async def test_ie_gap_detected_for_unapproved_merge(self, service_params_observed):
        """PR #2 was merged with no approval — must surface as INSUFFICIENT EVIDENCE."""
        result = await map_controls(service_params_observed, "agent-1")
        ie = result["insufficient_evidence"]
        pr2_ie = [i for i in ie if i.get("pr_number") == 2]
        assert len(pr2_ie) >= 1, "PR #2 (agent, 0 approvals, merged) must produce IE"

    @pytest.mark.asyncio
    async def test_returns_attested_only_controls(self, service_params_observed):
        assert "attested_only_controls" in await map_controls(service_params_observed, "agent-1")


# ══════════════════════════════════════════════════════════════════════════════
# RPC: cstp.exportEvidenceBundle (JSON)
# ══════════════════════════════════════════════════════════════════════════════

class TestExportEvidenceBundle:
    @pytest.mark.asyncio
    async def test_creates_json_file(self, service_params_observed, tmp_path):
        params = {
            **service_params_observed,
            "format": "json",
        }
        result = await export_evidence_bundle(params, "agent-1")
        assert "json_path" in result
        assert Path(result["json_path"]).exists()

    @pytest.mark.asyncio
    async def test_json_bundle_required_fields(self, service_params_observed, tmp_path):
        params = {
            **service_params_observed,
            "format": "json",
        }
        result = await export_evidence_bundle(params, "agent-1")
        bundle = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
        for field in [
            "version", "tool_version", "generated_at", "chain_head_hash",
            "chain_intact", "coverage", "total_events",
            "insufficient_evidence_count", "events", "mappings",
            "insufficient_evidence", "unmapped", "attested_only_controls",
        ]:
            assert field in bundle, f"Missing required field: {field}"

    @pytest.mark.asyncio
    async def test_bundle_has_no_blended_coverage_pct(self, service_params_observed, tmp_path):
        """JSON bundle must not contain a top-level blended coverage_pct."""
        params = {
            **service_params_observed,
            "format": "json",
        }
        result = await export_evidence_bundle(params, "agent-1")
        bundle = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
        assert "coverage_pct" not in bundle, (
            "JSON bundle must not have a blended coverage_pct at the top level"
        )
        assert "observed" in bundle["coverage"]
        assert "attested" in bundle["coverage"]

    @pytest.mark.asyncio
    async def test_chain_intact_true_for_valid_chain(self, service_params_observed, tmp_path):
        params = {
            **service_params_observed,
            "format": "json",
        }
        result = await export_evidence_bundle(params, "agent-1")
        assert result["chain_intact"] is True

    @pytest.mark.asyncio
    async def test_chain_intact_false_after_tamper(self, db_with_observed, tmp_path):
        """Tampered chain must make chain_intact=False (uses stored checkpoint — P2 fix)."""
        params = {
            "format": "json",
        }
        # First export to capture head hash / checkpoint
        await export_evidence_bundle(params, "agent-1")
        # Tamper and re-export
        with closing(sqlite3.connect(db_with_observed)) as conn:
            conn.execute("UPDATE events SET payload_json = '{\"tampered\":true}' WHERE seq = 1")
            conn.commit()
        result = await export_evidence_bundle(params, "agent-1")
        assert result["chain_intact"] is False

    @pytest.mark.asyncio
    async def test_p2_tail_truncation_detected(self, db_with_observed, tmp_path):
        """Tail truncation must be detected via stored checkpoint (P2 fix)."""
        params = {
            "format": "json",
        }
        # First bundle stores checkpoint
        await export_evidence_bundle(params, "agent-1")
        # Delete tail events
        with closing(sqlite3.connect(db_with_observed)) as conn:
            conn.execute("DELETE FROM events WHERE seq >= 4")
            conn.commit()
        # Second bundle: chain appears internally intact, but checkpoint mismatch
        result2 = await export_evidence_bundle(params, "agent-1")
        assert result2["chain_intact"] is False, (
            "P2 fix: tail truncation must be detected via persisted checkpoint"
        )

    @pytest.mark.asyncio
    async def test_valid_extension_stays_intact(self, db_with_observed, tmp_path):
        """Finding 5+6 regression: adding new events after export must not break chain.

        Before fix (:286): verify_chain(db, expected_head_hash=prev_hash) returned 0
        when the current head differed from prev_hash — treating valid growth as tampering.
        After fix: verify_at_checkpoint checks only up to the checkpoint seq, so new events
        do not cause a false positive.
        FAILS before fix. PASSES after fix.
        """
        params = {
            "format": "json",
        }

        # First export — captures checkpoint at seq 5
        r1 = await export_evidence_bundle(params, "agent-1")
        assert r1["chain_intact"] is True

        # Add a new legitimate event (valid chain extension)
        append_event("src", "pr_opened", "observed", {"pr_number": 99},
                     ts="2026-08-01T00:00:00Z", db_path=db_with_observed)

        # Second export: checkpoint at seq 5 still valid, chain extended to seq 6
        r2 = await export_evidence_bundle(params, "agent-1")
        assert r2["chain_intact"] is True, (
            "Finding 5+6: valid chain extension must not be reported as broken — "
            "verify_at_checkpoint allows growth beyond the checkpoint"
        )

    @pytest.mark.asyncio
    async def test_tail_deletion_then_verify_remains_detected(self, db_with_observed, tmp_path):
        """Finding 5+6 regression: tampering must remain detected after re-export (no self-healing).

        Before fix (:282): export stored the NEW (truncated) head as checkpoint even when
        chain_intact=False. The next verify_evidence_chain then used that head as the
        expected hash and reported intact — self-healing the tampered state.
        After fix: checkpoint is only promoted when chain_intact=True.
        FAILS before fix. PASSES after fix.
        """
        params = {
            "format": "json",
        }

        # First export (intact) — stores trusted checkpoint
        r1 = await export_evidence_bundle(params, "agent-1")
        assert r1["chain_intact"] is True

        # Delete tail events (tamper)
        with closing(sqlite3.connect(db_with_observed)) as conn:
            conn.execute("DELETE FROM events WHERE seq >= 4")
            conn.commit()

        # Second export: must detect the tampering
        r2 = await export_evidence_bundle(params, "agent-1")
        assert r2["chain_intact"] is False, "Tail deletion must be detected on re-export"

        # Subsequent verify: must STILL detect tampering — checkpoint was not promoted
        verify_result = await verify_evidence_chain({}, "agent-1")
        assert verify_result["intact"] is False, (
            "Finding 5+6: tampering must remain detected after re-export — "
            "checkpoint must not be self-healed to a tampered state"
        )

    @pytest.mark.asyncio
    async def test_unsupported_format_rejected_before_creating_dirs(self, service_params_observed,
                                                                      bundle_dir):
        """Finding 13: unsupported format must raise ValueError, not create dirs."""
        params = {
            **service_params_observed,
            "format": "jsn",  # typo
        }
        with pytest.raises(ValueError, match="Unsupported bundle format"):
            await export_evidence_bundle(params, "agent-1")
        assert not bundle_dir.exists(), (
            "Finding 13: output_dir must not be created when format is invalid"
        )

    @pytest.mark.asyncio
    async def test_immutable_on_regenerate(self, service_params_observed, tmp_path):
        """Each export creates a new file, never overwrites."""
        import asyncio as aio
        params = {
            **service_params_observed,
            "format": "json",
        }
        r1 = await export_evidence_bundle(params, "agent-1")
        await aio.sleep(1)  # extra guard for ts-based names
        r2 = await export_evidence_bundle(params, "agent-1")
        assert r1["json_path"] != r2["json_path"]
        assert Path(r1["json_path"]).exists()
        assert Path(r2["json_path"]).exists()


class TestCallerCannotChoosePaths:
    """Bundle and rules directories are server configuration, like the DB path.

    Before the fix an authenticated agent could pass output_dir to make the server
    create directories and write bundles anywhere it could reach, or pass an empty
    mappings_dir to get a bundle with zero control mappings that still read clean.
    """

    @pytest.mark.asyncio
    async def test_export_ignores_output_dir(self, service_params_observed, tmp_path,
                                             bundle_dir):
        elsewhere = tmp_path / "elsewhere"
        params = {**service_params_observed, "format": "json", "output_dir": str(elsewhere)}

        result = await export_evidence_bundle(params, "agent-1")

        assert not elsewhere.exists()
        assert Path(result["json_path"]).parent == bundle_dir

    @pytest.mark.asyncio
    async def test_export_ignores_mappings_dir(self, service_params_observed, tmp_path):
        empty_rules = tmp_path / "no-rules"
        empty_rules.mkdir()

        honest = await export_evidence_bundle(
            {**service_params_observed, "format": "json"}, "agent-1"
        )
        spoofed = await export_evidence_bundle(
            {**service_params_observed, "format": "json", "mappings_dir": str(empty_rules)},
            "agent-1",
        )

        assert spoofed["coverage"] == honest["coverage"]
        assert spoofed["coverage"]["observed"]["mapped_events"] > 0

    @pytest.mark.asyncio
    async def test_map_controls_ignores_mappings_dir(self, service_params_observed, tmp_path):
        empty_rules = tmp_path / "no-rules"
        empty_rules.mkdir()

        honest = await map_controls(service_params_observed, "agent-1")
        spoofed = await map_controls(
            {**service_params_observed, "mappings_dir": str(empty_rules)}, "agent-1"
        )

        assert spoofed == honest
        assert honest["coverage"]["observed"]["mapped_events"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# RPC: cstp.verifyEvidenceChain
# ══════════════════════════════════════════════════════════════════════════════

class TestVerifyEvidenceChain:
    @pytest.mark.asyncio
    async def test_intact_chain_returns_intact_true(self, service_params_observed):
        result = await verify_evidence_chain(service_params_observed, "agent-1")
        assert result["intact"] is True
        assert result["broken_at_seq"] is None

    @pytest.mark.asyncio
    async def test_accepts_expected_head_hash(self, service_params_observed):
        head = get_head_hash(service_params_observed["db_path"])
        params = {**service_params_observed, "expected_head_hash": head}
        result = await verify_evidence_chain(params, "agent-1")
        assert result["intact"] is True
        assert result["tail_check"] is True

    @pytest.mark.asyncio
    async def test_wrong_expected_head_hash_returns_broken(self, service_params_observed):
        params = {**service_params_observed, "expected_head_hash": "0" * 64}
        result = await verify_evidence_chain(params, "agent-1")
        assert result["intact"] is False
        assert result["broken_at_seq"] == 0  # tail sentinel

    @pytest.mark.asyncio
    async def test_uses_stored_bundle_head_hash(self, db_with_observed, tmp_path):
        """Without explicit expected_head_hash, uses last bundle checkpoint (P2 fix)."""
        # Generate a bundle to store the checkpoint
        await export_evidence_bundle({"format": "json"}, "agent-1")
        # Delete tail events — truncation should be detectable via stored checkpoint
        with closing(sqlite3.connect(db_with_observed)) as conn:
            conn.execute("DELETE FROM events WHERE seq >= 4")
            conn.commit()
        result = await verify_evidence_chain({}, "agent-1")
        assert result["intact"] is False
        assert result["tail_check"] is True

    @pytest.mark.asyncio
    async def test_tampered_record_returns_broken(self, db_with_observed):
        with closing(sqlite3.connect(db_with_observed)) as conn:
            conn.execute(
                "UPDATE events SET payload_json = '{\"tampered\":true}' WHERE seq = 2"
            )
            conn.commit()
        result = await verify_evidence_chain({}, "agent-1")
        assert result["intact"] is False
        assert result["broken_at_seq"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# Integration: end-to-end workflow
# ══════════════════════════════════════════════════════════════════════════════

class TestEndToEndWorkflow:
    @pytest.mark.asyncio
    async def test_full_workflow(self, tmp_path, monkeypatch):
        """Ingest → Link → MapControls → Export → Verify."""
        db_path = str(tmp_path / "workflow.db")
        init_db(db_path)
        monkeypatch.setattr(_svc, "DEFAULT_PROVENANCE_DB", db_path)

        # 1. Ingest observed events (with human actor signals — finding 8)
        ingest_result = await ingest_evidence({
            "events": [
                {"event_type": "pr_opened", "ts": "2026-01-01T10:00:00Z",
                 "payload": {"pr_number": 10, "is_agent_authored": True, "approval_count": 0}},
                {"event_type": "pr_review_approved", "ts": "2026-01-01T11:00:00Z",
                 "payload": {"pr_number": 10, "reviewer": "bob", "actor_is_human": True}},
                {"event_type": "pr_merged", "ts": "2026-01-01T12:00:00Z",
                 "payload": {"pr_number": 10, "is_agent_authored": True,
                              "approval_count": 1, "human_approval_count": 1}},
            ],
        }, "test-agent")
        assert ingest_result["ingested"] == 3

        # 2. Link a CSTP decision to observed events
        link_result = await link_evidence({
            "decision_id": "dec-workflow-test",
            "event_seqs": [1, 2, 3],
            "has_outcome": True,
            "stakes": "high",
        }, "test-agent")
        assert link_result["linked"] == 3

        # 3. Map controls
        map_result = await map_controls({}, "test-agent")
        assert map_result["coverage"]["observed"]["total_events"] == 3
        assert map_result["coverage"]["attested"]["total_events"] == 1
        assert "coverage_pct" not in map_result  # No blended metric

        # 4. Export bundle
        export_result = await export_evidence_bundle({
            "format": "json",
        }, "test-agent")
        assert export_result["chain_intact"] is True
        bundle = json.loads(Path(export_result["json_path"]).read_text(encoding="utf-8"))
        assert "coverage_pct" not in bundle  # No blended metric in JSON bundle
        assert bundle["coverage"]["observed"]["total_events"] == 3

        # 5. Verify chain
        verify_result = await verify_evidence_chain({}, "test-agent")
        assert verify_result["intact"] is True
