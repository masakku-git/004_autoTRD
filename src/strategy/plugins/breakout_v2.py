"""ブレイクアウト戦略プラグイン v2.0

v1.0からの変更点:
  - ベア相場フィルター追加: S&P500が弱気トレンドの時はBUYシグナルを出さない
    → 下落相場でのブレイクアウトは「フェイクアウト」になりやすい
  - 段階利確: 2×ATRで半分決済、4×ATRで残りを決済
  - ATRフロア: ATRに最低値（株価の2%）を設定してSLの最小幅を保証

直近20日高値を出来高増加（平均の1.5倍以上）と共に上抜けたら買い。
直近20日安値を出来高増加と共に下抜けたら売り。
全レジーム対応。
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, Signal


class BreakoutV2(BaseStrategy):
    name = "breakout"
    version = "2.0"
    target_regime = "any"

    def __init__(
        self,
        lookback: int = 20,
        volume_mult: float = 1.3,
        atr_period: int = 14,
        max_hold_days: int = 15,
    ):
        self.lookback = lookback
        self.volume_mult = volume_mult
        self.atr_period = atr_period
        self.max_hold_days = max_hold_days

    def generate_signals(
        self, ticker: str, df: pd.DataFrame, market_condition: dict
    ) -> Signal | None:
        if len(df) < self.lookback + 5:
            return None

        # ベア相場フィルター: S&P500が弱気トレンドの時はBUYを出さない。
        # 下落相場でのブレイクアウトは出来高を伴っても「フェイクアウト」が多く、
        # エントリー直後に反落するリスクが高い。
        if market_condition.get("sp500_trend") == "bear":
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

        # ATRフロア: 最低でも株価の2%を保証（低ボラ時のSL幅が狭すぎる問題を防止）
        atr = max(atr, current_price * 0.02)

        # Bullish breakout: price above recent high + volume confirmation
        if current_price > recent_high and current_volume > avg_volume * self.volume_mult:
            stop_loss = current_price - 2.0 * atr
            take_profit_1 = current_price + 2.0 * atr   # 段階利確: 半分決済
            take_profit = current_price + 4.0 * atr      # 残り決済
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
                price=round(current_price, 2),
                take_profit_1=round(take_profit_1, 2),
                max_hold_days=self.max_hold_days,
            )

        # Bearish breakdown: price below recent low + volume (for exit signals)
        if current_price < recent_low and current_volume > avg_volume * self.volume_mult:
            return Signal(
                ticker=ticker,
                action="SELL",
                confidence=self._calc_confidence(
                    recent_low, current_price, current_volume, avg_volume
                ),
                stop_loss=current_price + 2.0 * atr,
                take_profit=current_price - 4.0 * atr,
                reason=(
                    f"Bearish breakdown below {self.lookback}-day low "
                    f"({recent_low:.2f}). Vol={current_volume/avg_volume:.1f}x avg"
                ),
                price=round(current_price, 2),
                take_profit_1=round(current_price - 2.0 * atr, 2),
                max_hold_days=self.max_hold_days,
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
            "max_hold_days": self.max_hold_days,
        }
