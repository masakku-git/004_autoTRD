#!/usr/bin/env python3
"""
週次報告 Markdown 生成スクリプト

使い方:
    python3 scripts/gen_weekly_report.py              # 今週
    python3 scripts/gen_weekly_report.py 2026-05-20   # 指定日が属する週

出力:
    weeklyReport/YYYY-WXX.md

対象期間は指定日（省略時は今日）が属する ISO 週（月〜日）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
import csv

PROJECT_DIR = Path(__file__).resolve().parent.parent
CSV_DIR = PROJECT_DIR / "data" / "csv_export"
LOG_DIR = PROJECT_DIR / "logs"
REPORT_DIR = PROJECT_DIR / "weeklyReport"


def get_week_range(base: date | None = None) -> tuple[date, date]:
    base = base or date.today()
    monday = base - timedelta(days=base.weekday())
    sunday = monday + timedelta(days=6)
    return monday, min(sunday, date.today())


def load_csv(name: str) -> list[dict]:
    path = CSV_DIR / f"{name}.csv"
    if not path.exists():
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def fmt_usd(val) -> str:
    if val is None or val == '':
        return 'N/A'
    try:
        v = float(val)
        sign = '+' if v > 0 else ''
        return f"${sign}{v:.2f}"
    except (ValueError, TypeError):
        return str(val)


def fmt_pct(val) -> str:
    if val is None or val == '':
        return 'N/A'
    try:
        v = float(val)
        sign = '+' if v > 0 else ''
        return f"{sign}{v:.2f}%"
    except (ValueError, TypeError):
        return str(val)


def get_git_log(week_start: date, week_end: date) -> str:
    try:
        result = subprocess.run(
            [
                'git', 'log', '--oneline',
                f'--after={week_start.isoformat()} 00:00:00',
                f'--before={week_end.isoformat()} 23:59:59',
            ],
            capture_output=True, text=True, cwd=PROJECT_DIR
        )
        return result.stdout.strip()
    except Exception:
        return ''


def count_errors(week_start: date, week_end: date) -> int:
    log_path = LOG_DIR / "autotrd.log"
    if not log_path.exists():
        return 0
    target_dates = {
        (week_start + timedelta(days=i)).isoformat()
        for i in range((week_end - week_start).days + 1)
    }
    count = 0
    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if ' ERROR ' in line or ' CRITICAL ' in line:
                if any(d in line[:12] for d in target_dates):
                    count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="週次報告を生成する")
    parser.add_argument(
        "date", nargs="?", metavar="YYYY-MM-DD",
        help="対象日（省略時: 今日）。指定日が属する週のレポートを生成する"
    )
    args = parser.parse_args()

    base_date: date | None = None
    if args.date:
        try:
            base_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"エラー: 日付の形式が正しくありません: {args.date}（YYYY-MM-DD で指定してください）")
            sys.exit(1)

    week_start, week_end = get_week_range(base_date)
    iso_year, iso_week, _ = week_start.isocalendar()

    REPORT_DIR.mkdir(exist_ok=True)
    report_path = REPORT_DIR / f"{iso_year}-W{iso_week:02d}.md"

    trades = load_csv("trade_log")
    snapshots = load_csv("portfolio_snapshots")
    market_rows = load_csv("market_conditions")

    # 今週決済したトレード
    week_closed = [
        t for t in trades
        if t.get('status') == 'CLOSED'
        and t.get('exit_date', '')
        and week_start.isoformat() <= t['exit_date'] <= week_end.isoformat()
    ]
    # 保有中ポジション
    open_positions = [t for t in trades if t.get('status') == 'OPEN']

    # 最新スナップショット
    latest_snap = max(snapshots, key=lambda x: x.get('date', ''), default=None)

    # スナップショットから現在損益を取得
    positions_pnl: dict[str, dict] = {}
    if latest_snap and latest_snap.get('positions_json'):
        try:
            for p in json.loads(latest_snap['positions_json']):
                ticker = p.get('ticker', '').replace('US.', '')
                positions_pnl[ticker] = p
        except (json.JSONDecodeError, TypeError):
            pass

    # 今週の市況
    week_market = sorted(
        [m for m in market_rows
         if week_start.isoformat() <= m.get('date', '') <= week_end.isoformat()],
        key=lambda x: x.get('date', '')
    )

    # サマリー計算
    pnl_values = [float(t['pnl']) for t in week_closed if t.get('pnl') not in ('', None)]
    week_pnl = sum(pnl_values)
    win_trades = [t for t in week_closed if t.get('pnl') and float(t['pnl']) > 0]
    lose_trades = [t for t in week_closed if t.get('pnl') and float(t['pnl']) <= 0]
    win_rate = len(win_trades) / len(week_closed) * 100 if week_closed else 0

    best = max(week_closed, key=lambda t: float(t.get('pnl') or 0), default=None)
    worst = min(week_closed, key=lambda t: float(t.get('pnl') or 0), default=None)

    total_equity_str = 'N/A'
    if latest_snap:
        try:
            total_equity_str = f"${float(latest_snap['total_equity']):,.2f}"
        except (ValueError, TypeError):
            pass

    git_log = get_git_log(week_start, week_end)
    error_count = count_errors(week_start, week_end)

    # ── Markdown 生成 ─────────────────────────────────────────
    lines: list[str] = []

    lines += [
        f"# autoTRD 週次報告 — {iso_year}年第{iso_week}週"
        f"（{week_start.strftime('%m/%d')}〜{week_end.strftime('%m/%d')}）",
        "",
        f"生成日時: {date.today().isoformat()}",
        "",
        "---",
        "",
    ]

    # 1. 週次サマリー
    lines += ["## 1. 週次サマリー", ""]
    lines += [
        "| 項目 | 値 |",
        "|---|---|",
        f"| 週間決済損益 | {fmt_usd(week_pnl)} |",
        f"| 総資産（直近スナップショット） | {total_equity_str} |",
        f"| 決済トレード数 | {len(week_closed)}件 |",
        f"| 勝率 | {win_rate:.0f}%（{len(win_trades)}勝 {len(lose_trades)}敗） |",
    ]
    if best and best.get('pnl'):
        lines.append(f"| 最大利益 | {best['ticker']} {fmt_usd(best['pnl'])}（{fmt_pct(best.get('pnl_pct'))}） |")
    if worst and worst.get('pnl'):
        lines.append(f"| 最大損失 | {worst['ticker']} {fmt_usd(worst['pnl'])}（{fmt_pct(worst.get('pnl_pct'))}） |")
    lines.append(f"| ERRORログ件数 | {error_count}件 |")
    lines += ["", "---", ""]

    # 2. 決済履歴
    lines += ["## 2. 決済履歴（今週）", ""]
    if week_closed:
        lines += [
            "| 銘柄 | 戦略 | エントリー日 | 決済日 | 損益 | 損益率 |",
            "|---|---|---|---|---|---|",
        ]
        for t in sorted(week_closed, key=lambda x: x.get('exit_date', '')):
            lines.append(
                f"| {t['ticker']} | {t.get('strategy_name','')} "
                f"| {t.get('entry_date','')} | {t.get('exit_date','')} "
                f"| {fmt_usd(t.get('pnl'))} | {fmt_pct(t.get('pnl_pct'))} |"
            )
    else:
        lines.append("今週の決済トレードはありません。")
    lines += ["", "---", ""]

    # 3. 保有ポジション
    lines += ["## 3. 保有ポジション", ""]
    if open_positions:
        lines += [
            "| 銘柄 | 戦略 | エントリー日 | 取得価格 | 数量 | 現在損益 |",
            "|---|---|---|---|---|---|",
        ]
        for t in open_positions:
            ticker = t['ticker']
            pnl_info = positions_pnl.get(ticker, {})
            current_pnl = fmt_usd(pnl_info.get('pnl')) if pnl_info else 'N/A'
            try:
                price_str = f"${float(t.get('entry_price', 0)):.2f}"
            except (ValueError, TypeError):
                price_str = t.get('entry_price', '')
            lines.append(
                f"| {ticker} | {t.get('strategy_name','')} "
                f"| {t.get('entry_date','')} | {price_str} "
                f"| {t.get('quantity','')} | {current_pnl} |"
            )
    else:
        lines.append("保有ポジションはありません。")
    lines += ["", "---", ""]

    # 4. 市況状況
    lines += ["## 4. 市況状況（今週）", ""]
    if week_market:
        lines += [
            "| 日付 | S&P500トレンド | VIX | レジーム |",
            "|---|---|---|---|",
        ]
        for m in week_market:
            try:
                vix = f"{float(m.get('vix_level', 0)):.2f}"
            except (ValueError, TypeError):
                vix = m.get('vix_level', '')
            lines.append(
                f"| {m.get('date','')} | {m.get('sp500_trend','')} "
                f"| {vix} | {m.get('regime','')} |"
            )
    else:
        lines.append("今週の市況データがありません。")
    lines += ["", "---", ""]

    # 5. システム変更点
    lines += ["## 5. システム変更点", ""]
    if git_log:
        lines += ["```", git_log, "```"]
    else:
        lines.append("今週のコミットはありません。")
    lines += ["", "---", ""]

    # 6. 振り返り（Claude が記入）
    lines += [
        "## 6. 今週の振り返り",
        "",
        "### 良かった点",
        "",
        "<!-- Claude が以下のデータをもとに記入 -->",
        "",
        "### 悪かった点",
        "",
        "<!-- Claude が以下のデータをもとに記入 -->",
        "",
    ]

    report_path.write_text('\n'.join(lines), encoding='utf-8')

    print(f"✅ レポート生成完了: {report_path}")
    print(f"   対象期間: {week_start} 〜 {week_end}")
    print(f"   決済トレード: {len(week_closed)}件 / 保有ポジション: {len(open_positions)}件")


if __name__ == "__main__":
    main()
