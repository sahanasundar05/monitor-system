# ESP32 Firmware — `esp32_monitor.ino`

Reads two sensor streams and emits newline-delimited JSON events over USB
serial for `backend/serial_reader.py` to ingest.

## Hardware

| Component | Purpose | ESP32 pin |
|---|---|---|
| IR photointerrupter (drop-count) | Detects each drip falling in the chamber | GPIO4 (interrupt) |
| MPU-6050 (6-DOF IMU) | Accelerometer + gyro for tremor sensing | I2C: SDA=GPIO21, SCL=GPIO22 |

## Arduino IDE setup

1. Install the **ESP32** board package (Boards Manager -> search "esp32").
2. Install libraries via Library Manager: `Adafruit MPU6050`, `Adafruit Unified Sensor`.
3. Select your board (e.g. "ESP32 Dev Module") and the correct serial port.
4. Set `PATIENT_ID` and `DEPLOY_EPOCH_OFFSET` in the sketch before flashing
   (see "Time sync" below).
5. Upload.

## Output format

One JSON object per line:

```
{"timestamp":1723900000.123,"sensor_type":"drop_count","value":14.2,"patient_id":"P001","event_id":"esp-00000042"}
{"timestamp":1723900000.323,"sensor_type":"tremor","value":0.82,"patient_id":"P001","event_id":"esp-00000043"}
```

## Time sync

The sketch currently derives `timestamp` from `millis()` plus a fixed
`DEPLOY_EPOCH_OFFSET` set at flash time — good enough for a single-session
demo. For a multi-day or multi-device deployment, sync real Unix time via
NTP (`WiFi.h` + `configTime()`) so timestamps are trustworthy across
devices and reboots; the backend's conflict logic depends entirely on
`timestamp` for correlation, so clock accuracy directly affects decision
quality.

## Calibration & drift correction (bonus)

`computeTremorScore()` normalizes raw IMU motion energy against a fixed
divisor. For real patients this should instead be normalized against a
**per-patient baseline** captured during a short calibration period (e.g.
first 30s after attaching the device, patient at rest): record the average
motion-energy floor, then score subsequent samples as energy relative to
that floor. This absorbs sensor-mounting differences and slow IMU bias
drift without needing an external database — store the baseline as a
constant flashed per-device or entered via serial command at session start.

## No hardware? Use the synthetic generator

`backend/synthetic_data_generator.py` produces realistic JSON event
streams (including duplicates, late events, and malformed events) so the
backend, replay, and conflict-resolution logic can be fully exercised and
demoed without any ESP32 or sensors attached.
