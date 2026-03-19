#!/bin/bash
# Create PostgreSQL database and user for autoTRD
set -e

DB_NAME="autotrd"
DB_USER="autotrd"
DB_PASS="password"

echo "=== PostgreSQL Setup for autoTRD ==="

# Create user and database (run as postgres superuser)
sudo -u postgres psql <<EOF
CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';
CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};
GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
EOF

echo "Created database '${DB_NAME}' with user '${DB_USER}'"
echo ""
echo "Connection string: postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}"
echo ""
echo "IMPORTANT: Update the password in .env before production use!"
echo ""
echo "To initialize tables, run:"
echo "  python3 scripts/init_db.py"
