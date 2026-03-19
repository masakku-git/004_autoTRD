"""SMAクロスオーバー戦略プラグイン

ゴールデンクロス（短期SMA20が長期SMA50を上抜け）で買い、
デッドクロス（短期SMAが長期SMAを下抜け）で売り。
ストップロスはATRの2倍、利確はATRの3倍で設定。
トレンド相場向け。
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, Signal


class SMACrossover(BaseStrategy):
    name = "sma_crossover"
    version = "1.0"
    target_regime = "trending"

    def __init__(self, short_period: int = 20, long_period: int = 50, atr_period: int = 14):
        self.short_period = short_period
        self.long_period = long_period
        self.atr_period = atr_period

    def generate_signals(
        self, ticker: str, df: pd.DataFrame, market_condition: dict
    ) -> Signal | None:
        if len(df) < self.long_period + 2:
            return None

        close = df["Close"]
        sma_short = close.rolling(self.short_period).mean()
        sma_long = close.rolling(self.long_period).mean()

        # Current and previous crossover state
        curr_above = sma_short.iloc[-1] > sma_long.iloc[-1]
        prev_above = sma_short.iloc[-2] > sma_long.iloc[-2]

        # ATR for stop-loss/take-profit sizing
        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None

        current_price = close.iloc[-1]

        # Golden cross: BUY
        if curr_above and not prev_above:
            stop_loss = current_price - 2 * atr
            take_profit = current_price + 3 * atr
            return Signal(
                ticker=ticker,
                action="BUY",
                confidence=self._calc_confidence(df, sma_short, sma_long),
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=(
                    f"SMA{self.short_period} crossed above SMA{self.long_period}. "
                    f"Price={current_price:.2f}, ATR={atr:.2f}"
                ),
                price=round(current_price, 2),
            )

        # Death cross: SELL
        if not curr_above and prev_above:
            return Signal(
                ticker=ticker,
                action="SELL",
                confidence=self._calc_confidence(df, sma_short, sma_long),
                stop_loss=current_price + 2 * atr,
                take_profit=current_price - 3 * atr,
                reason=(
                    f"SMA{self.short_period} crossed below SMA{self.long_period}. "
                    f"Price={current_price:.2f}"
                ),
                price=round(current_price, 2),
            )

        return None

    def _calculate_atr(self, df: pd.DataFrame) -> float:
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(self.atr_period).mean()
        return float(atr.iloc[-1])

    def _calc_confidence(
        self, df: pd.DataFrame, sma_short: pd.Series, sma_long: pd.Series
    ) -> float:
        """Confidence based on volume confirmation and trend alignment."""
        confidence = 0.5

        # Volume confirmation: above-average volume increases confidence
        avg_vol = df["Volume"].rolling(20).mean().iloc[-1]
        if df["Volume"].iloc[-1] > avg_vol * 1.2:
            confidence += 0.2

        # Trend alignment with SMA200 (if enough data)
        if len(df) >= 200:
            sma200 = df["Close"].rolling(200).mean().iloc[-1]
            if df["Close"].iloc[-1] > sma200:
                confidence += 0.15

        return min(confidence, 1.0)

    def get_params(self) -> dict:
        return {
            "short_period": self.short_period,
            "long_period": self.long_period,
            "atr_period": self.atr_period,
        }
