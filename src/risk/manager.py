"""リスク管理（ポジションサイズ算出・5段階ルールチェック・日次損失制限）"""
from __future__ import annotations

from dataclasses import dataclass

from config.settings import settings
from src.broker.account import AccountInfo
from src.strategy.base import Signal
from src.utils.logger import logger


@dataclass
class TradeApproval:
    approved: bool
    quantity: int
    reason: str


def _regime_risk_multiplier(market_condition: dict | None) -> float:
    """市場レジームに応じてリスク量を調整する乗数を返す。

    勝率が低いレジーム（ベア相場・高VIX）では投資額を自動的に縮小し、
    良好なレジームでは通常通りのサイズでエントリーする。

    Returns:
        1.0: 通常（bull + VIX<25）
        0.75: やや慎重（neutral or VIX 25-30）
        0.5: 縮小（bear相場 or VIX>=30）
    """
    if market_condition is None:
        return 1.0

    sp500_trend = market_condition.get("sp500_trend", "neutral")
    vix = market_condition.get("vix_level", 20.0)
    if isinstance(vix, str):
        vix = 20.0  # フォールバック

    # ベア相場は常に半分
    if sp500_trend == "bear":
        return 0.5

    # VIX >= 30: 高恐怖指数 → 半分
    if vix >= 30:
        return 0.5

    # VIX 25-30 or neutral市場: やや慎重
    if vix >= 25 or sp500_trend == "neutral":
        return 0.75

    return 1.0


def approve_trade(
    signal: Signal,
    account: AccountInfo,
    market_condition: dict | None = None,
) -> TradeApproval:
    """Evaluate whether a trade should be executed based on risk rules.

    Rules:
    1. Max simultaneous positions
    2. Per-trade risk limit (2% of equity, adjusted by market regime)
    3. Max portfolio exposure (90%)
    4. Position must have stop-loss
    5. Single position cannot exceed 40% of equity
    """
    # Only validate BUY signals (SELL is always allowed for exits)
    if signal.action == "SELL":
        return TradeApproval(approved=True, quantity=0, reason="Exit signal approved")

    # Rule 1: Max positions
    current_positions = len(account.positions)
    if current_positions >= settings.max_positions:
        return TradeApproval(
            approved=False,
            quantity=0,
            reason=f"Max positions reached ({current_positions}/{settings.max_positions})",
        )

    # Rule 2: Check stop-loss exists
    if signal.stop_loss <= 0:
        return TradeApproval(
            approved=False, quantity=0, reason="No stop-loss defined"
        )

    # Rule 3: Max portfolio exposure
    max_investment = account.total_equity * settings.max_portfolio_exposure_pct
    available_cash = min(account.cash, max_investment - account.market_value)
    if available_cash <= 0:
        return TradeApproval(
            approved=False,
            quantity=0,
            reason=f"Exposure limit reached (cash={account.cash:.2f}, limit={max_investment:.2f})",
        )

    # Rule 4: Position sizing based on risk (環境適応型サイジング)
    # risk_amount = equity * risk_per_trade_pct * regime_multiplier
    # 勝率が低いレジームでは自動的に投資額を縮小する
    regime_mult = _regime_risk_multiplier(market_condition)
    risk_amount = account.total_equity * settings.risk_per_trade_pct * regime_mult
    # エントリー価格はシグナル生成時の実際の現在値を使う。
    # 修正前: (stop_loss + take_profit) / 2 → SL/TPの中間値を使っていたため、
    #         戦略によってはATR倍率が非対称（例: SL=2ATR, TP=3ATR）な場合に
    #         実際の価格と大きくズレてポジションサイズが不正確になっていた。
    # 修正後: signal.price（戦略が設定した現在値）を優先し、未設定（0）の場合のみ
    #         SL/TP中間値にフォールバックして互換性を維持する。
    entry_estimate = signal.price if signal.price > 0 else (signal.stop_loss + signal.take_profit) / 2
    risk_per_share = abs(entry_estimate - signal.stop_loss)

    if risk_per_share <= 0:
        return TradeApproval(
            approved=False, quantity=0, reason="Invalid risk per share (stop too close)"
        )

    quantity = int(risk_amount / risk_per_share)
    if quantity <= 0:
        return TradeApproval(
            approved=False,
            quantity=0,
            reason=f"Calculated quantity is 0 (risk=${risk_amount:.2f}, risk/share=${risk_per_share:.2f})",
        )

    # Rule 5: Position cost cannot exceed 20% of equity
    position_cost = quantity * entry_estimate
    max_position_cost = account.total_equity * 0.20
    if position_cost > max_position_cost:
        quantity = int(max_position_cost / entry_estimate)
        if quantity <= 0:
            return TradeApproval(
                approved=False,
                quantity=0,
                reason=f"Stock price too high for position limit (${entry_estimate:.2f})",
            )

    # Rule 6: Cannot exceed available cash
    if quantity * entry_estimate > available_cash:
        quantity = int(available_cash / entry_estimate)
        if quantity <= 0:
            return TradeApproval(
                approved=False, quantity=0, reason="Insufficient cash"
            )

    regime_info = f", regime_mult={regime_mult}" if regime_mult < 1.0 else ""
    logger.info(
        f"Trade approved: {signal.ticker} qty={quantity}, "
        f"risk=${risk_amount:.2f}, est_cost=${quantity * entry_estimate:.2f}{regime_info}"
    )
    return TradeApproval(
        approved=True,
        quantity=quantity,
        reason=f"Approved: {quantity} shares, risk=${risk_amount:.2f}{regime_info}",
    )


def check_daily_loss_limit(account: AccountInfo, prev_equity: float) -> bool:
    """Check if daily loss limit has been breached.

    Returns True if trading should be halted.
    """
    if prev_equity <= 0:
        return False

    daily_pnl_pct = (account.total_equity / prev_equity - 1)
    if daily_pnl_pct < -settings.daily_loss_limit_pct:
        logger.warning(
            f"DAILY LOSS LIMIT BREACHED: {daily_pnl_pct*100:.2f}% "
            f"(limit: -{settings.daily_loss_limit_pct*100:.1f}%)"
        )
        return True
    return False
