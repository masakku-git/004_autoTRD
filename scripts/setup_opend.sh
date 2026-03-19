#!/bin/bash
# Setup moomoo OpenD on Ubuntu VPS
set -e

INSTALL_DIR="/home/trader/Moomoo_OpenD"

echo "=== moomoo OpenD Setup ==="
echo "1. Download OpenD from moomoo website"
echo "   Visit: https://www.moomoo.com/download/openAPI"
echo ""
echo "2. Extract to $INSTALL_DIR"
echo "3. Edit OpenD.xml with your credentials"
echo ""

# Create systemd service
cat << 'SVCEOF' | sudo tee /etc/systemd/system/opend.service
[Unit]
Description=moomoo OpenD Gateway
After=network.target

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/Moomoo_OpenD
ExecStart=/home/trader/Moomoo_OpenD/OpenD
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF

echo "Created /etc/systemd/system/opend.service"
echo ""
echo "To start:"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable opend"
echo "  sudo systemctl start opend"
