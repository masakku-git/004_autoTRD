"""Daily orchestrator — main entry point for the trading system."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from config.settings import settings
from src.broker.account import get_account_info
from src.broker.executor_v2 import (
    close_trade_log,
    close_trade_log_by_id,
    create_trade_log,
    partial_close_trade_log,
    partial_close_trade_log_by_id,
    place_order,
)
from src.data.fetcher import get_ohlcv
from src.data.screener import run_screening
from src.models.base import get_session, init_db
from src.notify.notifier import send_notification
from src.risk.manager import TradeApproval, approve_trade, check_daily_loss_limit
from src.strategy.base import Signal
from src.strategy.critic import evaluate_signal
from src.strategy.registry import discover_strategies, get_strategy
from src.strategy.selector import assess_market_condition, select_strategies
from src.utils.helpers import is_us_market_day, today_jst, utcnow
from src.utils.logger import logger


def run_daily():
    """Execute the full daily trading workflow."""
    logger.info("=" * 60)
    logger.info(f"Daily run started at {utcnow().isoformat()}")
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

    # ※ ポートフォリオのスナップショット保存は scripts/save_eod_snapshot.py に分離
    #   （JST 13:00 では moomoo API が返す値が前 US 営業日の close のため、日付ラベルが
    #   1営業日ずれる問題があった。EOD snapshot は土曜 JST 07:00 cron で別途記録する。）

    # 前日比の損失が上限（3%）を超えていたら新規エントリーのみを停止する。
    # 売却ロジック（SL/段階TP/TP/最大保有期間/戦略固有exit/SELLシグナル）は
    # ポジション保護のため通常通り動かす。
    block_new_entries = False
    prev_snapshot = _get_previous_equity()
    if prev_snapshot > 0 and check_daily_loss_limit(account, prev_snapshot):
        msg = "日次損失上限に到達 — 新規エントリーを停止します（売却は継続）"
        logger.warning(msg)
        send_notification("新規エントリー停止", msg, level="warning")
        block_new_entries = True

    buy_signals = []
    sell_signals = []
    rejected_signals = []
    forced_exit_orders = []
    position_errors = []       # 強制エグジット判定に失敗した銘柄
    sell_signal_errors = []    # SELL判定に失敗した銘柄
    buy_signal_errors = []     # BUY判定に失敗した銘柄
    degraded_warnings = []     # 日次レポートに載せる実行時警告

    # --- Step 3: 強制エグジット（戦略固有チェック / SL / 段階TP / TP / 最大保有期間）---
    # 市場環境判定・スクリーニングより先に実行する。外部データ起因の障害が起きても
    # ポジション保護（損切り）だけは必ず走らせるため。1銘柄の失敗が残りポジションの
    # 判定を止めないよう、銘柄単位で例外を分離する。
    #
    # 同一銘柄を複数回に分けてエントリーした場合、trade_logにはOPEN行が複数存在する。
    # 各行（ロット）はエントリー日ごとに個別のSL/TP/TP1を持つため、ロット単位で判定する
    # （以前は最新ロットのSL/TPしか見ておらず、古いロットのTP1到達を検知できなかった）。
    for pos in account.positions:
        ticker = pos["ticker"].replace("US.", "")
        try:
            df = get_ohlcv(ticker)
            if df.empty:
                continue
            raw_close = df["Close"].iloc[-1]
            raw_high = df["High"].iloc[-1]
            if pd.isna(raw_close) or pd.isna(raw_high):
                # NaNとの比較は常にFalseとなり、SL/TP判定が無音で無効化される
                msg = (
                    f"{ticker}: 最新価格がNaNのため損切り/利確判定を実行できません。"
                    "moomooアプリで手動確認してください。"
                )
                logger.error(msg)
                send_notification("価格データ異常 — 損切り判定不可", msg, level="error")
                continue
            current_price = float(raw_close)
            today_high = float(raw_high)
            _update_highest_price(ticker, today_high)

            lots = _get_open_trades(ticker)
            if not lots:
                continue

            # brokerは銘柄単位の合算数量しか返さないため、ロットを処理するたびに減算していく
            remaining_broker_qty = pos.get("qty") or 0

            for lot in lots:
                if remaining_broker_qty <= 0:
                    break

                trade_id = lot["id"]
                lot_qty = min(lot.get("quantity") or 0, remaining_broker_qty)
                if lot_qty <= 0:
                    continue
                # 決済するロットもホールドするロットもbroker数量を占有する。
                # 決済時だけ減算すると、ホールド中ロットの株数を後続ロットが
                # 二重に売ってしまうため、ここで先に消費させる。
                remaining_broker_qty -= lot_qty

                sl = lot.get("stop_loss") or 0
                tp = lot.get("take_profit") or 0
                tp1 = lot.get("take_profit_1") or 0
                max_hold = lot.get("max_hold_days") or 20
                entry_date = lot.get("entry_date")
                lot_label = f"{ticker}(entry {entry_date})"

                # (1) 戦略固有のエグジットチェック（entry_price/highest_priceがロットごとに異なるため個別判定）
                suppress_tp = False
                strategy_name = lot.get("strategy_name", "")
                try:
                    strategy = get_strategy(strategy_name)
                    decision = strategy.check_exit(ticker, df, lot)
                    if decision is not None:
                        if decision.should_exit:
                            forced_signal = Signal(
                                ticker=ticker, action="SELL", confidence=1.0,
                                stop_loss=0, take_profit=0, reason=decision.reason,
                                price=current_price,
                            )
                            order = place_order(forced_signal, lot_qty)
                            close_trade_log_by_id(trade_id, order, current_price, sold_qty=lot_qty)
                            forced_exit_orders.append(f"FORCED-EXIT {lot_qty}x {lot_label}: {decision.reason}")
                            logger.info(f"Strategy exit: {decision.reason}")
                            continue
                        suppress_tp = decision.suppress_tp
                except KeyError:
                    pass  # 戦略が見つからない場合はデフォルトロジックを使用

                exit_reason = None

                # (2) ストップロス（常にチェック）
                if sl > 0 and current_price <= sl:
                    exit_reason = f"ストップロス発動 (SL=${sl:.2f}, 現在=${current_price:.2f})"

                # (3) 段階利確（TP1）: このロットの半分を決済
                elif tp1 > 0 and current_price >= tp1:
                    half_qty = max(lot_qty // 2, 1)
                    if half_qty < lot_qty:
                        forced_signal = Signal(
                            ticker=ticker, action="SELL", confidence=1.0,
                            stop_loss=0, take_profit=0,
                            reason=f"段階利確TP1到達 (TP1=${tp1:.2f}, 現在=${current_price:.2f})",
                            price=current_price,
                        )
                        order = place_order(forced_signal, half_qty)
                        partial_close_trade_log_by_id(trade_id, order, current_price, half_qty)
                        forced_exit_orders.append(
                            f"PARTIAL-EXIT {half_qty}x {lot_label}: 段階利確TP1=${tp1:.2f}"
                        )
                        logger.info(f"Staged TP1: sold {half_qty} of {lot_qty} shares (lot id={trade_id})")
                        continue
                    else:
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

                if exit_reason:
                    forced_signal = Signal(
                        ticker=ticker, action="SELL", confidence=1.0,
                        stop_loss=0, take_profit=0, reason=exit_reason,
                        price=current_price,
                    )
                    order = place_order(forced_signal, lot_qty)
                    close_trade_log_by_id(trade_id, order, current_price, sold_qty=lot_qty)
                    forced_exit_orders.append(f"FORCED-EXIT {lot_qty}x {lot_label}: {exit_reason}")
                    logger.info(f"Forced exit: {exit_reason}")
        except Exception as e:
            logger.exception(f"{ticker}: 強制エグジット処理でエラー")
            position_errors.append(f"{ticker}: {type(e).__name__}: {e}")

    if position_errors:
        send_notification(
            "保有ポジションの損切り判定エラー",
            "以下の銘柄で強制エグジット判定（SL/TP/最大保有期間）が実行できませんでした。\n"
            "該当銘柄の損切りは本日実行されていません。moomooアプリで手動確認してください。\n\n"
            + "\n".join(position_errors),
            level="error",
        )
        degraded_warnings.append(
            f"強制エグジット判定エラー: {len(position_errors)}銘柄（Slack通知済み）"
        )

    # 強制エグジット後にアカウント情報を再取得
    if forced_exit_orders:
        account = get_account_info()

    # --- Step 4: 市場環境の判定（S&P500トレンド・VIX・レジーム分類） ---
    # 判定不能でも売却系は続行する。データ劣化時は安全側として新規エントリーを停止。
    try:
        market_condition = assess_market_condition()
    except Exception:
        logger.exception("市場環境判定が失敗")
        market_condition = {
            "sp500_trend": "neutral",
            "vix_level": 0.0,
            "regime": "volatile",
            "data_degraded": True,
        }
    if market_condition.get("data_degraded"):
        block_new_entries = True
        send_notification(
            "市場データ取得失敗 — 新規エントリー停止",
            "^GSPC/^VIXの価格データが取得できず市場環境を判定できません。\n"
            "安全のため本日の新規エントリーを停止します（売却・損切りは通常通り実行）。\n"
            "SELL判定は前回レジームまたは防御的レジーム(volatile)で継続します。",
            level="warning",
        )
        degraded_warnings.append("市場データ劣化のため新規エントリー停止（Slack通知済み）")

    # --- Step 5: 保有ポジションに対して売却シグナルをチェック ---
    # 設計方針: 各ポジションは購入時の戦略でのみ売却判定する（戦略間のクロス介入を防ぐ）
    for pos in account.positions:
        ticker = pos["ticker"].replace("US.", "")
        try:
            df = get_ohlcv(ticker)
            if df.empty:
                continue
            trade_info = _get_open_trade_info(ticker)
            if not trade_info:
                continue
            strategy_name = trade_info.get("strategy_name", "")
            try:
                strategy = get_strategy(strategy_name)
            except KeyError:
                logger.warning(
                    f"{ticker}: 購入戦略 '{strategy_name}' がregistryに無いためSELL判定をスキップ"
                )
                continue
            signal = strategy.generate_signals(ticker, df, market_condition)
            if signal and signal.action == "SELL":
                # Critic evaluates SELL signals too (prevents panic selling)
                verdict = evaluate_signal(signal, df, market_condition, strategy.name)
                if verdict.approved:
                    signal.confidence = verdict.adjusted_confidence
                    sell_signals.append(signal)
                else:
                    rejected_signals.append((signal, verdict))
        except Exception as e:
            logger.exception(f"{ticker}: SELLシグナル判定でエラー")
            sell_signal_errors.append(f"{ticker}: {type(e).__name__}: {e}")

    if sell_signal_errors:
        send_notification(
            "SELLシグナル判定エラー",
            "以下の保有銘柄で戦略ベースのSELL判定が実行できませんでした\n"
            "（SL/TP等の強制エグジット判定は別途実行済みです）。\n\n"
            + "\n".join(sell_signal_errors),
            level="error",
        )
        degraded_warnings.append(
            f"SELL判定エラー: {len(sell_signal_errors)}銘柄（Slack通知済み）"
        )

    # --- Step 6: 銘柄スクリーニング（50銘柄→上位15銘柄に絞り込み） ---
    # 失敗しても損切り・売却判定（Step 3/5）は実行済み。通知して新規BUYのみスキップ。
    candidates = []
    screening_failed = False
    try:
        candidates = run_screening()
        logger.info(f"Screened {len(candidates)} candidates")
    except Exception as e:
        screening_failed = True
        logger.exception("スクリーニング失敗")
        send_notification(
            "スクリーニング失敗 — 新規BUYをスキップ",
            "銘柄スクリーニングが失敗したため、本日の新規エントリーはスキップします。\n"
            "保有ポジションの損切り・売却判定は実行済みです。\n\n"
            f"{type(e).__name__}: {e}",
            level="error",
        )

    # --- Step 7: スクリーニング通過銘柄に対して買いシグナルをチェック ---
    strategies = select_strategies(market_condition)
    for candidate in candidates:
        ticker = candidate["ticker"]
        try:
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
                        signal.screen_score = candidate["score"]
                        buy_signals.append(signal)
                    else:
                        rejected_signals.append((signal, verdict))
                    break
        except Exception as e:
            logger.exception(f"{ticker}: BUYシグナル判定でエラー")
            buy_signal_errors.append(f"{ticker}: {type(e).__name__}: {e}")

    if buy_signal_errors:
        send_notification(
            "BUYシグナル判定エラー",
            "以下の候補銘柄でBUYシグナル判定が実行できませんでした\n"
            "（新規エントリー機会の損失のみで、保有ポジションへの影響はありません）。\n\n"
            + "\n".join(buy_signal_errors),
            level="warning",
        )
        degraded_warnings.append(
            f"BUY判定エラー: {len(buy_signal_errors)}銘柄（Slack通知済み）"
        )

    logger.info(
        f"Signals: {len(buy_signals)} BUY, {len(sell_signals)} SELL, "
        f"{len(rejected_signals)} REJECTED by critic"
    )

    # --- Step 8: 注文実行（リスク管理チェック後に発注） ---
    executed_orders = []   # 成功した注文（dict形式）
    failed_orders = []     # 失敗した注文（dict形式）
    risk_rejected_orders = []

    # 売り注文を先に処理（資金を解放してから買いに回す）
    for signal in sell_signals:
        approval = approve_trade(signal, account, market_condition)
        if approval.approved:
            pos = next(
                (p for p in account.positions if signal.ticker in p["ticker"]),
                None,
            )
            # Step 3 のロット単位決済で既に売った分を二重に売らないよう、
            # broker数量とDB上の残OPEN数量の小さい方に制限する。
            # （Step 3 の売り注文は当日中は約定照合前で、口座数量に反映されないことがある）
            broker_qty = pos["qty"] if pos else 0
            qty = min(broker_qty, _get_open_qty(signal.ticker))
            if qty < broker_qty:
                logger.info(
                    f"{signal.ticker}: SELL数量を{broker_qty}→{qty}に制限"
                    "（Step 3で決済済みのロット分を除外）"
                )
            if qty > 0:
                order = place_order(signal, qty)
                close_trade_log(signal.ticker, order, signal.take_profit)
                entry = {
                    "action": "SELL",
                    "ticker": signal.ticker,
                    "qty": qty,
                    "price": signal.price,
                    "reason": signal.reason,
                    "status": order.status,
                }
                if order.status in ("SUBMITTED", "DRY_RUN"):
                    executed_orders.append(entry)
                else:
                    failed_orders.append(entry)

    # 売り注文・強制エグジット後に口座を再取得（現金解放を反映）
    if sell_signals or forced_exit_orders:
        account = get_account_info()

    # 買い注文をスクリーニングスコア順に処理（ポジションサイズはリスク管理が算出）
    # moomooへの残高反映タイムラグを考慮し、発注成功のたびに account.cash を手動で差し引く
    buy_signals.sort(key=lambda s: s.screen_score, reverse=True)
    for signal in buy_signals:
        if block_new_entries:
            risk_rejected_orders.append(
                (signal, TradeApproval(approved=False, quantity=0, reason="日次損失上限到達のため新規エントリー停止"))
            )
            continue
        approval = approve_trade(signal, account, market_condition)
        if approval.approved and approval.quantity > 0:
            order = place_order(signal, approval.quantity, strategy_name=_find_strategy_name_for_signal(signal, strategies))
            create_trade_log(signal, order, approval.quantity)
            est_cost = signal.price * approval.quantity if signal.price else 0
            entry = {
                "action": "BUY",
                "ticker": signal.ticker,
                "qty": approval.quantity,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "est_cost": est_cost,
                "reason": signal.reason,
                "status": order.status,
            }
            if order.status in ("SUBMITTED", "DRY_RUN"):
                executed_orders.append(entry)
                # 発注済コストを現金から差し引き、次の銘柄の承認判断に反映する
                account.cash = max(0.0, account.cash - est_cost)
            else:
                failed_orders.append(entry)
        else:
            risk_rejected_orders.append((signal, approval))

    # --- Step 9: 日次レポート作成 & Slack通知 ---
    summary = _build_summary(
        account, market_condition, candidates,
        forced_exit_orders, executed_orders, failed_orders, rejected_signals,
        buy_count=len(buy_signals), sell_count=len(sell_signals),
        risk_rejected_orders=risk_rejected_orders,
        screening_failed=screening_failed,
        degraded_warnings=degraded_warnings,
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
            "highest_price": trade.highest_price or trade.entry_price,
            "quantity": trade.quantity,
            "strategy_name": trade.strategy_name,
        }


def _get_open_trades(ticker: str) -> list[dict]:
    """TradeLogからオープンな全ロット（entry_date昇順）のSL/TP/エントリー日を取得する。

    同一銘柄に複数OPEN行がある場合、ロットごとに個別のSL/TP/TP1判定を行うために使う。
    """
    from sqlalchemy import select

    from src.models.trade import TradeLog

    with get_session() as session:
        trades = session.execute(
            select(TradeLog)
            .where(TradeLog.ticker == ticker)
            .where(TradeLog.status == "OPEN")
            .order_by(TradeLog.entry_date)
        ).scalars().all()
        return [
            {
                "id": trade.id,
                "stop_loss": trade.stop_loss or 0,
                "take_profit": trade.take_profit or 0,
                "take_profit_1": trade.take_profit_1 or 0,
                "max_hold_days": trade.max_hold_days or 20,
                "entry_date": trade.entry_date,
                "entry_price": trade.entry_price,
                "highest_price": trade.highest_price or trade.entry_price,
                "quantity": trade.quantity,
                "strategy_name": trade.strategy_name,
            }
            for trade in trades
        ]


def _get_open_qty(ticker: str) -> int:
    """trade_log上でまだOPENな合計株数を返す（SELL数量の上限として使う）。"""
    from sqlalchemy import func, select

    from src.models.trade import TradeLog

    with get_session() as session:
        total = session.execute(
            select(func.sum(TradeLog.quantity))
            .where(TradeLog.ticker == ticker)
            .where(TradeLog.status == "OPEN")
        ).scalar()
        return int(total or 0)


def _update_highest_price(ticker: str, today_high: float) -> None:
    """建値後の最高値を更新する（ブレークイーブン/トレーリングストップ判定に使用）。

    同銘柄に複数のOPEN行がある場合も全行を更新する。
    """
    from sqlalchemy import select

    from src.models.trade import TradeLog

    with get_session() as session:
        trades = session.execute(
            select(TradeLog)
            .where(TradeLog.ticker == ticker)
            .where(TradeLog.status == "OPEN")
        ).scalars().all()
        for trade in trades:
            baseline = trade.highest_price or trade.entry_price
            if today_high > baseline:
                trade.highest_price = today_high
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
    account, market_condition, candidates,
    forced_exit_orders, executed_orders, failed_orders, rejected_signals=None,
    buy_count: int = 0, sell_count: int = 0, risk_rejected_orders=None,
    screening_failed: bool = False, degraded_warnings=None,
) -> str:
    rejected_signals = rejected_signals or []
    risk_rejected_orders = risk_rejected_orders or []
    degraded_warnings = degraded_warnings or []

    regime_raw = market_condition.get("regime", "")
    trend_raw = market_condition.get("sp500_trend", "")
    vix = market_condition.get("vix_level", 0)

    regime_ja = {"trending": "トレンド相場", "range": "レンジ相場", "volatile": "高ボラ相場"}.get(regime_raw, regime_raw)
    trend_ja = {"bull": "強気(上昇)", "bear": "弱気(下落)", "neutral": "中立(横ばい)"}.get(trend_raw, trend_raw)

    regime_desc = {
        "trending": "明確なトレンドが出ている相場（ブレイクアウト・モメンタム戦略が有効）",
        "range":    "方向感のない横ばい相場（逆張り・レンジ戦略が有効）",
        "volatile": "VIX>30 の不安定な相場（新規エントリーを縮小・慎重モード）",
    }.get(regime_raw, "")

    mode_str = "DRY_RUN (模擬実行)" if settings.dry_run else "LIVE (本番取引)"
    trade_env_str = "REAL (本番口座)" if settings.moomoo_trade_env == "REAL" else "SIMULATE (模擬口座)"

    lines = [f"日付: {today_jst()}"]

    # 実行時警告（スクリーニング失敗=候補0件を「正常0件」と区別して表示する）
    if screening_failed or degraded_warnings:
        lines += ["", "【⚠ 実行時警告】"]
        if screening_failed:
            lines.append("  スクリーニング失敗のため新規BUYをスキップしました（Slack通知済み）")
        for w in degraded_warnings:
            lines.append(f"  {w}")

    lines += [
        "",
        "【市場環境】",
        f"  S&P500トレンド : {trend_ja}",
        f"  VIX           : {vix:.1f}",
        f"  レジーム       : {regime_ja}",
        f"  └ {regime_desc}",
    ]

    if trend_raw == "bear":
        lines.append("  ⚠ ベア相場のため全戦略がBUYシグナルを生成しません（新規買いなし）")
    if vix >= 30:
        lines.append(f"  ⚠ VIX={vix:.1f} (>=30) のためポジションサイズを50%縮小")
    elif vix >= 25:
        lines.append(f"  ⚠ VIX={vix:.1f} (>=25) のためポジションサイズを75%に縮小")

    lines += [
        "",
        "【資産状況】",
        f"  総資産        : ${account.total_equity:.2f}",
        f"  現金          : ${account.cash:.2f}",
        f"  保有ポジション : {len(account.positions)}件",
    ]

    if account.positions:
        lines.append("  ポジション詳細:")
        for p in account.positions:
            ticker = p.get("ticker", "").replace("US.", "")
            lines.append(
                f"    {ticker}: {p.get('qty')}株 "
                f"@ avg${p.get('avg_price', 0):.2f}  "
                f"評価額${p.get('market_value', 0):.2f}  "
                f"損益${p.get('pnl', 0):.2f}"
            )

    lines += [
        "",
        "【シグナル診断】",
        f"  スクリーニング通過 : {len(candidates)}銘柄",
        f"  BUYシグナル       : {buy_count}件",
        f"  SELLシグナル      : {sell_count}件",
        f"  Criticに却下      : {len(rejected_signals)}件",
        f"  リスク管理で却下  : {len(risk_rejected_orders)}件",
    ]

    # 強制エグジット
    if forced_exit_orders:
        lines.append("")
        lines.append(f"【強制決済 ({len(forced_exit_orders)}件)】")
        for o in forced_exit_orders:
            lines.append(f"  {o}")

    # 約定成功
    buy_exec = [o for o in executed_orders if o["action"] == "BUY"]
    sell_exec = [o for o in executed_orders if o["action"] == "SELL"]

    if executed_orders:
        lines.append("")
        lines.append(f"【約定成功 ({len(executed_orders)}件)】")
        for o in buy_exec:
            sl = o.get("stop_loss", 0)
            tp = o.get("take_profit", 0)
            cost = o.get("est_cost", 0)
            lines.append(
                f"  ✓ BUY {o['qty']}株 {o['ticker']}"
                f"  @ ${o.get('price', 0):.2f}"
                f"  SL:${sl:.2f}  TP:${tp:.2f}"
                f"  推定コスト:${cost:.2f}"
            )
            lines.append(f"    理由: {o['reason'][:80]}")
        for o in sell_exec:
            lines.append(
                f"  ✓ SELL {o['qty']}株 {o['ticker']}"
                f"  @ ${o.get('price', 0):.2f}"
            )
            lines.append(f"    理由: {o['reason'][:80]}")
    else:
        lines.append("")
        lines.append("【約定成功】 0件")

    # 発注失敗
    if failed_orders:
        lines.append("")
        lines.append(f"【発注失敗 ({len(failed_orders)}件)】")
        for o in failed_orders:
            cost = o.get("est_cost", 0)
            lines.append(
                f"  ✗ {o['action']} {o['qty']}株 {o['ticker']}"
                f"  推定コスト:${cost:.2f}"
            )

    # リスク管理で却下
    if risk_rejected_orders:
        lines.append("")
        lines.append(f"【リスク管理で却下 ({len(risk_rejected_orders)}件)】")
        for signal, approval in risk_rejected_orders:
            lines.append(f"  ✗ BUY {signal.ticker}: {approval.reason[:70]}")

    # Criticで却下
    if rejected_signals:
        lines.append("")
        lines.append(f"【Criticに却下 ({len(rejected_signals)}件)】")
        for signal, verdict in rejected_signals:
            top_objection = verdict.objections[0].reason if verdict.objections else "N/A"
            lines.append(
                f"  ✗ {signal.action} {signal.ticker} "
                f"(信頼度 {verdict.original_confidence:.2f}→{verdict.adjusted_confidence:.2f}): "
                f"{top_objection[:60]}"
            )

    # スクリーニング上位5銘柄
    if candidates:
        lines.append("")
        lines.append(f"【スクリーニング上位{min(5, len(candidates))}銘柄】")
        lines.append(f"  {'銘柄':<6} {'株価':>7} {'ATR%':>6} {'相対強度':>8} {'スコア':>7}")
        lines.append(f"  {'------':<6} {'-------':>7} {'------':>6} {'--------':>8} {'-------':>7}")
        for c in candidates[:5]:
            lines.append(
                f"  {c['ticker']:<6} "
                f"${c['last_close']:>6.2f} "
                f"{c['atr_pct']:>5.1f}% "
                f"{c['relative_strength']:>+7.1f}% "
                f"{c['score']:>7.2f}"
            )

    lines += [
        "",
        "【動作モード】",
        f"  Mode:     {mode_str}",
        f"  TradeEnv: {trade_env_str}",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    try:
        run_daily()
    except Exception as e:
        logger.exception("Daily run crashed with an unhandled exception")
        send_notification(
            "AutoTRD 実行エラー",
            f"日次実行が異常終了しました。Slack通知含め処理が完了していない可能性があります。\n\n{type(e).__name__}: {e}",
            level="error",
        )
        raise
