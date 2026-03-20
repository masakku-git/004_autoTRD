"""ブレイクアウト戦略プラグイン v3.0

v2.0からの変更点:
  - 出来高フィルター強化: 1.3倍 → 1.8倍（フェイクブレイクアウト大幅削減）
  - 終値確認フィルター追加: ブレイクアウト日が陽線であることを要求
  - 最小ブレイクアウト幅: 20日高値の0.5%以上を要求（微小ブレイクアウト除外）
  - ニュートラル市場ペナルティ: sp500_trend=neutral時は出来高2.16倍を要求
  - SL/TP調整: SL=1.5×ATR, TP1=1.5×ATR, TP2=3.0×ATR（損切り迅速化＋現実的利確）
  - max_hold_days: 15 → 10（長期保有の成績悪化に対応）
  - トレーリングストップ実装: check_exitで含み益1ATR以上時に最高値-1.5×ATRでトレール
  - 信頼度ベース: 0.4 → 0.45（フィルタ強化による品質向上を反映）

直近20日高値を出来高増加（平均の1.8倍以上）と共に上抜けたら買い。
直近20日安値を出来高増加と共に下抜けたら売り。
全レジーム対応。
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, ExitDecision, Signal


class BreakoutV3(BaseStrategy):
    name = "breakout"
    version = "3.0"
    target_regime = "any"

    def __init__(
        self,
        lookback: int = 20,
        volume_mult: float = 1.8,
        atr_period: int = 14,
        max_hold_days: int = 10,
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

        # ベア相場フィルター: S&P500が弱気トレンドの時はBUYを出さない
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

        # ATRフロア: 最低でも株価の2%を保証
        atr = max(atr, current_price * 0.02)

        # ニュートラル市場では出来高要件を厳格化（1.8 × 1.2 = 2.16倍）
        required_volume_mult = self.volume_mult
        if market_condition.get("sp500_trend") == "neutral":
            required_volume_mult = self.volume_mult * 1.2

        # Bullish breakout
        if current_price > recent_high and current_volume > avg_volume * required_volume_mult:
            # 終値確認: ブレイクアウト日が前日より高い（陽線確認）
            if len(close) >= 2 and close.iloc[-1] <= close.iloc[-2]:
                return None

            # 最小ブレイクアウト幅: 20日高値の0.5%以上を要求
            breakout_pct = (current_price - recent_high) / recent_high
            if breakout_pct < 0.005:
                return None

            stop_loss = current_price - 1.5 * atr
            take_profit_1 = current_price + 1.5 * atr  # 段階利確: 半分決済
            take_profit = current_price + 3.0 * atr     # 残り決済
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
                    f"({recent_high:.2f}), +{breakout_pct:.1%}. "
                    f"Vol={current_volume/avg_volume:.1f}x avg"
                ),
                price=round(current_price, 2),
                take_profit_1=round(take_profit_1, 2),
                max_hold_days=self.max_hold_days,
            )

        # Bearish breakdown
        if current_price < recent_low and current_volume > avg_volume * required_volume_mult:
            return Signal(
                ticker=ticker,
                action="SELL",
                confidence=self._calc_confidence(
                    recent_low, current_price, current_volume, avg_volume
                ),
                stop_loss=current_price + 1.5 * atr,
                take_profit=current_price - 3.0 * atr,
                reason=(
                    f"Bearish breakdown below {self.lookback}-day low "
                    f"({recent_low:.2f}). Vol={current_volume/avg_volume:.1f}x avg"
                ),
                price=round(current_price, 2),
                take_profit_1=round(current_price - 1.5 * atr, 2),
                max_hold_days=self.max_hold_days,
            )

        return None

    def check_exit(
        self, ticker: str, df: pd.DataFrame, trade_info: dict
    ) -> ExitDecision | None:
        """トレーリングストップ: 含み益が1ATR以上の時、最高値-1.5×ATRでトレール。"""
        if len(df) < self.atr_period + 5:
            return None

        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None
        atr = max(atr, float(df["Close"].iloc[-1]) * 0.02)

        entry_price = trade_info.get("entry_price", 0)
        highest_price = trade_info.get("highest_price", entry_price)
        current_price = float(df["Close"].iloc[-1])

        # 含み益が1ATR以上の場合にトレーリングストップを有効化
        if highest_price - entry_price >= atr:
            trailing_stop = highest_price - 1.5 * atr
            if current_price <= trailing_stop:
                return ExitDecision(
                    should_exit=True,
                    reason=(
                        f"Trailing stop triggered: price {current_price:.2f} "
                        f"<= trail {trailing_stop:.2f} "
                        f"(high {highest_price:.2f} - 1.5×ATR {atr:.2f})"
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
        breakout_pct = abs(price - level) / level
        vol_ratio = volume / avg_volume if avg_volume > 0 else 1.0
        # ベース0.45 + ブレイクアウト幅ボーナス + 出来高ボーナス（新閾値基準）
        confidence = 0.45 + min(breakout_pct * 10, 0.25) + min((vol_ratio - 1.8) * 0.15, 0.2)
        return min(confidence, 1.0)

    def get_params(self) -> dict:
        return {
            "lookback": self.lookback,
            "volume_mult": self.volume_mult,
            "atr_period": self.atr_period,
            "max_hold_days": self.max_hold_days,
        }
