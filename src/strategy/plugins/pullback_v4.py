"""押し目買い戦略プラグイン v4.0

v3.0からの変更点:
  - TP動的設定: 固定ATR倍率ではなく前回スイングハイ付近にTPを設定
    → 押し目買いは「トレンド回帰」を狙うため、直近高値への回帰を利確目標にする
    → TP = max(直近高値, エントリー+2.0×ATR) で最低利幅も保証
  - RSI決済追加: RSI < 50 でトレンド勢い喪失と判断し決済
    → 押し目からの反発が不十分な場合の早期撤退
  - max_hold_days: 15 → 60（スイングトレードとして十分な保有期間を確保）
  - トレーリングストップの動的化: ADX強度に応じてトレール幅を調整
    → ADX>=30: 1.5×ATR / ADX 20-30: 2.0×ATR / ADX<20: 2.5×ATR

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


class PullbackV4(BaseStrategy):
    name = "pullback"
    version = "4.0"
    target_regime = "trending"

    def __init__(
        self,
        sma_short: int = 20,
        sma_mid: int = 50,
        sma_long: int = 200,
        rsi_period: int = 14,
        rsi_low: float = 35.0,
        rsi_high: float = 60.0,
        rsi_exit_threshold: float = 50.0,
        atr_period: int = 14,
        adx_period: int = 14,
        swing_lookback: int = 40,
        max_hold_days: int = 60,
    ):
        self.sma_short = sma_short
        self.sma_mid = sma_mid
        self.sma_long = sma_long
        self.rsi_period = rsi_period
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        self.rsi_exit_threshold = rsi_exit_threshold
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.swing_lookback = swing_lookback
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
        high = df["High"]
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

        # 動的TP: 直近スイングハイ（前回高値）を基準にする
        # 押し目買いは「トレンド回帰」を狙うため、前回高値付近まで伸ばす
        lookback_bars = min(self.swing_lookback, len(high) - 1)
        swing_high = float(high.iloc[-lookback_bars - 1 : -1].max())

        # TP1: 前回高値 or エントリー+2ATR の大きい方（最低利幅を保証）
        take_profit_1 = max(swing_high, current_price + 2.0 * atr)

        # TP2: 前回高値+1ATR（ブレイクアウトの余地を残す）or エントリー+4ATR
        take_profit = max(swing_high + atr, current_price + 4.0 * atr)

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
                f"RSI={rsi:.1f}, SwingHigh={swing_high:.2f}"
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
        2. 動的トレーリングストップ: 含み益2ATR以上→ADX強度に応じたトレール幅で追従
        3. RSI決済: RSI < 50 でトレンド勢い喪失と判断し決済
        """
        if len(df) < max(self.atr_period, self.adx_period, self.rsi_period) + 5:
            return None

        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None
        atr = max(atr, float(df["Close"].iloc[-1]) * 0.02)

        entry_price = trade_info.get("entry_price", 0)
        highest_price = trade_info.get("highest_price", entry_price)
        current_price = float(df["Close"].iloc[-1])
        unrealized = highest_price - entry_price

        # 段階2: 含み益2ATR以上→ADX連動の動的トレーリングストップ
        if unrealized >= 2.0 * atr:
            trail_mult = self._dynamic_trail_multiplier(df)
            trailing_stop = highest_price - trail_mult * atr
            if current_price <= trailing_stop:
                return ExitDecision(
                    should_exit=True,
                    reason=(
                        f"Dynamic trailing stop: price {current_price:.2f} "
                        f"<= trail {trailing_stop:.2f} "
                        f"(high {highest_price:.2f} - {trail_mult:.1f}×ATR {atr:.2f})"
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

        # RSI決済: RSI < 50 でモメンタム減衰を検知
        # 含み益がある場合のみ適用（損失時はSL/ブレークイーブンに任せる）
        if current_price > entry_price:
            rsi = self._calculate_rsi(df["Close"])
            if not pd.isna(rsi) and rsi < self.rsi_exit_threshold:
                return ExitDecision(
                    should_exit=True,
                    reason=(
                        f"RSI exit: RSI={rsi:.1f} < {self.rsi_exit_threshold} "
                        f"(momentum fading, price={current_price:.2f})"
                    ),
                )

        return None

    def _dynamic_trail_multiplier(self, df: pd.DataFrame) -> float:
        """ADX強度に応じてトレーリングストップ幅を動的に調整する。
        ADX>30（強トレンド）: 1.5×ATR — 利益確保を優先
        ADX 20-30（通常）: 2.0×ATR — 利益を伸ばす余地を確保
        ADX<20（弱トレンド）: 2.5×ATR — ノイズによる早期退場を防止
        """
        adx = self._calculate_adx(df)
        if pd.isna(adx):
            return 2.0
        if adx > 30:
            return 1.5
        if adx >= 20:
            return 2.0
        return 2.5

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
            "rsi_exit_threshold": self.rsi_exit_threshold,
            "atr_period": self.atr_period,
            "swing_lookback": self.swing_lookback,
            "max_hold_days": self.max_hold_days,
        }
