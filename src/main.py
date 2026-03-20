"""Daily orchestrator — main entry point for the trading system."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from src.broker.account import get_account_info
from src.broker.executor import close_trade_log, create_trade_log, partial_close_trade_log, place_order
from src.data.fetcher import get_ohlcv
from src.data.screener import run_screening
from src.models.base import get_session, init_db
from src.models.portfolio import PortfolioSnapshot
from src.notify.notifier import send_notification
from src.risk.manager import approve_trade, check_daily_loss_limit
from src.strategy.base import Signal
from src.strategy.critic import evaluate_signal
from src.strategy.registry import discover_strategies, get_strategy
from src.strategy.selector import assess_market_condition, select_strategies
from src.utils.helpers import is_us_market_day, today_jst
from src.utils.logger import logger


def run_daily():
    """Execute the full daily trading workflow."""
    logger.info("=" * 60)
    logger.info(f"Daily run started at {datetime.utcnow().isoformat()}")
    logger.info(f"Mode: {'DRY_RUN' if settings.dry_run else 'LIVE'}")
    logger.info("=" * 60)

    # Skip non-trading days
    if not is_us_market_day():
        logger.info("Not a US market day, skipping")
        return

    # --- Step 1: DB初期化 & 戦略プラグイン読み込み ---
    init_db()
    discover_strategies()

    # --- Step 2: 口座情報の取得（残高・ポジション） ---
    account = get_account_info()
    logger.info(
        f"Account: equity=${account.total_equity:.2f}, "
        f"cash=${account.cash:.2f}, positions={len(account.positions)}"
    )

    # ポートフォリオのスナップショットをDBに保存（日次記録）
    _save_portfolio_snapshot(account)

    # 前日比の損失が上限（3%）を超えていたら新規エントリーを停止
    prev_snapshot = _get_previous_equity()
    if prev_snapshot > 0 and check_daily_loss_limit(account, prev_snapshot):
        msg = "日次損失上限に到達 — 新規エントリーを停止します"
        logger.warning(msg)
        send_notification("取引停止", msg, level="warning")
        return

    # --- Step 3: 市場環境の判定（S&P500トレンド・VIX・レジーム分類） ---
    market_condition = assess_market_condition()

    # --- Step 4: 銘柄スクリーニング（50銘柄→上位15銘柄に絞り込み） ---
    candidates = run_screening()
    logger.info(f"Screened {len(candidates)} candidates")

    # --- Step 5: シグナル生成 + Devil's Advocate（批判的評価）によるフィルタリング ---
    strategies = select_strategies(market_condition)
    buy_signals = []
    sell_signals = []
    rejected_signals = []
    forced_exit_orders = []

    # --- 強制エグジット（戦略固有チェック / SL / 段階TP / TP / 最大保有期間）---
    for pos in account.positions:
        ticker = pos["ticker"].replace("US.", "")
        df = get_ohlcv(ticker)
        if df.empty:
            continue
        current_price = float(df["Close"].iloc[-1])

        # TradeLogからSL/TP/エントリー日を取得
        trade_info = _get_open_trade_info(ticker)
        if not trade_info:
            continue

        sl = trade_info.get("stop_loss") or 0
        tp = trade_info.get("take_profit") or 0
        tp1 = trade_info.get("take_profit_1") or 0
        max_hold = trade_info.get("max_hold_days") or 20
        entry_date = trade_info.get("entry_date")
        trade_qty = trade_info.get("quantity") or (pos.get("qty") or 0)
        broker_qty = pos.get("qty") or 0

        # (1) 戦略固有のエグジットチェック
        suppress_tp = False
        strategy_name = trade_info.get("strategy_name", "")
        try:
            strategy = get_strategy(strategy_name)
            decision = strategy.check_exit(ticker, df, trade_info)
            if decision is not None:
                if decision.should_exit:
                    if broker_qty > 0:
                        forced_signal = Signal(
                            ticker=ticker, action="SELL", confidence=1.0,
                            stop_loss=0, take_profit=0, reason=decision.reason,
                            price=current_price,
                        )
                        order = place_order(forced_signal, broker_qty)
                        close_trade_log(ticker, order, current_price)
                        forced_exit_orders.append(f"FORCED-EXIT {broker_qty}x {ticker}: {decision.reason}")
                        logger.info(f"Strategy exit: {decision.reason}")
                    continue
                suppress_tp = decision.suppress_tp
        except KeyError:
            pass  # 戦略が見つからない場合はデフォルトロジックを使用

        exit_reason = None

        # (2) ストップロス（常にチェック）
        if sl > 0 and current_price <= sl:
            exit_reason = f"ストップロス発動 (SL=${sl:.2f}, 現在=${current_price:.2f})"

        # (3) 段階利確（TP1）: 半分を決済
        elif tp1 > 0 and current_price >= tp1:
            half_qty = max(trade_qty // 2, 1)
            if broker_qty > 0 and half_qty < broker_qty:
                forced_signal = Signal(
                    ticker=ticker, action="SELL", confidence=1.0,
                    stop_loss=0, take_profit=0,
                    reason=f"段階利確TP1到達 (TP1=${tp1:.2f}, 現在=${current_price:.2f})",
                    price=current_price,
                )
                order = place_order(forced_signal, half_qty)
                partial_close_trade_log(ticker, order, current_price, half_qty)
                forced_exit_orders.append(
                    f"PARTIAL-EXIT {half_qty}x {ticker}: 段階利確TP1=${tp1:.2f}"
                )
                logger.info(f"Staged TP1: sold {half_qty} of {broker_qty} shares")
            elif broker_qty > 0:
                # 1株しかない場合は全量決済
                exit_reason = f"段階利確TP1到達・全量決済 (TP1=${tp1:.2f}, 現在=${current_price:.2f})"

        # (4) 通常利確（suppress_tp=Trueならスキップ）
        elif not suppress_tp and tp > 0 and current_price >= tp:
            exit_reason = f"利確ターゲット到達 (TP=${tp:.2f}, 現在=${current_price:.2f})"

        # (5) 最大保有期間
        elif entry_date and max_hold > 0:
            holding_days = (today_jst() - entry_date).days
            if holding_days >= max_hold:
                exit_reason = f"最大保有期間{max_hold}日超過 ({holding_days}日経過)"

        if exit_reason and broker_qty > 0:
            forced_signal = Signal(
                ticker=ticker, action="SELL", confidence=1.0,
                stop_loss=0, take_profit=0, reason=exit_reason,
                price=current_price,
            )
            order = place_order(forced_signal, broker_qty)
            close_trade_log(ticker, order, current_price)
            forced_exit_orders.append(f"FORCED-EXIT {broker_qty}x {ticker}: {exit_reason}")
            logger.info(f"Forced exit: {exit_reason}")

    # 強制エグジット後にアカウント情報を再取得
    if forced_exit_orders:
        account = get_account_info()

    # 保有ポジションに対して売却シグナルをチェック
    for pos in account.positions:
        ticker = pos["ticker"].replace("US.", "")
        df = get_ohlcv(ticker)
        if df.empty:
            continue
        for strategy in strategies:
            signal = strategy.generate_signals(ticker, df, market_condition)
            if signal and signal.action == "SELL":
                # Critic evaluates SELL signals too (prevents panic selling)
                verdict = evaluate_signal(signal, df, market_condition, strategy.name)
                if verdict.approved:
                    signal.confidence = verdict.adjusted_confidence
                    sell_signals.append(signal)
                else:
                    rejected_signals.append((signal, verdict))
                break

    # スクリーニング通過銘柄に対して買いシグナルをチェック
    for candidate in candidates:
        ticker = candidate["ticker"]
        df = get_ohlcv(ticker, ensure_updated=False)
        if df.empty:
            continue
        for strategy in strategies:
            signal = strategy.generate_signals(ticker, df, market_condition)
            if signal and signal.action == "BUY":
                # Devil's Advocate critically evaluates every BUY signal
                verdict = evaluate_signal(signal, df, market_condition, strategy.name)
                if verdict.approved:
                    signal.confidence = verdict.adjusted_confidence
                    buy_signals.append(signal)
                else:
                    rejected_signals.append((signal, verdict))
                break

    logger.info(
        f"Signals: {len(buy_signals)} BUY, {len(sell_signals)} SELL, "
        f"{len(rejected_signals)} REJECTED by critic"
    )

    # --- Step 6: 注文実行（リスク管理チェック後に発注） ---
    executed_orders = []

    # 売り注文を先に処理（資金を解放してから買いに回す）
    for signal in sell_signals:
        approval = approve_trade(signal, account)
        if approval.approved:
            # Find quantity from existing position
            pos = next(
                (p for p in account.positions if signal.ticker in p["ticker"]),
                None,
            )
            qty = pos["qty"] if pos else 0
            if qty > 0:
                order = place_order(signal, qty)
                close_trade_log(signal.ticker, order, signal.take_profit)
                executed_orders.append(
                    f"SELL {qty}x {signal.ticker}: {signal.reason[:60]}"
                )

    # 買い注文を信頼度順に処理（ポジションサイズはリスク管理が算出）
    buy_signals.sort(key=lambda s: s.confidence, reverse=True)
    for signal in buy_signals:
        # Refresh account after sells
        account = get_account_info()
        approval = approve_trade(signal, account)
        if approval.approved and approval.quantity > 0:
            order = place_order(signal, approval.quantity)
            # signal生成元の戦略名を特定してorderに設定
            order.strategy_name = _find_strategy_name_for_signal(signal, strategies)
            create_trade_log(signal, order, approval.quantity)
            executed_orders.append(
                f"BUY {approval.quantity}x {signal.ticker}: {signal.reason[:60]}"
            )

    # --- Step 7: 日次レポート作成 & Slack通知 ---
    summary = _build_summary(
        account, market_condition, candidates,
        forced_exit_orders + executed_orders, rejected_signals
    )
    logger.info(summary)
    send_notification("日次トレーディングレポート", summary)

    logger.info("Daily run completed")


def _find_strategy_name_for_signal(signal: Signal, strategies: list) -> str:
    """シグナルのreasonから戦略名を特定する。"""
    for s in strategies:
        if s.name in (signal.reason or ""):
            return s.name
    # reasonにSMA/RSI/Breakoutキーワードがあれば推定
    reason_lower = (signal.reason or "").lower()
    for s in strategies:
        if s.name.replace("_", " ") in reason_lower or s.name.split("_")[0] in reason_lower:
            return s.name
    return strategies[0].name if strategies else "unknown"


def _get_open_trade_info(ticker: str) -> dict | None:
    """TradeLogからオープンポジションのSL/TP/エントリー日を取得する。"""
    from sqlalchemy import select

    from src.models.trade import TradeLog

    with get_session() as session:
        trade = session.execute(
            select(TradeLog)
            .where(TradeLog.ticker == ticker)
            .where(TradeLog.status == "OPEN")
            .order_by(TradeLog.entry_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if not trade:
            return None
        return {
            "stop_loss": trade.stop_loss or 0,
            "take_profit": trade.take_profit or 0,
            "take_profit_1": trade.take_profit_1 or 0,
            "max_hold_days": trade.max_hold_days or 20,
            "entry_date": trade.entry_date,
            "entry_price": trade.entry_price,
            "quantity": trade.quantity,
            "strategy_name": trade.strategy_name,
        }


def _save_portfolio_snapshot(account) -> None:
    from sqlalchemy import select

    with get_session() as session:
        existing = session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.date == today_jst())
        ).scalar_one_or_none()

        if existing:
            existing.total_equity = account.total_equity
            existing.cash = account.cash
            existing.positions_json = account.positions
            existing.num_positions = len(account.positions)
        else:
            snapshot = PortfolioSnapshot(
                date=today_jst(),
                total_equity=account.total_equity,
                cash=account.cash,
                positions_json=account.positions,
                num_positions=len(account.positions),
            )
            session.add(snapshot)
        session.commit()


def _get_previous_equity() -> float:
    from sqlalchemy import select

    from src.models.portfolio import PortfolioSnapshot

    with get_session() as session:
        result = session.execute(
            select(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.date.desc())
            .offset(1)
            .limit(1)
        ).scalar_one_or_none()
        return result.total_equity if result else 0.0


def _build_summary(
    account, market_condition, candidates, executed_orders, rejected_signals=None
) -> str:
    rejected_signals = rejected_signals or []
    regime_ja = {"trending": "トレンド", "range": "レンジ", "volatile": "高ボラ"}.get(
        market_condition.get("regime", ""), market_condition.get("regime", "N/A")
    )
    trend_ja = {"bull": "強気", "bear": "弱気", "neutral": "中立"}.get(
        market_condition.get("sp500_trend", ""), market_condition.get("sp500_trend", "N/A")
    )
    lines = [
        f"日付: {today_jst()}",
        f"市場: {regime_ja} (S&P500: {trend_ja}, VIX: {market_condition.get('vix_level', 0):.1f})",
        f"総資産: ${account.total_equity:.2f} | 現金: ${account.cash:.2f}",
        f"保有ポジション: {len(account.positions)}",
        f"スクリーニング候補: {len(candidates)}銘柄",
        f"約定注文: {len(executed_orders)}件",
    ]
    for order in executed_orders:
        lines.append(f"  - {order}")
    if rejected_signals:
        lines.append(f"批判評価で却下: {len(rejected_signals)}件")
        for signal, verdict in rejected_signals:
            top_objection = verdict.objections[0].reason if verdict.objections else "N/A"
            lines.append(
                f"  x {signal.action} {signal.ticker} "
                f"(信頼度 {verdict.original_confidence:.2f}->{verdict.adjusted_confidence:.2f}): "
                f"{top_objection[:60]}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    run_daily()
