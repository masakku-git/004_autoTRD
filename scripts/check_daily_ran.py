"""日次実行の死活監視（ウォッチドッグ）— 「通知が来ないこと自体」を異常として検知する。

run_daily() の例外は main.py の except で通知されるが、次のケースは通知が一切出ない:
  - タイマーが発火しなかった / サービスが起動前に落ちた（venv破損・import失敗・OOM等）
  - プロセスが強制終了された（run_daily の except に到達しない）
2026-07 に「通知が消えた10日間」が起きたため、実行時刻の30〜40分後に別プロセスで
「今日の日次実行が最後まで走ったか」をログから確認し、走っていなければ Slack に通知する。

判定はログの開始/完了マーカーで行う（main.py の run_daily が出力）:
  ok       : "Daily run completed" または "Not a US market day, skipping"（米国休場日）
  crashed  : "Daily run started" はあるが完了していない（途中で落ちた）
  not_run  : 今日の開始ログ自体が無い（タイマー不発・起動失敗）

限界: VPS自体が停止していればこの監視も動かない。その場合の検知には外部の死活監視
（外部サービスからの定期確認）が必要。

使い方:
    python scripts/check_daily_ran.py            # 異常なら Slack 通知して終了コード1
    python scripts/check_daily_ran.py --quiet    # 通知せず判定結果だけ表示
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

LOG_PATH = Path(__file__).parent.parent / "data" / "autotrd.log"

STARTED = "Daily run started"
COMPLETED = "Daily run completed"
SKIPPED = "Not a US market day, skipping"


def daily_run_status(log_text: str, today: date) -> str:
    """今日(today)分のログ行だけを見て ok / crashed / not_run を返す（純粋関数）。"""
    prefix = today.isoformat()
    started = completed = False
    for line in log_text.splitlines():
        if not line.startswith(prefix):
            continue
        if COMPLETED in line or SKIPPED in line:
            completed = True
        elif STARTED in line:
            started = True
    if completed:
        return "ok"
    return "crashed" if started else "not_run"


def _read_today_log(today: date) -> str:
    if not LOG_PATH.exists():
        return ""
    return LOG_PATH.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="日次実行が今日最後まで走ったか確認する")
    parser.add_argument("--quiet", action="store_true", help="Slack通知せず判定結果だけ表示")
    args = parser.parse_args()

    from src.utils.helpers import today_jst

    today = today_jst()
    status = daily_run_status(_read_today_log(today), today)
    print(f"{today} daily run status: {status}")
    if status == "ok":
        return 0

    if not args.quiet:
        from src.notify.notifier import send_notification

        detail = {
            "not_run": "今日の日次実行が始まっていません（タイマー不発・起動失敗の可能性）。",
            "crashed": "日次実行は開始しましたが最後まで完了していません（途中で異常終了した可能性）。",
        }[status]
        send_notification(
            "日次実行の異常（ウォッチドッグ）",
            f"{detail}\n"
            "影響: 損切り・売却・新規エントリー・日次レポートが実行されていない可能性があります。\n"
            "対応: `journalctl -u autotrader.service -n 100` と data/autotrd.log を確認し、"
            "必要なら moomoo アプリで保有ポジションを手動確認してください。",
            level="error",
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
