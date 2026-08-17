"""
test_conflict_engine.py

Automated tests covering (per spec):
  - Conflict detection and resolution (ambiguity, IV-only, tremor-only, none)
  - Replay determinism
  - Idempotency
  - Late event handling
  - Duplicate event handling
  - Missing patient_id / invalid sensor_type / missing value
  - Multi-patient isolation (bonus)

Run with:  cd backend && pytest ../tests -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from audit_log import AuditLog
from conflict_engine import (
    DECISION_IV_ALERT,
    DECISION_NONE,
    DECISION_REJECTED,
    DECISION_TREMOR_ALERT,
    ConflictResolver,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def resolver(tmp_path):
    audit_log = AuditLog(tmp_path / "audit.jsonl")
    return ConflictResolver(audit_log)


def load_fixture(name):
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


def run_fixture(resolver, fixture):
    results = {}
    order = []
    for raw in fixture["events"]:
        decision = resolver.process_raw(raw)
        order.append((decision.event_id, decision.to_dict()))
    return order


# ---------------------------------------------------------------------------
# Conflict detection & resolution
# ---------------------------------------------------------------------------

def test_signal_ambiguity_prioritizes_tremor(resolver):
    fixture = load_fixture("01_signal_ambiguity.json")
    order = run_fixture(resolver, fixture)
    assert order[0][1]["decision"] == DECISION_IV_ALERT
    assert order[1][1]["decision"] == DECISION_TREMOR_ALERT
    assert "ambiguity" in order[1][1]["resolution_logic"]


def test_iv_only_alert(resolver):
    fixture = load_fixture("02_iv_only_alert.json")
    order = run_fixture(resolver, fixture)
    assert order[0][1]["decision"] == DECISION_IV_ALERT


def test_tremor_only_alert(resolver):
    fixture = load_fixture("03_tremor_only_alert.json")
    order = run_fixture(resolver, fixture)
    assert order[0][1]["decision"] == DECISION_TREMOR_ALERT


def test_no_alert_below_thresholds(resolver):
    fixture = load_fixture("04_no_alert.json")
    order = run_fixture(resolver, fixture)
    assert all(d["decision"] == DECISION_NONE for _, d in order)


# ---------------------------------------------------------------------------
# Idempotency / duplicates
# ---------------------------------------------------------------------------

def test_duplicate_event_does_not_create_new_alert(resolver):
    fixture = load_fixture("05_duplicate_event.json")
    order = run_fixture(resolver, fixture)
    first, second = order[0][1], order[1][1]
    assert first["flags"] == []
    assert second["flags"] == ["duplicate"]
    assert first["decision"] == second["decision"]
    assert first["resolution_logic"] == second["resolution_logic"]


def test_reprocessing_same_event_object_is_idempotent(resolver):
    raw = {
        "timestamp": 1000.0, "sensor_type": "drop_count", "value": 20.0,
        "patient_id": "PX", "event_id": "same-id",
    }
    d1 = resolver.process_raw(raw)
    d2 = resolver.process_raw(raw)
    d3 = resolver.process_raw(raw)
    assert d1.decision == d2.decision == d3.decision == DECISION_IV_ALERT
    assert d2.flags == ["duplicate"]
    assert d3.flags == ["duplicate"]


# ---------------------------------------------------------------------------
# Late events
# ---------------------------------------------------------------------------

def test_late_event_is_flagged_but_still_processed(resolver):
    fixture = load_fixture("06_late_event.json")
    order = run_fixture(resolver, fixture)
    decisions_by_id = {eid: d for eid, d in order}
    assert decisions_by_id["evt-050"]["flags"] == []
    assert decisions_by_id["evt-051"]["flags"] == []
    assert decisions_by_id["evt-052"]["flags"] == ["late"]
    # late event must still be processed (not dropped) and produce a decision
    assert decisions_by_id["evt-052"]["decision"] in (
        DECISION_NONE, DECISION_IV_ALERT, DECISION_TREMOR_ALERT
    )


# ---------------------------------------------------------------------------
# Malformed / edge-case inputs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture_name", [
    "07_missing_patient_id.json",
    "08_invalid_sensor_type.json",
    "09_missing_value.json",
])
def test_malformed_events_are_rejected_not_crashed(resolver, fixture_name):
    fixture = load_fixture(fixture_name)
    order = run_fixture(resolver, fixture)
    assert order[0][1]["decision"] == DECISION_REJECTED
    assert order[0][1]["reason"] is not None


def test_missing_event_id_does_not_crash(resolver):
    raw = {"timestamp": 1.0, "sensor_type": "tremor", "value": 0.5, "patient_id": "PZ"}
    decision = resolver.process_raw(raw)
    assert decision.decision == DECISION_REJECTED


# ---------------------------------------------------------------------------
# Multi-patient isolation
# ---------------------------------------------------------------------------

def test_multi_patient_isolation(resolver):
    fixture = load_fixture("10_multi_patient_isolation.json")
    order = run_fixture(resolver, fixture)
    decisions_by_id = {eid: d for eid, d in order}
    assert decisions_by_id["evt-090"]["decision"] == DECISION_TREMOR_ALERT
    assert decisions_by_id["evt-091"]["decision"] == DECISION_IV_ALERT
    # P009's normal drip reading is unaffected by P010's high drip event
    assert decisions_by_id["evt-092"]["patient_id"] == "P009"
    assert decisions_by_id["evt-093"]["patient_id"] == "P010"
    # cross contamination check: no considered_event_id crosses patients
    for eid, d in order:
        pid = d["patient_id"]
        # fetch corresponding raw events for this patient only
        same_patient_ids = {e["event_id"] for e in fixture["events"] if e["patient_id"] == pid}
        assert set(d["considered_event_ids"]).issubset(same_patient_ids)


# ---------------------------------------------------------------------------
# Replay determinism
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture_name", [
    "01_signal_ambiguity.json",
    "02_iv_only_alert.json",
    "03_tremor_only_alert.json",
    "04_no_alert.json",
    "10_multi_patient_isolation.json",
])
def test_replay_is_deterministic(tmp_path, fixture_name):
    fixture = load_fixture(fixture_name)

    def run():
        audit_log = AuditLog(tmp_path / f"replay_{fixture_name}_{id(object())}.jsonl")
        r = ConflictResolver(audit_log)
        return [r.process_raw(raw).to_dict() for raw in fixture["events"]]

    run1 = run()
    run2 = run()

    # strip processed_at / wall-clock noise before comparing (not present on
    # Decision.to_dict(), but keep this future-proof)
    def strip(records):
        return [{k: v for k, v in r.items() if k != "processed_at"} for r in records]

    assert strip(run1) == strip(run2)


def test_replay_matches_expected_fixture_decisions():
    for name in ["01_signal_ambiguity.json", "02_iv_only_alert.json",
                 "03_tremor_only_alert.json", "04_no_alert.json"]:
        fixture = load_fixture(name)
        audit_log = AuditLog(Path("/tmp") / f"test_expected_{name}.jsonl")
        audit_log.clear()
        r = ConflictResolver(audit_log)
        got = [r.process_raw(raw).to_dict() for raw in fixture["events"]]
        expected = fixture["expected"]
        assert len(got) == len(expected)
        for g, e in zip(got, expected):
            assert g["event_id"] == e["event_id"]
            assert g["decision"] == e["decision"], (
                f"{name}: {e['event_id']} expected {e['decision']} got {g['decision']}"
            )


# ---------------------------------------------------------------------------
# Audit log persistence
# ---------------------------------------------------------------------------

def test_audit_log_records_every_decision(tmp_path):
    audit_log = AuditLog(tmp_path / "audit.jsonl")
    r = ConflictResolver(audit_log)
    fixture = load_fixture("01_signal_ambiguity.json")
    for raw in fixture["events"]:
        r.process_raw(raw)
    records = audit_log.read_all()
    assert len(records) == len(fixture["events"])
    for rec in records:
        assert "decision" in rec
        assert "resolution_logic" in rec
        assert "considered_event_ids" in rec
        assert "decision_timestamp" in rec
        assert "processed_at" in rec
