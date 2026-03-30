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
#
# --- 動作確認（口座不要） ---
#   trader.sh ping      — OpenD接続確認（ログイン状態チェック）
#   trader.sh check     — importテスト（パッケージ不足の検出）
#   trader.sh fetch     — yfinanceデータ取得テスト
#   trader.sh simulate  — バックテスト実行（過去データでシミュレーション）
#   trader.sh deploy    — GitHubから最新コードを取得・セットアップ

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

  # ===== 動作確認コマンド（口座不要） =====

  ping)
    echo "=== OpenD 接続確認 ==="
    cd "$PROJECT_DIR"
    if [ -d "venv" ]; then source venv/bin/activate; fi
    if [ -d ".venv" ]; then source .venv/bin/activate; fi
    python3 -c "
import sys, socket, time

HOST = '127.0.0.1'
PORT = 11111
TIMEOUT = 5

# Step 1: TCPポート疎通確認
try:
    sock = socket.create_connection((HOST, PORT), timeout=TIMEOUT)
    sock.close()
    print(f'  OK  TCP {HOST}:{PORT} — OpenDプロセスは応答しています')
except Exception as e:
    print(f'  NG  TCP {HOST}:{PORT} — 接続失敗: {e}')
    print('      OpenDが起動しているか確認してください: sudo systemctl status opend.service')
    sys.exit(1)

# Step 2: moomoo API ログイン状態確認
try:
    from moomoo import OpenQuoteContext
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    ret, data = ctx.get_global_state()
    ctx.close()
    if ret == 0:
        login_status = data.get('login_status', ['unknown'])[0] if hasattr(data, 'get') else 'connected'
        print(f'  OK  moomoo API — 接続成功 (status={login_status})')
        print()
        print('OpenD は正常にログイン済みです。取引システムを実行できます。')
    else:
        print(f'  NG  moomoo API — エラー: {data}')
        print('      OpenDの再起動が必要な可能性があります: sudo systemctl restart opend.service')
        sys.exit(1)
except Exception as e:
    print(f'  NG  moomoo API — 例外: {e}')
    print('      OpenDがログアウト状態の可能性があります。')
    print('      対処: sudo systemctl restart opend.service')
    sys.exit(1)
"
    ;;

  check)
    echo "=== importテスト ==="
    cd "$PROJECT_DIR"
    if [ -d "venv" ]; then source venv/bin/activate; fi
    if [ -d ".venv" ]; then source .venv/bin/activate; fi
    python3 -c "
import sys
errors = []
modules = [
    ('numpy', 'import numpy'),
    ('pandas', 'import pandas'),
    ('yfinance', 'import yfinance'),
    ('SQLAlchemy', 'import sqlalchemy'),
    ('pydantic', 'import pydantic'),
    ('requests', 'import requests'),
    ('yaml', 'import yaml'),
    ('データ取得 (fetcher)', 'from src.data.fetcher import get_ohlcv'),
    ('スクリーニング (screener)', 'from src.data.screener import run_screening'),
    ('戦略レジストリ (registry)', 'from src.strategy.registry import discover_strategies'),
    ('市場判定 (selector)', 'from src.strategy.selector import assess_market_condition'),
    ('バックテスト (engine)', 'from src.backtest.engine import run_backtest'),
    ('リスク管理 (manager)', 'from src.risk.manager import approve_trade'),
]
for name, stmt in modules:
    try:
        exec(stmt)
        print(f'  OK  {name}')
    except Exception as e:
        print(f'  NG  {name}: {e}')
        errors.append(name)
print()
if errors:
    print(f'エラー: {len(errors)}件のモジュールで問題があります')
    sys.exit(1)
else:
    print('全モジュールのimport OK')
"
    ;;

  fetch)
    echo "=== yfinance データ取得テスト ==="
    cd "$PROJECT_DIR"
    if [ -d "venv" ]; then source venv/bin/activate; fi
    if [ -d ".venv" ]; then source .venv/bin/activate; fi
    TICKER="${2:-AAPL}"
    echo "銘柄: ${TICKER}"
    echo ""
    python3 -c "
import yfinance as yf
ticker = '${TICKER}'
df = yf.download(ticker, period='5d', progress=False)
if df.empty:
    print(f'ERROR: {ticker} のデータを取得できませんでした')
else:
    print(df.to_string())
    print(f'\n取得行数: {len(df)}')
    print('データ取得 OK')
"
    ;;

  balance)
    echo "=== 口座残高 ==="
    cd "$PROJECT_DIR"
    if [ -d "venv" ]; then source venv/bin/activate; fi
    if [ -d ".venv" ]; then source .venv/bin/activate; fi
    python3 -c "
from src.broker.account import get_account_info
try:
    acc = get_account_info()
    print(f'Total Equity : \${acc.total_equity:,.2f} USD')
    print(f'Cash         : \${acc.cash:,.2f} USD')
    print(f'Market Value : \${acc.market_value:,.2f} USD')
    print(f'Positions    : {len(acc.positions)}件')
    if acc.positions:
        print()
        print('--- 保有ポジション ---')
        for p in acc.positions:
            print(f\"  {p['ticker']:6s}  {p['qty']}株  avg=\${p['avg_price']:.2f}  val=\${p['market_value']:.2f}  pnl=\${p['pnl']:.2f}\")
except Exception as e:
    print(f'ERROR: {e}')
    import sys; sys.exit(1)
"
    ;;

  simulate)
    echo "=== バックテスト（シミュレーション） ==="
    cd "$PROJECT_DIR"
    if [ -d "venv" ]; then source venv/bin/activate; fi
    if [ -d ".venv" ]; then source .venv/bin/activate; fi
    shift
    python3 scripts/simulate.py "$@"
    ;;

  deploy)
    echo "=== デプロイ（最新コード取得 & セットアップ） ==="
    cd "$PROJECT_DIR"
    echo "Pulling latest changes..."
    git pull origin main
    echo ""
    echo "Installing dependencies..."
    if [ -d "venv" ]; then source venv/bin/activate; fi
    if [ -d ".venv" ]; then source .venv/bin/activate; fi
    pip install -r requirements.txt --quiet
    echo ""
    echo "Deploy complete at $(date)"
    ;;

  *)
    echo "autoTRD 管理スクリプト"
    echo ""
    echo "使い方: $0 <command>"
    echo ""
    echo "--- 運用 ---"
    echo "  status     現在の状態を表示"
    echo "  start      タイマーを開始（自動実行ON）"
    echo "  stop       タイマーを停止（自動実行OFF）"
    echo "  run        今すぐ手動実行"
    echo "  log [N]    直近N行のログを表示（デフォルト50行）"
    echo "  time       実行スケジュールを表示"
    echo "  set-time   実行時刻を変更（例: $0 set-time 14:00）"
    echo "  install    systemdにサービス・タイマーを登録"
    echo ""
    echo "--- 動作確認（口座不要） ---"
    echo "  balance    口座残高を表示（USD）
  ping       OpenD接続確認（ログイン状態チェック）"
    echo "  check      importテスト（パッケージ不足の検出）"
    echo "  fetch [銘柄] yfinanceデータ取得テスト（デフォルト: AAPL）"
    echo "  simulate   バックテスト実行（過去データでシミュレーション）"
    echo "  deploy     GitHubから最新コード取得 & セットアップ"
    echo ""
    echo "時刻の目安 (UTC → JST):"
    echo "  13:00 UTC = 22:00 JST"
    echo "  13:30 UTC = 22:30 JST"
    echo "  14:00 UTC = 23:00 JST"
    ;;

esac
