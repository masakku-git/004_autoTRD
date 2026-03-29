#!/bin/bash
# ============================================================
# autoTRD データベース自動バックアップスクリプト
# 保存先: Google Drive (rclone 経由)
#
# 使い方:
#   bash scripts/backup_db_gdrive.sh
#
# 自動実行 (crontab -e で追加):
#   0 2 * * * /home/trader/autoTRD/scripts/backup_db_gdrive.sh >> /home/trader/logs/backup.log 2>&1
# ============================================================

set -euo pipefail

# ── 設定 ────────────────────────────────────────────────────
DB_USER="trader"
DB_NAME="autoTRD"
BACKUP_DIR="/home/trader/backup"
GDRIVE_REMOTE="gdrive"                        # rclone remote 名
GDRIVE_DIR="autoTRD/db_backups"              # Google Drive 上のフォルダパス
KEEP_LOCAL_DAYS=3                             # ローカルに保持する日数
KEEP_REMOTE_FILES=30                          # Google Drive に保持するファイル数
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

# ── ローカルバックアップディレクトリ作成 ─────────────────────
mkdir -p "$BACKUP_DIR"

# ── バックアップファイル名 ────────────────────────────────────
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP_FILE="${BACKUP_DIR}/autoTRD_db_${TIMESTAMP}.sql.gz"

# ── pg_dump 実行 ─────────────────────────────────────────────
echo "${LOG_PREFIX} pg_dump 開始: ${BACKUP_FILE}"
pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$BACKUP_FILE"

if [ $? -ne 0 ]; then
    echo "${LOG_PREFIX} ❌ pg_dump 失敗"
    exit 1
fi

FILE_SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
echo "${LOG_PREFIX} ✅ pg_dump 完了 (${FILE_SIZE})"

# ── Google Drive へアップロード ───────────────────────────────
echo "${LOG_PREFIX} Google Drive アップロード開始: ${GDRIVE_REMOTE}:${GDRIVE_DIR}/"
rclone copy "$BACKUP_FILE" "${GDRIVE_REMOTE}:${GDRIVE_DIR}/" --progress

if [ $? -ne 0 ]; then
    echo "${LOG_PREFIX} ❌ Google Drive アップロード失敗"
    exit 1
fi
echo "${LOG_PREFIX} ✅ Google Drive アップロード完了"

# ── Google Drive の古いファイルを削除（件数超過分） ───────────
echo "${LOG_PREFIX} Google Drive の古いファイルを整理中..."
REMOTE_FILES=$(rclone lsf "${GDRIVE_REMOTE}:${GDRIVE_DIR}/" --format "t;n" | sort -r | tail -n +$((KEEP_REMOTE_FILES + 1)) | cut -d';' -f2)

if [ -n "$REMOTE_FILES" ]; then
    while IFS= read -r fname; do
        echo "${LOG_PREFIX} 削除: ${fname}"
        rclone deletefile "${GDRIVE_REMOTE}:${GDRIVE_DIR}/${fname}"
    done <<< "$REMOTE_FILES"
    echo "${LOG_PREFIX} ✅ 古いリモートファイルを削除しました"
else
    echo "${LOG_PREFIX} リモートファイル数は上限以下です（削除なし）"
fi

# ── ローカルの古いファイルを削除 ──────────────────────────────
echo "${LOG_PREFIX} ローカルの古いバックアップを削除中 (${KEEP_LOCAL_DAYS}日以上前)..."
find "$BACKUP_DIR" -name "autoTRD_db_*.sql.gz" -mtime +${KEEP_LOCAL_DAYS} -delete
echo "${LOG_PREFIX} ✅ ローカル整理完了"

echo "${LOG_PREFIX} 🎉 バックアップ処理が正常に完了しました"
