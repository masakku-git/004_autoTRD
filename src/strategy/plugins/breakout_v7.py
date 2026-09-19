"""ブレイクアウト戦略プラグイン v7.0

v6.0からの変更点: **売買ロジックは一切変更なし**。エントリーの判定診断ログを追加しただけ。
  - generate_signals は v6 をそのまま呼んで結果を返す（シグナルは v6 と完全に一致する）
  - あわせて「どの条件で弾かれたか」を BREAKOUT-DIAG 行としてログ(DEBUG)に記録する
    → 8月末以降 breakout の発火がほぼ止まった理由を、後から数字で確認できる
    （scripts/summarize_breakout_diag.py で集計）

診断の判定順序は v6.generate_signals のフィルタ順と同じ:
  データ不足 → ベア相場 → SMA200割れ → ATR算出不能 → 20日高値ブレイク+出来高
reason の一覧:
  buy                 : BUYシグナル発火
  bearish_breakdown   : 20日安値割れ+出来高（v6ではSELLシグナル）
  too_little_data / bear_market / below_sma200 / atr_nan
  volume_low          : 高値は上抜けたが出来高が平均の volume_mult 倍に届かない（ニアミス）
  below_20d_high      : 出来高は足りたが高値を上抜けていない（ニアミス。gap_pct が小さいほど惜しい）
  no_breakout         : 高値も出来高も届かない
"""
from __future__ import annotations

import pandas as pd

# 注意: v6 は「モジュール」として import する。クラスを名前で import すると、registry が
# モジュール内の全 BaseStrategy 子クラスを名前順に登録するため、"_BreakoutV6" 等の別名が
# v7 より後に登録されて上書きしてしまう（"_" は "B" より後ろに並ぶ）。
import src.strategy.plugins.breakout_v6 as _v6
from src.strategy.base import Signal
from src.utils.logger import logger


class BreakoutV7(_v6.BreakoutV6):
    name = "breakout"
    version = "7.0"
    target_regime = "any"

    def generate_signals(
        self, ticker: str, df: pd.DataFrame, market_condition: dict
    ) -> Signal | None:
        signal = super().generate_signals(ticker, df, market_condition)  # 判定は v6 そのもの
        try:
            diag = self.diagnose_entry(df, market_condition)
            logger.debug(
                "BREAKOUT-DIAG ticker=%s result=%s reason=%s %s",
                ticker, "signal" if signal is not None else "skip", diag["reason"],
                " ".join(f"{k}={v}" for k, v in diag["metrics"].items()),
            )
        except Exception as e:  # 診断の失敗で売買判定を止めない
            logger.warning(f"BREAKOUT-DIAG failed for {ticker}: {type(e).__name__}: {e}")
        return signal

    def diagnose_entry(self, df: pd.DataFrame, market_condition: dict) -> dict:
        """v6.generate_signals のフィルタ順に沿って、BUYが出ない（出る）理由を返す。

        Returns: {"reason": str, "metrics": {指標名: 値}}。売買判定には一切使わない。
        """
        if len(df) < self.lookback + 5:
            return {"reason": "too_little_data", "metrics": {"rows": len(df)}}
        if market_condition.get("sp500_trend") == "bear":
            return {"reason": "bear_market", "metrics": {}}

        close, high, low, volume = df["Close"], df["High"], df["Low"], df["Volume"]
        price = float(close.iloc[-1])
        metrics: dict = {"price": round(price, 2), "regime": market_condition.get("regime")}

        if len(df) >= 200:
            sma200 = float(close.rolling(200).mean().iloc[-1])
            if price < sma200:
                metrics["below_sma200_pct"] = round((sma200 - price) / sma200 * 100, 2)
                return {"reason": "below_sma200", "metrics": metrics}

        recent_high = float(high.iloc[-(self.lookback + 1):-1].max())
        recent_low = float(low.iloc[-(self.lookback + 1):-1].min())
        avg_volume = float(volume.iloc[-(self.lookback + 1):-1].mean())
        if pd.isna(self._calculate_atr(df)):
            return {"reason": "atr_nan", "metrics": metrics}

        vol_ratio = float(volume.iloc[-1]) / avg_volume if avg_volume > 0 else 0.0
        price_above_high = price > recent_high
        vol_ok = float(volume.iloc[-1]) > avg_volume * self.volume_mult
        metrics.update({
            "high20": round(recent_high, 2),
            "gap_pct": round((recent_high - price) / recent_high * 100, 2),  # 正=高値まで届かない幅
            "vol_ratio": round(vol_ratio, 2),
            "vol_need": self.volume_mult,
        })

        if price_above_high and vol_ok:
            return {"reason": "buy", "metrics": metrics}
        if price < recent_low and vol_ok:
            return {"reason": "bearish_breakdown", "metrics": metrics}
        if price_above_high:
            return {"reason": "volume_low", "metrics": metrics}
        if vol_ok:
            return {"reason": "below_20d_high", "metrics": metrics}
        return {"reason": "no_breakout", "metrics": metrics}
