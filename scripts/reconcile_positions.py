#!/usr/bin/env python3
"""trade_log の status=OPEN 行を moomoo の実保有と照合して整合させる。

過去の close_trade_log バグ等で trade_log に status=OPEN のまま残った行
(実際は売却済み) を CLOSED に書き換える。reconcile_fills.py は約定価格の補正までは
やるが、status=OPEN/CLOSED の整合まではタッチしないため、本スクリプトを別途用意。

挙動:
    - moomoo の保有 (qty>0) を ticker → qty の dict にまとめる
    - trade_log の status=OPEN 行を ticker 単位でグループ化、quantity を合計
    - moomoo qty < trade_log 合計 qty なら、古い建玉から FIFO で CLOSED にマーク
    - moomoo に該当 ticker が無い場合は全 OPEN 行を CLOSED
    - moomoo qty > trade_log 合計の場合は警告のみ (DB 側に無いブローカー持ち高)

CLOSED にマークする際の補完:
    - exit_date: 該当 ticker の最終 SELL Order の filled_at (なければ today)
    - exit_price: 該当 SELL Order の filled_price (なければ NULL)
    - pnl / pnl_pct: 算出不能なため NULL
    - notes: '[reconcile_positions] auto-closed: position not held in moomoo' を追記

使い方:
    python3 scripts/reconcile_positions.py             # 反映
    python3 scripts/reconcile_positions.py --dry-run   # 差分のみ表示、DB 変更なし
"""
from __future__ import annotations

import argparse
import sys
from datetime import date as date_cls
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from src.broker.account import get_account_info  # noqa: E402
from src.models.base import get_session  # noqa: E402
from src.models.trade import Order, TradeLog  # noqa: E402
from src.utils.helpers import today_jst  # noqa: E402
from src.utils.logger import logger  # noqa: E402


NOTE_PREFIX = "[reconcile_positions]"


def _broker_holdings() -> dict[str, int]:
    """moomoo から ticker (US. プレフィックス除去) → qty の dict を取得 (qty>0 のみ)。"""
    info = get_account_info()
    out: dict[str, int] = {}
    for p in info.positions:
        ticker = str(p.get("ticker", "")).replace("US.", "")
        qty = int(p.get("qty") or 0)
        if qty > 0 and ticker:
            out[ticker] = qty
    return out


def _latest_sell_order(session, ticker: str) -> Order | None:
    """ticker (US. 有無どちらも) で最新の FILLED な SELL Order を返す。"""
    rows = (
        session.execute(
            select(Order)
            .where(Order.side == "SELL")
            .where(Order.ticker.in_([ticker, f"US.{ticker}"]))
            .order_by(Order.filled_at.desc().nullslast(), Order.created_at.desc())
        )
        .scalars()
        .all()
    )
    for o in rows:
        if (o.status or "").upper() == "FILLED" or o.filled_price:
            return o
    return rows[0] if rows else None


def _close_trade(trade: TradeLog, sell_order: Order | None, dry: bool) -> dict:
    """trade を CLOSED にマーク。dry なら DB 更新しない。差分情報を返す。"""
    new_exit_date: date_cls = today_jst()
    new_exit_price: float | None = None
    if sell_order:
        if sell_order.filled_at:
            new_exit_date = sell_order.filled_at.date()
        if sell_order.filled_price:
            new_exit_price = float(sell_order.filled_price)

    note_line = (
        f"{NOTE_PREFIX} auto-closed: position not held in moomoo "
        f"(reconciled {today_jst()})"
    )
    new_notes = (trade.notes or "")
    if NOTE_PREFIX not in new_notes:
        new_notes = (new_notes + "\n" + note_line).strip() if new_notes else note_line

    diff = {
        "id": trade.id,
        "ticker": trade.ticker,
        "entry_date": trade.entry_date,
        "quantity": trade.quantity,
        "old_status": trade.status,
        "new_status": "CLOSED",
        "new_exit_date": new_exit_date,
        "new_exit_price": new_exit_price,
        "sell_order_id": sell_order.id if sell_order else None,
    }

    if not dry:
        trade.status = "CLOSED"
        trade.exit_date = new_exit_date
        if new_exit_price is not None and trade.exit_price is None:
            trade.exit_price = new_exit_price
        if sell_order and trade.exit_order_id is None:
            trade.exit_order_id = sell_order.id
        trade.notes = new_notes
    return diff


def reconcile(dry_run: bool = False) -> None:
    holdings = _broker_holdings()
    logger.info(f"moomoo 保有: {len(holdings)} 銘柄 {holdings}")

    closed_count = 0
    skipped_count = 0
    warned_count = 0
    diffs: list[dict] = []

    with get_session() as session:
        open_trades = (
            session.execute(
                select(TradeLog)
                .where(TradeLog.status == "OPEN")
                .order_by(TradeLog.entry_date.asc(), TradeLog.id.asc())
            )
            .scalars()
            .all()
        )

        # ticker でグループ化
        by_ticker: dict[str, list[TradeLog]] = {}
        for t in open_trades:
            by_ticker.setdefault(t.ticker, []).append(t)

        for ticker, rows in by_ticker.items():
            broker_qty = holdings.get(ticker, 0)
            log_qty = sum(int(r.quantity or 0) for r in rows)

            if broker_qty >= log_qty:
                # 全件正しい (or ブローカーの方が多い)
                if broker_qty > log_qty:
                    logger.warning(
                        f"  {ticker}: ブローカー {broker_qty} 株 > trade_log OPEN 合計 "
                        f"{log_qty} 株 (DB 側に未記録の保有あり)"
                    )
                    warned_count += 1
                else:
                    skipped_count += len(rows)
                continue

            # broker_qty < log_qty → 差分を古い行から CLOSED
            to_close = log_qty - broker_qty
            sell_order = _latest_sell_order(session, ticker)
            for r in rows:
                if to_close <= 0:
                    skipped_count += 1
                    continue
                q = int(r.quantity or 0)
                if q <= to_close:
                    diff = _close_trade(r, sell_order, dry_run)
                    diffs.append(diff)
                    closed_count += 1
                    to_close -= q
                else:
                    # 部分のみ余剰 (本来は段階決済で扱う領域)。安全側として今は skip
                    logger.warning(
                        f"  {ticker}: trade id={r.id} qty={q} は部分残し対応が必要 → スキップ"
                    )
                    warned_count += 1

        if not dry_run:
            session.commit()

    print("\n=== reconcile_positions 結果 ===")
    print(f"  closed   : {closed_count} 件")
    print(f"  skipped  : {skipped_count} 件 (整合済み)")
    print(f"  warned   : {warned_count} 件 (要手動確認)")
    if diffs:
        print("\n--- CLOSED にマークした trade_log ---")
        for d in diffs:
            mark = "[DRY]" if dry_run else "[OK] "
            ep = f"${d['new_exit_price']:.2f}" if d['new_exit_price'] else "(none)"
            print(
                f"  {mark} id={d['id']:>4} {d['ticker']:8} qty={d['quantity']:>3} "
                f"entry={d['entry_date']} → exit={d['new_exit_date']} price={ep}"
            )
    if dry_run:
        print("\n※ --dry-run: DB は変更されていません。本番反映には --dry-run を外して再実行。")
    else:
        print("\n✅ DB に反映しました。")


def main() -> None:
    parser = argparse.ArgumentParser(description="trade_log の OPEN を moomoo 実保有で整合")
    parser.add_argument("--dry-run", action="store_true", help="DB を更新せず差分のみ表示")
    args = parser.parse_args()
    reconcile(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
