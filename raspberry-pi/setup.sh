#!/bin/bash
# CT TestBench Raspberry Pi 4 Setup Script
# Installs Docker, Docker Compose, and configures the system.

set -euo pipefail

echo "=== CT TestBench Raspberry Pi Setup ==="
echo "This script sets up the test bench server on a Raspberry Pi 4."
echo ""

# ── System update ─────────────────────────────────────────────────────────────
sudo apt-get update -y
sudo apt-get upgrade -y

# ── Install Docker ─────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "Docker installed."
else
    echo "Docker already installed."
fi

# ── Install Docker Compose ─────────────────────────────────────────────────────
if ! command -v docker-compose &>/dev/null && ! docker compose version &>/dev/null 2>&1; then
    echo "Installing Docker Compose..."
    sudo apt-get install -y docker-compose-plugin
fi

# ── Install Python dependencies (for optional RGB daemon) ─────────────────────
sudo apt-get install -y python3-pip python3-venv git

# ── Optional: WS2812B LED support ────────────────────────────────────────────
if [ "${INSTALL_RGB:-no}" = "yes" ]; then
    echo "Installing rpi_ws281x for RGB LED support..."
    sudo apt-get install -y python3-dev swig
    pip3 install rpi-ws281x
    echo "Enabling SPI and I2C..."
    sudo raspi-config nonint do_spi 0
    sudo raspi-config nonint do_i2c 0
fi

# ── Clone and configure testbench ─────────────────────────────────────────────
INSTALL_DIR="/opt/ct-testbench"
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Cloning miner-testbench..."
    sudo git clone https://github.com/cryptotherm/miner-testbench.git "$INSTALL_DIR"
    sudo chown -R $USER:$USER "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# Create .env if it doesn't exist
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "=== IMPORTANT: Edit $INSTALL_DIR/.env to set your config ==="
    echo "  - OPENAI_API_KEY"
    echo "  - PRINTER_IDENTIFIER (TCP IP of Brother QL-810W)"
    echo ""
fi

# ── Systemd service ───────────────────────────────────────────────────────────
sudo tee /etc/systemd/system/ct-testbench.service > /dev/null <<EOF
[Unit]
Description=CT TestBench Server
Requires=docker.service
After=docker.service network.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
User=$USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ct-testbench.service

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Start the server with:"
echo "  cd $INSTALL_DIR && docker compose up -d"
echo ""
echo "Or enable auto-start on boot:"
echo "  sudo systemctl start ct-testbench"
echo ""
echo "API will be available at: http://$(hostname -I | awk '{print $1}'):8000"
echo "WebSocket: ws://$(hostname -I | awk '{print $1}'):8000/ws/miners"
