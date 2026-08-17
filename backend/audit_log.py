"""
audit_log.py

Append-only, file-backed audit trail. No external database is used (per
constraints) -- decisions are persisted as JSON Lines (one JSON object per
line) which is trivially diffable, greppable, and safe for clinical review.

Each line is a fully self-contained audit record: what was decided, why,
which input events were considered, and when. `processed_at` (wall clock)
is recorded separately from `decision_timestamp` (event time) so it never
leaks into deterministic logic -- it exists purely for real-world
observability of ingestion latency.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        if not self.path.exists():
            self.path.touch()

    def record(self, decision) -> None:
        record = decision.to_dict()
        record["processed_at"] = time.time()
        line = json.dumps(record, sort_keys=True)
        with self._lock:
            with open(self.path, "a") as f:
                f.write(line + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def clear(self) -> None:
        with self._lock:
            with open(self.path, "w") as f:
                f.truncate(0)


def new_run_log_path(base_dir: str | Path, prefix: str = "audit") -> Path:
    """Generate a fresh, timestamped audit log path for a replay run so
    replays never clobber the live audit trail."""
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    return base_dir / f"{prefix}_{ts}.jsonl"
