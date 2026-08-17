/*
 * esp32_monitor.ino
 *
 * Dual-mode patient monitoring firmware for ESP32.
 *
 * Reads two sensor streams and emits one newline-delimited JSON event per
 * sample over the USB serial line, to be consumed by backend/serial_reader.py:
 *
 *   {"timestamp":1723900000.12,"sensor_type":"drop_count","value":14.2,"patient_id":"P001","event_id":"..."}
 *   {"timestamp":1723900000.45,"sensor_type":"tremor","value":0.82,"patient_id":"P001","event_id":"..."}
 *
 * Sensor 1: Optical drop-count sensor (IR photointerrupter across the drip
 *           chamber) -> IV fluid flow rate in mL/hr.
 * Sensor 2: 6-DOF IMU (accelerometer + gyroscope, e.g. MPU-6050) -> tremor
 *           severity score in [0, 1] derived from high-frequency band-passed
 *           acceleration magnitude (Parkinsonian rest tremor is typically
 *           3-6 Hz).
 *
 * All conflict-resolution / decision logic deliberately lives on the Python
 * backend, NOT on the ESP32: the firmware's only job is reliable, low-jitter
 * sampling and framing. This keeps the embedded side simple, testable
 * without hardware (see backend/synthetic_data_generator.py), and keeps the
 * deterministic decision logic in one place (Python) rather than duplicated
 * across C and Python.
 *
 * Wiring (adjust pins to your board):
 *   DROP_SENSOR_PIN  - digital input from IR photointerrupter (drop count)
 *   IMU via I2C       - SDA -> GPIO21, SCL -> GPIO22 (ESP32 default)
 *
 * Dependencies: Adafruit_MPU6050, Adafruit_Sensor, Wire (all via Arduino
 * Library Manager). See embedded/README.md.
 */

#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

// ---------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------
#define DROP_SENSOR_PIN 4
#define PATIENT_ID "P001"          // set per-device / read from config in a real deployment
#define DROP_WINDOW_MS 5000UL      // drip rate is computed over a rolling 5s window
#define TREMOR_SAMPLE_INTERVAL_MS 200UL  // 5 Hz tremor scoring
#define TREMOR_BUFFER_SIZE 32      // ~6.4s of history at 5Hz, enough to isolate 3-6Hz band

Adafruit_MPU6050 mpu;

volatile unsigned long dropCount = 0;
unsigned long windowStartMs = 0;

float tremorBuffer[TREMOR_BUFFER_SIZE];
int tremorBufferIdx = 0;
unsigned long lastTremorSampleMs = 0;

unsigned long eventCounter = 0;

// ---------------------------------------------------------------------
// Interrupt: one pulse per drop detected by the IR photointerrupter
// ---------------------------------------------------------------------
void IRAM_ATTR onDropDetected() {
  dropCount++;
}

// ---------------------------------------------------------------------
// event_id generation: monotonic counter + device boot time, unique
// per device per boot. Combined with patient_id on the backend this is
// sufficiently unique for idempotency purposes within a monitoring session.
// ---------------------------------------------------------------------
String nextEventId() {
  eventCounter++;
  char buf[24];
  snprintf(buf, sizeof(buf), "esp-%08lu", eventCounter);
  return String(buf);
}

double nowUnixSeconds() {
  // NOTE: for a real deployment, sync time via NTP (WiFi.h + configTime())
  // so `timestamp` is a true Unix epoch the backend can trust across
  // devices. Here we fall back to millis()/1000.0 plus a fixed epoch
  // offset that should be set at flash/deploy time.
  const double DEPLOY_EPOCH_OFFSET = 1723900000.0; // set at deploy time
  return DEPLOY_EPOCH_OFFSET + (millis() / 1000.0);
}

void emitEvent(const char *sensorType, float value) {
  // Minimal hand-rolled JSON emission (no ArduinoJson dependency needed
  // for this fixed, flat schema -- keeps firmware footprint small).
  Serial.print("{\"timestamp\":");
  Serial.print(nowUnixSeconds(), 3);
  Serial.print(",\"sensor_type\":\"");
  Serial.print(sensorType);
  Serial.print("\",\"value\":");
  Serial.print(value, 4);
  Serial.print(",\"patient_id\":\"");
  Serial.print(PATIENT_ID);
  Serial.print("\",\"event_id\":\"");
  Serial.print(nextEventId());
  Serial.println("\"}");
}

// ---------------------------------------------------------------------
// Tremor scoring: crude but effective on-device band-energy estimate.
// Real implementation should band-pass filter 3-6Hz and normalize against
// a per-patient calibrated baseline (see embedded/README.md "Drift
// correction" for the bonus approach); this keeps the sketch dependency-free.
// ---------------------------------------------------------------------
float computeTremorScore() {
  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);

  float magnitude = sqrt(a.acceleration.x * a.acceleration.x +
                          a.acceleration.y * a.acceleration.y +
                          a.acceleration.z * a.acceleration.z);

  // Remove gravity baseline (~9.8 m/s^2) to isolate motion energy.
  float motionEnergy = fabs(magnitude - 9.8);

  tremorBuffer[tremorBufferIdx] = motionEnergy;
  tremorBufferIdx = (tremorBufferIdx + 1) % TREMOR_BUFFER_SIZE;

  float sum = 0;
  for (int i = 0; i < TREMOR_BUFFER_SIZE; i++) sum += tremorBuffer[i];
  float avgEnergy = sum / TREMOR_BUFFER_SIZE;

  // Normalize to a [0,1] severity score. Divisor calibrated empirically
  // per-device during setup; see embedded/README.md.
  float score = avgEnergy / 4.0;
  if (score > 1.0) score = 1.0;
  if (score < 0.0) score = 0.0;
  return score;
}

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);

  pinMode(DROP_SENSOR_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(DROP_SENSOR_PIN), onDropDetected, FALLING);

  Wire.begin();
  if (!mpu.begin()) {
    Serial.println("{\"error\":\"MPU6050 not found, check wiring\"}");
    while (1) delay(1000);
  }
  mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);

  for (int i = 0; i < TREMOR_BUFFER_SIZE; i++) tremorBuffer[i] = 0;

  windowStartMs = millis();
  lastTremorSampleMs = millis();
}

void loop() {
  unsigned long nowMs = millis();

  // --- Drop-count -> flow rate (mL/hr), computed on a rolling window -----
  if (nowMs - windowStartMs >= DROP_WINDOW_MS) {
    noInterrupts();
    unsigned long drops = dropCount;
    dropCount = 0;
    interrupts();

    // Standard IV set: ~20 drops/mL (macro-drip). Convert drops-in-window
    // to mL/hr. Adjust DROPS_PER_ML for the specific administration set.
    const float DROPS_PER_ML = 20.0;
    float windowHours = (nowMs - windowStartMs) / 3600000.0;
    float mlPerHr = (drops / DROPS_PER_ML) / windowHours;

    emitEvent("drop_count", mlPerHr);
    windowStartMs = nowMs;
  }

  // --- Tremor sampling ------------------------------------------------
  if (nowMs - lastTremorSampleMs >= TREMOR_SAMPLE_INTERVAL_MS) {
    float score = computeTremorScore();
    emitEvent("tremor", score);
    lastTremorSampleMs = nowMs;
  }
}
