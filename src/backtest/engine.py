"""バックテストエンジン（過去データで戦略を検証し、リターン・シャープ比・勝率等を算出）"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from src.models.base import get_session
from src.models.strategy import BacktestResult, StrategyMeta
from src.strategy.base import BaseStrategy
from src.utils.logger import logger


@dataclass
class BacktestStats:
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    num_trades: int
    trades: list[dict]


def run_backtest(
    strategy: BaseStrategy,
    ticker: str,
    df: pd.DataFrame,
    market_condition: dict | None = None,
    initial_capital: float = 3300.0,
    commission_pct: float = 0.0,
) -> BacktestStats:
    """Run backtest for a strategy on historical data.

    Iterates through the DataFrame day by day, generates signals,
    and simulates trades with basic position management.
    """
    if market_condition is None:
        market_condition = {"regime": "range", "sp500_trend": "neutral", "vix_level": 20}

    capital = initial_capital
    position = 0  # shares held
    entry_price = 0.0
    trades: list[dict] = []
    equity_curve = []

    for i in range(60, len(df)):
        # Use data up to current day for signal generation
        window = df.iloc[: i + 1]
        current_price = float(df["Close"].iloc[i])
        current_date = df.index[i]

        signal = strategy.generate_signals(ticker, window, market_condition)

        if signal and signal.action == "BUY" and position == 0:
            # Calculate position size (use all available capital for simplicity)
            shares = int(capital * 0.95 / current_price)
            if shares > 0:
                cost = shares * current_price * (1 + commission_pct)
                capital -= cost
                position = shares
                entry_price = current_price

        elif signal and signal.action == "SELL" and position > 0:
            proceeds = position * current_price * (1 - commission_pct)
            pnl = proceeds - (position * entry_price)
            pnl_pct = (current_price / entry_price - 1) * 100
            trades.append(
                {
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "entry_date": str(df.index[i - 1]),
                    "exit_date": str(current_date),
                }
            )
            capital += proceeds
            position = 0
            entry_price = 0.0

        # Check stop-loss for open positions
        elif position > 0 and signal is None:
            # Use the last signal's stop-loss if available
            if entry_price > 0:
                stop_loss = entry_price * 0.95  # 5% trailing stop fallback
                if current_price <= stop_loss:
                    proceeds = position * current_price * (1 - commission_pct)
                    pnl = proceeds - (position * entry_price)
                    pnl_pct = (current_price / entry_price - 1) * 100
                    trades.append(
                        {
                            "entry_price": entry_price,
                            "exit_price": current_price,
                            "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl_pct, 2),
                            "entry_date": "stop_loss",
                            "exit_date": str(current_date),
                        }
                    )
                    capital += proceeds
                    position = 0
                    entry_price = 0.0

        # Track equity
        equity = capital + position * current_price
        equity_curve.append(equity)

    # Close any remaining position at last price
    if position > 0:
        last_price = float(df["Close"].iloc[-1])
        capital += position * last_price
        pnl = position * (last_price - entry_price)
        trades.append(
            {
                "entry_price": entry_price,
                "exit_price": last_price,
                "pnl": round(pnl, 2),
                "pnl_pct": round((last_price / entry_price - 1) * 100, 2),
                "entry_date": "final",
                "exit_date": str(df.index[-1]),
            }
        )

    # Calculate statistics
    equity_series = pd.Series(equity_curve) if equity_curve else pd.Series([initial_capital])
    total_return = (equity_series.iloc[-1] / initial_capital - 1) * 100

    # Daily returns for Sharpe ratio
    daily_returns = equity_series.pct_change().dropna()
    sharpe_ratio = 0.0
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe_ratio = float(daily_returns.mean() / daily_returns.std() * np.sqrt(252))

    # Max drawdown
    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max
    max_drawdown = float(drawdown.min() * 100) if len(drawdown) > 0 else 0.0

    # Win rate
    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0

    stats = BacktestStats(
        total_return=round(total_return, 2),
        sharpe_ratio=round(sharpe_ratio, 2),
        max_drawdown=round(max_drawdown, 2),
        win_rate=round(win_rate, 2),
        num_trades=len(trades),
        trades=trades,
    )

    logger.info(
        f"Backtest {strategy.name} on {ticker}: "
        f"return={stats.total_return}%, sharpe={stats.sharpe_ratio}, "
        f"drawdown={stats.max_drawdown}%, trades={stats.num_trades}, "
        f"win_rate={stats.win_rate}%"
    )
    return stats


def save_backtest_result(
    strategy: BaseStrategy, ticker: str, df: pd.DataFrame, stats: BacktestStats
) -> None:
    """Save backtest results to database."""
    with get_session() as session:
        # Find or create strategy metadata
        from sqlalchemy import select

        meta = session.execute(
            select(StrategyMeta).where(StrategyMeta.name == strategy.name)
        ).scalar_one_or_none()

        if not meta:
            meta = StrategyMeta(
                name=strategy.name,
                description=strategy.__class__.__doc__ or "",
                version=strategy.version,
                market_regime=strategy.target_regime,
            )
            session.add(meta)
            session.flush()

        result = BacktestResult(
            strategy_id=meta.id,
            ticker=ticker,
            start_date=df.index[0].date(),
            end_date=df.index[-1].date(),
            total_return=stats.total_return,
            sharpe_ratio=stats.sharpe_ratio,
            max_drawdown=stats.max_drawdown,
            win_rate=stats.win_rate,
            num_trades=stats.num_trades,
            params_json=strategy.get_params(),
            run_at=datetime.utcnow(),
        )
        session.add(result)
        session.commit()
