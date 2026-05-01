/*
 * CT TestBench LED Controller v1.2.0
 * Drives a WS2812B LED strip showing miner status per rack slot.
 *
 * Hardware: Heltec WiFi LoRa 32 V3 (ESP32-S3) or any ESP32 DevKit
 * LED Strip: WS2812B, DATA_PIN GPIO 5 (change as needed)
 * LEDs per slot: 8 (configurable via web UI)
 *
 * OTA: Uses /ota/check against splice-and-connect OTA server (cthome :8081)
 * API: Polls testbench server every 5s for miner status
 *
 * Colour scheme:
 *   PASS (healthy)     = solid green
 *   FAIL               = solid red
 *   WARN / testing     = solid orange
 *   OFFLINE / unknown  = dim blue
 *   HASHBOARD partial  = per-HB: green good / orange partial / red dead
 *   IDLE (no miner)    = off
 *
 * LED positions per slot (8 LEDs):
 *   [0]     = status pixel  (bright, shows overall)
 *   [1-3]   = hashboard HB1/HB2/HB3
 *   [4]     = divider (off)
 *   [5-7]   = spare / fan / psu indicators
 */

#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoJson.h>
#include <FastLED.h>
#include <HTTPClient.h>
#include <Update.h>
#include <heltec_unofficial.h>
#include <Preferences.h>

// ── Version ─────────────────────────────────────────────────────────────────
#define FW_VERSION   "1.7.0"
#define DEVICE_ID    "bench-led-01"
#define FW_NAME      "bench_led_v1_0_0"

// ── WiFi ─────────────────────────────────────────────────────────────────────
#define WIFI_SSID    "ctiot"
#define WIFI_PASS    "iotconnect"

// ── LED ──────────────────────────────────────────────────────────────────────
#define DATA_PIN     2
#define MAX_LEDS     200
#define LEDS_PER_SLOT 8
#define MAX_SLOTS    24

// ── Servers ──────────────────────────────────────────────────────────────────
// All configured via Preferences (persistent) — defaults shown
const char* DEFAULT_BENCH_HOST  = "http://10.0.0.133:8000";  // Pi testbench
const char* DEFAULT_OTA_HOST    = "http://10.0.0.248:8081";  // cthome OTA server
const int   POLL_INTERVAL_MS    = 5000;
const int   OTA_CHECK_INTERVAL  = 60000;  // check OTA every 60s

// ── LED Strip ────────────────────────────────────────────────────────────────
CRGB leds[MAX_LEDS];
int  numLeds   = 128;          // default; set via /api/led/config
int  numSlots  = 16;           // default

// ── Slot → miner mapping ─────────────────────────────────────────────────────
// slotMap[i] = miner_id or -1 for empty. Configured from testbench API or web UI.
int slotMap[MAX_SLOTS];
int slotStatus[MAX_SLOTS];  // 0=off 1=pass 2=fail 3=warn 4=testing 5=offline
uint8_t hbStatus[MAX_SLOTS][3]; // per-slot per-HB: 0=dead 1=low 2=good

// ── Colours ──────────────────────────────────────────────────────────────────
#define C_OFF        CRGB(0,0,0)
#define C_PASS       CRGB(0,200,0)
#define C_FAIL       CRGB(200,0,0)
#define C_WARN       CRGB(200,80,0)
#define C_TESTING    CRGB(0,80,200)
#define C_OFFLINE    CRGB(0,0,40)
#define C_HB_GOOD    CRGB(0,160,0)
#define C_HB_LOW     CRGB(180,60,0)
#define C_HB_DEAD    CRGB(160,0,0)
// C_IDLE is runtime-configurable — see idleColor below

// ── Preferences / config ─────────────────────────────────────────────────────
Preferences prefs;
String benchHost;
String otaHost;
int  brightness = 80;
CRGB idleColor  = CRGB(0, 0, 0);   // default off; user-configurable

// ── Display ──────────────────────────────────────────────────────────────────
unsigned long lastDisplayUpdate = 0;
int           displayPage       = 0;   // 0=status 1=network 2=slots
const int     DISPLAY_CYCLE_MS  = 4000;
bool          displayOn         = true;

// ── Flash override ───────────────────────────────────────────────────────────
struct FlashOverride {
  bool    active;
  int     slot;
  CRGB    color;
  uint32_t endMs;
};
FlashOverride flashOvr = {false, -1, CRGB::Black, 0};

// Per-slot color override (yellow = scanning, restore = back to normal)
CRGB slotColorOvr[MAX_SLOTS];
bool slotColorOvrActive[MAX_SLOTS];

// ── WiFi/server connectivity state ───────────────────────────────────────────
bool serverReachable    = false;
unsigned long lastServerOK = 0;
int  statusScrollPos    = 0;
unsigned long lastStatusScroll = 0;
#define STATUS_SCROLL_MS  120   // ms per step for scrolling status LED

// ── Timing ───────────────────────────────────────────────────────────────────
unsigned long lastPoll = 0;
unsigned long lastOTA  = 0;

// ── Web server for config / LED sync ─────────────────────────────────────────
#include <WebServer.h>
WebServer server(80);

// ── Prototypes ───────────────────────────────────────────────────────────────
void connectWiFi();
void pollTestbench();
void updateLEDs();
void checkOTA();
void handleRoot();
void handleConfig();
void handleStatus();
void handleSetSlot();
void handleBrightness();
void handleIdleColor();
void handleOTACheck();
void updateDisplay();
void handleDisplayToggle();
void handleFlash();
void handleSlotColor();
String getStatusJSON();

// ─────────────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.printf("\n=== CT Bench LED Controller %s ===\n", FW_VERSION);

  // Init Heltec display — V3 needs Vext rail raised BEFORE heltec_setup()
  heltec_ve(true);       // power OLED Vext rail (GPIO36 on V3)
  delay(50);             // wait for rail to stabilise
  heltec_setup();        // initialises Wire, SSD1306

  // Hard-reset the display controller
  display.resetDisplay();
  delay(20);
  display.init();
  display.setFont(ArialMT_Plain_10);
  display.flipScreenVertically();
  display.clear();
  display.drawString(0,  0, "CT Bench LED");
  display.drawString(0, 12, "v" FW_VERSION);
  display.drawString(0, 24, "Booting...");
  display.display();

  // Init LED strip — low brightness at boot to limit inrush current
  // Strip MUST be powered from external 5V, not ESP 3V3/USB pin
  FastLED.addLeds<WS2812B, DATA_PIN, GRB>(leds, MAX_LEDS);
  FastLED.setBrightness(20);   // low during boot, ramped after WiFi
  fill_solid(leds, MAX_LEDS, C_OFF);
  FastLED.show();
  delay(100);
  // Single-pixel boot indicator — minimal current draw
  leds[0] = C_WARN;
  FastLED.show();
  delay(200);
  leds[0] = C_OFF;
  FastLED.show();

  // Load config from NVS
  prefs.begin("led-ctrl", false);
  benchHost         = prefs.getString("benchHost",  DEFAULT_BENCH_HOST);
  otaHost           = prefs.getString("otaHost",    DEFAULT_OTA_HOST);
  brightness        = prefs.getInt("brightness",    80);
  numSlots          = prefs.getInt("numSlots",      16);
  numLeds           = prefs.getInt("numLeds",       128);
  idleColor.r       = prefs.getInt("idleR", 0);
  idleColor.g       = prefs.getInt("idleG", 0);
  idleColor.b       = prefs.getInt("idleB", 0);
  // Load slot map from prefs
  for (int i = 0; i < MAX_SLOTS; i++) {
    char key[12]; snprintf(key, sizeof(key), "slot%d", i);
    slotMap[i] = prefs.getInt(key, -1);
    slotStatus[i] = 5;  // offline until polled
  }
  prefs.end();

  // brightness ramp happens in connectWiFi after link up

  connectWiFi();

  // Initial display update now that WiFi is up
  updateDisplay();

  // Register HTTP handlers
  server.on("/",           HTTP_GET,  handleRoot);
  server.on("/config",     HTTP_POST, handleConfig);
  server.on("/status",     HTTP_GET,  handleStatus);
  server.on("/slot",       HTTP_POST, handleSetSlot);
  server.on("/brightness", HTTP_POST, handleBrightness);
  server.on("/brightness", HTTP_GET,  handleBrightness);
  server.on("/idle_color", HTTP_POST, handleIdleColor);
  server.on("/ota_check",  HTTP_GET,  handleOTACheck);
  server.on("/ota_check",  HTTP_POST, handleOTACheck);
  server.on("/display",    HTTP_POST, handleDisplayToggle);
  server.on("/flash",      HTTP_POST, handleFlash);
  server.on("/slot_color", HTTP_POST, handleSlotColor);
  server.begin();

  Serial.printf("HTTP server on :80  IP: %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("Bench: %s  OTA: %s\n", benchHost.c_str(), otaHost.c_str());
  Serial.printf("Slots: %d  LEDs: %d  Brightness: %d\n", numSlots, numLeds, brightness);
}

// ─────────────────────────────────────────────────────────────────────────────
void loop() {
  heltec_loop();
  server.handleClient();

  unsigned long now = millis();
  if (now - lastDisplayUpdate > DISPLAY_CYCLE_MS) {
    lastDisplayUpdate = now;
    updateDisplay();
  }

  if (!WiFi.isConnected()) {
    connectWiFi();
    return;
  }

  if (now - lastPoll > POLL_INTERVAL_MS) {
    lastPoll = now;
    pollTestbench();
    updateLEDs();
  }

  if (now - lastOTA > OTA_CHECK_INTERVAL) {
    lastOTA = now;
    checkOTA();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
void connectWiFi() {
  if (WiFi.isConnected()) return;
  Serial.printf("Connecting to %s ", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int tries = 0;
  while (!WiFi.isConnected() && tries < 40) {
    delay(500); Serial.print("."); tries++;
    // Flash red during connect
    for (int i = 0; i < 3; i++) leds[i] = (tries % 2 == 0) ? C_FAIL : C_OFF;
    FastLED.show();
  }
  if (WiFi.isConnected()) {
    Serial.printf("\nWiFi OK: %s\n", WiFi.localIP().toString().c_str());
    serverReachable = false;
    FastLED.setBrightness(brightness);  // ramp to full brightness now strip PSU is stable
    display.clear();
    display.drawString(0,  0, "WiFi OK");
    display.drawString(0, 12, WiFi.localIP().toString());
    display.drawString(0, 24, String(DEVICE_ID));
    display.display();
    for (int i = 0; i < 3; i++) leds[i] = C_PASS;
    FastLED.show(); delay(500);
    for (int i = 0; i < 3; i++) leds[i] = C_OFF;
    FastLED.show();
  } else {
    Serial.println("\nWiFi FAILED — retrying in 10s");
    delay(10000);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
void pollTestbench() {
  if (!WiFi.isConnected()) return;
  HTTPClient http;
  String url = benchHost + "/api/miners";
  http.begin(url);
  http.setTimeout(4000);
  int code = http.GET();
  if (code != 200) {
    Serial.printf("Poll %s → %d\n", url.c_str(), code);
    http.end();
    serverReachable = false;
    return;
  }
  serverReachable = true;
  lastServerOK = millis();
  String body = http.getString();
  http.end();

  // Parse JSON array of miners
  DynamicJsonDocument doc(8192);
  if (deserializeJson(doc, body)) return;
  JsonArray arr = doc.as<JsonArray>();

  // Reset all to offline
  for (int i = 0; i < MAX_SLOTS; i++) slotStatus[i] = 5;
  memset(hbStatus, 0, sizeof(hbStatus));

  for (JsonObject m : arr) {
    int mid      = m["id"] | -1;
    const char* loc = m["location"] | "";
    const char* st  = m["status"]   | "";
    float hr        = m["rt_hashrate"] | 0.0f;
    const char* lr  = m["last_test_result"] | "";

    // Find which slot this miner belongs to
    int slotIdx = -1;
    for (int i = 0; i < numSlots; i++) {
      if (slotMap[i] == mid) { slotIdx = i; break; }
    }
    // Also try matching by location label against our BENCH_LAYOUT order
    if (slotIdx < 0 && strlen(loc) > 0) {
      // slot index by location: A=0-3, B=4-6, C=7-10, D=11-14
      slotIdx = locationToSlotIdx(loc);
    }
    if (slotIdx < 0 || slotIdx >= numSlots) continue;

    // Determine status
    String status = String(lr).length() > 0 ? String(lr) : String(st);
    if (hr > 50)                                   slotStatus[slotIdx] = 1;  // pass
    else if (status == "pass" || status == "ready_to_deploy") slotStatus[slotIdx] = 1;
    else if (status == "fail" || status == "needs_repair")    slotStatus[slotIdx] = 2;
    else if (status == "testing")                             slotStatus[slotIdx] = 4;
    else if (hr > 0 && hr < 80)                               slotStatus[slotIdx] = 3;  // warn/underperforming
    else                                                       slotStatus[slotIdx] = 5;  // offline

    // Hashboard status
    JsonArray hbs = m["hashboards"].as<JsonArray>();
    int hi = 0;
    for (JsonObject hb : hbs) {
      if (hi >= 3) break;
      float pct = hb["health_pct"] | 0.0f;
      int chips = hb["chip_count"] | 0;
      int exp   = hb["expected_chips"] | 144;
      if (pct >= 90 && chips >= exp * 0.95) hbStatus[slotIdx][hi] = 2;      // good
      else if (pct >= 50)                   hbStatus[slotIdx][hi] = 1;      // partial
      else                                  hbStatus[slotIdx][hi] = 0;      // dead
      hi++;
    }
    // If no HB data but miner is passing, assume all good
    if (hi == 0 && slotStatus[slotIdx] == 1) {
      hbStatus[slotIdx][0] = hbStatus[slotIdx][1] = hbStatus[slotIdx][2] = 2;
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Map location string (e.g. "AL1-4") to slot index
int locationToSlotIdx(const char* loc) {
  if (!loc || strlen(loc) < 4) return -1;
  // Layout order: A=0-3, B=4-6, C=7-10, D=11-14
  // Within rack: slot numbers from label
  char rack = toupper(loc[0]);
  int n = atoi(strchr(loc, '-') ? strchr(loc, '-') + 1 : "0");
  int base = 0;
  if      (rack == 'A') base = 0;
  else if (rack == 'B') base = 4;
  else if (rack == 'C') base = 7;
  else if (rack == 'D') base = 11;
  else return -1;
  return base + (n - 1);
}

// ─────────────────────────────────────────────────────────────────────────────
void updateLEDs() {
  fill_solid(leds, numLeds, C_OFF);

  // Expire flash override
  if (flashOvr.active && millis() > flashOvr.endMs) {
    flashOvr.active = false;
  }

  // ── Status indicator when no active miners ──────────────────────────────
  // LED index (numSlots * LEDS_PER_SLOT) = first LED after all slots
  // We repurpose the LAST LED in the strip as a status pixel
  int statusLed = numLeds - 1;
  if (statusLed >= 0) {
    bool anyActive = false;
    for (int s = 0; s < numSlots; s++) {
      if (slotStatus[s] <= 3) { anyActive = true; break; }  // 0-3 = pass/fail/warn/testing
    }
    if (!WiFi.isConnected()) {
      // Scrolling red — no WiFi
      leds[statusLed] = (millis() % 600 < 300) ? C_FAIL : C_OFF;
    } else if (!serverReachable || (millis() - lastServerOK > 15000)) {
      // Scrolling yellow — WiFi OK but no server
      leds[statusLed] = (millis() % 600 < 300) ? C_WARN : C_OFF;
    } else {
      // Solid green — connected and polling
      leds[statusLed] = anyActive ? C_OFF : C_PASS;
    }
  }

  for (int slot = 0; slot < numSlots && slot < MAX_SLOTS; slot++) {
    int base = slot * LEDS_PER_SLOT;
    if (base + LEDS_PER_SLOT > numLeds) break;

    int st = slotStatus[slot];
    CRGB slotColor;
    switch (st) {
      case 1: slotColor = C_PASS;    break;
      case 2: slotColor = C_FAIL;    break;
      case 3: slotColor = C_WARN;    break;
      case 4: slotColor = C_TESTING; break;
      case 5: slotColor = C_OFFLINE; break;
      default: slotColor = idleColor; break;
    }

    // Flash override — whole slot blinks a single colour
    if (flashOvr.active && flashOvr.slot == slot) {
      CRGB fc = flashOvr.color;
      for (int j = 0; j < LEDS_PER_SLOT; j++) leds[base + j] = fc;
      continue;
    }
    // Per-slot colour override (e.g. yellow = awaiting scan)
    if (slotColorOvrActive[slot]) {
      for (int j = 0; j < LEDS_PER_SLOT; j++) leds[base + j] = slotColorOvr[slot];
      continue;
    }

    // LED 0 = status pixel (bright, overall status)
    leds[base + 0] = slotColor;

    // LEDs 1–3 = hashboard pixels
    for (int h = 0; h < 3; h++) {
      CRGB hc;
      switch (hbStatus[slot][h]) {
        case 2: hc = C_HB_GOOD; break;
        case 1: hc = C_HB_LOW;  break;
        case 0:
          // If slot is occupied (any status except offline/idle), dead HB = red
          hc = (st >= 1 && st <= 4) ? C_HB_DEAD : C_OFF;
          break;
      }
      leds[base + 1 + h] = hc;
    }

    // LED 4 = divider, off
    leds[base + 4] = C_OFF;

    // LEDs 5–7: spare (dim status echo)
    leds[base + 5] = leds[base + 6] = leds[base + 7] = (st == 0) ? idleColor :
      CRGB(slotColor.r / 5, slotColor.g / 5, slotColor.b / 5);
  }

  FastLED.show();
}

// ─────────────────────────────────────────────────────────────────────────────
// OTA update check
void checkOTA() {
  if (!WiFi.isConnected()) return;
  Serial.println("Checking OTA...");
  HTTPClient http;
  String url = otaHost + "/ota/check?device_id=" + DEVICE_ID + "&version=" + FW_VERSION + "&fw_name=" + FW_NAME;
  http.begin(url);
  http.setTimeout(5000);
  int code = http.GET();
  if (code != 200) { http.end(); return; }
  String body = http.getString();
  http.end();

  DynamicJsonDocument doc(512);
  if (deserializeJson(doc, body)) return;
  if (!doc["update_available"].as<bool>()) {
    Serial.println("OTA: up to date");
    return;
  }

  String dlUrl = doc["url"].as<String>();
  if (dlUrl.startsWith("/")) dlUrl = otaHost + dlUrl;
  Serial.printf("OTA: new firmware at %s\n", dlUrl.c_str());

  // Signal update on LEDs
  for (int i = 0; i < numLeds; i++) leds[i] = C_TESTING;
  FastLED.show();

  HTTPClient dlHttp;
  dlHttp.begin(dlUrl);
  dlHttp.setTimeout(30000);
  int dlCode = dlHttp.GET();
  if (dlCode != 200) { dlHttp.end(); Serial.printf("OTA DL failed: %d\n", dlCode); return; }

  int size = dlHttp.getSize();
  WiFiClient* stream = dlHttp.getStreamPtr();
  if (!Update.begin(size)) { dlHttp.end(); Serial.println("OTA begin failed"); return; }

  size_t written = Update.writeStream(*stream);
  dlHttp.end();

  if (Update.end() && Update.isFinished()) {
    Serial.println("OTA complete! Rebooting...");
    for (int i = 0; i < numLeds; i++) leds[i] = C_PASS;
    FastLED.show();
    delay(1000);
    ESP.restart();
  } else {
    Serial.printf("OTA failed: %s\n", Update.errorString());
    Update.abort();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Web server handlers
// ─────────────────────────────────────────────────────────────────────────────

String getStatusJSON() {
  DynamicJsonDocument doc(2048);
  doc["version"]    = FW_VERSION;
  doc["device_id"]  = DEVICE_ID;
  doc["ip"]         = WiFi.localIP().toString();
  doc["ssid"]       = WiFi.SSID();
  doc["rssi"]       = WiFi.RSSI();
  doc["num_leds"]   = numLeds;
  doc["num_slots"]  = numSlots;
  doc["brightness"]  = brightness;
  doc["bench_host"]  = benchHost;
  doc["ota_host"]    = otaHost;
  doc["display_on"]  = displayOn;
  char idleHex[8];
  snprintf(idleHex, sizeof(idleHex), "#%02x%02x%02x", idleColor.r, idleColor.g, idleColor.b);
  doc["idle_color"] = idleHex;
  JsonArray slots = doc.createNestedArray("slots");
  for (int i = 0; i < numSlots; i++) {
    JsonObject s = slots.createNestedObject();
    s["idx"]    = i;
    s["miner_id"] = slotMap[i];
    s["status"] = slotStatus[i];
    JsonArray hbs = s.createNestedArray("hb");
    hbs.add(hbStatus[i][0]);
    hbs.add(hbStatus[i][1]);
    hbs.add(hbStatus[i][2]);
  }
  String out; serializeJson(doc, out);
  return out;
}

void handleRoot() {
  // Serve mini config page for initial setup
  char idleHex[8];
  snprintf(idleHex, sizeof(idleHex), "#%02x%02x%02x", idleColor.r, idleColor.g, idleColor.b);
  String page = R"(<!DOCTYPE html><html><head><meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Bench LED Controller</title>
<style>
body{background:#0a0a0a;color:#f0f0f0;font-family:monospace;padding:1em;font-size:14px}
h2{color:#4da6ff;margin-bottom:.5em}
label{color:#888;display:block;margin:.6em 0 .2em}
input,select{background:#111;border:1px solid #333;color:#fff;
  padding:.4em .6em;border-radius:4px;width:100%;box-sizing:border-box}
button{background:#0055cc;color:#fff;border:none;padding:.5em 1.2em;
  border-radius:4px;cursor:pointer;margin:.4em .2em 0 0}
button.red{background:#550000;color:#cc4444}
.box{background:#111;border:1px solid #1a1a1a;border-radius:6px;
  padding:.8em 1em;margin-bottom:.8em}
.row{display:grid;grid-template-columns:1fr 1fr;gap:.5em}
.ok{color:#00cc44}.err{color:#cc4444}
#msg{margin-top:.5em;font-size:.85em}
</style></head><body>
<h2>⛏ Bench LED Controller )" + String(FW_VERSION) + R"(</h2>
<div id='status-box' class='box'>Loading…</div>
<div class='box'>
<b>Config</b>
<label>Bench Host</label>
<input id='bhost' value=')" + benchHost + R"('>
<label>OTA Host</label>
<input id='ohost' value=')" + otaHost + R"('>
<label>Brightness</label>
<div style='display:flex;align-items:center;gap:.6em'>
  <input id='bri' type='range' min='0' max='255' value=')" + String(brightness) + R"(' oninput='liveBri(this.value)' style='flex:1'>
  <span id='bri-val' style='width:2.5em;text-align:right'>" + String(brightness) + R"(</span>
</div>
<label>Idle Slot Color <span style='font-size:.8em;color:#555'>(empty slots)</span></label>
<div style='display:flex;align-items:center;gap:.6em'>
  <input id='idle-color' type='color' id='idle-color' value=')" + idleHex + R"(' oninput='liveIdle(this.value)' style='width:3em;height:2em;border:none;background:none;cursor:pointer'>
  <span style='font-size:.8em;color:#555'>Pick color — applied live</span>
</div>
<div class='row'>
<div><label>Total LEDs</label><input id='nleds' type='number' value=')" + String(numLeds) + R"('></div>
<div><label>Slots</label><input id='nslots' type='number' value=')" + String(numSlots) + R"('></div>
</div>
<button onclick='saveConfig()'>Save &amp; Restart</button>
<button class='red' onclick='checkOTA()'>Check OTA</button>
<div id='msg'></div>
</div>
<div class='box'>
<b>Slot Map</b> (slot → miner_id, -1 = empty)
<div id='slotmap'></div>
<button onclick='saveSlots()'>Save Slots</button>
</div>
<script>
function status(){
  fetch('/status').then(r=>r.json()).then(d=>{
    var s=document.getElementById('status-box');
    s.innerHTML='<b>IP:</b>'+d.ip+'  <b>RSSI:</b>'+d.rssi+'dBm  <b>Slots:</b>'+d.num_slots+'  <b>LEDs:</b>'+d.num_leds;
    var bs=document.getElementById('bri'); if(bs){bs.value=d.brightness;}
    var bv=document.getElementById('bri-val'); if(bv){bv.textContent=d.brightness;}
    var ic=document.getElementById('idle-color'); if(ic&&d.idle_color){ic.value=d.idle_color;}
    // Build slot grid
    var sm=document.getElementById('slotmap');
    sm.innerHTML=d.slots.map((s,i)=>`
      <div style='display:flex;align-items:center;gap:.4em;margin:.25em 0'>
        <span style='color:#555;width:4em'>Slot ${i}</span>
        <input type='number' id='s${i}' value='${s.miner_id}' style='width:5em'>
        <span style='color:${["#4da6ff","#00cc44","#cc4444","#ff9900","#4da6ff","#444","#333"][s.status]};font-size:.8em'>${["IDLE","PASS","FAIL","WARN","TEST","OFFL","UNK"][s.status]}</span>
      </div>`).join('');
  }).catch(e=>{ document.getElementById('status-box').innerHTML='<span class="err">'+e+'</span>'; });
}
function liveBri(v){
  document.getElementById('bri-val').textContent=v;
  fetch('/brightness?v='+v).then(r=>r.json()).then(d=>{}).catch(()=>{});
}
function liveIdle(v){
  fetch('/idle_color',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({color:v})}).catch(()=>{});
}
function saveConfig(){
  var d={bench_host:document.getElementById('bhost').value,
    ota_host:document.getElementById('ohost').value,
    brightness:parseInt(document.getElementById('bri').value),
    num_leds:parseInt(document.getElementById('nleds').value),
    num_slots:parseInt(document.getElementById('nslots').value)};
  fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})
    .then(r=>r.json()).then(d=>{ document.getElementById('msg').textContent=d.ok?'✅ Saved — rebooting…':'❌ '+JSON.stringify(d); })
    .catch(e=>{ document.getElementById('msg').textContent='❌ '+e; });
}
function saveSlots(){
  var ns=parseInt(document.getElementById('nslots').value||'16');
  var slots=[];
  for(var i=0;i<ns;i++){
    var el=document.getElementById('s'+i);
    slots.push(el?parseInt(el.value)||(-1):-1);
  }
  fetch('/slot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slots:slots})})
    .then(r=>r.json()).then(d=>{ document.getElementById('msg').textContent=d.ok?'✅ Slots saved':'❌ '+JSON.stringify(d); })
    .catch(e=>{ document.getElementById('msg').textContent='❌ '+e; });
}
function checkOTA(){
  document.getElementById('msg').textContent='Checking OTA…';
  fetch('/ota_check').then(r=>r.json()).then(d=>{
    document.getElementById('msg').textContent=d.updating?'🔄 Updating…':('✅ '+d.message);
  }).catch(e=>{ document.getElementById('msg').textContent='❌ '+e; });
}
status(); setInterval(status,5000);
</script></body></html>)";
  server.send(200, "text/html", page);
}

void handleStatus() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", getStatusJSON());
}

void handleConfig() {
  if (!server.hasArg("plain")) { server.send(400, "text/plain", "no body"); return; }
  DynamicJsonDocument doc(512);
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400, "text/plain", "bad json"); return; }

  prefs.begin("led-ctrl", false);
  if (doc.containsKey("bench_host")) { benchHost = doc["bench_host"].as<String>(); prefs.putString("benchHost", benchHost); }
  if (doc.containsKey("ota_host"))   { otaHost   = doc["ota_host"].as<String>();   prefs.putString("otaHost",   otaHost);   }
  if (doc.containsKey("brightness")) {
    brightness = doc["brightness"];
    prefs.putInt("brightness", brightness);
    FastLED.setBrightness(brightness);
  }
  if (doc.containsKey("idle_color")) {
    // Accept "#rrggbb" hex string
    String hex = doc["idle_color"].as<String>();
    hex.replace("#", "");
    long v = strtol(hex.c_str(), nullptr, 16);
    idleColor = CRGB((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF);
    prefs.putInt("idleR", idleColor.r);
    prefs.putInt("idleG", idleColor.g);
    prefs.putInt("idleB", idleColor.b);
  }
  if (doc.containsKey("num_leds"))   { numLeds = doc["num_leds"];   prefs.putInt("numLeds",   numLeds);  }
  if (doc.containsKey("num_slots"))  { numSlots = doc["num_slots"]; prefs.putInt("numSlots",  numSlots); }
  prefs.end();

  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", "{\"ok\":true}");
  delay(500);
  ESP.restart();
}

void handleSetSlot() {
  if (!server.hasArg("plain")) { server.send(400, "text/plain", "no body"); return; }
  DynamicJsonDocument doc(512);
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400, "text/plain", "bad json"); return; }

  prefs.begin("led-ctrl", false);
  if (doc.containsKey("slots")) {
    JsonArray arr = doc["slots"].as<JsonArray>();
    int i = 0;
    for (int mid : arr) {
      if (i >= MAX_SLOTS) break;
      slotMap[i] = mid;
      char key[12]; snprintf(key, sizeof(key), "slot%d", i);
      prefs.putInt(key, mid);
      i++;
    }
  } else if (doc.containsKey("idx") && doc.containsKey("miner_id")) {
    int idx = doc["idx"]; int mid = doc["miner_id"];
    if (idx >= 0 && idx < MAX_SLOTS) {
      slotMap[idx] = mid;
      char key[12]; snprintf(key, sizeof(key), "slot%d", idx);
      prefs.putInt(key, mid);
    }
  }
  prefs.end();

  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleBrightness() {
  if (server.hasArg("v")) {
    brightness = constrain(server.arg("v").toInt(), 0, 255);
    FastLED.setBrightness(brightness);
    // Persist without restart
    prefs.begin("led-ctrl", false);
    prefs.putInt("brightness", brightness);
    prefs.end();
    updateLEDs();  // reflect immediately
  }
  server.sendHeader("Access-Control-Allow-Origin", "*");
  // Return current value so slider can sync
  String resp = "{\"ok\":true,\"brightness\":" + String(brightness) + "}";
  server.send(200, "application/json", resp);
}

void handleIdleColor() {
  if (server.hasArg("plain")) {
    DynamicJsonDocument doc(128);
    if (!deserializeJson(doc, server.arg("plain"))) {
      String hex = doc["color"].as<String>();
      hex.replace("#", "");
      long v = strtol(hex.c_str(), nullptr, 16);
      idleColor = CRGB((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF);
      prefs.begin("led-ctrl", false);
      prefs.putInt("idleR", idleColor.r);
      prefs.putInt("idleG", idleColor.g);
      prefs.putInt("idleB", idleColor.b);
      prefs.end();
      updateLEDs();
    }
  }
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", "{\"ok\":true}");
}

void updateDisplay() {
  if (!displayOn) { display.displayOff(); return; }
  display.clear();
  display.setFont(ArialMT_Plain_10);
  displayPage = (displayPage + 1) % 3;

  if (displayPage == 0) {
    // Page 0: identity + status
    int pass=0, fail=0, warn=0, offl=0;
    for (int i=0; i<numSlots; i++) {
      switch(slotStatus[i]) {
        case 1: pass++; break; case 2: fail++; break;
        case 3: warn++; break; default: offl++; break;
      }
    }
    display.drawString(0,  0, "CT Bench LED v" FW_VERSION);
    display.drawString(0, 12, String("ID: ") + DEVICE_ID);
    // Row 3: connectivity status
    display.drawString(0, 24, WiFi.isConnected() ?
      (serverReachable ? "API: OK" : "API: ---") : "WiFi: ---");
    char line[32];
    snprintf(line, sizeof(line), "P:%d F:%d W:%d Off:%d", pass, fail, warn, offl);
    display.drawString(0, 24, line);
    snprintf(line, sizeof(line), "LEDs:%d Slots:%d Bri:%d", numLeds, numSlots, brightness);
    display.drawString(0, 36, line);
    display.drawString(0, 50, WiFi.isConnected() ? "WiFi OK" : "WiFi ERR");

  } else if (displayPage == 1) {
    // Page 1: network
    display.drawString(0,  0, "NETWORK");
    display.drawString(0, 12, WiFi.isConnected() ? WiFi.localIP().toString() : "Not connected");
    char rssi[20];
    snprintf(rssi, sizeof(rssi), "RSSI: %d dBm", WiFi.RSSI());
    display.drawString(0, 24, rssi);
    // Shorten bench host for display
    String bh = benchHost;
    if (bh.length() > 21) bh = bh.substring(bh.lastIndexOf('/') + 1);
    display.drawString(0, 36, "Bench: " + bh);
    display.drawString(0, 50, String("SSID: ") + WIFI_SSID);

  } else {
    // Page 2: slot status overview (8 slots per screen, two columns)
    display.drawString(0, 0, "SLOTS");
    const char* ST[] = {"--","OK","!!","??","??","--"};
    for (int i = 0; i < min(numSlots, 14); i++) {
      int col = (i < 7) ? 0 : 64;
      int row2 = ((i % 7) + 1) * 8;
      char buf[12];
      snprintf(buf, sizeof(buf), "%02d:%s", i, ST[min(slotStatus[i], 5)]);
      display.drawString(col, row2, buf);
    }
  }

  display.display();
}

void handleDisplayToggle() {
  if (server.hasArg("plain")) {
    DynamicJsonDocument doc(64);
    if (!deserializeJson(doc, server.arg("plain"))) {
      if (doc.containsKey("on")) displayOn = doc["on"].as<bool>();
    }
  } else {
    displayOn = !displayOn;  // toggle
  }
  updateDisplay();
  server.sendHeader("Access-Control-Allow-Origin", "*");
  char dispResp[64];
  snprintf(dispResp, sizeof(dispResp), "{\"ok\":true,\"display_on\":%s}", displayOn ? "true" : "false");
  server.send(200, "application/json", dispResp);
}

// Parse "#rrggbb" string to CRGB
CRGB hexToRGB(const String& hex) {
  String h = hex; h.replace("#", "");
  if (h.length() < 6) return CRGB::Black;
  long v = strtol(h.c_str(), nullptr, 16);
  return CRGB((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF);
}

void handleFlash() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  if (!server.hasArg("plain")) { server.send(400, "text/plain", "no body"); return; }
  DynamicJsonDocument doc(256);
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400, "text/plain", "bad json"); return; }
  int slot      = doc["slot"]        | -1;
  int dur       = doc["duration_ms"] | 2000;
  String color  = doc["color"]       | "#ffffff";
  if (slot < 0 || slot >= numSlots) { server.send(400, "text/plain", "bad slot"); return; }
  flashOvr.active = true;
  flashOvr.slot   = slot;
  flashOvr.color  = hexToRGB(color);
  flashOvr.endMs  = millis() + dur;
  updateLEDs();
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleSlotColor() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  if (!server.hasArg("plain")) { server.send(400, "text/plain", "no body"); return; }
  DynamicJsonDocument doc(256);
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400, "text/plain", "bad json"); return; }
  int slot     = doc["slot"]  | -1;
  String color = doc["color"] | "restore";
  if (slot < 0 || slot >= MAX_SLOTS) { server.send(400, "text/plain", "bad slot"); return; }
  if (color == "restore") {
    slotColorOvrActive[slot] = false;
  } else {
    slotColorOvr[slot]       = hexToRGB(color);
    slotColorOvrActive[slot] = true;
  }
  updateLEDs();
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleOTACheck() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", "{\"updating\":false,\"message\":\"OTA check queued\"}");
  lastOTA = 0;  // force check on next loop
}
