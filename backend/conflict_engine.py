"""
conflict_engine.py

Deterministic, idempotent, replayable conflict resolution for the dual-mode
patient monitoring system (IV drip rate vs. tremor detection).

Design principles (see README for full rationale):

1. DETERMINISM: All windowing/ordering decisions are made using the
   `timestamp` field carried on each event, never wall-clock time. This is
   what makes live processing and POST /replay produce byte-identical
   audit trails for the same input.
2. IDEMPOTENCY: Every event carries a unique `event_id`. Once an event_id
   has been resolved, reprocessing it (live retry, duplicate serial frame,
   replay overlap) returns the cached decision instead of computing / an
   alerting again.
3. AUDITABILITY: Every decision (including "none" and "rejected") is
   written as a structured record naming exactly which input events were
   considered and which rule fired.
4. ISOLATION: All state is keyed by patient_id, so multiple patients never
   influence each other's decisions (bonus requirement).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration / thresholds (spec: Functional Requirements)
# ---------------------------------------------------------------------------

TREMOR_THRESHOLD = 0.7          # tremor severity score
DRIP_THRESHOLD = 10.0           # mL/hr
TEMPORAL_WINDOW_SECONDS = 10.0  # correlation window between sensor streams

VALID_SENSOR_TYPES = {"drop_count", "tremor"}

DECISION_TREMOR_ALERT = "tremor_alert"
DECISION_IV_ALERT = "iv_alert"
DECISION_NONE = "none"
DECISION_REJECTED = "rejected"
DECISION_DUPLICATE = "duplicate"


class ValidationError(Exception):
    """Raised when an incoming event fails schema validation."""


@dataclass
class SensorEvent:
    timestamp: float
    sensor_type: str
    value: float
    patient_id: str
    event_id: str

    @staticmethod
    def from_dict(raw: dict) -> "SensorEvent":
        missing = [
            f for f in ("timestamp", "sensor_type", "value", "patient_id", "event_id")
            if f not in raw or raw[f] in (None, "")
        ]
        if missing:
            raise ValidationError(f"missing required field(s): {', '.join(missing)}")

        sensor_type = raw["sensor_type"]
        if sensor_type not in VALID_SENSOR_TYPES:
            raise ValidationError(f"invalid sensor_type: {sensor_type!r}")

        try:
            timestamp = float(raw["timestamp"])
        except (TypeError, ValueError):
            raise ValidationError(f"invalid timestamp: {raw['timestamp']!r}")

        try:
            value = float(raw["value"])
        except (TypeError, ValueError):
            raise ValidationError(f"invalid value: {raw['value']!r}")

        return SensorEvent(
            timestamp=timestamp,
            sensor_type=sensor_type,
            value=value,
            patient_id=str(raw["patient_id"]),
            event_id=str(raw["event_id"]),
        )

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "sensor_type": self.sensor_type,
            "value": self.value,
            "patient_id": self.patient_id,
            "event_id": self.event_id,
        }


@dataclass
class Decision:
    event_id: str
    patient_id: str
    decision: str
    resolution_logic: str
    decision_timestamp: float          # event-time, for determinism
    considered_event_ids: list = field(default_factory=list)
    flags: list = field(default_factory=list)   # e.g. ["late", "duplicate"]
    reason: Optional[str] = None       # populated for rejected/duplicate

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "patient_id": self.patient_id,
            "decision": self.decision,
            "resolution_logic": self.resolution_logic,
            "decision_timestamp": self.decision_timestamp,
            "considered_event_ids": self.considered_event_ids,
            "flags": self.flags,
            "reason": self.reason,
        }


class ConflictResolver:
    """
    Stateful, per-patient-isolated resolver. All state is held in memory
    plus persisted via an AuditLog instance so it can be rebuilt/replayed.
    """

    def __init__(self, audit_log):
        self.audit_log = audit_log
        # patient_id -> list[SensorEvent], kept sorted by timestamp
        self._buffers: dict[str, list[SensorEvent]] = {}
        # event_id -> Decision (idempotency cache)
        self._processed: dict[str, Decision] = {}
        # highest event timestamp seen per patient, used only for the
        # "late event" *label* -- never for decision logic itself.
        self._high_watermark: dict[str, float] = {}
        self._event_seq = itertools.count()  # stable tiebreaker for equal timestamps

    # -- public API ---------------------------------------------------

    def process_raw(self, raw: dict) -> Decision:
        """Validate + process a raw dict event. Never raises; validation
        failures become a 'rejected' audit record instead."""
        event_id = raw.get("event_id")
        try:
            event = SensorEvent.from_dict(raw)
        except ValidationError as exc:
            decision = Decision(
                event_id=str(event_id) if event_id else f"unknown-{next(self._event_seq)}",
                patient_id=str(raw.get("patient_id") or "unknown"),
                decision=DECISION_REJECTED,
                resolution_logic="schema_validation_failed",
                decision_timestamp=raw.get("timestamp") or 0.0,
                considered_event_ids=[],
                reason=str(exc),
            )
            self.audit_log.record(decision)
            return decision

        return self.process(event)

    def process(self, event: SensorEvent) -> Decision:
        # 1. Idempotency check -------------------------------------------------
        if event.event_id in self._processed:
            original = self._processed[event.event_id]
            dup = Decision(
                event_id=event.event_id,
                patient_id=event.patient_id,
                decision=original.decision,
                resolution_logic=original.resolution_logic,
                decision_timestamp=original.decision_timestamp,
                considered_event_ids=original.considered_event_ids,
                flags=["duplicate"],
                reason=f"event_id already processed; original decision reused",
            )
            self.audit_log.record(dup)
            return dup

        flags = []
        watermark = self._high_watermark.get(event.patient_id, event.timestamp)
        if event.timestamp < watermark:
            flags.append("late")
        self._high_watermark[event.patient_id] = max(watermark, event.timestamp)

        # 2. Insert into per-patient buffer, kept time-ordered -----------------
        buf = self._buffers.setdefault(event.patient_id, [])
        buf.append(event)
        buf.sort(key=lambda e: (e.timestamp, e.event_id))

        # Bound memory: an event can only ever correlate with events within
        # TEMPORAL_WINDOW_SECONDS of the highest timestamp seen so far for
        # this patient, so anything older than that is safe to evict --
        # it can no longer affect any future decision. This keeps buffer
        # size bounded regardless of run length (spec: <=10MB RAM).
        watermark_now = self._high_watermark[event.patient_id]
        cutoff = watermark_now - TEMPORAL_WINDOW_SECONDS
        buf[:] = [e for e in buf if e.timestamp >= cutoff]

        # 3. Find correlated events within the temporal window ------------------
        window_events = [
            e for e in buf
            if abs(e.timestamp - event.timestamp) <= TEMPORAL_WINDOW_SECONDS
        ]

        drip_events = [e for e in window_events if e.sensor_type == "drop_count"]
        tremor_events = [e for e in window_events if e.sensor_type == "tremor"]

        drip_exceeded = any(e.value > DRIP_THRESHOLD for e in drip_events)
        tremor_exceeded = any(e.value > TREMOR_THRESHOLD for e in tremor_events)

        max_drip = max((e.value for e in drip_events), default=None)
        max_tremor = max((e.value for e in tremor_events), default=None)

        # 4. Deterministic rule set (spec: Functional Requirements) -------------
        if tremor_exceeded and drip_exceeded:
            decision_val = DECISION_TREMOR_ALERT
            logic = (
                f"signal_ambiguity: tremor={max_tremor} > {TREMOR_THRESHOLD} and "
                f"drip={max_drip} > {DRIP_THRESHOLD} within {TEMPORAL_WINDOW_SECONDS}s "
                f"-> tremor prioritized as primary signal"
            )
            considered = [e.event_id for e in window_events
                          if e.sensor_type in ("tremor", "drop_count")
                          and (e.value > TREMOR_THRESHOLD if e.sensor_type == "tremor"
                               else e.value > DRIP_THRESHOLD)]
        elif drip_exceeded:
            decision_val = DECISION_IV_ALERT
            logic = f"drip={max_drip} > {DRIP_THRESHOLD}, tremor below threshold or absent"
            considered = [e.event_id for e in drip_events if e.value > DRIP_THRESHOLD]
        elif tremor_exceeded:
            decision_val = DECISION_TREMOR_ALERT
            logic = f"tremor={max_tremor} > {TREMOR_THRESHOLD}, drip below threshold or absent"
            considered = [e.event_id for e in tremor_events if e.value > TREMOR_THRESHOLD]
        else:
            decision_val = DECISION_NONE
            logic = "no threshold exceeded"
            considered = [event.event_id]

        # always ensure the triggering event itself is represented
        if event.event_id not in considered:
            considered.append(event.event_id)
        considered = sorted(set(considered))

        decision = Decision(
            event_id=event.event_id,
            patient_id=event.patient_id,
            decision=decision_val,
            resolution_logic=logic,
            decision_timestamp=event.timestamp,
            considered_event_ids=considered,
            flags=flags,
        )

        self._processed[event.event_id] = decision
        self.audit_log.record(decision)
        return decision

    def reset(self):
        """Used by the replay endpoint to reconstruct decisions from a clean
        state without disturbing the live in-process resolver."""
        self._buffers.clear()
        self._processed.clear()
        self._high_watermark.clear()
