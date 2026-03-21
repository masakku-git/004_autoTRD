"""ブレイクアウト戦略プラグイン v5.0

v4.0からの変更点（損大利小対策）:
  - 最大損失キャップ: SLをATRベースとエントリー価格-5%の厳しい方に設定
    → AMD -14.3%, QCOM -7.2%, SBUX -6.2%のような暴落損失を防止
  - ブレークイーブンストップ: 含み益1ATR以上でSLをエントリー価格に引き上げ
    → 含み益がマイナスに転落するのを防止（微益7/22件の改善）
  - 段階トレーリング: 含み益2ATR以上で最高値-1.5×ATRに引き締め
    → 大きな含み益の確保率を向上

直近20日高値を出来高増加（平均の1.5倍以上）と共に上抜けたら買い。
直近20日安値を出来高増加と共に下抜けたら売り。
全レジーム対応。
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, ExitDecision, Signal


class BreakoutV5(BaseStrategy):
    name = "breakout"
    version = "5.0"
    target_regime = "any"

    def __init__(
        self,
        lookback: int = 20,
        volume_mult: float = 1.5,
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

        # ベア相場フィルター: S&P500が弱気トレンドの時はBUYを出さない
        if market_condition.get("sp500_trend") == "bear":
            return None

        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

        current_price = close.iloc[-1]
        current_volume = volume.iloc[-1]

        # 個別銘柄SMA200フィルター: 下降トレンド銘柄でのBUYを防止
        if len(df) >= 200:
            sma200 = close.rolling(200).mean().iloc[-1]
            if current_price < sma200:
                return None

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
            # 最大損失キャップ: ATRベースと5%の厳しい方（高い方）を採用
            sl_atr = current_price - 2.0 * atr
            sl_cap = current_price * 0.95  # エントリー価格の-5%
            stop_loss = max(sl_atr, sl_cap)
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
                stop_loss=min(current_price + 2.0 * atr, current_price * 1.05),
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

    def check_exit(
        self, ticker: str, df: pd.DataFrame, trade_info: dict
    ) -> ExitDecision | None:
        """段階的エグジット管理:
        1. ブレークイーブンストップ: 含み益1ATR以上→SLをエントリー価格に引き上げ
        2. トレーリングストップ: 含み益2ATR以上→最高値-1.5×ATRに引き締め
        """
        if len(df) < self.atr_period + 5:
            return None

        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None
        atr = max(atr, float(df["Close"].iloc[-1]) * 0.02)

        entry_price = trade_info.get("entry_price", 0)
        highest_price = trade_info.get("highest_price", entry_price)
        current_price = float(df["Close"].iloc[-1])
        unrealized = highest_price - entry_price

        # 段階2: 含み益2ATR以上→引き締めトレーリング（最高値-1.5×ATR）
        if unrealized >= 2.0 * atr:
            trailing_stop = highest_price - 1.5 * atr
            if current_price <= trailing_stop:
                return ExitDecision(
                    should_exit=True,
                    reason=(
                        f"Tight trailing stop: price {current_price:.2f} "
                        f"<= trail {trailing_stop:.2f} "
                        f"(high {highest_price:.2f} - 1.5×ATR {atr:.2f})"
                    ),
                )

        # 段階1: 含み益1ATR以上→ブレークイーブンストップ
        if unrealized >= atr:
            if current_price <= entry_price:
                return ExitDecision(
                    should_exit=True,
                    reason=(
                        f"Break-even stop: price {current_price:.2f} "
                        f"<= entry {entry_price:.2f} "
                        f"(was up {unrealized/atr:.1f}×ATR)"
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
            "max_hold_days": self.max_hold_days,
        }
