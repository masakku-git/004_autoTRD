"""RSI Mean-Reversion strategy plugin.

Buy when RSI drops below oversold level and starts recovering.
Sell when RSI rises above overbought level and starts declining.
Best suited for range-bound markets.
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, Signal


class RSIReversal(BaseStrategy):
    name = "rsi_reversal"
    version = "1.0"
    target_regime = "range"

    def __init__(
        self,
        rsi_period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        atr_period: int = 14,
    ):
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.atr_period = atr_period

    def generate_signals(
        self, ticker: str, df: pd.DataFrame, market_condition: dict
    ) -> Signal | None:
        if len(df) < self.rsi_period + 5:
            return None

        rsi = self._calculate_rsi(df["Close"])
        if rsi.iloc[-1] is None or pd.isna(rsi.iloc[-1]):
            return None

        current_rsi = rsi.iloc[-1]
        prev_rsi = rsi.iloc[-2]
        current_price = df["Close"].iloc[-1]
        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None

        # Oversold reversal: RSI was below threshold and is now rising
        if prev_rsi < self.oversold and current_rsi > prev_rsi:
            stop_loss = current_price - 1.5 * atr
            take_profit = current_price + 2 * atr
            return Signal(
                ticker=ticker,
                action="BUY",
                confidence=self._calc_confidence(current_rsi, "oversold"),
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=(
                    f"RSI oversold reversal. RSI={current_rsi:.1f} "
                    f"(prev={prev_rsi:.1f}), Price={current_price:.2f}"
                ),
            )

        # Overbought reversal: RSI was above threshold and is now falling
        if prev_rsi > self.overbought and current_rsi < prev_rsi:
            return Signal(
                ticker=ticker,
                action="SELL",
                confidence=self._calc_confidence(current_rsi, "overbought"),
                stop_loss=current_price + 1.5 * atr,
                take_profit=current_price - 2 * atr,
                reason=(
                    f"RSI overbought reversal. RSI={current_rsi:.1f} "
                    f"(prev={prev_rsi:.1f}), Price={current_price:.2f}"
                ),
            )

        return None

    def _calculate_rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(self.rsi_period).mean()
        avg_loss = loss.rolling(self.rsi_period).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    def _calculate_atr(self, df: pd.DataFrame) -> float:
        high, low, close = df["High"], df["Low"], df["Close"]
        tr = pd.concat(
            [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
            axis=1,
        ).max(axis=1)
        return float(tr.rolling(self.atr_period).mean().iloc[-1])

    def _calc_confidence(self, rsi: float, condition: str) -> float:
        if condition == "oversold":
            # Lower RSI = stronger signal
            return min(0.5 + (self.oversold - rsi) * 0.02, 0.9)
        else:
            return min(0.5 + (rsi - self.overbought) * 0.02, 0.9)

    def get_params(self) -> dict:
        return {
            "rsi_period": self.rsi_period,
            "oversold": self.oversold,
            "overbought": self.overbought,
            "atr_period": self.atr_period,
        }
