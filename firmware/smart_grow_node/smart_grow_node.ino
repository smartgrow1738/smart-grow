/*
 * Smart-Grow :: ESP32 sensor node
 *
 * Reads the resistive soil moisture sensor, POSTs it to the backend, and drives the
 * relay ("pump") from the AI decision in the reply. No pump in the demo rig: the relay
 * click, the relay module's own LED and the ESP32's onboard LED show the pump state.
 *
 * Wiring (ESP32 DevKit):
 *   Soil module VCC -> 3V3   (NOT 5V: AO would exceed the ESP32's 3.3V ADC limit)
 *   Soil module GND -> GND
 *   Soil module AO  -> GPIO34 (ADC1 -- ADC2 pins stop working while Wi-Fi is on)
 *   Soil module DO  -> not connected
 *   Relay VCC       -> VIN / 5V
 *   Relay GND       -> GND
 *   Relay IN        -> GPIO26
 *   Onboard LED     =  GPIO2 (built in on most DevKit boards)
 *
 * Board: "ESP32 Dev Module" (Espressif ESP32 board package). No extra libraries needed.
 */

#include <WiFi.h>
#include <HTTPClient.h>

// ---------------- CONFIG: edit these ----------------
const char* WIFI_SSID  = "YOUR_WIFI_NAME";      // 2.4 GHz only
const char* WIFI_PASS  = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL = "http://192.168.0.128:8000/api/v1/telemetry";  // laptop IP (ipconfig)

// Calibration: read SOIL raw values from Serial Monitor, then set these.
const int DRY_RAW = 4095;   // probe in open air
const int WET_RAW = 1500;   // probe in a glass of water

// Many relay modules switch ON when IN is LOW. If yours is ON when it should be OFF, flip this.
const bool RELAY_ACTIVE_LOW = false;

const int SOIL_PIN  = 34;
const int RELAY_PIN = 26;
const int LED_PIN   = 2;

const unsigned long POST_INTERVAL_MS = 5000;  // one reading every 5 s
const unsigned long PUMP_PULSE_MS    = 3000;  // pump runs in short pulses, never latched on
// ----------------------------------------------------

void setPump(bool on) {
  digitalWrite(RELAY_PIN, (on != RELAY_ACTIVE_LOW) ? HIGH : LOW);
  digitalWrite(LED_PIN, on ? HIGH : LOW);
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("Connecting to Wi-Fi '%s'", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {  // ~20 s max
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWi-Fi OK, ESP32 IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\nWi-Fi FAILED (check SSID/password, 2.4 GHz). Retrying next cycle.");
  }
}

int readSoilRaw() {
  long sum = 0;
  for (int i = 0; i < 10; i++) {  // average out ADC noise
    sum += analogRead(SOIL_PIN);
    delay(5);
  }
  return sum / 10;
}

float rawToPercent(int raw) {
  float pct = (float)(DRY_RAW - raw) * 100.0f / (float)(DRY_RAW - WET_RAW);
  return constrain(pct, 0.0f, 100.0f);  // backend rejects anything outside 0-100
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(LED_PIN, OUTPUT);
  setPump(false);  // fail safe: pump off at boot
  analogReadResolution(12);
  Serial.println("\n=== Smart-Grow node starting ===");
  connectWifi();
}

void loop() {
  unsigned long started = millis();

  int raw = readSoilRaw();
  float moisture = rawToPercent(raw);
  Serial.printf("SOIL raw=%d -> %.1f%%  ", raw, moisture);

  bool pumpOn = false;  // anything that goes wrong leaves the pump OFF
  connectWifi();
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.setTimeout(5000);
    http.begin(SERVER_URL);
    http.addHeader("Content-Type", "application/json");

    // No temperature sensor on this rig: the backend assumes room temperature.
    char body[48];
    snprintf(body, sizeof(body), "{\"moisture\": %.1f}", moisture);
    int code = http.POST(body);

    if (code == 201) {
      String reply = http.getString();
      pumpOn = reply.indexOf("\"PUMP_ON\"") >= 0;
      int r = reply.indexOf("\"reason\":\"");
      String reason = (r >= 0) ? reply.substring(r + 10, reply.indexOf('"', r + 10)) : "";
      Serial.printf("-> %s (%s)\n", pumpOn ? "PUMP_ON" : "PUMP_OFF", reason.c_str());
    } else if (code > 0) {
      Serial.printf("-> HTTP %d: %s\n", code, http.getString().c_str());
    } else {
      Serial.printf("-> backend unreachable (%s)\n", http.errorToString(code).c_str());
    }
    http.end();
  }

  if (pumpOn) {
    setPump(true);  // relay clicks, LEDs on
    delay(PUMP_PULSE_MS);
    setPump(false);
  }

  unsigned long elapsed = millis() - started;
  if (elapsed < POST_INTERVAL_MS) delay(POST_INTERVAL_MS - elapsed);
}
