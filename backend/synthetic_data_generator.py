"""
synthetic_data_generator.py (bonus)

Generates synthetic event streams for load testing and edge-case fuzzing:
random mixes of drop_count / tremor events across multiple patients, with
configurable rates of duplicates, late events, and malformed events.

Usage:
    python synthetic_data_generator.py --patients 5 --events 500 \
        --duplicate-rate 0.05 --late-rate 0.05 --malformed-rate 0.02 \
        --out ../fixtures/synthetic_stress.json
"""

from __future__ import annotations

import argparse
import json
import random
import time
import uuid


def gen_event(patient_id: str, base_ts: float) -> dict:
    sensor_type = random.choice(["drop_count", "tremor"])
    if sensor_type == "drop_count":
        value = round(random.uniform(0, 30), 2)
    else:
        value = round(random.uniform(0, 1), 3)
    return {
        "timestamp": round(base_ts, 3),
        "sensor_type": sensor_type,
        "value": value,
        "patient_id": patient_id,
        "event_id": str(uuid.uuid4())[:8],
    }


def malform(event: dict) -> dict:
    choice = random.choice(["missing_patient", "bad_sensor", "missing_value"])
    e = dict(event)
    if choice == "missing_patient":
        e.pop("patient_id", None)
    elif choice == "bad_sensor":
        e["sensor_type"] = "unknown_sensor"
    elif choice == "missing_value":
        e.pop("value", None)
    return e


def generate(n_patients: int, n_events: int, duplicate_rate: float,
             late_rate: float, malformed_rate: float, seed: int | None = None) -> list[dict]:
    if seed is not None:
        random.seed(seed)

    patients = [f"P{100 + i}" for i in range(n_patients)]
    t0 = time.time()
    events: list[dict] = []
    clocks = {p: t0 for p in patients}

    for _ in range(n_events):
        p = random.choice(patients)
        clocks[p] += random.uniform(0.5, 8.0)
        event = gen_event(p, clocks[p])

        if random.random() < malformed_rate:
            event = malform(event)

        events.append(event)

        if random.random() < duplicate_rate:
            events.append(dict(event))  # exact duplicate, same event_id

        if random.random() < late_rate:
            late_event = gen_event(p, clocks[p] - random.uniform(5, 20))
            events.append(late_event)

    return events


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic sensor event streams")
    parser.add_argument("--patients", type=int, default=3)
    parser.add_argument("--events", type=int, default=100)
    parser.add_argument("--duplicate-rate", type=float, default=0.05)
    parser.add_argument("--late-rate", type=float, default=0.05)
    parser.add_argument("--malformed-rate", type=float, default=0.03)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="../fixtures/synthetic_stress.json")
    args = parser.parse_args()

    events = generate(
        args.patients, args.events, args.duplicate_rate,
        args.late_rate, args.malformed_rate, args.seed,
    )

    with open(args.out, "w") as f:
        json.dump({"description": "synthetically generated stress/edge-case stream",
                   "events": events}, f, indent=2)

    print(f"Wrote {len(events)} events to {args.out}")


if __name__ == "__main__":
    main()
