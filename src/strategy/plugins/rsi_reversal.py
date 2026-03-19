"""RSI逆張り戦略プラグイン

RSIが売られすぎ（30以下）から反発し始めたら買い。
RSIが買われすぎ（70以上）から下落し始めたら売り。
レンジ相場向け。
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

        # 売られすぎからの反発：RSIが閾値を「下から上へクロス」した時だけ発動。
        # 修正前: current_rsi > prev_rsi だと閾値以下で0.01上昇しても発動してしまう（弱すぎ）。
        # 修正後: prev_rsi < oversold かつ current_rsi >= oversold を条件にすることで、
        #         RSIが30を実際に上抜けた1本のみシグナルを発生させる。
        if prev_rsi < self.oversold and current_rsi >= self.oversold:
            stop_loss = current_price - 1.5 * atr
            take_profit = current_price + 2 * atr
            return Signal(
                ticker=ticker,
                action="BUY",
                confidence=self._calc_confidence(current_rsi, "oversold"),
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=(
                    f"RSI crossed above oversold threshold. RSI={current_rsi:.1f} "
                    f"(prev={prev_rsi:.1f}), Price={current_price:.2f}"
                ),
                price=round(current_price, 2),
            )

        # 買われすぎからの反落：RSIが閾値を「上から下へクロス」した時だけ発動。
        # 修正前: current_rsi < prev_rsi だと閾値以上で0.01下落しても発動してしまう（弱すぎ）。
        # 修正後: prev_rsi > overbought かつ current_rsi <= overbought を条件にする。
        if prev_rsi > self.overbought and current_rsi <= self.overbought:
            return Signal(
                ticker=ticker,
                action="SELL",
                confidence=self._calc_confidence(current_rsi, "overbought"),
                stop_loss=current_price + 1.5 * atr,
                take_profit=current_price - 2 * atr,
                reason=(
                    f"RSI crossed below overbought threshold. RSI={current_rsi:.1f} "
                    f"(prev={prev_rsi:.1f}), Price={current_price:.2f}"
                ),
                price=round(current_price, 2),
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
