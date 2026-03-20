"""SMAクロスオーバー戦略プラグイン v4.0

v3.0からの変更点（V2ベースに戻し、SMA200フィルタのみ追加）:
  - ADX閾値をV2に復帰: 30 → 25（V3の30ではシグナルがほぼ出なくなっていた）
  - SMA30スロープ確認を削除（V3で追加したが、有効なGCまで排除していた）
  - ニュートラル市場ADX強化を削除（ADX>=35は厳しすぎた）
  - SL/TPをV2に復帰: SL=2.0×ATR, TP=4.0×ATR
    → V3の1.5/3.0は利幅を削りすぎた
  - max_hold_daysをV2に復帰: 12 → 20日
    → V2の10月sma_crossover(14日保有, +$223)のような中期トレードを取り逃さない
  - 個別銘柄SMA200フィルターは維持: close < SMA200ならBUY禁止
    → V3で唯一有効だったフィルタ（12月INTC -12.9%等の防止）
  - トレーリングストップ追加: 含み益1ATR以上で最高値-2.0×ATRでトレール

ゴールデンクロス（短期SMA10が長期SMA30を上抜け）で買い、
デッドクロス（短期SMAが長期SMAを下抜け）で売り。
"""
from __future__ import annotations

import pandas as pd

from src.strategy.base import BaseStrategy, ExitDecision, Signal


class SMACrossoverV4(BaseStrategy):
    name = "sma_crossover"
    version = "4.0"
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

        # ベア相場フィルター
        if market_condition.get("sp500_trend") == "bear":
            return None

        close = df["Close"]

        # 個別銘柄SMA200フィルター: 下降トレンド銘柄でのBUYを防止
        # V3で有効だったフィルタ（12月INTC -12.9%、CRM -3.0%等の防止）
        if len(df) >= 200:
            sma200 = close.rolling(200).mean().iloc[-1]
            if close.iloc[-1] < sma200:
                return None

        # SMA or EMA の選択
        ma_label = "EMA" if self.use_ema else "SMA"
        if self.use_ema:
            ma_short = close.ewm(span=self.short_period, adjust=False).mean()
            ma_long = close.ewm(span=self.long_period, adjust=False).mean()
        else:
            ma_short = close.rolling(self.short_period).mean()
            ma_long = close.rolling(self.long_period).mean()

        # ローソク足終値確定後のエントリー: 前日と前々日のクロスを検出
        curr_above = ma_short.iloc[-2] > ma_long.iloc[-2]   # 前日（終値確定済み）
        prev_above = ma_short.iloc[-3] > ma_long.iloc[-3]   # 前々日

        # クロスが発生していない場合は早期リターン
        if curr_above == prev_above:
            return None

        # ATR for stop-loss/take-profit sizing
        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None

        # ADXフィルター: トレンド強度が不十分な時はシグナルを出さない
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

    def check_exit(
        self, ticker: str, df: pd.DataFrame, trade_info: dict
    ) -> ExitDecision | None:
        """トレーリングストップ: 含み益が1ATR以上の時、最高値-2.0×ATRでトレール。"""
        if len(df) < self.atr_period + 5:
            return None

        atr = self._calculate_atr(df)
        if pd.isna(atr):
            return None

        entry_price = trade_info.get("entry_price", 0)
        highest_price = trade_info.get("highest_price", entry_price)
        current_price = float(df["Close"].iloc[-1])

        if highest_price - entry_price >= atr:
            trailing_stop = highest_price - 2.0 * atr
            if current_price <= trailing_stop:
                return ExitDecision(
                    should_exit=True,
                    reason=(
                        f"Trailing stop triggered: price {current_price:.2f} "
                        f"<= trail {trailing_stop:.2f} "
                        f"(high {highest_price:.2f} - 2.0×ATR {atr:.2f})"
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
