"""SMAクロスオーバー戦略プラグイン v2.0

v1.0からの変更点:
  - ベア相場フィルター追加: S&P500が弱気トレンドの時はBUYシグナルを出さない
  - ADXフィルター追加: ADX >= 25（トレンド強度が十分な時のみ発動）
    → ADX < 25のダマシクロスによる損失を防ぐ
  - ローソク足終値確定後のエントリー: クロス翌日の始値でエントリー
  - EMAフォールバック: use_ema=TrueでEMA（指数移動平均）に切り替え可能

ゴールデンクロス（短期SMA10が長期SMA30を上抜け）で買い、
デッドクロス（短期SMAが長期SMAを下抜け）で売り。
ストップロスはATRの2倍、利確はATRの4倍で設定。
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, Signal


class SMACrossoverV2(BaseStrategy):
    name = "sma_crossover"
    version = "2.0"
    target_regime = "trending"

    def __init__(
        self,
        short_period: int = 10,
        long_period: int = 30,
        atr_period: int = 14,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        max_hold_days: int = 20,
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
        # 翌日始値エントリーのため、クロス検出に3本分の余裕が必要
        if len(df) < self.long_period + 3:
            return None

        # ベア相場フィルター: S&P500が弱気トレンドの時はBUYを出さない。
        # トレンドフォロー戦略のため、下落トレンド中のゴールデンクロスは
        # 「デッドキャットバウンス」になりやすく損失リスクが高い。
        if market_condition.get("sp500_trend") == "bear":
            return None

        close = df["Close"]

        # SMA or EMA の選択（SMA10/30が機能しない場合のEMAフォールバック）
        ma_label = "EMA" if self.use_ema else "SMA"
        if self.use_ema:
            ma_short = close.ewm(span=self.short_period, adjust=False).mean()
            ma_long = close.ewm(span=self.long_period, adjust=False).mean()
        else:
            ma_short = close.rolling(self.short_period).mean()
            ma_long = close.rolling(self.long_period).mean()

        # ローソク足終値確定後のエントリー: 前日と前々日のクロスを検出し、
        # 当日の始値でエントリーする（クロス翌日の始値エントリー）
        curr_above = ma_short.iloc[-2] > ma_long.iloc[-2]   # 前日（終値確定済み）
        prev_above = ma_short.iloc[-3] > ma_long.iloc[-3]   # 前々日

        # クロスが発生していない場合は早期リターン（ADX計算コストを省く）
        if curr_above == prev_above:
            return None

        # ATR for stop-loss/take-profit sizing
        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None

        # ADXフィルター: トレンド強度が不十分な時はシグナルを出さない。
        # ADX < 25 はレンジ相場を示し、SMA/EMAクロスはダマシになりやすい。
        adx = self._calculate_adx(df)
        if pd.isna(adx) or adx < self.adx_threshold:
            return None

        # エントリー価格は当日の始値（クロス翌日の始値）
        entry_price = float(df["Open"].iloc[-1])

        # Golden cross: BUY
        if curr_above and not prev_above:
            stop_loss = entry_price - 2 * atr
            take_profit = entry_price + 4 * atr
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
                max_hold_days=self.max_hold_days,
            )

        # Death cross: SELL
        if not curr_above and prev_above:
            return Signal(
                ticker=ticker,
                action="SELL",
                confidence=self._calc_confidence(df, ma_short, ma_long, adx),
                stop_loss=entry_price + 2 * atr,
                take_profit=entry_price - 4 * atr,
                reason=(
                    f"{ma_label}{self.short_period} crossed below {ma_label}{self.long_period}. "
                    f"Entry(Open)={entry_price:.2f}, ADX={adx:.1f}"
                ),
                price=round(entry_price, 2),
                max_hold_days=self.max_hold_days,
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
        """ADX（平均方向性指数）をWilderのEMAで計算する。

        ADX >= 25: トレンドあり（SMAクロスが有効）
        ADX < 25:  レンジ相場（SMAクロスはダマシになりやすい）
        """
        high = df["High"]
        low = df["Low"]
        close = df["Close"]

        up_move = high.diff()
        down_move = -low.diff()

        # +DM: 上昇幅が下落幅より大きく、かつ正の値の時のみ有効
        dm_plus = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        # -DM: 下落幅が上昇幅より大きく、かつ正の値の時のみ有効
        dm_minus = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        # True Range をWilderのEMAで平滑化
        tr = pd.concat(
            [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
            axis=1,
        ).max(axis=1)
        atr_s = tr.ewm(com=self.adx_period - 1, adjust=False).mean()

        # +DI / -DI の計算
        di_plus = 100 * dm_plus.ewm(com=self.adx_period - 1, adjust=False).mean() / atr_s
        di_minus = 100 * dm_minus.ewm(com=self.adx_period - 1, adjust=False).mean() / atr_s

        # DX → ADX（WilderのEMAで平滑化）
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

        # 出来高確認: 平均の1.2倍以上なら信頼度UP
        avg_vol = df["Volume"].rolling(20).mean().iloc[-1]
        if df["Volume"].iloc[-1] > avg_vol * 1.2:
            confidence += 0.1

        # SMA200との整合性
        if len(df) >= 200:
            sma200 = df["Close"].rolling(200).mean().iloc[-1]
            if df["Close"].iloc[-1] > sma200:
                confidence += 0.1

        # ADX強度ボーナス: ADXが高いほど信頼度UP（最大+0.2）
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
