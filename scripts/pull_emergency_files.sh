#!/bin/bash
# VPS から Git 管理外の重要ファイルを emergency_recovery/ に取得する
set -e

VPS_HOST="trader@157.180.91.249"
REMOTE_PROJECT="/home/trader/autoTRD"
LOCAL_DEST="$(cd "$(dirname "$0")/.." && pwd)/emergency_recovery"

echo "=== 緊急復旧ファイル取得 ==="
echo "取得元: ${VPS_HOST}"
echo "保存先: ${LOCAL_DEST}"
echo ""

# .env
echo "[1/3] .env を取得中..."
scp "${VPS_HOST}:${REMOTE_PROJECT}/.env" "${LOCAL_DEST}/.env"
echo "      -> ${LOCAL_DEST}/.env"

# rclone.conf
echo "[2/3] rclone.conf を取得中..."
scp "${VPS_HOST}:~/.config/rclone/rclone.conf" "${LOCAL_DEST}/rclone.conf"
echo "      -> ${LOCAL_DEST}/rclone.conf"

# sync_logs.sh（VPS管理外のためローカルからコピー）
echo "[3/3] sync_logs.sh をローカルからコピー中..."
cp "$(dirname "$0")/sync_logs.sh" "${LOCAL_DEST}/sync_logs.sh"
echo "      -> ${LOCAL_DEST}/sync_logs.sh"

echo ""
echo "取得完了: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""
echo "【注意】このフォルダは Git 管理外です。"
echo "        定期的にこのスクリプトを実行して最新状態を保ってください。"
