#!/usr/bin/env python3
"""data/moomoo_eod_history.csv から portfolio_snapshots を後方補正。

main.py の旧スナップショット保存ロジックは JST 22:00 (= 米市場オープン直前) に走るため
total_equity が "前 US 営業日の close" を反映しており、moomoo 画面の同日値とずれる。
本スクリプトは CSV に手動で記録した moomoo の EOD クローズ値で既存レコードを上書きし、
ダッシュボードの履歴グラフを moomoo と一致させる。

使い方:
    1. data/moomoo_eod_history.csv の total_equity 列に moomoo の値を記入
    2. python3 scripts/backfill_eod_history.py
    3. ダッシュボード再生成 (bash maintenance/dashboard.sh --no-sync)

仕様:
    - total_equity 空欄行はスキップ（部分的に書ける）
    - 既存レコードがあれば UPDATE、なければ INSERT
    - positions_json は空配列 [] に上書き
      （viz_trades.py の _recompute_snapshot が qty=0 端株を引かないように）
    - cash は CSV に書かれていれば反映、空欄なら既存値を保持
    - num_positions は既存値を保持（CSV にこの情報は無いため）
    - 冪等: 何度実行しても同じ結果
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from src.models.base import get_session  # noqa: E402
from src.models.portfolio import PortfolioSnapshot  # noqa: E402

CSV_PATH = PROJECT_ROOT / "data" / "moomoo_eod_history.csv"


def parse_csv(path: Path) -> list[tuple[date, float, float | None]]:
    """CSV を読み (date, total_equity, cash) のリストに変換。空欄行や invalid 行はスキップ。"""
    rows: list[tuple[date, float, float | None]] = []
    if not path.exists():
        raise SystemExit(f"CSV が見つかりません: {path}")
    with path.open() as f:
        for i, raw in enumerate(f, start=1):
            line = raw.rstrip("\n").strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("date,"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            date_s, total_s = parts[0], parts[1]
            cash_s = parts[2] if len(parts) >= 3 else ""
            if not total_s:
                continue
            try:
                d = date.fromisoformat(date_s)
            except ValueError:
                print(f"  ⚠ line {i}: 不正な日付 '{date_s}' → スキップ")
                continue
            try:
                total = float(total_s.replace("$", "").replace(",", ""))
            except ValueError:
                print(f"  ⚠ line {i}: 不正な total_equity '{total_s}' → スキップ")
                continue
            cash_val: float | None = None
            if cash_s:
                try:
                    cash_val = float(cash_s.replace("$", "").replace(",", ""))
                except ValueError:
                    print(f"  ⚠ line {i}: 不正な cash '{cash_s}' → 空欄扱い")
            rows.append((d, total, cash_val))
    return rows


def backfill(rows: list[tuple[date, float, float | None]]) -> tuple[int, int]:
    """rows を DB に反映。(updated_count, inserted_count) を返す。"""
    updated = inserted = 0
    with get_session() as s:
        for d, total, cash_val in rows:
            existing = s.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.date == d)
            ).scalar_one_or_none()
            if existing:
                existing.total_equity = total
                existing.positions_json = []  # 端株差し引きをバイパス
                if cash_val is not None:
                    existing.cash = cash_val
                action = "updated"
                updated += 1
            else:
                s.add(
                    PortfolioSnapshot(
                        date=d,
                        total_equity=total,
                        cash=cash_val if cash_val is not None else 0.0,
                        positions_json=[],
                        num_positions=0,
                    )
                )
                action = "inserted"
                inserted += 1
            cash_str = f" cash=${cash_val:,.2f}" if cash_val is not None else ""
            print(f"  {action}: {d} total=${total:,.2f}{cash_str}")
        s.commit()
    return updated, inserted


def main() -> None:
    print(f"📄 CSV: {CSV_PATH}")
    rows = parse_csv(CSV_PATH)
    if not rows:
        print("更新対象なし（total_equity 空欄のみ）")
        return
    print(f"  → {len(rows)} 件の有効行を検出\n")
    updated, inserted = backfill(rows)
    print(f"\n✅ 完了: updated={updated}, inserted={inserted}")
    print("   ダッシュボード再生成: bash maintenance/dashboard.sh --no-sync")


if __name__ == "__main__":
    main()
