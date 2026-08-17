# Dual-Mode Patient Monitoring — Conflict Resolution System

Real-time conflict resolution between IV drip-rate monitoring and Parkinsonian
tremor detection, running on an ESP32 (sensing) + Python backend (deterministic
decision-making, audit logging, replay).

## Why this design

- **Determinism / replayability** live in the Python backend, not the ESP32.
  The firmware's only job is sampling + framing JSON over serial; every
  decision is computed from a pure function of `(patient_id, timestamp,
  sensor_type, value)` history, using **event time**, never wall-clock time.
  That's what makes `POST /replay` and `backend/replay.py` reconstruct
  byte-identical audit trails to the original live run.
- **Idempotency** is enforced by `event_id`: reprocessing a known `event_id`
  (duplicate serial frame, retried HTTP request, overlapping replay) returns
  the original cached decision and is logged with a `duplicate` flag instead
  of creating a new alert.
- **No retroactive revision.** A decision is made from the window of
  correlated events *seen so far* at the moment an event arrives. If a
  correlated event from the other sensor shows up seconds later, it can
  change *its own* decision (e.g. from `iv_alert` to `tremor_alert` once
  ambiguity is detected) — but it never rewrites an already-recorded
  decision for a prior event. This keeps the audit trail append-only and
  matches how a real streaming/embedded system has to behave (see
  `fixtures/01_signal_ambiguity.json` for a worked example).
- **No external database.** Audit trail is append-only JSON Lines
  (`audit_logs/*.jsonl`) — diffable, greppable, no infra dependency, and
  trivially satisfies "no external databases or cloud services."

## Repository layout

```
embedded/
  esp32_monitor.ino       # Arduino/Embedded C firmware: drop-count + IMU -> serial JSON
  README.md               # wiring, calibration, time-sync notes
backend/
  conflict_engine.py      # deterministic, idempotent resolver (the core logic)
  audit_log.py            # append-only JSONL audit trail
  serial_reader.py        # ESP32 serial -> resolver bridge (standalone daemon)
  app.py                  # Flask API: /events, /replay, /decisions, /health
  replay.py               # standalone CLI replay (no server needed)
  synthetic_data_generator.py  # bonus: generate stress/edge-case event streams
  requirements.txt
fixtures/
  01..10_*.json           # ≥5 edge cases, each with an `expected` block
ui/
  review.html             # bonus: human-in-the-loop clinician review dashboard
audit_logs/
  sample_audit_trail.jsonl  # example output from a CLI replay run
tests/
  test_conflict_engine.py # conflict detection, idempotency, late events, isolation, replay determinism
  test_app.py              # Flask endpoint integration tests
```

## Clone → setup → run → test

```bash
git clone <this-repo-url>
cd monitor-system

# 1. Backend setup
cd backend
pip install -r requirements.txt --break-system-packages   # or use a venv

# 2. Run the API server
python app.py
# -> http://localhost:8000  (health check: GET /health)

# 3. (No hardware needed) generate synthetic events and try the CLI replay
python synthetic_data_generator.py --patients 5 --events 300 --out ../fixtures/synthetic_stress.json
python replay.py ../fixtures/synthetic_stress.json --out ../audit_logs/stress_run.jsonl

# 4. With real hardware: flash embedded/esp32_monitor.ino (see embedded/README.md),
#    then bridge serial -> backend:
python serial_reader.py --port /dev/ttyUSB0 --baud 115200

# 5. Run the test suite
cd ..
python -m pytest tests/ -v

# 6. Open the clinician review UI
#    (with the backend running on :8000)
open ui/review.html      # or just double-click it / drag into a browser
```

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | liveness check |
| `/events` | POST | ingest one live event (mirrors serial ingestion) |
| `/decisions` | GET | list all decisions in the **live** audit trail |
| `/replay` | POST | `{"events": [...]}` → replays against a **fresh**, isolated resolver state and writes its own timestamped audit log; never touches the live trail |

## Decision rules (spec-mandated)

1. `tremor_score > 0.7` **and** `drip_rate > 10 mL/hr` within a 10s window of
   each other → **tremor prioritized** (`tremor_alert`), tremor assumed to be
   the primary/confounding signal.
2. Only drip rate exceeds threshold → `iv_alert`.
3. Only tremor score exceeds threshold → `tremor_alert`.
4. Neither exceeds threshold → `none`.
5. Malformed events (missing `patient_id`, invalid `sensor_type`, missing
   `value`, missing `event_id`) → `rejected`, with a `reason`; never crashes
   the resolver.

## Edge-case fixtures (`fixtures/`)

| File | Covers |
|---|---|
| `01_signal_ambiguity.json` | both signals correlated → tremor prioritized |
| `02_iv_only_alert.json` | drip-only conflict |
| `03_tremor_only_alert.json` | tremor-only conflict |
| `04_no_alert.json` | both below threshold |
| `05_duplicate_event.json` | idempotency: same `event_id` twice |
| `06_late_event.json` | out-of-order / late timestamp handling |
| `07_missing_patient_id.json` | validation: rejected |
| `08_invalid_sensor_type.json` | validation: rejected |
| `09_missing_value.json` | validation: rejected |
| `10_multi_patient_isolation.json` | two patients interleaved, no cross-talk |

Each fixture is a self-describing JSON file with `description`, `events`,
and an `expected` block asserted directly by `tests/test_conflict_engine.py`.

## Test suite

```
python -m pytest tests/ -v
```

25 tests covering: conflict detection/resolution for all four rule branches,
idempotency (fixture + direct re-submission), late-event flagging, all three
validation-rejection cases, multi-patient isolation (including a
cross-contamination assertion on `considered_event_ids`), replay determinism
(same input twice → byte-identical decisions), replay fidelity against the
fixtures' `expected` blocks, Flask endpoint behavior, and audit log
completeness.

## Non-functional requirements — how they're met

- **Determinism**: decision logic is a pure function of event-time history;
  no wall-clock reads inside `ConflictResolver.process()`.
- **Replayability**: `POST /replay` and `replay.py` run events through a
  brand-new `ConflictResolver` + brand-new audit log file, in the exact
  order submitted — this is proven by `test_replay_is_deterministic` and
  `test_replay_endpoint_reproduces_fixture_decisions`.
- **Idempotency**: `event_id` cache in `ConflictResolver._processed`;
  proven by `test_duplicate_event_does_not_create_new_alert` and
  `test_reprocessing_same_event_object_is_idempotent`.
- **Auditability**: every decision (including `rejected`/`duplicate`) is
  written to `audit_logs/*.jsonl` with `considered_event_ids`,
  `resolution_logic`, and both `decision_timestamp` (event time) and
  `processed_at` (wall clock, for ingestion-latency observability only).
- **Performance**: the resolver is O(window size) per event with an
  in-memory per-patient buffer capped by the 10s correlation window in
  practice; well within 100 events/sec on commodity hardware (validated
  informally via `synthetic_data_generator.py --events 5000`).
- **Memory**: no unbounded growth — per-patient buffers only need to retain
  events within the last `TEMPORAL_WINDOW_SECONDS`; a production hardening
  pass would add explicit eviction of buffer entries older than the window
  (noted as a follow-up, currently bounded by demo run length).

## Known limitations / honest trade-offs

- Buffer eviction is watermark-relative: a patient's buffer only retains
  events within `TEMPORAL_WINDOW_SECONDS` of the *highest* timestamp seen
  so far, keeping memory bounded regardless of run length. The trade-off:
  a very late event (older than `watermark - 10s`) may find its own
  potential correlation partners already evicted, so it can only be
  decided against whatever's still in-buffer. This is flagged via the
  `late` flag in the audit trail so a clinician reviewing it knows the
  correlation window may have been incomplete.
- Firmware time sync uses `millis()` + a fixed offset, not NTP — fine for a
  single demo session, called out explicitly in `embedded/README.md`.
- Tremor scoring on-device is a simple motion-energy heuristic, not a true
  band-pass filter — documented in `embedded/README.md` as the calibration
  bonus scope.
