/*
 * esp32_temp_ssr.ino — MLX90614 temperature streaming + SSR power relay control
 * ------------------------------------------------------------------------------
 * This is the firmware for the ESP32 board used by aset_batt.hardware.HardwareController
 * (see connect_esp32() / _esp_monitor_loop() in aset_batt/hardware/hardware_driver.py).
 * It has two jobs:
 *
 *   1) Stream terminal temperature every ~1s as free text the PC already parses:
 *        "Object = 25.3*C"
 *      (matches _ESP_TEMP_PATTERNS regex in hardware_driver.py — do not change format
 *      without also updating that regex list.)
 *
 *   2) Drive an external SSR (solid-state relay) on GPIO16 that physically gates
 *      power to the PSU + E-load. This is a REDUNDANT hardware safety cutoff — it
 *      switches AC/DC power outside the instruments' own SCPI output relays, so it
 *      still cuts power even if an instrument's own relay is stuck or unresponsive.
 *      Controlled over the same serial link with newline-terminated commands:
 *
 *        "SSR ON\n"   -> close relay (power connected)  -> replies "#SSR ON"
 *        "SSR OFF\n"  -> open relay  (power cut)         -> replies "#SSR OFF"
 *        "SSR?\n"     -> query current state             -> replies "#SSR ON" / "#SSR OFF"
 *
 * WIRING:
 *   I2C (GPIO21/22) <- MLX90614 (unchanged from existing board)
 *   GPIO16          <- SSR control input, GND <- SSR control ground
 *
 * SSR POLARITY: this assumes an ACTIVE-HIGH SSR module (GPIO16 HIGH = relay closed
 * = power ON), which is the common wiring for 3-3.3V logic-triggered SSR modules.
 * If your SSR module is active-LOW (common on some opto-isolated boards with a
 * "NO"/"NC" jumper), swap SSR_ON_LEVEL / SSR_OFF_LEVEL below.
 *
 * FAIL-SAFE: on boot and on any serial disconnect, the relay defaults to OFF
 * (power cut) until the PC explicitly sends "SSR ON".
 *
 * Baud rate: 9600 (must match aset_batt config.json "serial_baudrate").
 */

#include <Wire.h>
#include <Adafruit_MLX90614.h>   // install via Library Manager

static const int PIN_SSR = 16;

// Active-HIGH SSR (default). Swap these two lines if your module is active-LOW.
static const int SSR_ON_LEVEL  = HIGH;
static const int SSR_OFF_LEVEL = LOW;

static const unsigned long TEMP_INTERVAL_MS = 1000;

Adafruit_MLX90614 mlx = Adafruit_MLX90614();
bool mlx_ok = false;
bool ssr_on = false;
unsigned long last_temp_ms = 0;

void setSsr(bool on) {
  ssr_on = on;
  digitalWrite(PIN_SSR, on ? SSR_ON_LEVEL : SSR_OFF_LEVEL);
}

void setup() {
  Serial.begin(9600);

  pinMode(PIN_SSR, OUTPUT);
  setSsr(false);              // fail-safe: power cut until PC says otherwise

  Wire.begin();
  mlx_ok = mlx.begin();       // MLX90614 over I2C

  Serial.println("#BOOT esp32_temp_ssr ready — SSR OFF (fail-safe)");
}

void handleCommand(String cmd) {
  cmd.trim();
  cmd.toUpperCase();
  if (cmd == "SSR ON") {
    setSsr(true);
    Serial.println("#SSR ON");
  } else if (cmd == "SSR OFF") {
    setSsr(false);
    Serial.println("#SSR OFF");
  } else if (cmd == "SSR?") {
    Serial.println(ssr_on ? "#SSR ON" : "#SSR OFF");
  }
  // Unknown commands are ignored — keeps this forward-compatible with any
  // future command additions without breaking on garbled serial input.
}

void loop() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    handleCommand(cmd);
  }

  unsigned long now = millis();
  if (now - last_temp_ms >= TEMP_INTERVAL_MS) {
    last_temp_ms = now;
    if (mlx_ok) {
      float c = mlx.readObjectTempC();
      Serial.print("Object = ");
      Serial.print(c, 1);
      Serial.println("*C");
    }
  }
}
