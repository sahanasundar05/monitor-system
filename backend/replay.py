"""
replay.py

Standalone CLI replay script (does not require the Flask server to be
running). Loads a JSON array of events from a fixture file, replays them
deterministically, and prints/saves the resulting decisions + audit trail.

Usage:
    python replay.py ../fixtures/edge_case_ambiguous_conflict.json
    python replay.py ../fixtures/*.json --out ../audit_logs/manual_replay.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from audit_log import AuditLog
from conflict_engine import ConflictResolver


def load_events(paths: list[str]) -> list[dict]:
    events = []
    for p in paths:
        with open(p) as f:
            data = json.load(f)
        if isinstance(data, dict) and "events" in data:
            data = data["events"]
        if not isinstance(data, list):
            raise ValueError(f"{p} does not contain a JSON array of events")
        events.extend(data)
    return events


def main():
    parser = argparse.ArgumentParser(description="Replay recorded sensor events")
    parser.add_argument("fixtures", nargs="+", help="one or more JSON fixture files")
    parser.add_argument("--out", default=None, help="path to write the replay audit log")
    args = parser.parse_args()

    # Events are processed in the exact order they appear in the fixture
    # file(s) -- this is what lets replay reconstruct the identical audit
    # trail as the original live run (see app.py's /replay docstring for
    # why arrival order, not timestamp order, is preserved).
    events = load_events(args.fixtures)

    out_path = args.out or "../audit_logs/cli_replay.jsonl"
    audit_log = AuditLog(out_path)
    audit_log.clear()
    resolver = ConflictResolver(audit_log)

    decisions = [resolver.process_raw(raw).to_dict() for raw in events]

    print(f"Replayed {len(events)} event(s) -> {len(decisions)} decision(s)")
    for d in decisions:
        print(f"  [{d['patient_id']}] {d['event_id']} -> {d['decision']} "
              f"({d['resolution_logic']}) flags={d['flags']}")
    print(f"Audit trail written to: {out_path}")


if __name__ == "__main__":
    sys.exit(main())
