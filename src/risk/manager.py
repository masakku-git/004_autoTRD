"""Risk management and position sizing."""
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


def approve_trade(
    signal: Signal,
    account: AccountInfo,
) -> TradeApproval:
    """Evaluate whether a trade should be executed based on risk rules.

    Rules:
    1. Max simultaneous positions
    2. Per-trade risk limit (2% of equity)
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

    # Rule 4: Position sizing based on risk
    # risk_amount = equity * risk_per_trade_pct
    # quantity = risk_amount / (entry_price - stop_loss)
    risk_amount = account.total_equity * settings.risk_per_trade_pct
    # Estimate entry price (use stop_loss to derive; entry ≈ between stop_loss and take_profit)
    entry_estimate = (signal.stop_loss + signal.take_profit) / 2
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

    # Rule 5: Position cost cannot exceed 40% of equity
    position_cost = quantity * entry_estimate
    max_position_cost = account.total_equity * 0.40
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

    logger.info(
        f"Trade approved: {signal.ticker} qty={quantity}, "
        f"risk=${risk_amount:.2f}, est_cost=${quantity * entry_estimate:.2f}"
    )
    return TradeApproval(
        approved=True,
        quantity=quantity,
        reason=f"Approved: {quantity} shares, risk=${risk_amount:.2f}",
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
