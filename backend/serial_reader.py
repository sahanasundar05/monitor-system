"""
serial_reader.py

Reads newline-delimited JSON events from the ESP32 over a serial port and
feeds them into a ConflictResolver. The ESP32 firmware (embedded/esp32_monitor.ino)
prints one JSON object per line for every drop-count or IMU tremor sample,
e.g.:

    {"timestamp":1723900000.12,"sensor_type":"drop_count","value":14.2,"patient_id":"P001","event_id":"a1b2c3"}

This module is intentionally decoupled from Flask so it can run as a
standalone ingestion daemon (`python serial_reader.py --port /dev/ttyUSB0`)
or be imported and driven in-process by app.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from audit_log import AuditLog
from conflict_engine import ConflictResolver

try:
    import serial  # pyserial
except ImportError:  # pyserial is optional until real hardware is attached
    serial = None


def handle_line(resolver: ConflictResolver, line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        print(f"[serial_reader] dropping malformed line: {line!r}", file=sys.stderr)
        return None
    decision = resolver.process_raw(raw)
    return decision.to_dict()


def run(port: str, baud: int, audit_path: str):
    if serial is None:
        raise RuntimeError(
            "pyserial is not installed. Run: pip install pyserial --break-system-packages"
        )
    audit_log = AuditLog(audit_path)
    resolver = ConflictResolver(audit_log)

    with serial.Serial(port, baud, timeout=1) as ser:
        print(f"[serial_reader] listening on {port} @ {baud} baud")
        while True:
            raw_line = ser.readline()
            if not raw_line:
                continue
            decision = handle_line(resolver, raw_line.decode("utf-8", errors="replace"))
            if decision:
                print(f"[serial_reader] {decision}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ESP32 serial event ingestion")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--audit-path", default="../audit_logs/live_audit.jsonl")
    args = parser.parse_args()
    run(args.port, args.baud, args.audit_path)
