"""
test_app.py

Integration tests for the Flask API: /health, /events, /replay, /decisions.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import app as app_module

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_events_endpoint_single_event(client):
    resp = client.post("/events", json={
        "timestamp": 5000.0, "sensor_type": "tremor", "value": 0.9,
        "patient_id": "PAPI", "event_id": "api-evt-1",
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["decision"] == "tremor_alert"


def test_events_endpoint_rejects_malformed_body(client):
    resp = client.post("/events", data="not json", content_type="text/plain")
    assert resp.status_code == 400


def test_replay_endpoint_reproduces_fixture_decisions(client):
    with open(FIXTURES_DIR / "01_signal_ambiguity.json") as f:
        fixture = json.load(f)

    resp = client.post("/replay", json={"events": fixture["events"]})
    assert resp.status_code == 200
    body = resp.get_json()
    decisions = body["decisions"]

    expected = fixture["expected"]
    assert len(decisions) == len(expected)
    for got, exp in zip(decisions, expected):
        assert got["event_id"] == exp["event_id"]
        assert got["decision"] == exp["decision"]


def test_replay_endpoint_rejects_non_array(client):
    resp = client.post("/replay", json={"events": "not-a-list"})
    assert resp.status_code == 400


def test_replay_is_isolated_from_live_state(client):
    """Replaying events must never mutate the live audit trail / resolver."""
    before = client.get("/decisions").get_json()

    with open(FIXTURES_DIR / "10_multi_patient_isolation.json") as f:
        fixture = json.load(f)
    client.post("/replay", json={"events": fixture["events"]})

    after = client.get("/decisions").get_json()
    assert before == after  # live audit trail untouched by replay
