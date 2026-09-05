#!/bin/bash
# ============================================================
# autoTRD 本番DB 読み取り専用クエリスクリプト（ローカルで実行）
#
# VPS へ SSH し、読み取り専用ロール autotrd_ro で psql を実行する。
# CSV エクスポート → rsync を経由せず、その場で SELECT を確認できる。
#
# 使い方（ローカルで）:
#   bash scripts/query_db.sh "SELECT * FROM trade_log ORDER BY id DESC LIMIT 10"
#   bash scripts/query_db.sh --csv "SELECT ticker, pnl FROM trade_log"
#   bash scripts/query_db.sh -f path/to/query.sql
#   bash scripts/query_db.sh --tables            # テーブル一覧
#   bash scripts/query_db.sh --schema trade_log  # カラム定義
#
# 書き込みは二重にブロックされる:
#   1. autotrd_ro ロールに SELECT 権限しか付与しない
#   2. default_transaction_read_only=on をサーバ側で強制
#
# 事前準備は scripts/db_readonly_setup.md を参照。
# ============================================================

set -euo pipefail

VPS_HOST="trader@157.180.91.249"
DB_USER="autotrd_ro"
DB_NAME="autotrd"
STATEMENT_TIMEOUT_MS=30000

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

FMT_OPTS=(-P pager=off)
SQL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --csv)
      FMT_OPTS+=(--csv)
      shift
      ;;
    --tsv)
      FMT_OPTS+=(-A -F $'\t')
      shift
      ;;
    -f|--file)
      [[ $# -ge 2 ]] || { echo "エラー: -f にファイルパスが必要です" >&2; exit 1; }
      [[ -f "$2" ]] || { echo "エラー: ファイルが見つかりません: $2" >&2; exit 1; }
      SQL="$(cat "$2")"
      shift 2
      ;;
    --tables)
      SQL="SELECT table_name, (SELECT count(*) FROM information_schema.columns c WHERE c.table_name = t.table_name AND c.table_schema = 'public') AS columns FROM information_schema.tables t WHERE table_schema = 'public' ORDER BY table_name;"
      shift
      ;;
    --schema)
      [[ $# -ge 2 ]] || { echo "エラー: --schema にテーブル名が必要です" >&2; exit 1; }
      SQL="SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema = 'public' AND table_name = '${2//\'/\'\'}' ORDER BY ordinal_position;"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    -*)
      echo "エラー: 不明なオプション: $1" >&2
      exit 1
      ;;
    *)
      SQL="$1"
      shift
      ;;
  esac
done

if [[ -z "${SQL}" ]]; then
  echo "エラー: SQL が指定されていません" >&2
  echo "" >&2
  usage
fi

# SQL は stdin 経由で psql に渡す（引用符のエスケープ事故を避けるため）
printf '%s\n' "${SQL}" | ssh -o BatchMode=yes "${VPS_HOST}" \
  "PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=${STATEMENT_TIMEOUT_MS}' \
   psql -h localhost -U ${DB_USER} -d ${DB_NAME} -v ON_ERROR_STOP=1 $(printf '%q ' "${FMT_OPTS[@]}") -f -"
