"""約定確認の遅延ジョブ — moomoo の実約定情報で Order/TradeLog を更新する。

main.py が 22:00 JST に発注した成行注文は、米国市場の寄付き（22:30〜23:30 JST）で
約定する。executor._poll_for_fill は60秒で打ち切るため、ほぼ全件で filled_price が
DB に反映されない（status='SUBMITTED' のまま）。

本スクリプトは寄付き完了後（systemd timer で 01:00 JST = 16:00 UTC）に走り、
moomoo から実約定情報を取得して Order と TradeLog を実際の dealt_avg_price で
更新する。

Usage:
    python scripts/reconcile_fills.py              # 当日分のみ
    python scripts/reconcile_fills.py --days 30    # 過去N日分（バックフィル用）
    python scripts/reconcile_fills.py --dry-run    # DBは更新せず差分のみ表示
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from config.settings import settings
from src.models.base import get_session
from src.models.trade import Order, TradeLog
from src.notify.notifier import send_notification
from src.utils.helpers import utcnow
from src.utils.logger import logger

# OpenD が応答不能な場合に無期限ハングしないためのタイムアウト（秒）
_OPEND_TIMEOUT_SEC = 30


def _trd_env():
    from moomoo import TrdEnv
    return TrdEnv.SIMULATE if settings.moomoo_trade_env == "SIMULATE" else TrdEnv.REAL


def _open_ctx():
    from moomoo import OpenSecTradeContext, SecurityFirm, TrdMarket
    return OpenSecTradeContext(
        host=settings.moomoo_host,
        port=settings.moomoo_port,
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.FUTUJP,
    )


def _fetch_broker_orders(days: int) -> dict:
    """moomoo から注文一覧を取得し、broker_order_id をキーにした dict を返す。

    OpenD が無応答の場合に備え、broker/account.py と同じ
    ThreadPoolExecutor + タイムアウトのパターンで保護する。
    """

    def _query() -> dict:
        ctx = _open_ctx()
        try:
            if days <= 1:
                ret, df = ctx.order_list_query(trd_env=_trd_env(), acc_id=settings.moomoo_acc_id)
            else:
                end = utcnow().date()
                start = end - timedelta(days=days)
                ret, df = ctx.history_order_list_query(
                    start=start.isoformat(),
                    end=end.isoformat(),
                    trd_env=_trd_env(),
                    acc_id=settings.moomoo_acc_id,
                )
            if ret != 0:
                raise RuntimeError(f"moomoo query failed: {df}")
            if df is None or df.empty:
                return {}
            return {str(row["order_id"]): row for _, row in df.iterrows()}
        finally:
            ctx.close()

    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_query)
        try:
            return future.result(timeout=_OPEND_TIMEOUT_SEC)
        except FuturesTimeoutError:
            raise RuntimeError(
                f"OpenD接続タイムアウト（{_OPEND_TIMEOUT_SEC}秒）— OpenDのセッションを確認してください"
            )


_PROBE_REF_TICKERS = ("T", "KO")  # 買付余力の照会に使う参照銘柄（株価が低く、1株単位の分解能が細かい順）


def build_probe_line(label: str, funds: dict, ref: tuple | None, sells_today: list[tuple]) -> str:
    """売却約定後に売却代金が買付余力として使えているかを判定するための1行ログを組み立てる（純粋関数）。

    funds: accinfo_query の cash/us_cash/avl_withdrawal_cash/frozen_cash/power
    ref: (参照銘柄, 参照価格, max_cash_buy) または None
    sells_today: 当日約定の売り [(銘柄, 株数, 約定価格)]
    読み方: max_cash_buy(指値・参照価格) が cash/参照価格 とほぼ一致すれば、売却代金を含む現金全体が
    買付余力として使えている。売却代金分だけ少なければ、その分は未受渡で使えていない。
    """
    proceeds = sum(q * px for _, q, px in sells_today)
    parts = [
        f"label={label}",
        f"cash={funds.get('cash')}", f"us_cash={funds.get('us_cash')}",
        f"avl_withdrawal_cash={funds.get('avl_withdrawal_cash')}",
        f"frozen_cash={funds.get('frozen_cash')}", f"power={funds.get('power')}",
        f"sells_today={[f'{s}x{q}@{px:.2f}' for s, q, px in sells_today]}",
        f"sell_proceeds={proceeds:.2f}",
    ]
    if ref is not None:
        tk, px, max_buy = ref
        cash = funds.get("us_cash") if funds.get("us_cash") is not None else funds.get("cash")
        implied = int(float(cash) // px) if cash is not None and px else None
        without = int((float(cash) - proceeds) // px) if cash is not None and px else None
        parts.append(
            f"ref={tk}@{px:.2f} max_cash_buy={max_buy} cash_implied={implied} "
            f"if_proceeds_unusable={without}"
        )
    return "BUYPOWER-PROBE " + " ".join(parts)


def _query_probe(ref_price_lookup) -> tuple[dict, tuple | None]:
    """accinfo_query と acctradinginfo_query（指値, 参照銘柄）で現在の現金と買付余力を読む（読み取りのみ）。"""
    from moomoo import Currency, OrderType

    def _q() -> tuple[dict, tuple | None]:
        ctx = _open_ctx()
        try:
            ret, f = ctx.accinfo_query(trd_env=_trd_env(), currency=Currency.USD, acc_id=settings.moomoo_acc_id)
            if ret != 0:
                raise RuntimeError(f"accinfo_query failed: {f}")
            funds = {k: f[k].iloc[0] for k in ("cash", "us_cash", "avl_withdrawal_cash", "frozen_cash", "power") if k in f}
            funds = {k: (None if v != v or v == "N/A" else v) for k, v in funds.items()}
            ref = None
            for tk in _PROBE_REF_TICKERS:
                px = ref_price_lookup(tk)
                if not px:
                    continue
                ret, d = ctx.acctradinginfo_query(
                    order_type=OrderType.NORMAL, code=f"US.{tk}", price=float(px),
                    trd_env=_trd_env(), acc_id=settings.moomoo_acc_id,
                )
                if ret == 0:
                    ref = (tk, float(px), int(d.iloc[0]["max_cash_buy"]))
                    break
            return funds, ref
        finally:
            ctx.close()

    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_q)
        try:
            return future.result(timeout=_OPEND_TIMEOUT_SEC)
        except FuturesTimeoutError:
            raise RuntimeError(f"OpenD接続タイムアウト（{_OPEND_TIMEOUT_SEC}秒）— 買付余力の照会に失敗")


def probe_buying_power(label: str = "post_fill") -> str:
    """当日約定した売りの代金が買付余力として使えているかを、読み取りのみで記録する。

    売却と購入の実行タイミングを分ける設計（future_improvements.md）の前提確認用。
    reconcile は寄付き約定の後（01:00 JST）に走るので、「同じ米国営業日の約定直後」の状態を見られる。
    失敗しても呼び出し元の処理には影響させない（ログのみ）。
    """
    from src.data.fetcher import get_ohlcv

    def _last_close(tk: str):
        df = get_ohlcv(tk, ensure_updated=False)
        return None if df.empty else float(df["Close"].iloc[-1])

    today = utcnow().date()
    with get_session() as session:
        sold = session.execute(
            select(Order).where(Order.side == "SELL", Order.status == "FILLED", Order.filled_price.isnot(None))
        ).scalars().all()
        sells_today = [(o.ticker, o.quantity, float(o.filled_price)) for o in sold
                       if o.created_at is not None and o.created_at.date() == today]
    funds, ref = _query_probe(_last_close)
    line = build_probe_line(label, funds, ref, sells_today)
    logger.info(line)
    return line


_FEE_BATCH = 400  # order_fee_query は1リクエスト最大400注文（レート制限: 30秒10回）


def _fetch_commissions(broker_order_ids: list[str]) -> dict[str, float]:
    """moomoo order_fee_query で注文ごとの手数料合計(fee_amount)を取得する。

    公式: https://openapi.moomoo.com/moomoo-api-doc/en/trade/order-fee-query.html
    模擬取引口座は手数料を照会できない（REAL のみ）。
    """
    if not broker_order_ids:
        return {}

    def _query() -> dict[str, float]:
        ctx = _open_ctx()
        try:
            result: dict[str, float] = {}
            for i in range(0, len(broker_order_ids), _FEE_BATCH):
                chunk = broker_order_ids[i:i + _FEE_BATCH]
                ret, df = ctx.order_fee_query(
                    order_id_list=chunk, trd_env=_trd_env(), acc_id=settings.moomoo_acc_id
                )
                if ret != 0:
                    raise RuntimeError(f"moomoo order_fee_query failed: {df}")
                for _, row in df.iterrows():
                    fee = row["fee_amount"]
                    if fee == fee:  # NaN除外
                        result[str(row["order_id"])] = round(float(fee), 4)
            return result
        finally:
            ctx.close()

    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_query)
        try:
            return future.result(timeout=_OPEND_TIMEOUT_SEC)
        except FuturesTimeoutError:
            raise RuntimeError(f"OpenD接続タイムアウト（{_OPEND_TIMEOUT_SEC}秒）— 手数料の取得に失敗")


def sync_commissions(dry_run: bool, fetch=_fetch_commissions) -> int:
    """FILLED なのに commission が未取得(NULL)の注文へ、実手数料を記録する。

    約定確認(reconcile)の直後に走らせる。commission が NULL の FILLED 注文を全件対象にするため、
    初回実行で過去分のバックフィルも兼ね、取得に失敗した日があっても翌日以降に自動で埋まる。
    """
    with get_session() as session:
        rows = session.execute(
            select(Order).where(
                Order.status == "FILLED",
                Order.broker_order_id.isnot(None),
                Order.commission.is_(None),
            )
        ).scalars().all()
        if not rows:
            logger.info("手数料が未取得の約定済み注文はありません")
            return 0

        fees = fetch([str(o.broker_order_id) for o in rows])
        updated = 0
        for order in rows:
            fee = fees.get(str(order.broker_order_id))
            if fee is None:
                logger.warning(f"手数料を取得できず: order id={order.id} {order.ticker}")
                continue
            logger.info(f"COMMISSION: order id={order.id} {order.side} {order.ticker} ${fee:.2f}")
            if not dry_run:
                order.commission = fee
            updated += 1
        if not dry_run:
            session.commit()
    logger.info(f"手数料を記録: {updated}/{len(rows)}件 (dry_run={dry_run})")
    return updated


def _update_trade_log(session, order: Order, actual_price: float) -> None:
    """Orderの side に応じて TradeLog の entry_price / exit_price と pnl を更新する。

    - BUY: entry_price を実約定価格に置き換え。CLOSED 済みなら pnl も再計算
    - SELL: trade_log.exit_order_id == order.id の trade を更新

    同一注文を複数の trade_log 行が参照するケースに対応するため全行を更新する
    （段階決済の分割CLOSED行が entry_order_id を共有する / 複数OPEN行の一括
    クローズが exit_order_id を共有する）。
    """
    if order.side == "BUY":
        trades = session.execute(
            select(TradeLog).where(TradeLog.entry_order_id == order.id)
        ).scalars().all()
        for trade in trades:
            old = trade.entry_price
            trade.entry_price = actual_price
            if trade.status == "CLOSED" and trade.exit_price is not None:
                trade.pnl = (trade.exit_price - actual_price) * trade.quantity
                if actual_price > 0:
                    trade.pnl_pct = (trade.exit_price / actual_price - 1) * 100
            logger.info(
                f"  → trade_log id={trade.id} {trade.ticker}: entry_price ${old:.2f} → ${actual_price:.2f}"
            )
    elif order.side == "SELL":
        trades = session.execute(
            select(TradeLog).where(TradeLog.exit_order_id == order.id)
        ).scalars().all()
        for trade in trades:
            old = trade.exit_price
            trade.exit_price = actual_price
            trade.pnl = (actual_price - trade.entry_price) * trade.quantity
            if trade.entry_price > 0:
                trade.pnl_pct = (actual_price / trade.entry_price - 1) * 100
            old_str = f"${old:.2f}" if old is not None else "None"
            logger.info(
                f"  → trade_log id={trade.id} {trade.ticker}: exit_price {old_str} → ${actual_price:.2f} "
                f"(pnl=${trade.pnl:.2f}, {trade.pnl_pct:.2f}%)"
            )


def reconcile(days: int, dry_run: bool) -> dict:
    """SUBMITTED な Order を moomoo の実約定情報で更新し、関連 TradeLog も補正する。"""
    broker_orders = _fetch_broker_orders(days)
    logger.info(f"moomoo から {len(broker_orders)} 件の注文情報を取得（過去{days}日分）")

    counts = {"filled": 0, "cancelled": 0, "partial": 0, "pending": 0, "missing": 0}

    with get_session() as session:
        rows = session.execute(
            select(Order).where(
                Order.status == "SUBMITTED",
                Order.broker_order_id.isnot(None),
            )
        ).scalars().all()

        logger.info(f"DB に SUBMITTED 状態の注文が {len(rows)} 件")

        for order in rows:
            broker = broker_orders.get(str(order.broker_order_id))
            if broker is None:
                logger.debug(f"order id={order.id}: moomoo側に該当なし（範囲外）")
                counts["missing"] += 1
                continue

            broker_status = str(broker["order_status"])
            dealt_price = float(broker.get("dealt_avg_price", 0) or 0)

            if broker_status == "FILLED_ALL" and dealt_price > 0:
                ref = order.price if order.price is not None else 0.0
                logger.info(
                    f"FILLED: order id={order.id} {order.side} {order.ticker} "
                    f"@ ${dealt_price:.2f} (ref=${ref:.2f}, "
                    f"diff={(dealt_price - ref):+.2f}/{((dealt_price/ref-1)*100 if ref else 0):+.2f}%)"
                )
                if not dry_run:
                    order.filled_price = dealt_price
                    order.filled_at = utcnow()
                    order.status = "FILLED"
                    _update_trade_log(session, order, dealt_price)
                counts["filled"] += 1

            elif broker_status in ("CANCELLED_ALL", "CANCELLED_PART", "FAILED"):
                logger.warning(
                    f"CANCELLED/FAILED: order id={order.id} {order.ticker} broker_status={broker_status}"
                )
                if not dry_run:
                    order.status = "CANCELLED"
                counts["cancelled"] += 1

            elif broker_status == "FILLED_PART":
                logger.warning(
                    f"PARTIAL: order id={order.id} {order.ticker} "
                    f"dealt_qty={broker.get('dealt_qty')}, avg=${dealt_price:.2f} — 要手動確認"
                )
                counts["partial"] += 1

            else:
                logger.info(f"PENDING: order id={order.id} {order.ticker} broker_status={broker_status}")
                counts["pending"] += 1

        if not dry_run:
            session.commit()

    logger.info(
        f"完了: filled={counts['filled']}, cancelled={counts['cancelled']}, "
        f"partial={counts['partial']}, pending={counts['pending']}, missing={counts['missing']} "
        f"(dry_run={dry_run})"
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="moomoo の実約定情報で Order/TradeLog を更新")
    parser.add_argument("--days", type=int, default=1, help="遡る日数（1=当日のみ、>=2でhistory_order_list_query）")
    parser.add_argument("--dry-run", action="store_true", help="DBを更新せず差分のみ表示")
    args = parser.parse_args()
    try:
        reconcile(days=args.days, dry_run=args.dry_run)
        try:
            probe_buying_power("post_fill")
        except Exception:
            logger.exception("買付余力の記録に失敗（約定照合には影響なし）")
        try:
            sync_commissions(dry_run=args.dry_run)
        except Exception as e:
            # 手数料の取得失敗は約定照合の成否に影響させない（翌日以降に自動で再取得される）
            logger.exception("手数料の記録に失敗")
            send_notification(
                "手数料の記録失敗 (reconcile_fills)",
                "約定済み注文の手数料(commission)をmoomooから取得できませんでした。\n"
                "影響: 手数料の集計が一時的に欠けるだけで、売買・損益(pnl)には影響しません。\n"
                "翌日の実行で未取得分は自動的に再取得されます。\n\n"
                f"{type(e).__name__}: {e}",
                level="warning",
            )
    except Exception as e:
        logger.exception("reconcile_fills failed")
        send_notification(
            "約定情報の照合失敗 (reconcile_fills)",
            "moomooの実約定価格がOrder/TradeLogに反映されていません。\n"
            "影響: entry/exit価格と実現損益(pnl)が仮値のままです。\n"
            "対応: scripts/reconcile_fills.py --days 2 を手動再実行してください。\n\n"
            f"{type(e).__name__}: {e}",
            level="error",
        )
        raise


if __name__ == "__main__":
    main()
