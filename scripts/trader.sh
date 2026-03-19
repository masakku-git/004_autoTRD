#!/bin/bash
# autoTRD 管理スクリプト — タイマー・サービスの状態確認・変更を簡単に行う
#
# 使い方:
#   trader.sh status    — 現在の状態を表示
#   trader.sh start     — タイマーを開始（自動実行ON）
#   trader.sh stop      — タイマーを停止（自動実行OFF）
#   trader.sh run       — 今すぐ手動実行
#   trader.sh log       — 直近のログを表示
#   trader.sh time      — 実行スケジュールを表示
#   trader.sh set-time  — 実行時刻を変更（例: trader.sh set-time 14:00）
#   trader.sh install   — systemdにサービス・タイマーを登録

set -e

SERVICE="autotrader.service"
TIMER="autotrader.timer"
PROJECT_DIR="/home/trader/autoTRD"

# UTC時刻(HH:MM)をJST(+9時間)に変換して表示する関数
utc_to_jst() {
  local utc_time="$1"
  local utc_h=$(echo "$utc_time" | cut -d: -f1)
  local utc_m=$(echo "$utc_time" | cut -d: -f2)
  local jst_h=$(( (10#$utc_h + 9) % 24 ))
  printf "%02d:%02d" "$jst_h" "$utc_m"
}

# タイマーファイルからUTC時刻(HH:MM)を取得する関数
get_schedule_utc() {
  local line
  line=$(grep "OnCalendar" /etc/systemd/system/"$TIMER" 2>/dev/null) || true
  echo "$line" | grep -oE '[0-9]{2}:[0-9]{2}' | head -1
}

case "${1}" in

  status)
    echo "=== autoTRD Status ==="
    echo ""
    # タイマー状態
    if systemctl is-active --quiet "$TIMER" 2>/dev/null; then
      echo "Timer:  ON (自動実行が有効)"
    else
      echo "Timer:  OFF (自動実行が無効)"
    fi
    echo ""
    # スケジュール（UTC & JST）
    echo "--- Schedule ---"
    UTC_TIME=$(get_schedule_utc)
    if [ -n "$UTC_TIME" ]; then
      JST_TIME=$(utc_to_jst "$UTC_TIME")
      echo "実行時刻: ${UTC_TIME} UTC / ${JST_TIME} JST (平日のみ)"
      echo ""
      systemctl list-timers "$TIMER" --no-pager 2>/dev/null || true
    else
      echo "(タイマー未登録)"
    fi
    echo ""
    # 最終実行結果
    echo "--- Last Run ---"
    systemctl status "$SERVICE" --no-pager -l 2>/dev/null | head -15 || echo "(未実行)"
    echo ""
    # DRY_RUN設定
    if grep -q "^DRY_RUN=true" "$PROJECT_DIR/.env" 2>/dev/null; then
      echo "Mode:   DRY_RUN (模擬実行)"
    else
      echo "Mode:   LIVE (本番取引)"
    fi
    ;;

  start)
    echo "タイマーを開始します..."
    sudo systemctl start "$TIMER"
    sudo systemctl enable "$TIMER"
    echo "Done: 自動実行が有効になりました"
    systemctl list-timers "$TIMER" --no-pager
    ;;

  stop)
    echo "タイマーを停止します..."
    sudo systemctl stop "$TIMER"
    sudo systemctl disable "$TIMER"
    echo "Done: 自動実行が無効になりました"
    ;;

  run)
    echo "手動実行を開始します..."
    cd "$PROJECT_DIR"
    source .venv/bin/activate
    python -m src.main
    ;;

  log)
    LINES="${2:-50}"
    echo "=== 直近${LINES}行のログ ==="
    journalctl -u "$SERVICE" --no-pager -n "$LINES" 2>/dev/null || \
      tail -n "$LINES" "$PROJECT_DIR/data/autotrd.log" 2>/dev/null || \
      echo "ログが見つかりません"
    ;;

  time)
    echo "=== 現在のスケジュール ==="
    UTC_TIME=$(get_schedule_utc)
    if [ -n "$UTC_TIME" ]; then
      JST_TIME=$(utc_to_jst "$UTC_TIME")
      echo "実行時刻: ${UTC_TIME} UTC / ${JST_TIME} JST (平日のみ)"
      echo ""
      grep "OnCalendar" /etc/systemd/system/"$TIMER"
    else
      echo "(タイマー未登録)"
    fi
    echo ""
    echo "変更するには: $0 set-time HH:MM"
    echo "例: $0 set-time 14:00  (UTC) = 23:00 JST"
    ;;

  set-time)
    if [ -z "$2" ]; then
      echo "使い方: $0 set-time HH:MM (UTC)"
      echo "例: $0 set-time 13:00  → 22:00 JST"
      echo "    $0 set-time 14:00  → 23:00 JST"
      exit 1
    fi
    NEW_TIME="$2"
    JST_TIME=$(utc_to_jst "$NEW_TIME")
    echo "実行時刻を ${NEW_TIME} UTC (${JST_TIME} JST) に変更します..."
    sudo sed -i "s/OnCalendar=.*/OnCalendar=Mon..Fri *-*-* ${NEW_TIME}:00 UTC/" /etc/systemd/system/"$TIMER"
    sudo systemctl daemon-reload
    # タイマーが有効なら再起動
    if systemctl is-active --quiet "$TIMER" 2>/dev/null; then
      sudo systemctl restart "$TIMER"
    fi
    echo "Done: スケジュールを変更しました"
    echo "実行時刻: ${NEW_TIME} UTC / ${JST_TIME} JST (平日のみ)"
    ;;

  install)
    echo "systemdにサービス・タイマーを登録します..."
    sudo cp "$PROJECT_DIR/systemd/autotrader.service" /etc/systemd/system/
    sudo cp "$PROJECT_DIR/systemd/autotrader.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    echo "Done: 登録完了"
    echo ""
    echo "次のステップ:"
    echo "  $0 start    — タイマーを開始"
    echo "  $0 status   — 状態を確認"
    ;;

  *)
    echo "autoTRD 管理スクリプト"
    echo ""
    echo "使い方: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  status     現在の状態を表示"
    echo "  start      タイマーを開始（自動実行ON）"
    echo "  stop       タイマーを停止（自動実行OFF）"
    echo "  run        今すぐ手動実行"
    echo "  log [N]    直近N行のログを表示（デフォルト50行）"
    echo "  time       実行スケジュールを表示"
    echo "  set-time   実行時刻を変更（例: $0 set-time 14:00）"
    echo "  install    systemdにサービス・タイマーを登録"
    echo ""
    echo "時刻の目安 (UTC → JST):"
    echo "  13:00 UTC = 22:00 JST"
    echo "  13:30 UTC = 22:30 JST"
    echo "  14:00 UTC = 23:00 JST"
    ;;

esac
