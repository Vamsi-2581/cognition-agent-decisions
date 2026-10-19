"""F055 Decision Provenance & Control Evidence service.

Provides five CSTP methods that turn GitHub PR history and CSTP decision records
into an auditor-facing evidence bundle mapped to SR 11-7 and NIST AI RMF controls.

Two evidence classes are maintained and NEVER blended:
  observed  — third-party GitHub events, hash-chained for tamper-evidence
  attested  — first-party CSTP decision records linked via cstp.linkEvidence

See website/specs/f055-decision-provenance.md for the full design spec.
"""

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .provenance.mapping import (
    MAPPINGS_DIR,
    MappingResult,
    apply_rules,
    load_rules,
)
from .storage.provenance import (
    EVIDENCE_CLASS_ATTESTED,
    EVIDENCE_CLASS_OBSERVED,
    append_event,
    append_events_batch,
    get_head,
    get_head_hash,
    get_last_bundle_checkpoint,
    get_seq_for_hash,
    init_db,
    store_bundle_checkpoint,
    validate_observed_seqs,
    verify_at_checkpoint,
    verify_chain,
)

logger = logging.getLogger("cstp.provenance")

DEFAULT_PROVENANCE_DB = os.environ.get(
    "PROVENANCE_DB",
    str(Path.home() / ".cstp" / "provenance.db"),
)

# Like the DB path, the bundle directory and the control-mapping rules are server
# configuration. Taken from the request, a caller could make the server create
# and write files in any directory it can reach, or point the rules engine at an
# empty directory and get a bundle that maps nothing yet reads as clean.
DEFAULT_BUNDLE_DIR = os.environ.get(
    "PROVENANCE_BUNDLE_DIR",
    str(Path.home() / ".cstp" / "bundles"),
)


def _get_db_path(params: dict[str, Any]) -> str:
    """Return the server-configured provenance DB path.

    The path is determined by the PROVENANCE_DB environment variable or the
    compiled-in default. RPC callers cannot override the path — doing so would
    allow any authenticated agent to redirect reads/writes to an arbitrary file.
    """
    return DEFAULT_PROVENANCE_DB


def _ensure_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)


def _unique_ts_tag() -> str:
    """Generate a unique timestamp tag for bundle filenames (microseconds + UUID)."""
    now = datetime.now(UTC)
    return now.strftime("%Y%m%dT%H%M%S") + f"_{now.microsecond:06d}_{uuid.uuid4().hex[:8]}Z"


# ── cstp.ingestEvidence ───────────────────────────────────────────────────────


async def ingest_evidence(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Ingest observed events into the hash chain.

    Params:
        events   — list of event dicts, each with: event_type, ts, source, payload
        source   — default source label if not specified per-event (default: "external")

    All ingested events receive evidence_class='observed'. The caller cannot override
    this — observed events must come from external systems only.

    The entire batch is validated before any event is committed. If any event is
    invalid, no events are appended (atomic batch semantics).

    Returns:
        ingested      — count of events appended
        head_hash     — hash of the new chain head after ingestion
        first_seq     — sequence number of the first appended event
    """
    db_path = _get_db_path(params)
    _ensure_db(db_path)

    raw_events = params.get("events")
    if not raw_events or not isinstance(raw_events, list):
        raise ValueError("'events' must be a non-empty list")

    default_source = params.get("source", "external")

    # Validate ALL events before committing any (atomic batch — finding 11)
    validated: list[tuple[str, str, str, dict, str | None]] = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            raise ValueError("Each event must be a dict")
        event_type = ev.get("event_type")
        if not event_type:
            raise ValueError("Each event must have 'event_type'")

        payload = ev.get("payload", {})
        # Reject non-dict payloads before they can poison the store (finding 10)
        if not isinstance(payload, dict):
            raise ValueError(
                f"Event payload must be a dict, got: {type(payload).__name__}"
            )

        ts = ev.get("ts")
        source = ev.get("source") or default_source
        validated.append((source, event_type, EVIDENCE_CLASS_OBSERVED, payload, ts))

    # Append atomically in one transaction (finding 11)
    results = append_events_batch(validated, db_path)
    first_seq = results[0][0] if results else None
    head_hash = get_head_hash(db_path)

    return {
        "ingested": len(results),
        "head_hash": head_hash,
        "first_seq": first_seq,
    }


# ── cstp.linkEvidence ─────────────────────────────────────────────────────────


async def link_evidence(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Correlate an existing CSTP decision to observed events.

    This creates an attested evidence event (evidence_class='attested') in the
    provenance store that references the CSTP decision. The attested event is
    separate from the observed hash chain — its integrity derives from the CSTP
    decision store, not a hash chain.

    Params:
        decision_id  — CSTP decision ID to correlate
        event_seqs   — list of observed event sequence numbers being linked
        stakes       — stakes level from the CSTP decision (optional, for mapping)
        has_outcome  — whether the decision has a recorded outcome (optional)

    Returns:
        linked        — count of event seqs linked
        decision_id   — echoed decision ID
        attested_seq  — sequence number of the synthetic attested event created
    """
    db_path = _get_db_path(params)
    _ensure_db(db_path)

    decision_id = params.get("decision_id")
    if not decision_id:
        raise ValueError("'decision_id' is required")

    event_seqs = params.get("event_seqs", [])
    if not isinstance(event_seqs, list):
        raise ValueError("'event_seqs' must be a list")

    # Validate event_seqs: must exist, be observed class, no duplicates (finding 4)
    validate_observed_seqs(event_seqs, db_path)

    stakes = params.get("stakes")
    has_outcome = params.get("has_outcome", False)

    payload: dict[str, Any] = {
        "decision_id": decision_id,
        "event_seqs": event_seqs,
        "has_outcome": bool(has_outcome),
    }
    if stakes:
        payload["stakes"] = stakes

    # The attested synthetic event goes into the events table with evidence_class='attested'.
    # It does NOT extend the observed hash chain — it has its own chain entry using its
    # evidence_class in the preimage, making the class itself tamper-evident.
    ts = datetime.now(UTC).isoformat()
    seq, _ = append_event(
        source=f"cstp:{agent_id}",
        event_type="cstp_decision_linked",
        evidence_class=EVIDENCE_CLASS_ATTESTED,
        payload=payload,
        ts=ts,
        db_path=db_path,
    )

    return {
        "linked": len(event_seqs),
        "decision_id": decision_id,
        "attested_seq": seq,
    }


# ── cstp.mapControls ──────────────────────────────────────────────────────────


def _mapping_result_to_dict(mr: MappingResult) -> dict[str, Any]:
    """Serialise MappingResult to a JSON-safe dict."""
    return {
        "coverage": {
            "observed": {
                "total_events": mr.observed.total_events,
                "mapped_events": mr.observed.mapped_events,
                "unmapped_events": mr.observed.unmapped_events,
                "coverage_pct": mr.observed.coverage_pct,
            },
            "attested": {
                "total_events": mr.attested.total_events,
                "mapped_events": mr.attested.mapped_events,
                "unmapped_events": mr.attested.unmapped_events,
                "coverage_pct": mr.attested.coverage_pct,
            },
        },
        "mappings": mr.mappings,
        "insufficient_evidence": mr.insufficient_evidence,
        "insufficient_evidence_count": mr.total_insufficient_evidence,
        "unmapped": mr.unmapped,
        "attested_only_controls": sorted(mr.attested_only_controls),
    }


async def map_controls(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Run the YAML rules engine and return per-class control mappings.

    Coverage is reported separately for observed and attested evidence.
    There is no single blended coverage_pct.
    Controls satisfied only by attested evidence are flagged attested_only=True.

    Rules always come from the packaged MAPPINGS_DIR; a ``mappings_dir`` param is
    ignored (see DEFAULT_BUNDLE_DIR).

    Returns:
        coverage.observed   — {total_events, mapped_events, unmapped_events, coverage_pct}
        coverage.attested   — same structure for attested class
        mappings            — list of mapping entries, each with evidence_class
        insufficient_evidence  — list of gap entries
        attested_only_controls — control IDs satisfied ONLY by attested evidence
    """
    db_path = _get_db_path(params)
    _ensure_db(db_path)

    events = get_events_from_db(db_path)
    rules = load_rules(MAPPINGS_DIR)
    mr = apply_rules(events, rules)

    return _mapping_result_to_dict(mr)


def get_events_from_db(db_path: str) -> list[dict]:
    """Load all events from the provenance DB (thin wrapper for service use)."""
    from .storage.provenance import get_events
    return get_events(db_path)


# ── cstp.exportEvidenceBundle ─────────────────────────────────────────────────


async def export_evidence_bundle(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Emit the evidence bundle as JSON (and optionally PDF).

    The bundle checkpoint is only promoted after successful chain verification
    to prevent a tampered chain from self-healing on the next export (finding 5+6).
    Verification uses verify_at_checkpoint so legitimate chain growth after the
    previous export does not falsely report the chain broken (finding 5+6).

    Params:
        format        — "json" (default) or "pdf" or "both"

    Bundles are written to the server-configured DEFAULT_BUNDLE_DIR and rules come
    from MAPPINGS_DIR; ``output_dir`` and ``mappings_dir`` params are ignored.

    Returns:
        json_path      — path to JSON bundle file (if format includes json)
        pdf_path       — path to PDF bundle file (if format includes pdf)
        chain_head_hash — head hash at generation time
        chain_intact   — result of checkpoint-based verification
        coverage       — per-class coverage (observed and attested, never blended)
        insufficient_evidence_count — count of control gaps
    """
    db_path = _get_db_path(params)
    _ensure_db(db_path)

    # Validate format before creating directories (finding 13)
    fmt = params.get("format", "json").lower()
    if fmt not in ("json", "pdf", "both"):
        raise ValueError(
            f"Unsupported bundle format: {fmt!r}. Must be 'json', 'pdf', or 'both'."
        )

    output_dir = Path(DEFAULT_BUNDLE_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run mapping engine
    events = get_events_from_db(db_path)
    rules = load_rules(MAPPINGS_DIR)
    mr = apply_rules(events, rules)

    # Checkpoint state machine (findings 5+6):
    #
    # :286 fix — use verify_at_checkpoint so legitimate new events beyond the last
    # checkpoint do NOT cause a false "chain broken" result.
    #
    # :282 fix — only store a new checkpoint after successful verification. If the
    # chain is broken, the checkpoint stays at the last trusted state, preventing
    # the tampered head from being recorded as "trusted" and self-healing on the
    # next verify call.
    prev_checkpoint = get_last_bundle_checkpoint(db_path)
    head = get_head(db_path)
    chain_head = head[1] if head else None
    chain_head_seq = head[0] if head else 0

    if prev_checkpoint:
        cp_seq, cp_hash = prev_checkpoint
        broken_at = verify_at_checkpoint(db_path, cp_seq, cp_hash)
        chain_intact = broken_at is None
    else:
        broken_at = verify_chain(db_path)
        chain_intact = broken_at is None

    # Only promote checkpoint when chain is intact
    if chain_intact and head:
        store_bundle_checkpoint(chain_head_seq, chain_head or "", fmt, db_path)

    generated_at = datetime.now(UTC).isoformat()
    ts_tag = _unique_ts_tag()

    result: dict[str, Any] = {
        "chain_head_hash": chain_head,
        "chain_intact": chain_intact,
        "coverage": _mapping_result_to_dict(mr)["coverage"],
        "insufficient_evidence_count": mr.total_insufficient_evidence,
        "attested_only_controls": sorted(mr.attested_only_controls),
    }

    bundle_data: dict[str, Any] = {
        "version": "1.0",
        "tool_version": "cstp-f055",
        "generated_at": generated_at,
        "chain_head_hash": chain_head,
        "chain_intact": chain_intact,
        "coverage": _mapping_result_to_dict(mr)["coverage"],
        "total_events": len(events),
        "insufficient_evidence_count": mr.total_insufficient_evidence,
        "attested_only_controls": sorted(mr.attested_only_controls),
        "events": [{k: v for k, v in e.items() if k != "payload_json"} for e in events],
        "mappings": mr.mappings,
        "insufficient_evidence": mr.insufficient_evidence,
        "unmapped": mr.unmapped,
    }

    if fmt in ("json", "both"):
        json_path = output_dir / f"bundle_{ts_tag}.json"
        # O_EXCL: fail if file already exists to prevent clobbering immutable bundles
        try:
            with open(json_path, "x", encoding="utf-8") as f:
                f.write(json.dumps(bundle_data, indent=2, sort_keys=False, ensure_ascii=False))
        except FileExistsError:
            ts_tag = _unique_ts_tag()
            json_path = output_dir / f"bundle_{ts_tag}.json"
            with open(json_path, "x", encoding="utf-8") as f:
                f.write(json.dumps(bundle_data, indent=2, sort_keys=False, ensure_ascii=False))
        result["json_path"] = str(json_path)

    if fmt in ("pdf", "both"):
        pdf_path = _generate_pdf(mr, bundle_data, output_dir, ts_tag)
        result["pdf_path"] = str(pdf_path)

    return result


def _generate_pdf(
    mr: MappingResult,
    bundle_data: dict[str, Any],
    output_dir: Path,
    ts_tag: str,
) -> Path:
    """Generate a human-readable PDF evidence bundle. Requires reportlab."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for PDF generation. "
            "Install it with: pip install 'cognition-agent-decisions[pdf]' "
            "or: pip install reportlab"
        ) from exc

    path = output_dir / f"bundle_{ts_tag}.pdf"
    chain_head = bundle_data["chain_head_hash"]
    chain_intact = bundle_data["chain_intact"]

    base = getSampleStyleSheet()

    def style(name: str, **kwargs: Any) -> ParagraphStyle:
        return ParagraphStyle(name, parent=base["Normal"], **kwargs)

    accent = colors.HexColor("#0f3460")
    gap_color = colors.HexColor("#ffdddd")
    header_color = colors.HexColor("#e8eaf6")

    h1 = style("H1", fontSize=18, fontName="Helvetica-Bold", spaceAfter=8,
                textColor=accent)
    h3 = style("H3", fontSize=10, fontName="Helvetica-Bold", spaceAfter=3,
                textColor=colors.HexColor("#0f3460"), spaceBefore=8)
    body = style("Body", fontSize=9, leading=13, spaceAfter=4)
    small = style("Small", fontSize=8, leading=11,
                  textColor=colors.HexColor("#555555"))
    ok_style = style("OK", fontSize=9, leading=13, textColor=colors.HexColor("#006600"))

    def tbl_style(hdr_color: Any = None) -> TableStyle:
        if hdr_color is None:
            hdr_color = header_color
        return TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), hdr_color),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f5f5f5")]),
        ])

    doc = SimpleDocTemplate(
        str(path), pagesize=letter,
        rightMargin=0.75 * inch, leftMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title="CSTP Provenance Evidence Bundle",
        author="Cognition Engines",
    )

    story: list[Any] = []
    gen_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    # Cover
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph("CSTP PROVENANCE", style(
        "Cover", fontSize=24, fontName="Helvetica-Bold",
        textColor=accent, alignment=TA_CENTER)))
    story.append(Paragraph(
        "Agent Decision Evidence Bundle — F055",
        style("SubCover", fontSize=12, alignment=TA_CENTER,
              textColor=colors.HexColor("#555"))))
    story.append(Spacer(1, 0.2 * inch))

    obs = bundle_data["coverage"]["observed"]
    att = bundle_data["coverage"]["attested"]

    cover_data = [
        ["Generated", gen_ts],
        ["Chain integrity", "INTACT" if chain_intact else "BROKEN — do not rely on this bundle"],
        ["Observed events", str(obs["total_events"])],
        ["Observed coverage", f"{obs['coverage_pct']}%  ({obs['mapped_events']}/{obs['total_events']})"],
        ["Attested events", str(att["total_events"])],
        ["Attested coverage", f"{att['coverage_pct']}%  ({att['mapped_events']}/{att['total_events']})"],
        ["INSUFFICIENT EVIDENCE", str(bundle_data["insufficient_evidence_count"]) + " gap(s)"],
        ["Attested-only controls", ", ".join(bundle_data["attested_only_controls"]) or "none"],
    ]
    cover_tbl = Table(cover_data, colWidths=[2.0 * inch, 4.5 * inch])
    cover_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1),
         [colors.white, colors.HexColor("#f5f5f5")]),
        ("TEXTCOLOR", (1, 1), (1, 1),
         colors.HexColor("#006600") if chain_intact else colors.HexColor("#cc0000")),
        ("TEXTCOLOR", (1, 6), (1, 6),
         colors.HexColor("#cc0000") if bundle_data["insufficient_evidence_count"]
         else colors.HexColor("#006600")),
    ]))
    story.append(cover_tbl)
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(
        f"<b>Chain head hash:</b> "
        f"<font name='Courier' size='7'>{chain_head or 'N/A'}</font>",
        small))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        "<b>Two-class evidence model:</b> Observed events (GitHub PRs, third-party) "
        "are hash-chained and tamper-evident. Attested events (CSTP decisions) are "
        "self-reported by the agent and carry lower audit weight. Coverage is reported "
        "separately — a single blended percentage would obscure the strength of each class.",
        body))
    story.append(PageBreak())

    # Section 1: Framework Mappings
    story.append(Paragraph("Section 1: Framework Control Mappings", h1))

    if mr.mappings:
        map_data = [["Framework", "Stage", "Control ID", "Control Name",
                     "Ev. Class", "Status"]]
        seen: set[tuple[str, str]] = set()
        for m in mr.mappings:
            key = (m.get("framework", ""), m.get("function_id", ""))
            if key in seen:
                continue
            seen.add(key)
            label = (m.get("control_name") or "")[:40]
            status = "PARTIAL/CONTESTED" if m.get("stretch") else "OK"
            if m.get("attested_only"):
                status = "ATTESTED-ONLY"
            ev_cls = m.get("evidence_class", "observed")
            map_data.append([
                Paragraph(m.get("framework", ""), small),
                Paragraph(m.get("stage", ""), small),
                Paragraph(m.get("function_id", ""), small),
                Paragraph(label, small),
                Paragraph(ev_cls, small),
                Paragraph(status, small),
            ])
        map_tbl = Table(map_data,
                        colWidths=[1.0 * inch, 1.1 * inch, 0.8 * inch,
                                   2.2 * inch, 0.8 * inch, 1.1 * inch])
        map_tbl.setStyle(tbl_style())
        story.append(map_tbl)
    else:
        story.append(Paragraph("No framework mappings produced.", ok_style))

    story.append(PageBreak())

    # Section 2: INSUFFICIENT EVIDENCE Gaps
    story.append(Paragraph("Section 2: INSUFFICIENT EVIDENCE Gaps", h1))
    if not mr.insufficient_evidence:
        story.append(Paragraph("No INSUFFICIENT EVIDENCE gaps detected.", ok_style))
    else:
        story.append(Paragraph(
            "These control stages could not be evidenced from available data. "
            "Each gap must be resolved before this bundle can be accepted as complete "
            "evidence in a regulatory examination.", body))
        for ie in mr.insufficient_evidence:
            story.append(Paragraph(
                f"Gap: {ie.get('framework')} / {ie.get('function_id')} — "
                f"{ie.get('control_name')} (seq {ie.get('seq')})", h3))
            gap_data = [
                ["Framework", ie.get("framework", "")],
                ["Control", f"{ie.get('stage', '')} / {ie.get('function_id', '')}"],
                ["Control Name", ie.get("control_name", "")],
                ["Evidence Class", ie.get("evidence_class", "observed")],
                ["Reason", (ie.get("reason") or "").strip()],
            ]
            gap_tbl = Table(gap_data, colWidths=[1.5 * inch, 5.0 * inch])
            gap_tbl.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("BACKGROUND", (0, 0), (-1, -1), gap_color),
            ]))
            story.append(gap_tbl)
            story.append(Spacer(1, 0.08 * inch))

    story.append(PageBreak())

    # Section 3: Attested-Only Controls
    story.append(Paragraph("Section 3: Attested-Only Controls", h1))
    story.append(Paragraph(
        "Controls in this section are supported ONLY by attested (self-reported) evidence. "
        "No observed third-party evidence supports these controls. An auditor will discount "
        "these heavily — they require corroboration from an independent evidence source.", body))
    if mr.attested_only_controls:
        ao_data = [["Control ID", "Framework", "Control Name"]]
        seen_ao: set[str] = set()
        for m in mr.mappings:
            fid = m.get("function_id", "")
            if fid in mr.attested_only_controls and fid not in seen_ao:
                seen_ao.add(fid)
                ao_data.append([fid, m.get("framework", ""), m.get("control_name", "")])
        if len(ao_data) > 1:
            ao_tbl = Table(ao_data, colWidths=[1.0 * inch, 1.5 * inch, 4.0 * inch])
            ao_tbl.setStyle(tbl_style(hdr_color=colors.HexColor("#fff3cd")))
            story.append(ao_tbl)
        else:
            story.append(Paragraph("No attested-only controls identified.", ok_style))
    else:
        story.append(Paragraph("No attested-only controls identified.", ok_style))

    story.append(PageBreak())

    # Section 4: Chain Integrity
    story.append(Paragraph("Section 4: Chain Integrity Verification", h1))
    story.append(Paragraph(
        "The observed event store uses a SHA-256 hash chain. Each record's hash covers "
        "its sequence number, timestamp, event type, evidence class, canonical payload, "
        "and the hash of the previous record (as canonical JSON). The checkpoint is "
        "stored at bundle generation time; verification is performed against that "
        "checkpoint to detect tail truncation and tampering.", body))
    chain_data = [
        ["Chain status", "INTACT — all hashes verified" if chain_intact
                        else "BROKEN — records have been tampered with"],
        ["Head hash", chain_head or "N/A"],
        ["Hash algorithm", "SHA-256"],
        ["Preimage format",
         'canonical_json({"seq":N,"ts":T,"event_type":E,"evidence_class":C,"payload":P,"prev_hash":H})'],
    ]
    chain_tbl = Table(chain_data, colWidths=[1.8 * inch, 4.7 * inch])
    chain_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1),
         [colors.white, colors.HexColor("#f5f5f5")]),
        ("TEXTCOLOR", (1, 0), (1, 0),
         colors.HexColor("#006600") if chain_intact else colors.HexColor("#cc0000")),
    ]))
    story.append(chain_tbl)

    doc.build(story)
    return path


# ── cstp.verifyEvidenceChain ──────────────────────────────────────────────────


async def verify_evidence_chain(params: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Verify hash chain integrity.

    Uses verify_at_checkpoint so tail-truncation is detectable while allowing
    the chain to grow beyond the last checkpoint without false positives.

    If the caller supplies expected_head_hash, the seq for that hash is looked
    up in the chain and used as the checkpoint. If the stored bundle checkpoint
    exists (from a previous export), that is used. Otherwise falls back to
    internal-only verification (tail truncation invisible — warn caller via
    tail_check=False in the response).

    Params:
        expected_head_hash — optional; overrides the stored bundle checkpoint

    Returns:
        intact         — True if chain is intact up to the checkpoint
        broken_at_seq  — seq of broken record, or None if intact
        head_hash      — current chain head hash
        expected_hash  — the hash verified against
        tail_check     — True if tail-truncation detection was active
    """
    db_path = _get_db_path(params)
    _ensure_db(db_path)

    caller_hash: str | None = params.get("expected_head_hash")
    tail_check = False
    broken_at: int | None = None
    expected_hash: str | None = None

    if caller_hash:
        # Look up where this hash sits in the chain
        seq = get_seq_for_hash(caller_hash, db_path)
        if seq is None:
            broken_at = 0  # hash not found in current chain
        else:
            broken_at = verify_at_checkpoint(db_path, seq, caller_hash)
        tail_check = True
        expected_hash = caller_hash
    else:
        stored_checkpoint = get_last_bundle_checkpoint(db_path)
        if stored_checkpoint:
            cp_seq, cp_hash = stored_checkpoint
            broken_at = verify_at_checkpoint(db_path, cp_seq, cp_hash)
            tail_check = True
            expected_hash = cp_hash
        else:
            broken_at = verify_chain(db_path)

    head = get_head(db_path)
    head_hash = head[1] if head else None

    return {
        "intact": broken_at is None,
        "broken_at_seq": broken_at,
        "head_hash": head_hash,
        "expected_hash": expected_hash,
        "tail_check": tail_check,
    }
