"""
app.py

Flask backend for the dual-mode patient monitoring conflict resolver.

Endpoints
---------
POST /events          Ingest a single live sensor event (used by the serial
                       bridge, or directly for testing without hardware).
POST /replay          Accepts {"events": [...]} (JSON array of raw events),
                       replays them in deterministic order against a FRESH
                       resolver state, and returns the resulting decisions +
                       audit trail. Writes to its own timestamped audit log
                       file so it never mutates the live audit trail.
GET  /decisions        Returns all decisions recorded in the live audit log.
GET  /health            Liveness check.

Only a REST API is used where the MVP explicitly requires it (the /replay
endpoint is called out in the spec). No database, no cloud services.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request

from audit_log import AuditLog, new_run_log_path
from conflict_engine import ConflictResolver

BASE_DIR = Path(__file__).resolve().parent.parent
AUDIT_DIR = BASE_DIR / "audit_logs"
LIVE_AUDIT_PATH = AUDIT_DIR / "live_audit.jsonl"

app = Flask(__name__)

live_audit_log = AuditLog(LIVE_AUDIT_PATH)
live_resolver = ConflictResolver(live_audit_log)


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/events")
def ingest_event():
    """Ingest one live event (mirrors what serial_reader.py does per line)."""
    raw = request.get_json(force=True, silent=True)
    if raw is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    decision = live_resolver.process_raw(raw)
    return jsonify(decision.to_dict())


@app.get("/decisions")
def list_decisions():
    return jsonify(live_audit_log.read_all())


@app.post("/replay")
def replay():
    """
    Body: {"events": [ {timestamp, sensor_type, value, patient_id, event_id}, ... ]}

    Replays events against a brand-new resolver + brand-new audit log file.

    IMPORTANT: events are processed in the exact order given in the array,
    NOT re-sorted by timestamp. This is what makes replay reconstruct the
    *identical* audit trail as the original live run: arrival order drives
    idempotency and "late" flagging, while the correlation window itself
    is computed purely from each event's `timestamp` field. So determinism
    holds either way, but fidelity to the original run requires preserving
    the original arrival order. Callers who want a strict chronological
    reprocessing can pre-sort their own array before submitting.
    """
    body = request.get_json(force=True, silent=True) or {}
    events = body.get("events")
    if not isinstance(events, list):
        return jsonify({"error": "'events' must be a JSON array"}), 400

    run_audit_path = new_run_log_path(AUDIT_DIR, prefix="replay")
    replay_audit_log = AuditLog(run_audit_path)
    replay_resolver = ConflictResolver(replay_audit_log)

    decisions = [replay_resolver.process_raw(raw).to_dict() for raw in events]

    return jsonify({
        "decisions": decisions,
        "audit_log_path": str(run_audit_path.relative_to(BASE_DIR)),
        "event_count": len(events),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
