/*
 * esp32_fast_r0.ino — high-speed R0 transient capture for the ASET battery bench
 * ----------------------------------------------------------------------------
 * Method 3 (see docs/method3_fast_r0.md): the GW Instek PEL-3111 generates a sharp
 * current step in Dynamic mode and pulses TRIG OUT at the step. This ESP32 waits for
 * that trigger, then fast-samples two analog channels — battery terminal voltage and
 * the load's IMON current-monitor output — into a buffer for ~1 s, and streams the
 * buffer to the PC over UART. The PC computes the instantaneous ohmic R0 that the
 * ~5 Hz SCPI readback cannot resolve.
 *
 * It ALSO keeps the existing low-rate MLX90614 terminal-temperature role (the ESP32's
 * current job in the project), reported on demand so the PC keeps getting temperature.
 *
 * STATUS: UNTESTED design sketch. Verify pin map, signal levels, and the J1/TRIG OUT
 * pinout against your PEL-3111 before wiring. Calibrate the ADC against the GW Instek
 * MEAS readings (the calibrated reference) — raw ESP32 ADC is noisy and non-linear.
 *
 * WIRING (ADC1 pins only — ADC2 is unusable while Wi-Fi is on):
 *   GPIO34 (ADC1_CH6) <- battery terminal voltage via ~1:5 divider + op-amp buffer (<=3.3V)
 *   GPIO35 (ADC1_CH7) <- PEL IMON (use the 0-1V low range direct, or 0-10V via ~1:4 divider)
 *   GPIO27           <- PEL TRIG OUT (4.5V pulse) via 2:1 divider / level shifter to 3.3V
 *   GND              <- PEL A COM (load negative terminal) — single common ground
 *   I2C (GPIO21/22)  <- MLX90614 (unchanged)
 *
 * SERIAL PROTOCOL (115200 baud, newline-terminated commands from PC):
 *   "ARM\n"   -> arm one capture; on the next TRIG edge, sample then stream:
 *                  header  "#CAP n=<count> fs_us=<avg_dt>"
 *                  rows    "<t_us>,<adc_v>,<adc_i>"  (raw counts; PC applies calibration)
 *                  footer  "#END"
 *   "TEMP\n"  -> "#TEMP <celsius>"   (MLX90614 object temperature)
 *   "RAW\n"   -> "#RAW <adc_v> <adc_i>"  (one instantaneous reading, for 2-point cal)
 *   "PING\n"  -> "#PONG"
 */

#include <Wire.h>
#include <Adafruit_MLX90614.h>   // install via Library Manager

// ---- pin map (verify against your wiring) ---------------------------------
static const int PIN_ADC_V = 34;   // battery voltage (divided + buffered)
static const int PIN_ADC_I = 35;   // PEL IMON
static const int PIN_TRIG  = 27;   // PEL TRIG OUT (level-shifted to 3.3V)

// ---- capture config -------------------------------------------------------
static const uint32_t CAP_SAMPLES   = 4000;   // ~1 s at ~4 kSps (tune to RAM/rate)
static const uint32_t CAP_TIMEOUT_US = 1500000UL;  // give up if no trigger in 1.5 s

// buffers (uint16 keeps RAM modest: 4000*2*2B = 16 KB)
static uint16_t bufV[CAP_SAMPLES];
static uint16_t bufI[CAP_SAMPLES];
static uint32_t bufT[CAP_SAMPLES];

volatile bool g_armed = false;
volatile bool g_triggered = false;

Adafruit_MLX90614 mlx = Adafruit_MLX90614();
bool mlx_ok = false;

void IRAM_ATTR onTrigger() {
  if (g_armed && !g_triggered) g_triggered = true;
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);                 // 0..4095
  analogSetPinAttenuation(PIN_ADC_V, ADC_11db);  // full 0..~3.3V span
  analogSetPinAttenuation(PIN_ADC_I, ADC_11db);
  pinMode(PIN_TRIG, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_TRIG), onTrigger, RISING);

  Wire.begin();
  mlx_ok = mlx.begin();                      // MLX90614 over I2C
  Serial.println("#BOOT esp32_fast_r0 ready");
}

// Sample both channels as fast as analogRead allows (~tens of kSps on ESP32).
void doCapture() {
  uint32_t t0 = micros();
  uint32_t n = 0;
  // tight loop — read both channels, timestamp relative to trigger
  while (n < CAP_SAMPLES) {
    bufT[n] = micros() - t0;
    bufV[n] = analogRead(PIN_ADC_V);
    bufI[n] = analogRead(PIN_ADC_I);
    n++;
  }
  uint32_t span = bufT[n - 1] ? bufT[n - 1] : 1;
  Serial.printf("#CAP n=%lu fs_us=%lu\n", (unsigned long)n,
                (unsigned long)(span / n));
  for (uint32_t k = 0; k < n; k++) {
    Serial.printf("%lu,%u,%u\n", (unsigned long)bufT[k], bufV[k], bufI[k]);
  }
  Serial.println("#END");
}

void armAndWait() {
  g_triggered = false;
  g_armed = true;
  uint32_t t_arm = micros();
  while (!g_triggered) {
    if (micros() - t_arm > CAP_TIMEOUT_US) {
      g_armed = false;
      Serial.println("#ERR no trigger (timeout)");
      return;
    }
  }
  g_armed = false;
  doCapture();
}

void loop() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd == "ARM") {
    armAndWait();
  } else if (cmd == "TEMP") {
    float c = mlx_ok ? mlx.readObjectTempC() : NAN;
    Serial.printf("#TEMP %.2f\n", c);
  } else if (cmd == "RAW") {
    Serial.printf("#RAW %u %u\n", analogRead(PIN_ADC_V), analogRead(PIN_ADC_I));
  } else if (cmd == "PING") {
    Serial.println("#PONG");
  }
}
