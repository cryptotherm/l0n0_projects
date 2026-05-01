# Raspberry Pi Setup

This directory contains scripts for deploying CT TestBench on a Raspberry Pi 4.

## Hardware Requirements

- Raspberry Pi 4 (4GB or 8GB recommended)
- MicroSD card 32GB+
- Active cooling (miners generate heat nearby)
- Network: wired Ethernet (same subnet as miners)
- Optional: WS2812B LED strip on GPIO 18 (Pin 12)

## Quick Setup

```bash
# On the Raspberry Pi:
git clone https://github.com/cryptotherm/miner-testbench.git
cd miner-testbench/raspberry-pi
chmod +x setup.sh
./setup.sh

# Edit config:
nano /opt/ct-testbench/.env

# Start services:
cd /opt/ct-testbench && docker compose up -d
```

## RGB LED Strip (Optional)

Wire a WS2812B strip to GPIO 18 (physical pin 12), 5V power, and GND.

```bash
# Install dependencies:
pip3 install rpi-ws281x

# Start daemon:
python3 rgb_daemon.py --port 8001 --led-count 60 --led-pin 18

# Or as a service (control via main API):
sudo cp rgb-daemon.service /etc/systemd/system/
sudo systemctl enable --now rgb-daemon
```

Control via HTTP:
```bash
curl -X POST http://localhost:8001/rgb/set \
  -H 'Content-Type: application/json' \
  -d '{"pattern": "blink", "color": "#FF0000", "speed_ms": 500}'
```

## Network Notes

- The server uses `network_mode: host` in Docker Compose to receive UDP broadcast from miners
- Ensure the Pi is on the same Layer 2 network as the miners
- Test bench subnet: any — just set `PRINTER_IDENTIFIER` in `.env` to the printer's IP

## Port Reference

| Port | Service |
|------|---------|
| 8000 | TestBench API + WebSocket |
| 8086 | InfluxDB |
| 5432 | PostgreSQL (localhost only) |
| 8001 | RGB Daemon (if running) |
| 14235/udp | Miner discovery broadcast |
