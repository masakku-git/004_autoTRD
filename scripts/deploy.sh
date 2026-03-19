#!/bin/bash
# Deploy latest code from GitHub to VPS
set -e

PROJECT_DIR="/home/trader/autoTRD"
cd "$PROJECT_DIR"

echo "Pulling latest changes..."
git pull origin main

echo "Installing dependencies..."
pip install -r requirements.txt --quiet 2>/dev/null || pip install -e . --quiet

echo "Running database migrations..."
alembic upgrade head 2>/dev/null || python scripts/init_db.py

echo "Deploy complete at $(date)"
