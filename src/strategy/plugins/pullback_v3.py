"""押し目買い戦略プラグイン v3.0

v2.0からの変更点（損大利小対策）:
  - 最大損失キャップ: SLにエントリー価格-5%の絶対上限を追加
    → QCOM -7.2%($-273), AMD -14.3%($-310)のような暴落損失を防止
  - ブレークイーブンストップ: 含み益1ATR以上でSLをエントリー価格に引き上げ
    → 含み益がマイナスに転落するのを防止
  - 段階トレーリング: 含み益2ATR以上で最高値-1.5×ATRに引き締め
    → 大きな含み益の確保率を向上

上昇トレンド中の押し目（SMA20付近への調整）で買うコンサバティブ戦略。
ブレイクアウト（高値追い）の代替として、より有利なエントリーポイントを狙う。

エントリー条件（全て必須）:
  1. close > SMA50 > SMA200（上昇トレンド構造確認）
  2. close <= SMA20 × 1.03（SMA20付近まで押し目）
  3. RSI(14)が35〜60（過熱なし、売られすぎでもない「休憩」状態）
  4. 当日出来高 < 20日平均出来高の1.2倍（売り圧力が弱い＝健全な調整）
  5. sp500_trend != "bear"
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, ExitDecision, Signal


class PullbackV3(BaseStrategy):
    name = "pullback"
    version = "3.0"
    target_regime = "trending"

    def __init__(
        self,
        sma_short: int = 20,
        sma_mid: int = 50,
        sma_long: int = 200,
        rsi_period: int = 14,
        rsi_low: float = 35.0,
        rsi_high: float = 60.0,
        atr_period: int = 14,
        adx_period: int = 14,
        max_hold_days: int = 15,
    ):
        self.sma_short = sma_short
        self.sma_mid = sma_mid
        self.sma_long = sma_long
        self.rsi_period = rsi_period
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.max_hold_days = max_hold_days

    def generate_signals(
        self, ticker: str, df: pd.DataFrame, market_condition: dict
    ) -> Signal | None:
        # 最低200日+αのデータが必要
        if len(df) < self.sma_long + 5:
            return None

        # ベア相場フィルター
        if market_condition.get("sp500_trend") == "bear":
            return None

        close = df["Close"]
        volume = df["Volume"]
        current_price = float(close.iloc[-1])
        current_volume = float(volume.iloc[-1])

        # SMA計算
        sma20 = close.rolling(self.sma_short).mean()
        sma50 = close.rolling(self.sma_mid).mean()
        sma200 = close.rolling(self.sma_long).mean()

        sma20_val = float(sma20.iloc[-1])
        sma50_val = float(sma50.iloc[-1])
        sma200_val = float(sma200.iloc[-1])

        # 条件1: 上昇トレンド構造 close > SMA50 > SMA200
        if not (current_price > sma50_val > sma200_val):
            return None

        # 条件2: SMA20付近まで押し目（SMA20の3%以内）
        if current_price > sma20_val * 1.03:
            return None

        # 条件3: RSIが35〜60（「休憩」ゾーン）
        rsi = self._calculate_rsi(close)
        if pd.isna(rsi) or not (self.rsi_low <= rsi <= self.rsi_high):
            return None

        # 条件4: 出来高が平均の1.2倍以下（売り圧力が弱い＝健全な調整）
        avg_volume = float(volume.iloc[-20:].mean())
        if avg_volume <= 0 or current_volume >= avg_volume * 1.2:
            return None

        # ATR計算
        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None
        atr = max(atr, current_price * 0.02)

        # ストップロス: ATRベース・SMA50サポート・5%キャップの最も厳しい方を採用
        sl_atr = current_price - 2.0 * atr
        sl_sma = sma50_val - 0.5 * atr  # SMA50のすぐ下
        sl_cap = current_price * 0.95    # 最大損失キャップ: -5%
        stop_loss = max(sl_atr, sl_sma, sl_cap)

        take_profit_1 = current_price + 2.0 * atr  # 段階利確: 半分決済
        take_profit = current_price + 4.0 * atr     # 残り決済

        confidence = self._calc_confidence(df, close, volume, sma200_val, atr)

        return Signal(
            ticker=ticker,
            action="BUY",
            confidence=confidence,
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            reason=(
                f"Pullback to SMA{self.sma_short} in uptrend. "
                f"Price={current_price:.2f}, SMA20={sma20_val:.2f}, "
                f"RSI={rsi:.1f}, Vol={current_volume/avg_volume:.1f}x avg"
            ),
            price=round(current_price, 2),
            take_profit_1=round(take_profit_1, 2),
            max_hold_days=self.max_hold_days,
        )

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

        # 段階2: 含み益2ATR以上→引き締めトレーリング
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

    def _calculate_rsi(self, close: pd.Series) -> float:
        """RSIをWilderのEMAで計算する。"""
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(com=self.rsi_period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=self.rsi_period - 1, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return float(val) if not pd.isna(val) else float("nan")

    def _calc_confidence(
        self,
        df: pd.DataFrame,
        close: pd.Series,
        volume: pd.Series,
        sma200_val: float,
        atr: float,
    ) -> float:
        """信頼度: ベース0.50 + 出来高パターン + SMA200乖離 + ADX。"""
        confidence = 0.50

        # 出来高3日連続減少: 秩序ある調整
        if len(volume) >= 3:
            if volume.iloc[-1] < volume.iloc[-2] < volume.iloc[-3]:
                confidence += 0.10

        # SMA200から5%以上乖離（上方）: 強い上昇トレンド
        current_price = float(close.iloc[-1])
        if sma200_val > 0 and (current_price - sma200_val) / sma200_val > 0.05:
            confidence += 0.10

        # ADX > 25: トレンドが明確
        adx = self._calculate_adx(df)
        if not pd.isna(adx) and adx > 25:
            confidence += 0.10

        return min(confidence, 0.80)

    def _calculate_adx(self, df: pd.DataFrame) -> float:
        """ADX（平均方向性指数）をWilderのEMAで計算する。"""
        high = df["High"]
        low = df["Low"]
        close = df["Close"]

        up_move = high.diff()
        down_move = -low.diff()

        dm_plus = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        dm_minus = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        tr = pd.concat(
            [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
            axis=1,
        ).max(axis=1)
        atr_s = tr.ewm(com=self.adx_period - 1, adjust=False).mean()

        di_plus = 100 * dm_plus.ewm(com=self.adx_period - 1, adjust=False).mean() / atr_s
        di_minus = 100 * dm_minus.ewm(com=self.adx_period - 1, adjust=False).mean() / atr_s

        di_sum = (di_plus + di_minus).replace(0, float("nan"))
        dx = 100 * (di_plus - di_minus).abs() / di_sum
        adx = dx.ewm(com=self.adx_period - 1, adjust=False).mean()
        return float(adx.iloc[-1])

    def get_params(self) -> dict:
        return {
            "sma_short": self.sma_short,
            "sma_mid": self.sma_mid,
            "sma_long": self.sma_long,
            "rsi_period": self.rsi_period,
            "rsi_low": self.rsi_low,
            "rsi_high": self.rsi_high,
            "atr_period": self.atr_period,
            "max_hold_days": self.max_hold_days,
        }
