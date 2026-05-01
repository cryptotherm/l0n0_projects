# CT TestBench LED Controller

ESP32-based WS2812B LED strip driver that shows live miner status on the physical test bench rack.

## Hardware

- **Board:** Heltec WiFi LoRa 32 V3 (ESP32-S3) — or any ESP32 with SSD1306 OLED
- **LED Strip:** WS2812B addressable, DATA on **GPIO 5** (change `DATA_PIN` if needed)
- **Power:** 5V external for LED strip (don't power >~10 LEDs from the ESP directly)
- **Display:** Built-in SSD1306 OLED via `heltec_unofficial`

## LED Layout

8 LEDs per rack slot:

| LED index | Purpose |
|-----------|---------|
| 0 | Overall slot status (bright) |
| 1–3 | Hashboard 1 / 2 / 3 health |
| 4 | Spacer (always off) |
| 5–7 | Dim echo of slot status |

## Colour Scheme

| Status | Colour |
|--------|--------|
| PASS (≥80 TH/s) | 🟢 Green |
| FAIL / needs repair | 🔴 Red |
| WARN / underperforming | 🟠 Orange |
| TESTING | 🔵 Blue |
| OFFLINE | Dim blue |
| Empty slot | Configurable (default off) |
| Hashboard good | Green |
| Hashboard partial | Orange |
| Hashboard dead | Red |

## WiFi

Connects to `ctiot` / `iotconnect` by default. Change `WIFI_SSID` / `WIFI_PASS` in the sketch.

## OLED Display Pages

Cycles every 4 seconds:

| Page | Content |
|------|---------|
| 0 | Version, device ID, Pass/Fail/Warn counts, brightness |
| 1 | IP address, RSSI, bench host URL, SSID |
| 2 | Slot status grid (14 slots, two columns) |

Immediately after WiFi connects: shows IP + device ID.

## Web UI

Browse to the ESP32's IP in any browser for the config page:

- **Bench Host** — URL of the Pi testbench server (default `http://10.0.0.133:8000`)
- **OTA Host** — URL of the splice-and-connect OTA server (default `http://10.0.0.248:8081`)
- **Brightness** — live range slider 0–255, applied immediately, no restart
- **Idle Color** — HTML color picker for empty slots, applied live, persisted
- **Slot Map** — assign miner IDs to physical LED strip positions
- **Save & Restart** — persist config to NVS and reboot
- **Check OTA** — trigger immediate firmware update check

## HTTP API

All endpoints support `Access-Control-Allow-Origin: *` (called directly from the kiosk UI).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/status` | JSON: version, IP, RSSI, slots array, brightness, idle_color |
| POST | `/config` | JSON body: bench_host, ota_host, brightness, num_leds, num_slots, idle_color |
| POST | `/slot` | JSON body: `{slots: [miner_id, ...]}` or `{idx, miner_id}` |
| GET/POST | `/brightness?v=N` | Set brightness 0–255 live |
| POST | `/idle_color` | JSON body: `{color: "#rrggbb"}` — set empty-slot colour live |
| GET/POST | `/ota_check` | Trigger OTA firmware check immediately |
| POST | `/display` | JSON body: `{on: bool}` — toggle OLED on/off |

## OTA Updates

Firmware updates are served by the **splice-and-connect OTA server** running on cthome at `http://10.0.0.248:8081`.

### How it works

1. ESP32 polls `/ota/check?device_id=bench-led-01&version=X.Y.Z&fw_name=bench_led_v1_0_0` every 60 seconds
2. OTA server looks for `bench-led-01.bin` in its firmware directory
3. If server version > current version: downloads binary, flashes via `Update.h`, reboots
4. During update: all LEDs turn blue; on success all turn green before reboot

### Deploying a new firmware version

```bash
# 1. Compile on cthome
~/.local/bin/arduino-cli compile \
  --fqbn esp32:esp32:heltec_wifi_lora_32_V3 \
  --output-dir /tmp/bench_led_build \
  ~/miner-testbench/firmware/bench_led/

# 2. Copy binary to OTA server firmware dir
cp /tmp/bench_led_build/bench_led_v1_0_0.ino.bin \
   ~/splice-and-connect/server/firmware/bench-led-01.bin

# 3. Update version file (must be higher than current FW_VERSION)
echo "1.3.0" > ~/splice-and-connect/server/firmware/bench-led-01.version

# 4. Restart OTA server (or it will pick up on next check)
pm2 restart ota-server
```

The ESP will pull and flash the update within 60 seconds (or immediately via `POST /ota_check`).

### OTA server location

- **Host:** cthome (`10.0.0.248` / Tailscale `100.85.100.85`)
- **Port:** 8081 (pm2 process: `ota-server`)
- **Repo:** `cryptotherm/splice-and-connect` → `server/ota_server.py`
- **Firmware dir:** `~/splice-and-connect/server/firmware/`
- **Binary name:** `bench-led-01.bin`
- **Version file:** `bench-led-01.version`

### First-time flash (Arduino IDE)

1. Install libraries: **FastLED**, **ArduinoJson**, **heltec_unofficial** (from GitHub: `ropg/heltec_esp32_lora_v3`)
2. Board: `Heltec WiFi LoRa 32(V3)` via esp32 board package
3. Open `firmware/bench_led/bench_led_v1_0_0.ino`
4. Select USB port → Upload
5. After first flash, all future updates happen automatically via OTA

## Kiosk LED Sync Panel

In the testbench kiosk (Settings tab → LED Strip Sync):

1. Enter ESP32 IP → **Ping** — confirms connection, shows firmware version
2. Slot grid shows each rack position → miner assignment → live LED status colour
3. **Push Slot Map** — sends current assignments to ESP32 (survives reboot)
4. **OTA Update** — triggers immediate firmware check

## Version History

| Version | Changes |
|---------|---------|
| 1.0.0 | Initial — LED strip, WiFi, poll testbench, OTA, web config |
| 1.1.0 | Brightness slider (live, no restart) + idle color picker |
| 1.2.0 | OLED display (3-page cycle), fix OTA URL param, fix compile errors |
