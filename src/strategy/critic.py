"""Devil's Advocate（批判的評価エージェント）— 売買シグナルを多角的に検証し、不適切な取引を排除する。

各チェック関数がシグナルを懐疑的に評価し、失敗の可能性がある理由を探す。
指摘(Objection)ごとに信頼度が減点され、閾値を下回ると取引が却下される。

チェックは独立・加算式 — 既存チェックを変更せずに新チェックを追加可能。
全評価結果はDBに保存され、PDCA分析に活用する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from src.models.base import get_session
from src.models.critic import CriticEvaluation
from src.strategy.base import Signal
from src.utils.logger import logger

# Minimum confidence after critic review to proceed with the trade
APPROVAL_THRESHOLD = 0.25


@dataclass
class Objection:
    """A single objection raised by the critic."""

    check: str  # name of the check that raised it
    penalty: float  # confidence reduction (0.0 - 1.0)
    reason: str  # human-readable explanation


@dataclass
class CriticVerdict:
    """Final verdict from the Devil's Advocate."""

    approved: bool
    original_confidence: float
    adjusted_confidence: float
    objections: list[Objection] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Individual critic checks
# Each returns a list of Objections (empty list = no objection).
# ---------------------------------------------------------------------------


def check_trend_contradiction(  # チェック1: 市場トレンドとの矛盾（弱気相場での買い等）
    signal: Signal, df: pd.DataFrame, market_condition: dict
) -> list[Objection]:
    """Reject BUY in bear market or SELL in strong bull market."""
    objections = []
    trend = market_condition.get("sp500_trend", "neutral")

    if signal.action == "BUY" and trend == "bear":
        objections.append(
            Objection(
                check="trend_contradiction",
                penalty=0.25,
                reason=f"Buying against a bearish S&P500 trend — "
                f"most stocks fall in bear markets",
            )
        )
    elif signal.action == "BUY" and trend == "neutral":
        objections.append(
            Objection(
                check="trend_contradiction",
                penalty=0.05,
                reason="Market trend is neutral — no tailwind for longs",
            )
        )
    return objections


def check_vix_risk(  # チェック2: VIX高水準時のリスク警告
    signal: Signal, df: pd.DataFrame, market_condition: dict
) -> list[Objection]:
    """High VIX = high uncertainty. Penalize new entries."""
    objections = []
    vix = market_condition.get("vix_level", 20)

    if signal.action == "BUY":
        if vix > 35:
            objections.append(
                Objection(
                    check="vix_risk",
                    penalty=0.30,
                    reason=f"VIX at {vix:.1f} — extreme fear, "
                    f"entering new positions is reckless",
                )
            )
        elif vix > 25:
            objections.append(
                Objection(
                    check="vix_risk",
                    penalty=0.15,
                    reason=f"VIX at {vix:.1f} — elevated volatility, "
                    f"stop-losses may get triggered prematurely",
                )
            )
    return objections


def check_volume_decline(  # チェック3: 出来高減少（シグナルの信頼性低下）
    signal: Signal, df: pd.DataFrame, market_condition: dict
) -> list[Objection]:
    """A breakout or crossover on declining volume is unreliable."""
    objections = []
    if len(df) < 10 or signal.action != "BUY":
        return objections

    recent_vol = df["Volume"].iloc[-5:].mean()
    prior_vol = df["Volume"].iloc[-20:-5].mean()

    if prior_vol > 0 and recent_vol < prior_vol * 0.7:
        objections.append(
            Objection(
                check="volume_decline",
                penalty=0.20,
                reason=f"Volume declining: recent avg "
                f"{recent_vol:,.0f} vs prior {prior_vol:,.0f} — "
                f"signal lacks conviction",
            )
        )
    return objections


def check_overextended_price(  # チェック4: 過度な値動き（高値追い・パニック売り検出）
    signal: Signal, df: pd.DataFrame, market_condition: dict
) -> list[Objection]:
    """Buying after a large recent run-up is chasing. Selling after a crash is panic."""
    objections = []
    if len(df) < 20:
        return objections

    close = df["Close"]
    pct_change_5d = (close.iloc[-1] / close.iloc[-6] - 1) * 100
    pct_change_20d = (close.iloc[-1] / close.iloc[-21] - 1) * 100

    if signal.action == "BUY":
        if pct_change_5d > 10:
            objections.append(
                Objection(
                    check="overextended_price",
                    penalty=0.25,
                    reason=f"Price up {pct_change_5d:.1f}% in 5 days — "
                    f"chasing a move increases risk of buying the top",
                )
            )
        elif pct_change_20d > 20:
            objections.append(
                Objection(
                    check="overextended_price",
                    penalty=0.15,
                    reason=f"Price up {pct_change_20d:.1f}% in 20 days — "
                    f"extended move, mean reversion risk is elevated",
                )
            )

    if signal.action == "SELL":
        if pct_change_5d < -10:
            objections.append(
                Objection(
                    check="overextended_price",
                    penalty=0.15,
                    reason=f"Price down {pct_change_5d:.1f}% in 5 days — "
                    f"panic selling at a potential bounce point",
                )
            )
    return objections


def check_risk_reward_ratio(  # チェック5: リスク/リワード比（1.5:1未満は警告）
    signal: Signal, df: pd.DataFrame, market_condition: dict
) -> list[Objection]:
    """Risk/reward below 1:1.5 is not worth the trade."""
    objections = []
    if signal.action != "BUY":
        return objections

    current_price = float(df["Close"].iloc[-1])
    risk = abs(current_price - signal.stop_loss)
    reward = abs(signal.take_profit - current_price)

    if risk <= 0:
        objections.append(
            Objection(
                check="risk_reward_ratio",
                penalty=0.30,
                reason="Stop-loss is at or above entry — no valid risk calculation",
            )
        )
        return objections

    rr_ratio = reward / risk
    if rr_ratio < 1.0:
        objections.append(
            Objection(
                check="risk_reward_ratio",
                penalty=0.30,
                reason=f"Risk/reward ratio {rr_ratio:.2f}:1 — "
                f"risking more than the potential gain",
            )
        )
    elif rr_ratio < 1.5:
        objections.append(
            Objection(
                check="risk_reward_ratio",
                penalty=0.10,
                reason=f"Risk/reward ratio {rr_ratio:.2f}:1 — "
                f"marginal, prefer 1.5:1 or better",
            )
        )
    return objections


def check_resistance_proximity(  # チェック6: レジスタンス近接（60日高値付近での買い警告）
    signal: Signal, df: pd.DataFrame, market_condition: dict
) -> list[Objection]:
    """Buying near a strong resistance level reduces upside."""
    objections = []
    if len(df) < 60 or signal.action != "BUY":
        return objections

    current_price = float(df["Close"].iloc[-1])
    high_60d = float(df["High"].iloc[-60:].max())

    # If price is within 2% of 60-day high, it may face resistance
    if high_60d > 0 and (high_60d - current_price) / high_60d < 0.02:
        objections.append(
            Objection(
                check="resistance_proximity",
                penalty=0.08,
                reason=f"Price ${current_price:.2f} is within 2% of "
                f"60-day high ${high_60d:.2f} — likely resistance overhead",
            )
        )
    return objections


def check_recent_loss_on_same_ticker(  # チェック7: 同一銘柄での直近損失履歴
    signal: Signal, df: pd.DataFrame, market_condition: dict
) -> list[Objection]:
    """If the same strategy recently lost money on this ticker, be skeptical."""
    objections = []
    if signal.action != "BUY":
        return objections

    from sqlalchemy import select

    from src.models.trade import TradeLog

    with get_session() as session:
        recent_losses = (
            session.execute(
                select(TradeLog)
                .where(TradeLog.ticker == signal.ticker)
                .where(TradeLog.status == "CLOSED")
                .where(TradeLog.pnl < 0)
                .order_by(TradeLog.exit_date.desc())
                .limit(3)
            )
            .scalars()
            .all()
        )

    if len(recent_losses) >= 2:
        total_loss = sum(t.pnl for t in recent_losses if t.pnl is not None)
        objections.append(
            Objection(
                check="recent_loss_on_ticker",
                penalty=0.20,
                reason=f"{len(recent_losses)} recent losing trades on "
                f"{signal.ticker} (total ${total_loss:.2f}) — "
                f"pattern may not work for this stock",
            )
        )
    return objections


def check_low_liquidity_hours(  # チェック8: 流動性不足（日次出来高$5M未満）
    signal: Signal, df: pd.DataFrame, market_condition: dict
) -> list[Objection]:
    """Warn if average volume is low for the stock's price level."""
    objections = []
    if len(df) < 20 or signal.action != "BUY":
        return objections

    avg_volume = float(df["Volume"].iloc[-20:].mean())
    current_price = float(df["Close"].iloc[-1])
    daily_dollar_volume = avg_volume * current_price

    # Less than $5M daily dollar volume = thin liquidity for swing trading
    if daily_dollar_volume < 5_000_000:
        objections.append(
            Objection(
                check="low_liquidity",
                penalty=0.15,
                reason=f"Daily dollar volume ${daily_dollar_volume:,.0f} "
                f"is thin — slippage and fill risk are elevated",
            )
        )
    return objections


# ---------------------------------------------------------------------------
# Registry of all critic checks
# ---------------------------------------------------------------------------

ALL_CHECKS = [
    check_trend_contradiction,
    check_vix_risk,
    check_volume_decline,
    check_overextended_price,
    check_risk_reward_ratio,
    check_resistance_proximity,
    check_recent_loss_on_same_ticker,
    check_low_liquidity_hours,
]


# ---------------------------------------------------------------------------
# Main evaluation entry point
# ---------------------------------------------------------------------------


def evaluate_signal(
    signal: Signal,
    df: pd.DataFrame,
    market_condition: dict,
    strategy_name: str = "unknown",
    save_to_db: bool = True,
) -> CriticVerdict:
    """Run all critic checks on a signal and produce a verdict.

    Args:
        signal: The trading signal to evaluate
        df: OHLCV DataFrame for the ticker
        market_condition: Current market condition dict
        strategy_name: Name of the strategy that generated the signal
        save_to_db: Whether to persist evaluation to database

    Returns:
        CriticVerdict with approval decision and all objections
    """
    all_objections: list[Objection] = []

    for check_fn in ALL_CHECKS:
        try:
            objections = check_fn(signal, df, market_condition)
            all_objections.extend(objections)
        except Exception as e:
            logger.warning(f"Critic check {check_fn.__name__} failed: {e}")

    # Calculate adjusted confidence
    total_penalty = sum(o.penalty for o in all_objections)
    adjusted_confidence = max(signal.confidence - total_penalty, 0.0)
    approved = adjusted_confidence >= APPROVAL_THRESHOLD

    # Build summary
    if all_objections:
        objection_lines = [f"  - [{o.check}] (-{o.penalty:.2f}) {o.reason}" for o in all_objections]
        summary = (
            f"{'APPROVED' if approved else 'REJECTED'}: "
            f"{signal.action} {signal.ticker} | "
            f"confidence {signal.confidence:.2f} -> {adjusted_confidence:.2f} "
            f"(threshold={APPROVAL_THRESHOLD})\n"
            f"Objections ({len(all_objections)}):\n" + "\n".join(objection_lines)
        )
    else:
        summary = (
            f"APPROVED: {signal.action} {signal.ticker} | "
            f"confidence {signal.confidence:.2f} — no objections raised"
        )

    verdict = CriticVerdict(
        approved=approved,
        original_confidence=signal.confidence,
        adjusted_confidence=adjusted_confidence,
        objections=all_objections,
        summary=summary,
    )

    # Log
    log_level = logger.info if approved else logger.warning
    log_level(f"Critic: {summary}")

    # Save to DB
    if save_to_db:
        _save_evaluation(signal, verdict, strategy_name)

    return verdict


def _save_evaluation(signal: Signal, verdict: CriticVerdict, strategy_name: str) -> None:
    """Persist critic evaluation to database for PDCA review."""
    with get_session() as session:
        record = CriticEvaluation(
            ticker=signal.ticker,
            action=signal.action,
            strategy_name=strategy_name,
            original_confidence=verdict.original_confidence,
            adjusted_confidence=verdict.adjusted_confidence,
            objections=[
                {"check": o.check, "penalty": o.penalty, "reason": o.reason}
                for o in verdict.objections
            ],
            num_objections=len(verdict.objections),
            verdict="APPROVED" if verdict.approved else "REJECTED",
            verdict_reason=verdict.summary,
            approved=verdict.approved,
            evaluated_at=datetime.utcnow(),
        )
        session.add(record)
        session.commit()
