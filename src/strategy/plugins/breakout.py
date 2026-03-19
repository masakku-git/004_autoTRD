"""Price Breakout strategy plugin.

Buy when price breaks above recent high with volume confirmation.
Works in trending and volatile markets.
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, Signal


class Breakout(BaseStrategy):
    name = "breakout"
    version = "1.0"
    target_regime = "any"

    def __init__(
        self,
        lookback: int = 20,
        volume_mult: float = 1.5,
        atr_period: int = 14,
    ):
        self.lookback = lookback
        self.volume_mult = volume_mult
        self.atr_period = atr_period

    def generate_signals(
        self, ticker: str, df: pd.DataFrame, market_condition: dict
    ) -> Signal | None:
        if len(df) < self.lookback + 5:
            return None

        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

        current_price = close.iloc[-1]
        current_volume = volume.iloc[-1]

        # Lookback high/low (excluding today)
        recent_high = high.iloc[-(self.lookback + 1) : -1].max()
        recent_low = low.iloc[-(self.lookback + 1) : -1].min()
        avg_volume = volume.iloc[-(self.lookback + 1) : -1].mean()

        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None

        # Bullish breakout: price above recent high + volume confirmation
        if current_price > recent_high and current_volume > avg_volume * self.volume_mult:
            stop_loss = recent_low
            take_profit = current_price + 3 * atr
            return Signal(
                ticker=ticker,
                action="BUY",
                confidence=self._calc_confidence(
                    current_price, recent_high, current_volume, avg_volume
                ),
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=(
                    f"Bullish breakout above {self.lookback}-day high "
                    f"({recent_high:.2f}). Vol={current_volume/avg_volume:.1f}x avg"
                ),
            )

        # Bearish breakdown: price below recent low + volume (for exit signals)
        if current_price < recent_low and current_volume > avg_volume * self.volume_mult:
            return Signal(
                ticker=ticker,
                action="SELL",
                confidence=self._calc_confidence(
                    recent_low, current_price, current_volume, avg_volume
                ),
                stop_loss=recent_high,
                take_profit=current_price - 3 * atr,
                reason=(
                    f"Bearish breakdown below {self.lookback}-day low "
                    f"({recent_low:.2f}). Vol={current_volume/avg_volume:.1f}x avg"
                ),
            )

        return None

    def _calculate_atr(self, df: pd.DataFrame) -> float:
        high, low, close = df["High"], df["Low"], df["Close"]
        tr = pd.concat(
            [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
            axis=1,
        ).max(axis=1)
        return float(tr.rolling(self.atr_period).mean().iloc[-1])

    def _calc_confidence(
        self, price: float, level: float, volume: float, avg_volume: float
    ) -> float:
        # Confidence based on breakout magnitude and volume
        breakout_pct = abs(price - level) / level
        vol_ratio = volume / avg_volume if avg_volume > 0 else 1.0
        confidence = 0.4 + min(breakout_pct * 10, 0.3) + min((vol_ratio - 1) * 0.1, 0.2)
        return min(confidence, 1.0)

    def get_params(self) -> dict:
        return {
            "lookback": self.lookback,
            "volume_mult": self.volume_mult,
            "atr_period": self.atr_period,
        }
