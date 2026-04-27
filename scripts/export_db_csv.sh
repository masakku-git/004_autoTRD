#!/bin/bash
# ============================================================
# autoTRD DB → CSV 一括エクスポートスクリプト（VPS側で実行）
#
# 使い方（VPS上で）:
#   bash scripts/export_db_csv.sh
#
# 出力先: data/csv_export/*.csv
# ============================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${PROJECT_DIR}/data"
CSV_DIR="${DATA_DIR}/csv_export"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

mkdir -p "${CSV_DIR}"

PSQL_OPTS=(-h localhost -U autotrd -d autotrd)

TABLES=(
  orders
  trade_log
  portfolio_snapshots
  market_conditions
  screening_results
  strategy_metadata
)

echo "${LOG_PREFIX} DB から CSV をエクスポートします..."
echo "${LOG_PREFIX} 出力先: ${CSV_DIR}"
echo ""

cd "${DATA_DIR}"

for tbl in "${TABLES[@]}"; do
  out_path="csv_export/${tbl}.csv"
  echo "  - ${tbl} → ${out_path}"
  psql "${PSQL_OPTS[@]}" -c "\COPY ${tbl} TO '${out_path}' CSV HEADER"
done

echo ""
echo "${LOG_PREFIX} ✅ エクスポート完了 ($(ls -1 ${CSV_DIR}/*.csv | wc -l) ファイル)"
