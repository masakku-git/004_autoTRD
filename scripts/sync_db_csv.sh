#!/bin/bash
# ============================================================
# autoTRD CSV エクスポート同期スクリプト（VPS → ローカル）
#
# 事前にVPSで `bash scripts/export_db_csv.sh` を実行しておくこと。
#
# 使い方（ローカルで）:
#   bash scripts/sync_db_csv.sh
#
# ============================================================

set -euo pipefail

VPS_HOST="trader@157.180.91.249"
VPS_CSV_DIR="/home/trader/autoTRD/data/csv_export"
LOCAL_CSV_DIR="$(cd "$(dirname "$0")/.." && pwd)/data/csv_export"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

mkdir -p "${LOCAL_CSV_DIR}"

echo "${LOG_PREFIX} VPS から CSV を同期します..."
echo "${LOG_PREFIX} 接続先: ${VPS_HOST}:${VPS_CSV_DIR}"
echo "${LOG_PREFIX} 保存先: ${LOCAL_CSV_DIR}"
echo ""

rsync -avz --progress \
  --include="*.csv" \
  --exclude="*" \
  "${VPS_HOST}:${VPS_CSV_DIR}/" \
  "${LOCAL_CSV_DIR}/"

echo ""
echo "${LOG_PREFIX} ✅ 同期完了"
