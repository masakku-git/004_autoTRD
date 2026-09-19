"""押し目買い戦略プラグイン v5.0

v4.0からの変更点: **TP1（段階利確）消化後のトレーリングストップ幅を絞る**。それ以外は v4 と同一。
  - エントリー条件・SL・TP・RSI決済・ブレークイーブンは v4 のまま
  - TP1消化済みロット(tp1_hit=True)のトレール幅を、ADX連動の値(1.5/2.0/2.5×ATR)と
    tp1_trail_cap の小さい方にする（既定 1.5×ATR）
    → TP1 で利益の半分を確定した後の残りは「含み益を守る」フェーズなので、
      高値からの吐き出しを抑える。TP1未消化のロットは v4 と同じ幅で、値動きに余裕を持たせる。

背景: 2026-08 の XOM は高値 $168.64 から $158.24 まで(-6.2%)下がって決済され、
TP1 消化後の残りの利益が +7〜8% から +2〜3% に縮んだ（トレール幅 2.0×ATR＋約定ラグ）。

判定は check_exit の1点だけが v4 と異なる（generate_signals は v4 をそのまま継承する）。
"""
from __future__ import annotations

import pandas as pd

# v4 は「モジュール」として import する（クラスを名前で import すると、registry がモジュール内の
# 全 BaseStrategy 子クラスを名前順に登録し、別名が v5 を上書きしてしまうため）。
import src.strategy.plugins.pullback_v4 as _v4
from src.strategy.base import ExitDecision


class PullbackV5(_v4.PullbackV4):
    name = "pullback"
    version = "5.0"
    target_regime = "trending"

    def __init__(self, tp1_trail_cap: float = 1.5, **kwargs):
        super().__init__(**kwargs)
        self.tp1_trail_cap = tp1_trail_cap

    def check_exit(
        self, ticker: str, df: pd.DataFrame, trade_info: dict
    ) -> ExitDecision | None:
        """v4 の check_exit と同じ。違いは TP1 消化後のトレール幅の上限のみ。"""
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

        # 段階2: 含み益2ATR以上→トレーリングストップ（TP1消化後は幅を絞る）
        if unrealized >= 2.0 * atr:
            trail_mult = self._dynamic_trail_multiplier(df)
            tightened = bool(trade_info.get("tp1_hit")) and trail_mult > self.tp1_trail_cap
            if tightened:
                trail_mult = self.tp1_trail_cap
            trailing_stop = highest_price - trail_mult * atr
            if current_price <= trailing_stop:
                return ExitDecision(
                    should_exit=True,
                    reason=(
                        f"Dynamic trailing stop{' (TP1後に短縮)' if tightened else ''}: "
                        f"price {current_price:.2f} <= trail {trailing_stop:.2f} "
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

        # RSI決済: 含み益がある場合のみ
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

    def get_params(self) -> dict:
        return {**super().get_params(), "tp1_trail_cap": self.tp1_trail_cap}
