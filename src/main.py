"""Daily orchestrator — main entry point for the trading system."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from src.broker.account import get_account_info
from src.broker.executor import close_trade_log, create_trade_log, place_order
from src.data.fetcher import get_ohlcv
from src.data.screener import run_screening
from src.models.base import get_session, init_db
from src.models.portfolio import PortfolioSnapshot
from src.notify.notifier import send_notification
from src.risk.manager import approve_trade, check_daily_loss_limit
from src.strategy.critic import evaluate_signal
from src.strategy.registry import discover_strategies
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
            order.strategy_name = signal.reason.split(".")[0] if signal.reason else "unknown"
            create_trade_log(signal, order, approval.quantity)
            executed_orders.append(
                f"BUY {approval.quantity}x {signal.ticker}: {signal.reason[:60]}"
            )

    # --- Step 7: 日次レポート作成 & Slack通知 ---
    summary = _build_summary(
        account, market_condition, candidates, executed_orders, rejected_signals
    )
    logger.info(summary)
    send_notification("日次トレーディングレポート", summary)

    logger.info("Daily run completed")


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
