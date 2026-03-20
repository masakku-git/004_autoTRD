"""SMAクロスオーバー戦略プラグイン v3.0

v2.0からの変更点:
  - ADX閾値引き上げ: 25 → 30（弱トレンドのダマシ排除、12月0%勝率の再発防止）
  - 個別銘柄SMA200フィルター追加: BUYはclose > SMA200の銘柄のみ
    → 12月のINTC(-12.9%)、CRM(-3.0%)等の下降トレンド銘柄での損失を防止
  - SMA30スロープ確認: 長期MAが上昇中であることを要求
  - ニュートラル市場強化: sp500_trend=neutral時はADX >= 35を要求
  - SL/TP調整: SL=1.5×ATR, TP1=1.5×ATR(段階利確追加), TP2=3.0×ATR
  - max_hold_days: 20 → 12（INTC 12日-12.9%のような長期損失を防止）
  - トレーリングストップ実装: check_exitで含み益1ATR以上時に最高値-1.5×ATRでトレール

ゴールデンクロス（短期SMA10が長期SMA30を上抜け）で買い、
デッドクロス（短期SMAが長期SMAを下抜け）で売り。
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, ExitDecision, Signal


class SMACrossoverV3(BaseStrategy):
    name = "sma_crossover"
    version = "3.0"
    target_regime = "trending"

    def __init__(
        self,
        short_period: int = 10,
        long_period: int = 30,
        atr_period: int = 14,
        adx_period: int = 14,
        adx_threshold: float = 30.0,
        max_hold_days: int = 12,
        use_ema: bool = False,
    ):
        self.short_period = short_period
        self.long_period = long_period
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.max_hold_days = max_hold_days
        self.use_ema = use_ema

    def generate_signals(
        self, ticker: str, df: pd.DataFrame, market_condition: dict
    ) -> Signal | None:
        if len(df) < self.long_period + 3:
            return None

        # ベア相場フィルター
        if market_condition.get("sp500_trend") == "bear":
            return None

        close = df["Close"]

        # 個別銘柄SMA200フィルター: 下降トレンド銘柄でのBUYを防止
        if len(df) >= 200:
            sma200 = close.rolling(200).mean().iloc[-1]
            if close.iloc[-1] < sma200:
                return None

        # SMA or EMA
        ma_label = "EMA" if self.use_ema else "SMA"
        if self.use_ema:
            ma_short = close.ewm(span=self.short_period, adjust=False).mean()
            ma_long = close.ewm(span=self.long_period, adjust=False).mean()
        else:
            ma_short = close.rolling(self.short_period).mean()
            ma_long = close.rolling(self.long_period).mean()

        # クロス検出（前日と前々日の比較）
        curr_above = ma_short.iloc[-2] > ma_long.iloc[-2]
        prev_above = ma_short.iloc[-3] > ma_long.iloc[-3]

        if curr_above == prev_above:
            return None

        # ATR
        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None

        # ADXフィルター（ニュートラル市場ではさらに厳格化）
        adx = self._calculate_adx(df)
        if pd.isna(adx):
            return None

        effective_adx_threshold = self.adx_threshold
        if market_condition.get("sp500_trend") == "neutral":
            effective_adx_threshold = 35.0

        if adx < effective_adx_threshold:
            return None

        # SMA30スロープ確認: 長期MAが上昇中であることを要求
        if len(ma_long) >= 5 and ma_long.iloc[-1] <= ma_long.iloc[-5]:
            # ゴールデンクロスだが長期MAが下降中 → 弱いシグナル、スキップ
            if curr_above and not prev_above:
                return None

        entry_price = float(df["Open"].iloc[-1])

        # Golden cross: BUY
        if curr_above and not prev_above:
            stop_loss = entry_price - 1.5 * atr
            take_profit_1 = entry_price + 1.5 * atr  # 段階利確（V3で追加）
            take_profit = entry_price + 3.0 * atr
            return Signal(
                ticker=ticker,
                action="BUY",
                confidence=self._calc_confidence(df, ma_short, ma_long, adx),
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                reason=(
                    f"{ma_label}{self.short_period} crossed above {ma_label}{self.long_period}. "
                    f"Entry(Open)={entry_price:.2f}, ATR={atr:.2f}, ADX={adx:.1f}"
                ),
                price=round(entry_price, 2),
                take_profit_1=round(take_profit_1, 2),
                max_hold_days=self.max_hold_days,
            )

        # Death cross: SELL
        if not curr_above and prev_above:
            return Signal(
                ticker=ticker,
                action="SELL",
                confidence=self._calc_confidence(df, ma_short, ma_long, adx),
                stop_loss=entry_price + 1.5 * atr,
                take_profit=entry_price - 3.0 * atr,
                reason=(
                    f"{ma_label}{self.short_period} crossed below {ma_label}{self.long_period}. "
                    f"Entry(Open)={entry_price:.2f}, ADX={adx:.1f}"
                ),
                price=round(entry_price, 2),
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

        entry_price = trade_info.get("entry_price", 0)
        highest_price = trade_info.get("highest_price", entry_price)
        current_price = float(df["Close"].iloc[-1])

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
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(self.atr_period).mean()
        return float(atr.iloc[-1])

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

    def _calc_confidence(
        self,
        df: pd.DataFrame,
        sma_short: pd.Series,
        sma_long: pd.Series,
        adx: float,
    ) -> float:
        """信頼度: 出来高確認 + SMA200トレンド + ADX強度で算出。"""
        confidence = 0.5

        avg_vol = df["Volume"].rolling(20).mean().iloc[-1]
        if df["Volume"].iloc[-1] > avg_vol * 1.2:
            confidence += 0.1

        if len(df) >= 200:
            sma200 = df["Close"].rolling(200).mean().iloc[-1]
            if df["Close"].iloc[-1] > sma200:
                confidence += 0.1

        confidence += min((adx - self.adx_threshold) * 0.005, 0.2)

        return min(confidence, 1.0)

    def get_params(self) -> dict:
        return {
            "short_period": self.short_period,
            "long_period": self.long_period,
            "atr_period": self.atr_period,
            "adx_period": self.adx_period,
            "adx_threshold": self.adx_threshold,
            "max_hold_days": self.max_hold_days,
            "use_ema": self.use_ema,
        }
