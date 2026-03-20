#!/usr/bin/env python3
"""過去データシミュレーション — 指定日付で日次トレーディングロジックを再現する。

DB不要・Slack通知なし。yfinanceから直接データを取得してインメモリで処理する。
各日付でOHLCVデータを切り詰めて使用し、実際の日次ワークフロー
（市場判定→スクリーニング→シグナル生成→批判評価→リスク管理→約定）を再現する。

使い方:
    python3 scripts/simulate.py 2025-03-19 2025-03-20 2025-03-21
    python3 scripts/simulate.py 2025-03-19:2025-03-23   # 範囲指定（平日のみ）
    python3 scripts/simulate.py 2025-03-19:2025-03-23 --capital 5000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import yfinance as yf

# --- DB依存を回避するため、settingsとstrategy系のimportを工夫 ---
# settings は直接使わず、パラメータをローカルに定義
# strategy プラグインのみ直接importする

from src.strategy.base import BaseStrategy, Signal

# スクリーニング対象銘柄
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B",
    "UNH", "JNJ", "V", "XOM", "JPM", "PG", "MA", "HD", "CVX", "MRK",
    "ABBV", "LLY", "PEP", "KO", "COST", "AVGO", "WMT", "MCD", "CSCO",
    "ACN", "TMO", "ABT", "DHR", "CRM", "NKE", "ORCL", "TXN", "AMD",
    "PM", "UPS", "NEE", "UNP", "LOW", "MS", "GS", "BLK", "ISRG",
    "INTC", "QCOM", "AMAT", "ADP", "SBUX",
]

SP500_TICKER = "^GSPC"
VIX_TICKER = "^VIX"

# リスク管理パラメータ
MAX_POSITIONS = 3
RISK_PER_TRADE_PCT = 0.02
MAX_PORTFOLIO_EXPOSURE_PCT = 0.90

# Critic閾値
APPROVAL_THRESHOLD = 0.25


# ===========================================================================
# yfinance データ取得（DB不要版）
# ===========================================================================

def fetch_all_data(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """yfinanceから全銘柄の過去2年分データを一括取得"""
    all_data: dict[str, pd.DataFrame] = {}
    total = len(tickers)

    for i, ticker in enumerate(tickers):
        print(f"\r  [{i+1}/{total}] {ticker}...", end="", flush=True)
        try:
            df = yf.download(ticker, period="2y", progress=False, auto_adjust=False)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                all_data[ticker] = df
        except Exception as e:
            print(f"\n  警告: {ticker} のデータ取得失敗: {e}")
        if i < total - 1:
            time.sleep(0.3)

    print(f"\r  {total}/{total} 銘柄のデータ取得完了" + " " * 20)
    return all_data


# ===========================================================================
# 戦略プラグイン読み込み（registry.pyのDB依存を回避）
# ===========================================================================

def load_strategies() -> list[BaseStrategy]:
    """戦略プラグインをインスタンス化して返す"""
    strategies = []
    # plugins ディレクトリから直接import（v2版）
    from src.strategy.plugins.sma_crossover_v2 import SMACrossoverV2
    from src.strategy.plugins.breakout_v2 import BreakoutV2
    from src.strategy.plugins.rsi_reversal_v2 import RSIReversalV2

    strategies.append(SMACrossoverV2())
    strategies.append(BreakoutV2())
    strategies.append(RSIReversalV2())
    return strategies


def select_strategies_for_regime(all_strategies: list[BaseStrategy], regime: str) -> list[BaseStrategy]:
    """レジームに合った戦略を選択"""
    return [s for s in all_strategies if s.target_regime in (regime, "any")]


# ===========================================================================
# 市場環境判定（インメモリ版）
# ===========================================================================

def assess_market_condition_at(
    sp500_df: pd.DataFrame, vix_df: pd.DataFrame, sim_date: date
) -> dict:
    """指定日時点の市場環境を判定"""
    condition = {
        "date": sim_date,
        "sp500_trend": "neutral",
        "vix_level": 0.0,
        "market_breadth": 0.0,
        "regime": "range",
    }

    sp500_slice = sp500_df[sp500_df.index <= pd.Timestamp(sim_date)]
    vix_slice = vix_df[vix_df.index <= pd.Timestamp(sim_date)]

    if len(sp500_slice) >= 200:
        close = sp500_slice["Close"]
        sma50 = close.rolling(50).mean().iloc[-1]
        sma200 = close.rolling(200).mean().iloc[-1]
        current = close.iloc[-1]
        condition["sp500_close"] = float(current)
        condition["sp500_sma200"] = float(sma200)
        if current > sma200 and sma50 > sma200:
            condition["sp500_trend"] = "bull"
        elif current < sma200 and sma50 < sma200:
            condition["sp500_trend"] = "bear"

    if not vix_slice.empty:
        condition["vix_level"] = float(vix_slice["Close"].iloc[-1])

    vix = condition["vix_level"]
    trend = condition["sp500_trend"]
    if vix > 30:
        condition["regime"] = "volatile"
    elif trend in ("bull", "bear"):
        condition["regime"] = "trending"
    else:
        condition["regime"] = "range"

    return condition


# ===========================================================================
# スクリーニング（インメモリ版）
# ===========================================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def screen_ticker(ticker: str, df: pd.DataFrame) -> dict | None:
    lookback = 20
    if len(df) < lookback:
        return None

    recent = df.tail(lookback)
    last_close = float(recent["Close"].iloc[-1])
    avg_volume = float(recent["Volume"].mean())

    if last_close < 5.0 or last_close > 500.0:
        return None
    if avg_volume < 500_000:
        return None

    atr = calculate_atr(df)
    if atr.empty or pd.isna(atr.iloc[-1]):
        return None
    atr_pct = (float(atr.iloc[-1]) / last_close) * 100
    if atr_pct < 1.0:
        return None

    # Relative strength
    rs = 0.0
    if len(df) >= lookback:
        rs = (df["Close"].iloc[-1] / df["Close"].iloc[-lookback] - 1) * 100

    return {
        "ticker": ticker,
        "last_close": round(last_close, 2),
        "avg_volume": int(avg_volume),
        "atr_pct": round(atr_pct, 2),
        "relative_strength": round(float(rs), 2),
    }


def run_screening_at(
    all_data: dict[str, pd.DataFrame], sim_date: date, top_n: int = 15
) -> list[dict]:
    candidates = []
    for ticker in DEFAULT_UNIVERSE:
        df = all_data.get(ticker)
        if df is None or df.empty:
            continue
        df_slice = df[df.index <= pd.Timestamp(sim_date)]
        if df_slice.empty:
            continue
        result = screen_ticker(ticker, df_slice)
        if result:
            result["score"] = float(result["relative_strength"] * 0.6 + result["atr_pct"] * 0.4)
            candidates.append(result)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


# ===========================================================================
# Critic（DB書き込み不要版 — ロジックを直接実装）
# ===========================================================================

def evaluate_signal_local(
    signal: Signal, df: pd.DataFrame, market_condition: dict
) -> dict:
    """シグナルを批判的に評価する（DB不要版）"""
    objections = []

    # チェック1: トレンド矛盾
    trend = market_condition.get("sp500_trend", "neutral")
    if signal.action == "BUY" and trend == "bear":
        objections.append({"check": "trend_contradiction", "penalty": 0.25,
                           "reason": "弱気相場での買いエントリー"})
    elif signal.action == "BUY" and trend == "neutral":
        objections.append({"check": "trend_contradiction", "penalty": 0.05,
                           "reason": "市場トレンドが中立 — 上昇の追い風なし"})

    # チェック2: VIXリスク
    vix = market_condition.get("vix_level", 20)
    if signal.action == "BUY":
        if vix > 35:
            objections.append({"check": "vix_risk", "penalty": 0.30,
                               "reason": f"VIX {vix:.1f} — 極度の恐怖"})
        elif vix > 25:
            objections.append({"check": "vix_risk", "penalty": 0.15,
                               "reason": f"VIX {vix:.1f} — ボラティリティ上昇"})

    # チェック3: 出来高減少
    if len(df) >= 20 and signal.action == "BUY":
        recent_vol = float(df["Volume"].iloc[-5:].mean())
        prior_vol = float(df["Volume"].iloc[-20:-5].mean())
        if prior_vol > 0 and recent_vol < prior_vol * 0.7:
            objections.append({"check": "volume_decline", "penalty": 0.20,
                               "reason": f"出来高減少: {recent_vol:,.0f} vs {prior_vol:,.0f}"})

    # チェック4: 過度な値動き
    if len(df) >= 21:
        close = df["Close"]
        pct_5d = (float(close.iloc[-1]) / float(close.iloc[-6]) - 1) * 100
        pct_20d = (float(close.iloc[-1]) / float(close.iloc[-21]) - 1) * 100
        if signal.action == "BUY":
            if pct_5d > 10:
                objections.append({"check": "overextended", "penalty": 0.25,
                                   "reason": f"5日で{pct_5d:.1f}%上昇 — 高値追い"})
            elif pct_20d > 20:
                objections.append({"check": "overextended", "penalty": 0.15,
                                   "reason": f"20日で{pct_20d:.1f}%上昇 — 平均回帰リスク"})

    # チェック5: リスク/リワード比
    if signal.action == "BUY":
        current_price = float(df["Close"].iloc[-1])
        risk = abs(current_price - signal.stop_loss)
        reward = abs(signal.take_profit - current_price)
        if risk > 0:
            rr = reward / risk
            if rr < 1.0:
                objections.append({"check": "risk_reward", "penalty": 0.30,
                                   "reason": f"R/R比 {rr:.2f}:1 — リスク過大"})
            elif rr < 1.5:
                objections.append({"check": "risk_reward", "penalty": 0.10,
                                   "reason": f"R/R比 {rr:.2f}:1 — 最低1.5:1推奨"})

    # チェック6: レジスタンス近接
    if len(df) >= 60 and signal.action == "BUY":
        current_price = float(df["Close"].iloc[-1])
        high_60d = float(df["High"].iloc[-60:].max())
        if high_60d > 0 and (high_60d - current_price) / high_60d < 0.02:
            objections.append({"check": "resistance", "penalty": 0.08,
                               "reason": f"60日高値${high_60d:.2f}の2%以内"})

    # チェック7: 流動性
    if len(df) >= 20 and signal.action == "BUY":
        avg_vol = float(df["Volume"].iloc[-20:].mean())
        current_price = float(df["Close"].iloc[-1])
        daily_dollar_vol = avg_vol * current_price
        if daily_dollar_vol < 5_000_000:
            objections.append({"check": "low_liquidity", "penalty": 0.15,
                               "reason": f"日次出来高${daily_dollar_vol:,.0f} < $5M"})

    total_penalty = sum(o["penalty"] for o in objections)
    adjusted = max(signal.confidence - total_penalty, 0.0)
    approved = adjusted >= APPROVAL_THRESHOLD

    return {
        "approved": approved,
        "original_confidence": signal.confidence,
        "adjusted_confidence": adjusted,
        "objections": objections,
    }


# ===========================================================================
# リスク管理（インメモリ版）
# ===========================================================================

def approve_trade_local(signal: Signal, total_equity: float, cash: float,
                        market_value: float, num_positions: int) -> dict:
    """トレード承認（DB不要版）"""
    if signal.action == "SELL":
        return {"approved": True, "quantity": 0, "reason": "売却承認"}

    if num_positions >= MAX_POSITIONS:
        return {"approved": False, "quantity": 0,
                "reason": f"ポジション上限 ({num_positions}/{MAX_POSITIONS})"}

    if signal.stop_loss <= 0:
        return {"approved": False, "quantity": 0, "reason": "ストップロスなし"}

    max_investment = total_equity * MAX_PORTFOLIO_EXPOSURE_PCT
    available_cash = min(cash, max_investment - market_value)
    if available_cash <= 0:
        return {"approved": False, "quantity": 0, "reason": "エクスポージャー上限"}

    risk_amount = total_equity * RISK_PER_TRADE_PCT
    entry_est = signal.price if signal.price > 0 else (signal.stop_loss + signal.take_profit) / 2
    risk_per_share = abs(entry_est - signal.stop_loss)
    if risk_per_share <= 0:
        return {"approved": False, "quantity": 0, "reason": "リスク/株算出不可"}

    qty = int(risk_amount / risk_per_share)
    if qty <= 0:
        return {"approved": False, "quantity": 0, "reason": "数量0"}

    # 40%上限
    if qty * entry_est > total_equity * 0.40:
        qty = int(total_equity * 0.40 / entry_est)

    # 現金上限
    if qty * entry_est > available_cash:
        qty = int(available_cash / entry_est)

    if qty <= 0:
        return {"approved": False, "quantity": 0, "reason": "現金不足"}

    return {"approved": True, "quantity": qty,
            "reason": f"承認: {qty}株, リスク=${risk_amount:.2f}"}


# ===========================================================================
# ポートフォリオシミュレーター
# ===========================================================================

class SimulatedPortfolio:
    def __init__(self, initial_cash: float = 3300.0):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: list[dict] = []
        self.closed_trades: list[dict] = []
        self.daily_snapshots: list[dict] = []

    def get_market_value(self, prices: dict[str, float]) -> float:
        return sum(
            p["qty"] * prices.get(p["ticker"], p["entry_price"])
            for p in self.positions
        )

    def get_total_equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.get_market_value(prices)

    def buy(self, ticker: str, qty: int, price: float, sim_date: date, reason: str,
            strategy_name: str = "", regime: str = "", confidence: float = 0.0,
            entry_reason: str = "",
            stop_loss: float = 0.0, take_profit: float = 0.0, take_profit_1: float = 0.0,
            max_hold_days: int = 20):
        cost = qty * price
        if cost > self.cash:
            return None
        self.cash -= cost
        pos = {"ticker": ticker, "qty": qty, "entry_price": price, "entry_date": sim_date,
               "strategy": strategy_name, "regime": regime, "confidence": confidence,
               "entry_reason": entry_reason,
               "stop_loss": stop_loss, "take_profit": take_profit,
               "take_profit_1": take_profit_1,
               "max_hold_days": max_hold_days}
        self.positions.append(pos)
        return pos

    def sell(self, ticker: str, price: float, sim_date: date, reason: str,
             strategy_name: str = "", regime: str = ""):
        pos = next((p for p in self.positions if p["ticker"] == ticker), None)
        if not pos:
            return None
        proceeds = pos["qty"] * price
        pnl = proceeds - (pos["qty"] * pos["entry_price"])
        pnl_pct = (price / pos["entry_price"] - 1) * 100
        holding_days = (sim_date - pos["entry_date"]).days
        trade = {
            "ticker": ticker, "qty": pos["qty"],
            "entry_price": pos["entry_price"], "exit_price": price,
            "entry_date": pos["entry_date"], "exit_date": sim_date,
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "entry_strategy": pos.get("strategy", ""),
            "exit_strategy": strategy_name,
            "entry_regime": pos.get("regime", ""),
            "exit_regime": regime,
            "entry_confidence": pos.get("confidence", 0),
            "entry_reason": pos.get("entry_reason", ""),
            "holding_days": holding_days,
        }
        self.closed_trades.append(trade)
        self.cash += proceeds
        self.positions.remove(pos)
        return trade

    def snapshot(self, sim_date: date, prices: dict[str, float]) -> float:
        mv = self.get_market_value(prices)
        total = self.cash + mv
        self.daily_snapshots.append({
            "date": sim_date,
            "total_equity": round(total, 2),
            "cash": round(self.cash, 2),
            "market_value": round(mv, 2),
            "num_positions": len(self.positions),
            "positions": [f"{p['ticker']}({p['qty']}@{p['entry_price']:.2f})" for p in self.positions],
        })
        return total


# ===========================================================================
# 1日分のシミュレーション
# ===========================================================================

def simulate_one_day(
    sim_date: date,
    portfolio: SimulatedPortfolio,
    all_data: dict[str, pd.DataFrame],
    sp500_df: pd.DataFrame,
    vix_df: pd.DataFrame,
    all_strategies: list[BaseStrategy],
) -> dict:
    day_report = {"date": sim_date, "executed": [], "rejected": [], "market_condition": {}}

    # 市場環境判定
    market_condition = assess_market_condition_at(sp500_df, vix_df, sim_date)
    day_report["market_condition"] = market_condition

    # 当日の終値マップ
    current_prices = {}
    for ticker, df in all_data.items():
        df_slice = df[df.index <= pd.Timestamp(sim_date)]
        if not df_slice.empty:
            current_prices[ticker] = float(df_slice["Close"].iloc[-1])

    # レジームに合った戦略
    regime = market_condition.get("regime", "range")
    strategies = select_strategies_for_regime(all_strategies, regime)

    buy_signals = []       # (signal, strategy_name)
    sell_signals = []      # (signal, strategy_name)
    rejected_signals = []  # (signal, verdict)

    # --- 強制エグジット（SL/TP/時間ベース） ---
    forced_exit_tickers = set()
    for pos in list(portfolio.positions):
        ticker = pos["ticker"]
        price = current_prices.get(ticker, pos["entry_price"])
        sl = pos.get("stop_loss", 0)
        tp = pos.get("take_profit", 0)
        max_hold = pos.get("max_hold_days", 20)
        holding_days = (sim_date - pos["entry_date"]).days

        # ストップロス発動
        if sl > 0 and price <= sl:
            trade = portfolio.sell(ticker, price, sim_date,
                                   f"ストップロス発動 (SL=${sl:.2f}, 現在=${price:.2f})",
                                   strategy_name=pos.get("strategy", ""),
                                   regime=regime)
            if trade:
                day_report["executed"].append(
                    f"STOP-LOSS {trade['qty']}x {ticker} @ ${price:.2f} "
                    f"(損益: ${trade['pnl']:+.2f} / {trade['pnl_pct']:+.1f}%)")
                forced_exit_tickers.add(ticker)
            continue

        # 段階利確TP1到達（半分決済）
        tp1 = pos.get("take_profit_1", 0)
        if tp1 > 0 and price >= tp1:
            half_qty = max(1, pos["qty"] // 2)
            if half_qty < pos["qty"]:
                partial_pnl = (price - pos["entry_price"]) * half_qty
                portfolio.cash += half_qty * price
                pos["qty"] -= half_qty
                pos["take_profit_1"] = 0  # TP1消費済み
                day_report["executed"].append(
                    f"TAKE-PROFIT-1 {half_qty}x {ticker} @ ${price:.2f} "
                    f"(段階利確 PnL: ${partial_pnl:+.2f})")
            else:
                # qty=1の場合は全決済
                trade = portfolio.sell(ticker, price, sim_date,
                                       f"段階利確TP1到達 (TP1=${tp1:.2f}, 現在=${price:.2f})",
                                       strategy_name=pos.get("strategy", ""),
                                       regime=regime)
                if trade:
                    day_report["executed"].append(
                        f"TAKE-PROFIT-1 {trade['qty']}x {ticker} @ ${price:.2f} "
                        f"(損益: ${trade['pnl']:+.2f} / {trade['pnl_pct']:+.1f}%)")
                    forced_exit_tickers.add(ticker)
            continue

        # 戦略固有エグジットチェック（check_exitメソッド）
        suppress_tp = False
        for strategy in strategies:
            df = all_data.get(ticker)
            if df is not None:
                df_slice = df[df.index <= pd.Timestamp(sim_date)]
                if not df_slice.empty:
                    trade_info = {"take_profit": tp, "stop_loss": sl}
                    exit_decision = getattr(strategy, 'check_exit', lambda *a: None)(ticker, df_slice, trade_info)
                    if exit_decision is not None:
                        if exit_decision.should_exit:
                            trade = portfolio.sell(ticker, price, sim_date,
                                                   exit_decision.reason,
                                                   strategy_name=pos.get("strategy", ""),
                                                   regime=regime)
                            if trade:
                                day_report["executed"].append(
                                    f"STRATEGY-EXIT {trade['qty']}x {ticker} @ ${price:.2f} "
                                    f"(損益: ${trade['pnl']:+.2f} / {trade['pnl_pct']:+.1f}%) "
                                    f"[{exit_decision.reason[:50]}]")
                                forced_exit_tickers.add(ticker)
                            break
                        elif exit_decision.suppress_tp:
                            suppress_tp = True
                        break

        if ticker in forced_exit_tickers:
            continue

        # 利確ターゲット到達（suppress_tp=Trueの場合はスキップ）
        if not suppress_tp and tp > 0 and price >= tp:
            trade = portfolio.sell(ticker, price, sim_date,
                                   f"利確ターゲット到達 (TP=${tp:.2f}, 現在=${price:.2f})",
                                   strategy_name=pos.get("strategy", ""),
                                   regime=regime)
            if trade:
                day_report["executed"].append(
                    f"TAKE-PROFIT {trade['qty']}x {ticker} @ ${price:.2f} "
                    f"(損益: ${trade['pnl']:+.2f} / {trade['pnl_pct']:+.1f}%)")
                forced_exit_tickers.add(ticker)
            continue

        # 最大保有期間超過
        if holding_days >= max_hold:
            trade = portfolio.sell(ticker, price, sim_date,
                                   f"最大保有期間{max_hold}日超過 ({holding_days}日経過)",
                                   strategy_name=pos.get("strategy", ""),
                                   regime=regime)
            if trade:
                day_report["executed"].append(
                    f"TIME-EXIT {trade['qty']}x {ticker} @ ${price:.2f} "
                    f"(損益: ${trade['pnl']:+.2f} / {trade['pnl_pct']:+.1f}%, {holding_days}日)")
                forced_exit_tickers.add(ticker)
            continue

    # --- 保有ポジションのSELLチェック（強制エグジット済みを除く） ---
    for pos in list(portfolio.positions):
        ticker = pos["ticker"]
        if ticker in forced_exit_tickers:
            continue
        df = all_data.get(ticker)
        if df is None:
            continue
        df_slice = df[df.index <= pd.Timestamp(sim_date)]
        if df_slice.empty:
            continue
        for strategy in strategies:
            signal = strategy.generate_signals(ticker, df_slice, market_condition)
            if signal and signal.action == "SELL":
                verdict = evaluate_signal_local(signal, df_slice, market_condition)
                if verdict["approved"]:
                    signal.confidence = verdict["adjusted_confidence"]
                    sell_signals.append((signal, strategy.name))
                else:
                    rejected_signals.append((signal, verdict))
                break

    # --- スクリーニング→BUYチェック ---
    candidates = run_screening_at(all_data, sim_date)
    for candidate in candidates:
        ticker = candidate["ticker"]
        if any(p["ticker"] == ticker for p in portfolio.positions):
            continue
        df = all_data.get(ticker)
        if df is None:
            continue
        df_slice = df[df.index <= pd.Timestamp(sim_date)]
        if df_slice.empty:
            continue
        for strategy in strategies:
            signal = strategy.generate_signals(ticker, df_slice, market_condition)
            if signal and signal.action == "BUY":
                verdict = evaluate_signal_local(signal, df_slice, market_condition)
                if verdict["approved"]:
                    signal.confidence = verdict["adjusted_confidence"]
                    buy_signals.append((signal, strategy.name))
                else:
                    rejected_signals.append((signal, verdict))
                break

    # --- 約定シミュレーション ---

    # 売り先行
    for signal, strat_name in sell_signals:
        price = current_prices.get(signal.ticker, 0)
        if price > 0:
            trade = portfolio.sell(signal.ticker, price, sim_date, signal.reason,
                                  strategy_name=strat_name, regime=regime)
            if trade:
                day_report["executed"].append(
                    f"SELL {trade['qty']}x {signal.ticker} @ ${price:.2f} "
                    f"(損益: ${trade['pnl']:+.2f} / {trade['pnl_pct']:+.1f}%) "
                    f"[{signal.reason[:50]}]"
                )

    # 買い（信頼度順）
    buy_signals.sort(key=lambda s: s[0].confidence, reverse=True)
    for signal, strat_name in buy_signals:
        total_eq = portfolio.get_total_equity(current_prices)
        mv = portfolio.get_market_value(current_prices)
        approval = approve_trade_local(
            signal, total_eq, portfolio.cash, mv, len(portfolio.positions)
        )
        if approval["approved"] and approval["quantity"] > 0:
            price = current_prices.get(signal.ticker, 0)
            if price > 0:
                pos = portfolio.buy(signal.ticker, approval["quantity"], price, sim_date, signal.reason,
                                    strategy_name=strat_name, regime=regime,
                                    confidence=signal.confidence,
                                    entry_reason=signal.reason,
                                    stop_loss=signal.stop_loss,
                                    take_profit=signal.take_profit,
                                    take_profit_1=getattr(signal, 'take_profit_1', 0.0),
                                    max_hold_days=signal.max_hold_days)
                if pos:
                    day_report["executed"].append(
                        f"BUY {approval['quantity']}x {signal.ticker} @ ${price:.2f} "
                        f"(コスト: ${approval['quantity'] * price:,.2f}) "
                        f"[{signal.reason[:50]}]"
                    )

    # 却下シグナル
    for signal, verdict in rejected_signals:
        top_obj = verdict["objections"][0]["reason"] if verdict["objections"] else "N/A"
        day_report["rejected"].append(
            f"{signal.action} {signal.ticker} "
            f"(信頼度 {verdict['original_confidence']:.2f}->{verdict['adjusted_confidence']:.2f}) "
            f"[{top_obj[:60]}]"
        )

    total_equity = portfolio.snapshot(sim_date, current_prices)
    day_report["total_equity"] = total_equity
    day_report["cash"] = portfolio.cash
    day_report["num_positions"] = len(portfolio.positions)
    day_report["candidates_count"] = len(candidates)

    return day_report


# ===========================================================================
# 日付パース
# ===========================================================================

def parse_dates(args: list[str]) -> list[date]:
    dates = []
    for arg in args:
        if ":" in arg:
            start_str, end_str = arg.split(":", 1)
            start = datetime.strptime(start_str, "%Y-%m-%d").date()
            end = datetime.strptime(end_str, "%Y-%m-%d").date()
            current = start
            while current <= end:
                if current.weekday() < 5:
                    dates.append(current)
                current += timedelta(days=1)
        else:
            d = datetime.strptime(arg, "%Y-%m-%d").date()
            dates.append(d)
    return sorted(set(dates))


# ===========================================================================
# レポート出力
# ===========================================================================

def print_report(day_report: dict):
    mc = day_report["market_condition"]
    regime_ja = {"trending": "トレンド", "range": "レンジ", "volatile": "高ボラ"}.get(mc["regime"], mc["regime"])
    trend_ja = {"bull": "強気", "bear": "弱気", "neutral": "中立"}.get(mc["sp500_trend"], mc["sp500_trend"])

    print(f"\n{'='*70}")
    print(f"  {day_report['date']}  |  市場: {regime_ja} (S&P500: {trend_ja}, VIX: {mc['vix_level']:.1f})")
    print(f"  総資産: ${day_report['total_equity']:,.2f}  |  現金: ${day_report['cash']:,.2f}  |  ポジション: {day_report['num_positions']}")
    print(f"  スクリーニング候補: {day_report['candidates_count']}銘柄")
    print(f"{'='*70}")

    if day_report["executed"]:
        print("  [約定]")
        for order in day_report["executed"]:
            print(f"    -> {order}")
    else:
        print("  [約定] なし")

    if day_report["rejected"]:
        print(f"  [却下] {len(day_report['rejected'])}件")
        for rej in day_report["rejected"][:5]:  # 上位5件のみ表示
            print(f"    x {rej}")
        if len(day_report["rejected"]) > 5:
            print(f"    ... 他 {len(day_report['rejected'])-5}件")


def print_summary(portfolio: SimulatedPortfolio):
    print(f"\n{'#'*70}")
    print(f"  シミュレーション結果サマリー")
    print(f"{'#'*70}")

    initial = portfolio.initial_cash
    final = portfolio.daily_snapshots[-1]["total_equity"] if portfolio.daily_snapshots else initial
    total_return = (final / initial - 1) * 100

    print(f"  初期資産:   ${initial:,.2f}")
    print(f"  最終資産:   ${final:,.2f}")
    print(f"  リターン:   {total_return:+.2f}%")
    print(f"  取引回数:   {len(portfolio.closed_trades)}件（決済済み）")

    if portfolio.closed_trades:
        wins = sum(1 for t in portfolio.closed_trades if t["pnl"] > 0)
        print(f"  勝率:       {wins}/{len(portfolio.closed_trades)} ({wins/len(portfolio.closed_trades)*100:.0f}%)")
        total_pnl = sum(t["pnl"] for t in portfolio.closed_trades)
        print(f"  実現損益:   ${total_pnl:+,.2f}")
        print()
        print("  [決済済みトレード一覧]")
        for t in portfolio.closed_trades:
            print(
                f"    {t['entry_date']} -> {t['exit_date']}  "
                f"{t['ticker']}  {t['qty']}株  "
                f"${t['entry_price']:.2f} -> ${t['exit_price']:.2f}  "
                f"損益: ${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%)  "
                f"[{t['reason'][:40]}]"
            )

    if portfolio.positions:
        print()
        print("  [未決済ポジション]")
        for p in portfolio.positions:
            print(f"    {p['ticker']}  {p['qty']}株 @ ${p['entry_price']:.2f}  (エントリー: {p['entry_date']})")

    print()
    print("  [日次資産推移]")
    for snap in portfolio.daily_snapshots:
        pct = (snap["total_equity"] / initial - 1) * 100
        bar_len = int(abs(pct) * 2)
        if pct >= 0:
            bar = "+" * max(bar_len, 0)
            sign = "+"
        else:
            bar = "-" * max(bar_len, 0)
            sign = ""
        pos_str = ", ".join(snap["positions"]) if snap["positions"] else "なし"
        print(
            f"    {snap['date']}  ${snap['total_equity']:>8,.2f} ({sign}{pct:.2f}%)  "
            f"[{pos_str}]  {bar}"
        )
    print()


# ===========================================================================
# HTML レポート生成
# ===========================================================================

REGIME_JA = {"trending": "トレンド", "range": "レンジ", "volatile": "高ボラ"}
TREND_JA = {"bull": "強気", "bear": "弱気", "neutral": "中立"}


def _build_chart_block(chart_data_json: str) -> str:
    """チャートポップアップのHTMLとJSを生成する（f-stringから分離してブレース問題を回避）"""

    modal_html = """
<style>
.chart-btn {
  background: none; border: 1px solid #3a5a7a; border-radius: 5px;
  color: #5a9ac0; font-size: 0.85em; padding: 3px 8px; cursor: pointer;
  transition: all .15s; font-weight: 600;
}
.chart-btn:hover { background: rgba(90,154,192,.15); border-color: #5a9ac0; }
#cm-overlay {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75);
  z-index: 9999; align-items: center; justify-content: center;
}
#cm-overlay.open { display: flex; }
#cm-box {
  background: #1e2228; border: 1px solid #3a4050; border-radius: 12px;
  width: min(900px, 95vw); max-height: 90vh; overflow: hidden;
  box-shadow: 0 20px 60px rgba(0,0,0,.7); display: flex; flex-direction: column;
}
#cm-header {
  padding: 14px 18px 12px; border-bottom: 1px solid #2e3440;
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}
#cm-ticker-link {
  font-size: 1.25rem; font-weight: 700; color: #7ab8e0;
  text-decoration: none; font-family: monospace;
}
#cm-ticker-link:hover { text-decoration: underline; }
#cm-pnl { font-size: 1rem; font-weight: 600; font-family: monospace; }
#cm-meta { font-size: 0.78rem; color: #7a8090; margin-left: auto; }
#cm-close {
  background: none; border: none; color: #7a8090; font-size: 1.3rem;
  cursor: pointer; padding: 0 4px; line-height: 1;
}
#cm-close:hover { color: #c0c8d8; }
#cm-canvas-wrap { padding: 12px 16px 0; }
canvas#cm-canvas { width: 100%; display: block; }
#cm-footer {
  padding: 10px 18px 14px; font-size: 0.78rem; color: #6a7080;
  border-top: 1px solid #2e3440; display: flex; gap: 24px; flex-wrap: wrap;
}
#cm-nodata {
  padding: 60px; text-align: center; color: #5a6070; font-size: 0.9rem;
}
</style>

<div id="cm-overlay" onclick="cmOverlayClick(event)">
  <div id="cm-box">
    <div id="cm-header">
      <a id="cm-ticker-link" href="#" target="_blank" rel="noopener noreferrer">--</a>
      <span id="cm-pnl"></span>
      <span id="cm-meta"></span>
      <button id="cm-close" onclick="closeChart()">✕</button>
    </div>
    <div id="cm-canvas-wrap">
      <div id="cm-nodata" style="display:none;">チャートデータなし（シミュレーション時に価格データが利用できませんでした）</div>
      <canvas id="cm-canvas" height="380"></canvas>
    </div>
    <div id="cm-footer">
      <span id="cm-entry-info"></span>
      <span id="cm-exit-info"></span>
      <span id="cm-hold-info"></span>
    </div>
  </div>
</div>"""

    js_code = """
<script>
const CHART_DATA = """ + chart_data_json + """;

function showChart(idx) {
  const d = CHART_DATA[idx];
  // ヘッダー
  const yf = 'https://finance.yahoo.com/chart/' + d.ticker;
  document.getElementById('cm-ticker-link').textContent = d.ticker;
  document.getElementById('cm-ticker-link').href = yf;
  const pnlEl = document.getElementById('cm-pnl');
  const sign = d.pnl >= 0 ? '+' : '';
  pnlEl.textContent = sign + '$' + d.pnl.toFixed(2) + ' (' + sign + d.pnl_pct.toFixed(1) + '%)';
  pnlEl.style.color = d.pnl >= 0 ? '#5ab87a' : '#c05858';
  document.getElementById('cm-meta').textContent = d.entry_date + ' → ' + d.exit_date + '  ' + d.qty + '株';
  // フッター
  document.getElementById('cm-entry-info').textContent  = '▲ エントリー: ' + d.entry_date + '  $' + d.entry_price.toFixed(2);
  document.getElementById('cm-exit-info').textContent   = '▼ 決済: ' + d.exit_date + '  $' + d.exit_price.toFixed(2);
  const hold = Math.round((new Date(d.exit_date) - new Date(d.entry_date)) / 86400000);
  document.getElementById('cm-hold-info').textContent   = '保有: ' + hold + '日';
  // チャート描画
  const nodata = document.getElementById('cm-nodata');
  const canvas = document.getElementById('cm-canvas');
  if (!d.ohlcv || d.ohlcv.length === 0) {
    nodata.style.display = 'block'; canvas.style.display = 'none';
  } else {
    nodata.style.display = 'none'; canvas.style.display = 'block';
    requestAnimationFrame(() => drawChart(canvas, d));
  }
  document.getElementById('cm-overlay').classList.add('open');
}

function closeChart() { document.getElementById('cm-overlay').classList.remove('open'); }
function cmOverlayClick(e) { if (e.target.id === 'cm-overlay') closeChart(); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeChart(); });

function drawChart(canvas, data) {
  const candles = data.ohlcv;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.parentElement.clientWidth - 32;
  const H = 380;
  canvas.style.width  = W + 'px';
  canvas.style.height = H + 'px';
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const PAD = { top: 36, right: 16, bottom: 64, left: 72 };
  const chartW = W - PAD.left - PAD.right;
  const chartH = H - PAD.top - PAD.bottom;
  const n = candles.length;

  // 価格レンジ
  const allPrices = candles.flatMap(c => [c.h, c.l]);
  const pMin = Math.min(...allPrices);
  const pMax = Math.max(...allPrices);
  const pRange = pMax - pMin || pMin * 0.01;
  const yMin = pMin - pRange * 0.08;
  const yMax = pMax + pRange * 0.08;

  const xPos = i => PAD.left + (i + 0.5) * (chartW / n);
  const yPos = p => PAD.top + chartH - ((p - yMin) / (yMax - yMin)) * chartH;
  const candleW = Math.max(2, Math.floor(chartW / n * 0.7));

  // 背景
  ctx.fillStyle = '#181b20';
  ctx.fillRect(0, 0, W, H);

  // グリッド
  const nY = 5;
  for (let j = 0; j <= nY; j++) {
    const price = yMin + (yMax - yMin) * j / nY;
    const y = Math.round(yPos(price)) + 0.5;
    ctx.strokeStyle = '#2a303a'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(W - PAD.right, y); ctx.stroke();
    ctx.fillStyle = '#5a6070'; ctx.font = '11px monospace'; ctx.textAlign = 'right';
    ctx.fillText('$' + price.toFixed(2), PAD.left - 6, y + 4);
  }

  // ローソク足描画
  candles.forEach((c, i) => {
    const x = Math.round(xPos(i));
    const isUp = c.c >= c.o;
    const col = isUp ? '#3a8a5a' : '#a04040';
    const colBright = isUp ? '#5ab87a' : '#e05858';
    // ひげ
    ctx.strokeStyle = colBright; ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x + 0.5, yPos(c.h));
    ctx.lineTo(x + 0.5, yPos(c.l));
    ctx.stroke();
    // 実体
    const y1 = yPos(Math.max(c.o, c.c));
    const y2 = yPos(Math.min(c.o, c.c));
    const bh = Math.max(1, y2 - y1);
    ctx.fillStyle = col;
    ctx.fillRect(x - candleW / 2, y1, candleW, bh);
    ctx.strokeStyle = colBright; ctx.lineWidth = 0.5;
    ctx.strokeRect(x - candleW / 2, y1, candleW, bh);
  });

  // エントリー線
  const entryIdx = candles.findIndex(c => c.t === data.entry_date);
  const exitIdx  = candles.findIndex(c => c.t === data.exit_date);

  function drawVLine(idx, color, label, price, priceLabel) {
    if (idx < 0) return;
    const x = Math.round(xPos(idx)) + 0.5;
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = color; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x, PAD.top); ctx.lineTo(x, H - PAD.bottom); ctx.stroke();
    ctx.restore();
    // 横線（エントリー/決済価格）
    const yp = yPos(price);
    ctx.save();
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = color + 'aa'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD.left, yp); ctx.lineTo(W - PAD.right, yp); ctx.stroke();
    ctx.restore();
    // 底部ラベル
    ctx.fillStyle = color; ctx.textAlign = 'center';
    ctx.font = 'bold 10px sans-serif';
    ctx.fillText(label, x, H - PAD.bottom + 16);
    ctx.font = '10px monospace';
    ctx.fillText('$' + priceLabel, x, H - PAD.bottom + 29);
  }

  drawVLine(entryIdx, '#4aaa70', '▲ IN',  data.entry_price, data.entry_price.toFixed(2));
  drawVLine(exitIdx,  '#d06060', '▼ OUT', data.exit_price,  data.exit_price.toFixed(2));

  // 日付軸
  const step = Math.max(1, Math.floor(n / 7));
  ctx.fillStyle = '#5a6070'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
  candles.forEach((c, i) => {
    if (i % step === 0 || i === n - 1) {
      ctx.fillText(c.t.slice(5), Math.round(xPos(i)), H - PAD.bottom + 44);
    }
  });

  // タイトル（左上）
  ctx.font = 'bold 13px monospace'; ctx.textAlign = 'left';
  ctx.fillStyle = '#b0b8c8';
  ctx.fillText(data.ticker, PAD.left, 22);
  const tw = ctx.measureText(data.ticker + '  ').width;
  const sign = data.pnl >= 0 ? '+' : '';
  ctx.fillStyle = data.pnl >= 0 ? '#5ab87a' : '#e05858';
  ctx.font = 'bold 13px monospace';
  ctx.fillText(sign + '$' + data.pnl.toFixed(2) + ' (' + sign + data.pnl_pct.toFixed(1) + '%)', PAD.left + tw, 22);
}
</script>"""

    return modal_html + js_code


def generate_html_report(
    portfolio: SimulatedPortfolio,
    day_reports: list[dict],
    sim_dates: list[date],
    all_data: dict | None = None,
) -> str:
    """シミュレーション結果をHTML形式で生成する"""
    initial = portfolio.initial_cash
    final = portfolio.daily_snapshots[-1]["total_equity"] if portfolio.daily_snapshots else initial
    total_return = (final / initial - 1) * 100
    date_range = f"{sim_dates[0]} ~ {sim_dates[-1]}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 勝率計算
    num_trades = len(portfolio.closed_trades)
    wins = sum(1 for t in portfolio.closed_trades if t["pnl"] > 0)
    win_rate = (wins / num_trades * 100) if num_trades else 0
    total_pnl = sum(t["pnl"] for t in portfolio.closed_trades)

    # リターンのクラス
    return_class = "positive" if total_return >= 0 else "negative"

    # --- 日次テーブル行 ---
    daily_rows = ""
    for snap in portfolio.daily_snapshots:
        pct = (snap["total_equity"] / initial - 1) * 100
        pct_class = "positive" if pct >= 0 else "negative"
        pos_str = ", ".join(snap["positions"]) if snap["positions"] else "-"
        daily_rows += f"""
        <tr>
          <td>{snap['date']}</td>
          <td class="num">${snap['total_equity']:,.2f}</td>
          <td class="num">${snap['cash']:,.2f}</td>
          <td class="num">${snap['market_value']:,.2f}</td>
          <td class="num {pct_class}">{pct:+.2f}%</td>
          <td>{snap['num_positions']}</td>
          <td class="positions-cell">{pos_str}</td>
        </tr>"""

    # --- 約定トレード行 ---
    trade_rows = ""
    for report in day_reports:
        for order in report["executed"]:
            mc = report["market_condition"]
            regime = REGIME_JA.get(mc["regime"], mc["regime"])
            trade_rows += f"""
        <tr>
          <td>{report['date']}</td>
          <td>{order}</td>
          <td>{regime}</td>
        </tr>"""

    # --- 決済済みトレード行 ---
    # 戦略の説明マップ
    strategy_descriptions = {
        "sma_crossover": "SMAクロスオーバー (v2.0) — トレンドフォロー戦略。SMA20/50のクロスで売買。ADXフィルター・ベア相場フィルター付き。",
        "rsi_reversal": "RSI逆張り (v2.0) — 平均回帰戦略。RSIの30/70クロスオーバーで売買。Wilder EMA使用。",
        "breakout": "ブレイクアウト (v2.0) — 20日高値/安値突破+出来高1.5倍で売買。ベア相場フィルター付き。",
    }
    regime_descriptions = {
        "trending": "トレンド相場と判定されたため",
        "range": "レンジ相場と判定されたため",
        "volatile": "高ボラティリティ相場と判定されたため",
    }

    closed_rows = ""
    chart_dataset = []
    for i, t in enumerate(portfolio.closed_trades):
        pnl_class = "positive" if t["pnl"] >= 0 else "negative"
        entry_d = t["entry_date"]
        exit_d  = t["exit_date"]
        ticker  = t["ticker"]
        # ---- チャートデータ構築 ----
        ohlcv_list = []
        if all_data and ticker in all_data:
            df = all_data[ticker]
            start_ts = pd.Timestamp(entry_d - timedelta(days=20))
            end_ts   = pd.Timestamp(exit_d   + timedelta(days=20))
            df_slice = df[(df.index >= start_ts) & (df.index <= end_ts)]
            for dt, row in df_slice.iterrows():
                try:
                    ohlcv_list.append({
                        "t": dt.strftime("%Y-%m-%d"),
                        "o": round(float(row["Open"]),   2),
                        "h": round(float(row["High"]),   2),
                        "l": round(float(row["Low"]),    2),
                        "c": round(float(row["Close"]),  2),
                        "v": int(row["Volume"]),
                    })
                except Exception:
                    pass
        chart_dataset.append({
            "ticker":      ticker,
            "entry_date":  str(entry_d),
            "exit_date":   str(exit_d),
            "entry_price": round(t["entry_price"], 2),
            "exit_price":  round(t["exit_price"],  2),
            "pnl":         round(t["pnl"],         2),
            "pnl_pct":     round(t["pnl_pct"],     2),
            "qty":         t["qty"],
            "reason":      t.get("reason", ""),
            "ohlcv":       ohlcv_list,
        })
        # ---- テーブル行 ----
        entry_strat = t.get("entry_strategy", "")
        strat_desc = strategy_descriptions.get(entry_strat, entry_strat)
        entry_regime = t.get("entry_regime", "")
        regime_reason = regime_descriptions.get(entry_regime, entry_regime)
        strat_reason = f"{strat_desc}<br><small>採用理由: {regime_reason}、当戦略が選択された</small>"
        entry_reason = t.get("entry_reason", "")
        closed_rows += f"""
        <tr>
          <td>{t['entry_date']}</td>
          <td>{t['exit_date']}</td>
          <td><button class="chart-btn" onclick="showChart({i})">📈 {ticker}</button></td>
          <td class="num">{t['qty']}</td>
          <td class="num">${t['entry_price']:.2f}</td>
          <td class="num">${t['exit_price']:.2f}</td>
          <td class="num {pnl_class}">${t['pnl']:+.2f}</td>
          <td class="num {pnl_class}">{t['pnl_pct']:+.1f}%</td>
          <td class="strategy-cell">{strat_reason}</td>
          <td class="reason-cell">{entry_reason}</td>
          <td class="reason-cell">{t['reason']}</td>
        </tr>"""

    # --- 未決済ポジション行 ---
    open_rows = ""
    for p in portfolio.positions:
        open_rows += f"""
        <tr>
          <td><strong>{p['ticker']}</strong></td>
          <td class="num">{p['qty']}</td>
          <td class="num">${p['entry_price']:.2f}</td>
          <td>{p['entry_date']}</td>
        </tr>"""

    # --- 却下シグナル行 ---
    rejected_rows = ""
    for report in day_reports:
        for rej in report["rejected"]:
            rejected_rows += f"""
        <tr>
          <td>{report['date']}</td>
          <td>{rej}</td>
        </tr>"""

    # --- 日次詳細カード ---
    day_cards = ""
    for report in day_reports:
        mc = report["market_condition"]
        regime = REGIME_JA.get(mc["regime"], mc["regime"])
        trend = TREND_JA.get(mc["sp500_trend"], mc["sp500_trend"])
        pct = (report["total_equity"] / initial - 1) * 100
        pct_class = "positive" if pct >= 0 else "negative"

        executed_html = ""
        if report["executed"]:
            for o in report["executed"]:
                is_buy = o.startswith("BUY")
                badge_class = "badge-buy" if is_buy else "badge-sell"
                badge_text = "BUY" if is_buy else "SELL"
                executed_html += f'<div class="order-item"><span class="badge {badge_class}">{badge_text}</span> {o[o.index(" ")+1:]}</div>'
        else:
            executed_html = '<div class="no-action">シグナルなし</div>'

        rejected_html = ""
        if report["rejected"]:
            for r in report["rejected"][:3]:
                rejected_html += f'<div class="rejected-item">{r}</div>'
            if len(report["rejected"]) > 3:
                rejected_html += f'<div class="rejected-more">他 {len(report["rejected"])-3}件</div>'

        day_cards += f"""
    <div class="day-card">
      <div class="day-card-header">
        <div class="day-date">{report['date']}</div>
        <div class="day-tags">
          <span class="tag tag-regime">{regime}</span>
          <span class="tag tag-trend">{trend}</span>
          <span class="tag tag-vix">VIX {mc['vix_level']:.1f}</span>
        </div>
      </div>
      <div class="day-card-body">
        <div class="day-metrics">
          <div class="metric">
            <div class="metric-label">総資産</div>
            <div class="metric-value">${report['total_equity']:,.2f}</div>
          </div>
          <div class="metric">
            <div class="metric-label">騰落率</div>
            <div class="metric-value {pct_class}">{pct:+.2f}%</div>
          </div>
          <div class="metric">
            <div class="metric-label">ポジション</div>
            <div class="metric-value">{report['num_positions']}</div>
          </div>
          <div class="metric">
            <div class="metric-label">候補</div>
            <div class="metric-value">{report['candidates_count']}銘柄</div>
          </div>
        </div>
        <div class="day-orders">{executed_html}</div>
        {"<div class='day-rejected'>" + rejected_html + "</div>" if rejected_html else ""}
      </div>
    </div>"""

    # --- チャートデータ（SVG） ---
    snapshots = portfolio.daily_snapshots
    if len(snapshots) >= 2:
        values = [s["total_equity"] for s in snapshots]
        min_val = min(values) * 0.995
        max_val = max(values) * 1.005
        val_range = max_val - min_val if max_val > min_val else 1
        chart_w = 800
        chart_h = 250
        padding = 40

        points = []
        for i, v in enumerate(values):
            x = padding + (i / (len(values) - 1)) * (chart_w - 2 * padding)
            y = chart_h - padding - ((v - min_val) / val_range) * (chart_h - 2 * padding)
            points.append(f"{x:.1f},{y:.1f}")

        polyline = " ".join(points)
        fill_points = f"{points[0].split(',')[0]},{chart_h - padding} " + polyline + f" {points[-1].split(',')[0]},{chart_h - padding}"

        # Y軸ラベル
        y_labels = ""
        for i in range(5):
            val = min_val + (val_range * i / 4)
            y = chart_h - padding - (i / 4) * (chart_h - 2 * padding)
            y_labels += f'<text x="{padding - 5}" y="{y + 4}" text-anchor="end" class="chart-label">${val:,.0f}</text>'
            y_labels += f'<line x1="{padding}" y1="{y}" x2="{chart_w - padding}" y2="{y}" class="chart-grid"/>'

        # X軸ラベル（間引き）
        x_labels = ""
        step = max(1, len(snapshots) // 6)
        for i in range(0, len(snapshots), step):
            x = padding + (i / (len(values) - 1)) * (chart_w - 2 * padding)
            x_labels += f'<text x="{x}" y="{chart_h - padding + 20}" text-anchor="middle" class="chart-label">{snapshots[i]["date"].strftime("%m/%d")}</text>'

        # 基準線
        baseline_y = chart_h - padding - ((initial - min_val) / val_range) * (chart_h - 2 * padding)

        chart_svg = f"""
    <svg viewBox="0 0 {chart_w} {chart_h + 10}" class="equity-chart">
      {y_labels}
      {x_labels}
      <line x1="{padding}" y1="{baseline_y}" x2="{chart_w - padding}" y2="{baseline_y}" class="chart-baseline"/>
      <polygon points="{fill_points}" class="chart-fill"/>
      <polyline points="{polyline}" class="chart-line"/>
    </svg>"""
    else:
        chart_svg = '<div class="no-action">データが不足しています</div>'

    # --- HTML組み立て ---
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AutoTRD シミュレーション結果 — {date_range}</title>
<style>
  :root {{
    --bg-main: #f5f0eb;
    --bg-card: #ffffff;
    --bg-section: #ede8e3;
    --bg-code: #e8e3de;
    --text-primary: #4a4541;
    --text-secondary: #7a726b;
    --text-heading: #5c554e;
    --accent-sage: #8fa89a;
    --accent-sage-light: #c5d5cb;
    --accent-sage-bg: #eef3f0;
    --accent-dusty-rose: #c4a0a0;
    --accent-dusty-rose-light: #e0cece;
    --accent-dusty-rose-bg: #f5ecec;
    --accent-slate: #8e9aaa;
    --accent-slate-light: #c4ccda;
    --accent-slate-bg: #edf0f4;
    --accent-sand: #c4b49a;
    --accent-sand-light: #ddd3c3;
    --accent-sand-bg: #f5f0e8;
    --accent-lavender: #a89ab8;
    --accent-lavender-light: #d0c7dc;
    --accent-lavender-bg: #f0ecf4;
    --accent-terracotta: #c4a088;
    --accent-terracotta-light: #dcc8b8;
    --accent-terracotta-bg: #f5ede6;
    --border-color: #ddd5cd;
    --shadow: 0 2px 12px rgba(74, 69, 65, 0.06);
    --radius: 12px;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: "Helvetica Neue", "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
    background: var(--bg-main);
    color: var(--text-primary);
    line-height: 1.8;
    font-size: 15px;
  }}

  .header {{
    background: linear-gradient(135deg, #8e9aaa 0%, #8fa89a 50%, #a89ab8 100%);
    color: white;
    padding: 48px 40px;
    text-align: center;
  }}
  .header h1 {{ font-size: 2.2em; font-weight: 700; letter-spacing: 0.04em; margin-bottom: 8px; text-shadow: 0 1px 4px rgba(0,0,0,0.1); }}
  .header .subtitle {{ font-size: 1.05em; opacity: 0.92; }}
  .header .meta {{ margin-top: 12px; font-size: 0.85em; opacity: 0.7; }}

  /* Sidebar Layout */
  .layout {{ display: flex; min-height: calc(100vh - 140px); }}
  .sidebar {{
    width: 230px; flex-shrink: 0; background: var(--bg-card);
    border-right: 1px solid var(--border-color);
    position: sticky; top: 0; height: 100vh; overflow-y: auto;
    padding: 24px 0; z-index: 100;
  }}
  .sidebar-title {{ font-size: 0.72em; font-weight: 700; color: var(--text-secondary);
    text-transform: uppercase; letter-spacing: 0.08em; padding: 12px 20px 8px; }}
  .sidebar a {{
    display: block; color: var(--text-secondary); text-decoration: none;
    padding: 9px 20px; font-size: 0.88em; transition: all 0.2s;
    border-left: 3px solid transparent;
  }}
  .sidebar a:hover {{ background: var(--accent-sage-bg); color: var(--text-primary); border-left-color: var(--accent-sage); }}
  .sidebar a.active {{ background: var(--accent-sage-bg); color: var(--accent-sage); border-left-color: var(--accent-sage); font-weight: 600; }}

  .container {{ flex: 1; max-width: 1100px; padding: 40px 24px 80px; }}

  .section {{
    background: var(--bg-card);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 32px;
    border: 1px solid var(--border-color);
  }}
  .section-header {{
    padding: 24px 32px 20px;
    border-bottom: 1px solid var(--border-color);
    display: flex; align-items: center; gap: 14px;
    position: sticky; top: 0; z-index: 20;
    background: var(--bg-card);
    border-radius: var(--radius) var(--radius) 0 0;
  }}
  .section-header .icon {{
    width: 44px; height: 44px; border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.3em; flex-shrink: 0;
  }}
  .section-header h2 {{ font-size: 1.25em; color: var(--text-heading); font-weight: 600; }}
  .section-body {{ padding: 24px 32px; }}

  /* Summary Cards */
  .summary-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px;
    margin-bottom: 8px;
  }}
  .summary-card {{
    background: var(--bg-section);
    border-radius: 10px;
    padding: 20px;
    text-align: center;
  }}
  .summary-card .label {{ font-size: 0.8em; color: var(--text-secondary); margin-bottom: 4px; }}
  .summary-card .value {{ font-size: 1.6em; font-weight: 700; color: var(--text-heading); }}
  .summary-card .value.positive {{ color: #5a8a6a; }}
  .summary-card .value.negative {{ color: #b05050; }}

  /* Tables */
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9em; }}
  .table-scroll {{ max-height: 70vh; overflow-y: auto; }}
  th {{ background: var(--bg-section); color: var(--text-secondary); font-weight: 600; text-align: left;
       padding: 10px 14px; font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.05em;
       position: sticky; top: 0; z-index: 10; }}
  td {{ padding: 10px 14px; border-bottom: 1px solid #eee; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #faf8f6; }}
  .num {{ font-variant-numeric: tabular-nums; text-align: right; font-family: "SF Mono", "Consolas", monospace; }}
  .positive {{ color: #5a8a6a; }}
  .negative {{ color: #b05050; }}
  .positions-cell {{ font-size: 0.82em; color: var(--text-secondary); max-width: 300px; word-break: break-all; }}
  .reason-cell {{ font-size: 0.82em; color: var(--text-secondary); max-width: 280px; }}
  .strategy-cell {{ font-size: 0.82em; color: var(--text-secondary); max-width: 320px; line-height: 1.5; }}
  .ticker-link {{ color: var(--accent-sage); text-decoration: none; font-weight: 700; border-bottom: 1px dashed var(--accent-sage); }}
  .ticker-link:hover {{ color: #5a8a6a; border-bottom-style: solid; }}

  /* Day Cards */
  .day-card {{
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    margin-bottom: 16px;
    overflow: hidden;
  }}
  .day-card-header {{
    padding: 14px 20px;
    background: var(--bg-section);
    display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;
  }}
  .day-date {{ font-weight: 700; font-size: 1.05em; color: var(--text-heading); }}
  .day-tags {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .tag {{
    padding: 3px 10px; border-radius: 12px; font-size: 0.75em; font-weight: 600;
  }}
  .tag-regime {{ background: var(--accent-sage-bg); color: var(--accent-sage); }}
  .tag-trend {{ background: var(--accent-slate-bg); color: var(--accent-slate); }}
  .tag-vix {{ background: var(--accent-sand-bg); color: var(--accent-sand); }}

  .day-card-body {{ padding: 16px 20px; }}
  .day-metrics {{ display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px; }}
  .metric .metric-label {{ font-size: 0.75em; color: var(--text-secondary); }}
  .metric .metric-value {{ font-size: 1.1em; font-weight: 600; font-variant-numeric: tabular-nums; }}

  .day-orders {{ margin-top: 8px; }}
  .order-item {{ padding: 6px 0; font-size: 0.88em; }}
  .no-action {{ color: var(--text-secondary); font-size: 0.88em; font-style: italic; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 6px;
    font-size: 0.75em; font-weight: 700; letter-spacing: 0.05em; margin-right: 6px;
  }}
  .badge-buy {{ background: var(--accent-sage-bg); color: #5a8a6a; }}
  .badge-sell {{ background: var(--accent-dusty-rose-bg); color: #b05050; }}

  .day-rejected {{ margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border-color); }}
  .rejected-item {{ font-size: 0.82em; color: var(--text-secondary); padding: 3px 0; }}
  .rejected-more {{ font-size: 0.8em; color: var(--text-secondary); font-style: italic; }}

  /* Chart */
  .equity-chart {{ width: 100%; height: auto; }}
  .chart-line {{ fill: none; stroke: var(--accent-sage); stroke-width: 2.5; stroke-linejoin: round; }}
  .chart-fill {{ fill: var(--accent-sage-bg); opacity: 0.5; }}
  .chart-grid {{ stroke: #e8e3de; stroke-width: 0.5; }}
  .chart-baseline {{ stroke: var(--accent-sand); stroke-width: 1; stroke-dasharray: 6 3; }}
  .chart-label {{ font-size: 11px; fill: var(--text-secondary); font-family: "SF Mono", "Consolas", monospace; }}

  .footer {{
    text-align: center;
    padding: 32px;
    color: var(--text-secondary);
    font-size: 0.82em;
  }}
</style>
</head>
<body>

<div class="header">
  <h1>AutoTRD Simulation Report</h1>
  <div class="subtitle">{date_range}</div>
  <div class="meta">Generated: {now_str} | Initial Capital: ${initial:,.2f}</div>
</div>

<div class="layout">

<nav class="sidebar">
  <div class="sidebar-title">Contents</div>
  <a href="#summary">サマリー</a>
  <a href="#chart">資産推移チャート</a>
  <a href="#daily">日次詳細</a>
  <a href="#trades">決済トレード</a>
  <a href="#positions">ポジション</a>
  <a href="#timeline">日次テーブル</a>
  <a href="#rejected">却下シグナル</a>
</nav>

<div class="container">

  <!-- Summary -->
  <div class="section" id="summary">
    <div class="section-header">
      <div class="icon" style="background:var(--accent-sage-bg);">&#x1f4ca;</div>
      <div><h2>シミュレーション結果サマリー</h2></div>
    </div>
    <div class="section-body">
      <div class="summary-grid">
        <div class="summary-card">
          <div class="label">初期資産</div>
          <div class="value">${initial:,.2f}</div>
        </div>
        <div class="summary-card">
          <div class="label">最終資産</div>
          <div class="value {return_class}">${final:,.2f}</div>
        </div>
        <div class="summary-card">
          <div class="label">リターン</div>
          <div class="value {return_class}">{total_return:+.2f}%</div>
        </div>
        <div class="summary-card">
          <div class="label">決済トレード</div>
          <div class="value">{num_trades}件</div>
        </div>
        <div class="summary-card">
          <div class="label">勝率</div>
          <div class="value">{win_rate:.0f}%</div>
        </div>
        <div class="summary-card">
          <div class="label">実現損益</div>
          <div class="value {"positive" if total_pnl >= 0 else "negative"}">${total_pnl:+,.2f}</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Equity Chart -->
  <div class="section" id="chart">
    <div class="section-header">
      <div class="icon" style="background:var(--accent-slate-bg);">&#x1f4c8;</div>
      <div><h2>資産推移チャート</h2></div>
    </div>
    <div class="section-body">
      {chart_svg}
    </div>
  </div>

  <!-- Daily Detail Cards -->
  <div class="section" id="daily">
    <div class="section-header">
      <div class="icon" style="background:var(--accent-lavender-bg);">&#x1f4c5;</div>
      <div><h2>日次詳細</h2></div>
    </div>
    <div class="section-body">
      {day_cards}
    </div>
  </div>

  <!-- Closed Trades -->
  <div class="section" id="trades">
    <div class="section-header">
      <div class="icon" style="background:var(--accent-sage-bg);">&#x1f4b0;</div>
      <div><h2>決済済みトレード</h2></div>
    </div>
    <div class="section-body">
      {"<div class='table-scroll'><table><tr><th>エントリー</th><th>決済</th><th>銘柄</th><th>数量</th><th>買値</th><th>売値</th><th>損益</th><th>損益%</th><th>採用戦略・理由</th><th>エントリー根拠</th><th>決済理由</th></tr>" + closed_rows + "</table></div>" if closed_rows else '<div class="no-action">決済済みトレードはありません</div>'}
    </div>
  </div>

  <!-- Open Positions -->
  <div class="section" id="positions">
    <div class="section-header">
      <div class="icon" style="background:var(--accent-sand-bg);">&#x1f4bc;</div>
      <div><h2>未決済ポジション</h2></div>
    </div>
    <div class="section-body">
      {"<table><tr><th>銘柄</th><th>数量</th><th>取得価格</th><th>エントリー日</th></tr>" + open_rows + "</table>" if open_rows else '<div class="no-action">未決済ポジションはありません</div>'}
    </div>
  </div>

  <!-- Daily Table -->
  <div class="section" id="timeline">
    <div class="section-header">
      <div class="icon" style="background:var(--accent-terracotta-bg);">&#x1f4c4;</div>
      <div><h2>日次資産推移テーブル</h2></div>
    </div>
    <div class="section-body">
      <div class="table-scroll">
      <table>
        <tr><th>日付</th><th>総資産</th><th>現金</th><th>時価</th><th>騰落率</th><th>Pos</th><th>ポジション</th></tr>
        {daily_rows}
      </table>
      </div>
    </div>
  </div>

  <!-- Rejected Signals -->
  <div class="section" id="rejected">
    <div class="section-header">
      <div class="icon" style="background:var(--accent-dusty-rose-bg);">&#x1f6ab;</div>
      <div><h2>却下されたシグナル</h2></div>
    </div>
    <div class="section-body">
      {"<div class='table-scroll'><table><tr><th>日付</th><th>詳細</th></tr>" + rejected_rows + "</table></div>" if rejected_rows else '<div class="no-action">却下されたシグナルはありません</div>'}
    </div>
  </div>

</div>
</div>

<div class="footer">
  AutoTRD Simulation Report &mdash; Generated by simulate.py
</div>

<script>
// Scroll spy: highlight active sidebar link
(function() {{
  const links = document.querySelectorAll('.sidebar a[href^="#"]');
  const sections = Array.from(links).map(a => document.getElementById(a.getAttribute('href').slice(1))).filter(Boolean);
  function onScroll() {{
    let current = '';
    for (const sec of sections) {{
      if (sec.getBoundingClientRect().top <= 120) current = sec.id;
    }}
    links.forEach(a => {{
      a.classList.toggle('active', a.getAttribute('href') === '#' + current);
    }});
  }}
  window.addEventListener('scroll', onScroll, {{ passive: true }});
  onScroll();
}})();
</script>

</body>
</html>"""
    # チャートポップアップを注入
    chart_block = _build_chart_block(json.dumps(chart_dataset, ensure_ascii=False))
    html = html.replace("</body>\n</html>", chart_block + "\n</body>\n</html>")
    return html


# ===========================================================================
# 分析 HTML レポート生成
# ===========================================================================

def generate_analysis_html(
    portfolio: SimulatedPortfolio,
    day_reports: list[dict],
    sim_dates: list[date],
) -> str:
    """シミュレーション結果を多角的に分析したHTMLレポートを生成"""
    initial = portfolio.initial_cash
    final = portfolio.daily_snapshots[-1]["total_equity"] if portfolio.daily_snapshots else initial
    total_return = (final / initial - 1) * 100
    date_range = f"{sim_dates[0]} ~ {sim_dates[-1]}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    trades = portfolio.closed_trades

    # ===== 1. 戦略別パフォーマンス =====
    strat_stats = {}
    for t in trades:
        s = t.get("entry_strategy", "unknown") or "unknown"
        if s not in strat_stats:
            strat_stats[s] = {"trades": 0, "wins": 0, "total_pnl": 0.0, "pnls": [], "holding": []}
        strat_stats[s]["trades"] += 1
        strat_stats[s]["total_pnl"] += t["pnl"]
        strat_stats[s]["pnls"].append(t["pnl"])
        strat_stats[s]["holding"].append(t.get("holding_days", 0))
        if t["pnl"] > 0:
            strat_stats[s]["wins"] += 1

    strat_rows = ""
    for name, st in sorted(strat_stats.items(), key=lambda x: x[1]["total_pnl"]):
        wr = (st["wins"] / st["trades"] * 100) if st["trades"] else 0
        avg_pnl = st["total_pnl"] / st["trades"] if st["trades"] else 0
        avg_hold = sum(st["holding"]) / len(st["holding"]) if st["holding"] else 0
        max_loss = min(st["pnls"]) if st["pnls"] else 0
        max_win = max(st["pnls"]) if st["pnls"] else 0
        pnl_class = "positive" if st["total_pnl"] >= 0 else "negative"
        strat_rows += f"""<tr>
          <td><strong>{name}</strong></td>
          <td class="num">{st['trades']}</td>
          <td class="num">{wr:.0f}%</td>
          <td class="num {pnl_class}">${st['total_pnl']:+,.2f}</td>
          <td class="num">${avg_pnl:+,.2f}</td>
          <td class="num negative">${max_loss:+,.2f}</td>
          <td class="num positive">${max_win:+,.2f}</td>
          <td class="num">{avg_hold:.0f}日</td>
        </tr>"""

    # ===== 2. レジーム別パフォーマンス =====
    regime_stats = {}
    for t in trades:
        r = t.get("entry_regime", "unknown") or "unknown"
        if r not in regime_stats:
            regime_stats[r] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
        regime_stats[r]["trades"] += 1
        regime_stats[r]["total_pnl"] += t["pnl"]
        if t["pnl"] > 0:
            regime_stats[r]["wins"] += 1

    regime_ja = {"trending": "トレンド", "range": "レンジ", "volatile": "高ボラ", "unknown": "不明"}
    regime_rows = ""
    for r, st in sorted(regime_stats.items(), key=lambda x: x[1]["total_pnl"]):
        wr = (st["wins"] / st["trades"] * 100) if st["trades"] else 0
        pnl_class = "positive" if st["total_pnl"] >= 0 else "negative"
        regime_rows += f"""<tr>
          <td><strong>{regime_ja.get(r, r)}</strong></td>
          <td class="num">{st['trades']}</td>
          <td class="num">{wr:.0f}%</td>
          <td class="num {pnl_class}">${st['total_pnl']:+,.2f}</td>
        </tr>"""

    # レジーム日数分布
    regime_day_count = {}
    for report in day_reports:
        r = report["market_condition"].get("regime", "unknown")
        regime_day_count[r] = regime_day_count.get(r, 0) + 1
    regime_dist_rows = ""
    for r, count in sorted(regime_day_count.items(), key=lambda x: -x[1]):
        pct = count / len(day_reports) * 100
        regime_dist_rows += f"""<tr>
          <td>{regime_ja.get(r, r)}</td><td class="num">{count}日</td><td class="num">{pct:.0f}%</td>
        </tr>"""

    # ===== 3. リスク/リワード分析 =====
    wins_pnl = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses_pnl = [t["pnl"] for t in trades if t["pnl"] <= 0]
    avg_win = sum(wins_pnl) / len(wins_pnl) if wins_pnl else 0
    avg_loss = sum(losses_pnl) / len(losses_pnl) if losses_pnl else 0
    profit_factor = abs(sum(wins_pnl) / sum(losses_pnl)) if losses_pnl and sum(losses_pnl) != 0 else 0
    expectancy = sum(t["pnl"] for t in trades) / len(trades) if trades else 0

    # ===== 4. 保有期間分析 =====
    holding_analysis = []
    for t in trades:
        hd = t.get("holding_days", 0)
        holding_analysis.append({
            "ticker": t["ticker"],
            "holding_days": hd,
            "pnl": t["pnl"],
            "pnl_pct": t["pnl_pct"],
            "strategy": t.get("entry_strategy", ""),
        })
    holding_analysis.sort(key=lambda x: x["holding_days"], reverse=True)

    holding_rows = ""
    for h in holding_analysis:
        pnl_class = "positive" if h["pnl"] >= 0 else "negative"
        holding_rows += f"""<tr>
          <td>{h['ticker']}</td><td class="num">{h['holding_days']}日</td>
          <td class="num {pnl_class}">${h['pnl']:+,.2f}</td>
          <td class="num {pnl_class}">{h['pnl_pct']:+.1f}%</td>
          <td>{h['strategy']}</td>
        </tr>"""

    # ===== 5. Critic却下分析 =====
    total_rejected = sum(len(r["rejected"]) for r in day_reports)
    total_executed = sum(len(r["executed"]) for r in day_reports)

    # ===== 6. 月別パフォーマンス =====
    monthly_pnl = {}
    for t in trades:
        month_key = t["exit_date"].strftime("%Y-%m")
        if month_key not in monthly_pnl:
            monthly_pnl[month_key] = {"pnl": 0.0, "trades": 0, "wins": 0}
        monthly_pnl[month_key]["pnl"] += t["pnl"]
        monthly_pnl[month_key]["trades"] += 1
        if t["pnl"] > 0:
            monthly_pnl[month_key]["wins"] += 1

    monthly_rows = ""
    for m in sorted(monthly_pnl.keys()):
        st = monthly_pnl[m]
        wr = (st["wins"] / st["trades"] * 100) if st["trades"] else 0
        pnl_class = "positive" if st["pnl"] >= 0 else "negative"
        monthly_rows += f"""<tr>
          <td>{m}</td><td class="num">{st['trades']}</td>
          <td class="num">{wr:.0f}%</td>
          <td class="num {pnl_class}">${st['pnl']:+,.2f}</td>
        </tr>"""

    # ===== 7. ドローダウン分析 =====
    equities = [s["total_equity"] for s in portfolio.daily_snapshots]
    max_dd = 0.0
    peak = equities[0] if equities else initial
    dd_start = dd_end = sim_dates[0] if sim_dates else date.today()
    current_dd_start = dd_start
    for i, eq in enumerate(equities):
        if eq > peak:
            peak = eq
            current_dd_start = portfolio.daily_snapshots[i]["date"]
        dd = (eq - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
            dd_start = current_dd_start
            dd_end = portfolio.daily_snapshots[i]["date"]

    # ===== 8. 改善提案の生成 =====
    suggestions = []

    # 勝率が低い
    win_count = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = (win_count / len(trades) * 100) if trades else 0
    if win_rate < 40:
        suggestions.append({
            "title": "勝率が低い（{:.0f}%）".format(win_rate),
            "detail": "エントリー条件が甘い可能性。Criticの承認閾値（現在0.35）を0.45~0.50に引き上げ、低信頼度シグナルを排除することを検討。",
            "severity": "high",
        })

    # 損大利小
    if avg_loss != 0 and abs(avg_win / avg_loss) < 1.0:
        suggestions.append({
            "title": "損大利小（平均利益${:+.2f} vs 平均損失${:+.2f}）".format(avg_win, avg_loss),
            "detail": "利確が早すぎるか、損切りが遅い。ストップロスをATRの1.5倍（現在2倍）に縮め、テイクプロフィットをATRの4倍（現在3倍）に拡大することを検討。",
            "severity": "high",
        })

    # トレンドフォローが弱気相場で機能していない
    for s_name, st in strat_stats.items():
        if "sma" in s_name.lower() and st["total_pnl"] < -100:
            suggestions.append({
                "title": f"{s_name}戦略の損失が大きい（${st['total_pnl']:+,.2f}）",
                "detail": "SMAクロスオーバーは弱気相場ではダマシが多発。S&P500がSMA200を下回った時はSMA戦略のBUYシグナルを無効化するフィルターを追加すべき。",
                "severity": "high",
            })
        if "breakout" in s_name.lower() and st["total_pnl"] < -50:
            suggestions.append({
                "title": f"{s_name}戦略が出口専用になっている",
                "detail": "ブレイクアウト戦略がエントリーではなく損切りトリガーとして機能。ブレイクアウト買いの出来高フィルター（現在1.5倍）を2.0倍に強化することを検討。",
                "severity": "medium",
            })

    # 保有期間が長すぎる
    long_holds = [t for t in trades if t.get("holding_days", 0) > 30 and t["pnl"] < 0]
    if long_holds:
        suggestions.append({
            "title": "長期保有で損失拡大（{}件が30日超保有で損失）".format(len(long_holds)),
            "detail": "時間ベースのストップ（例：20営業日経過で強制決済）を追加し、ダラダラ保有による機会損失と損失拡大を防ぐべき。",
            "severity": "high",
        })

    # ポジションなし期間が長い
    no_pos_days = sum(1 for s in portfolio.daily_snapshots if s["num_positions"] == 0)
    total_days = len(portfolio.daily_snapshots)
    if total_days > 0 and no_pos_days / total_days > 0.5:
        suggestions.append({
            "title": f"資金の稼働率が低い（{no_pos_days}/{total_days}日がポジションなし）",
            "detail": "全期間の{:.0f}%で現金が遊んでいる。レンジ相場でのRSI戦略の閾値を緩める（RSI<35で買い、現在<30）か、ボリンジャーバンド戦略を追加して機会を増やすことを検討。".format(no_pos_days / total_days * 100),
            "severity": "medium",
        })

    # Profit Factor
    if profit_factor < 1.0 and profit_factor > 0:
        suggestions.append({
            "title": f"プロフィットファクターが1.0未満（{profit_factor:.2f}）",
            "detail": "総利益 < 総損失。現在のシステムでは長期的にマイナスリターンが続く。戦略の根本的な見直しか、相場環境フィルターの強化が必要。",
            "severity": "critical",
        })

    suggestion_cards = ""
    severity_colors = {
        "critical": ("var(--accent-dusty-rose)", "var(--accent-dusty-rose-bg)"),
        "high": ("#c48a50", "#faf0e4"),
        "medium": ("var(--accent-sand)", "var(--accent-sand-bg)"),
        "low": ("var(--accent-sage)", "var(--accent-sage-bg)"),
    }
    for i, sug in enumerate(suggestions, 1):
        color, bg = severity_colors.get(sug["severity"], severity_colors["medium"])
        suggestion_cards += f"""
    <div class="suggestion-card" style="border-left: 4px solid {color}; background: {bg};">
      <div class="suggestion-title">提案{i}: {sug['title']}</div>
      <div class="suggestion-detail">{sug['detail']}</div>
    </div>"""

    # ===== 個別トレード散布図（保有日数 vs 損益%）SVG =====
    scatter_svg = ""
    if trades:
        chart_w, chart_h = 600, 300
        pad = 50
        max_hold = max(t.get("holding_days", 1) for t in trades) or 1
        pnl_pcts = [t["pnl_pct"] for t in trades]
        max_pnl = max(abs(p) for p in pnl_pcts) if pnl_pcts else 10
        max_pnl = max(max_pnl, 5)  # 最低5%スケール

        dots = ""
        for t in trades:
            hd = t.get("holding_days", 0)
            x = pad + (hd / max_hold) * (chart_w - 2 * pad)
            y = chart_h / 2 - (t["pnl_pct"] / max_pnl) * (chart_h / 2 - pad)
            color = "#5a8a6a" if t["pnl"] > 0 else "#b05050"
            r = max(5, min(12, abs(t["pnl"]) / 20))
            dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.0f}" fill="{color}" opacity="0.7"><title>{t["ticker"]} {t.get("entry_strategy","")} {t["holding_days"]}日 {t["pnl_pct"]:+.1f}%</title></circle>'

        # 軸
        zero_y = chart_h / 2
        scatter_svg = f"""
    <svg viewBox="0 0 {chart_w} {chart_h}" class="equity-chart">
      <line x1="{pad}" y1="{zero_y}" x2="{chart_w-pad}" y2="{zero_y}" stroke="#ddd5cd" stroke-width="1" stroke-dasharray="4"/>
      <line x1="{pad}" y1="{pad}" x2="{pad}" y2="{chart_h-pad}" stroke="#ddd5cd" stroke-width="0.5"/>
      <text x="{chart_w/2}" y="{chart_h-5}" text-anchor="middle" class="chart-label">保有日数</text>
      <text x="12" y="{chart_h/2}" text-anchor="middle" class="chart-label" transform="rotate(-90,12,{chart_h/2})">損益%</text>
      <text x="{pad-5}" y="{zero_y+4}" text-anchor="end" class="chart-label">0%</text>
      <text x="{pad-5}" y="{pad+4}" text-anchor="end" class="chart-label">+{max_pnl:.0f}%</text>
      <text x="{pad-5}" y="{chart_h-pad+4}" text-anchor="end" class="chart-label">-{max_pnl:.0f}%</text>
      <text x="{chart_w-pad}" y="{zero_y+16}" text-anchor="end" class="chart-label">{max_hold}日</text>
      {dots}
    </svg>"""

    # ===== HTML =====
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AutoTRD 分析レポート — {date_range}</title>
<style>
  :root {{
    --bg-main: #f5f0eb; --bg-card: #ffffff; --bg-section: #ede8e3; --bg-code: #e8e3de;
    --text-primary: #4a4541; --text-secondary: #7a726b; --text-heading: #5c554e;
    --accent-sage: #8fa89a; --accent-sage-light: #c5d5cb; --accent-sage-bg: #eef3f0;
    --accent-dusty-rose: #c4a0a0; --accent-dusty-rose-bg: #f5ecec;
    --accent-slate: #8e9aaa; --accent-slate-bg: #edf0f4;
    --accent-sand: #c4b49a; --accent-sand-bg: #f5f0e8;
    --accent-lavender: #a89ab8; --accent-lavender-bg: #f0ecf4;
    --accent-terracotta: #c4a088; --accent-terracotta-bg: #f5ede6;
    --border-color: #ddd5cd; --shadow: 0 2px 12px rgba(74,69,65,0.06); --radius: 12px;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:"Helvetica Neue","Hiragino Sans","Noto Sans JP",sans-serif;
         background:var(--bg-main); color:var(--text-primary); line-height:1.8; font-size:15px; }}
  .header {{ background:linear-gradient(135deg,#b05050 0%,#c48a50 40%,#8e9aaa 100%);
             color:white; padding:48px 40px; text-align:center; }}
  .header h1 {{ font-size:2.2em; font-weight:700; margin-bottom:8px; text-shadow:0 1px 4px rgba(0,0,0,0.1); }}
  .header .subtitle {{ font-size:1.05em; opacity:0.92; }}
  .header .meta {{ margin-top:12px; font-size:0.85em; opacity:0.7; }}
  /* Sidebar Layout */
  .layout {{ display:flex; min-height:calc(100vh - 140px); }}
  .sidebar {{
    width:230px; flex-shrink:0; background:var(--bg-card);
    border-right:1px solid var(--border-color);
    position:sticky; top:0; height:100vh; overflow-y:auto;
    padding:24px 0; z-index:100;
  }}
  .sidebar-title {{ font-size:0.72em; font-weight:700; color:var(--text-secondary);
    text-transform:uppercase; letter-spacing:0.08em; padding:12px 20px 8px; }}
  .sidebar a {{
    display:block; color:var(--text-secondary); text-decoration:none;
    padding:9px 20px; font-size:0.88em; transition:all 0.2s;
    border-left:3px solid transparent;
  }}
  .sidebar a:hover {{ background:var(--accent-sage-bg); color:var(--text-primary); border-left-color:var(--accent-sage); }}
  .sidebar a.active {{ background:var(--accent-sage-bg); color:var(--accent-sage); border-left-color:var(--accent-sage); font-weight:600; }}
  .container {{ flex:1; max-width:1100px; padding:40px 24px 80px; }}
  .section {{ background:var(--bg-card); border-radius:var(--radius); box-shadow:var(--shadow);
              margin-bottom:32px; border:1px solid var(--border-color); }}
  .section-header {{ padding:24px 32px 20px; border-bottom:1px solid var(--border-color);
                     display:flex; align-items:center; gap:14px;
                     position:sticky; top:0; z-index:20; background:var(--bg-card);
                     border-radius:var(--radius) var(--radius) 0 0; }}
  .section-header .icon {{ width:44px; height:44px; border-radius:10px;
    display:flex; align-items:center; justify-content:center; font-size:1.3em; flex-shrink:0; }}
  .section-header h2 {{ font-size:1.25em; color:var(--text-heading); font-weight:600; }}
  .section-body {{ padding:24px 32px; }}

  .kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:14px; margin-bottom:24px; }}
  .kpi {{ background:var(--bg-section); border-radius:10px; padding:16px; text-align:center; }}
  .kpi .label {{ font-size:0.78em; color:var(--text-secondary); margin-bottom:2px; }}
  .kpi .value {{ font-size:1.5em; font-weight:700; color:var(--text-heading); }}
  .kpi .value.positive {{ color:#5a8a6a; }}
  .kpi .value.negative {{ color:#b05050; }}
  .kpi .value.critical {{ color:#b05050; font-weight:800; }}

  .table-scroll {{ max-height:70vh; overflow-y:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.9em; }}
  th {{ background:var(--bg-section); color:var(--text-secondary); font-weight:600;
       text-align:left; padding:10px 14px; font-size:0.82em; text-transform:uppercase; letter-spacing:0.05em;
       position:sticky; top:0; z-index:10; }}
  td {{ padding:10px 14px; border-bottom:1px solid #eee; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover td {{ background:#faf8f6; }}
  .num {{ font-variant-numeric:tabular-nums; text-align:right; font-family:"SF Mono","Consolas",monospace; }}
  .positive {{ color:#5a8a6a; }}
  .negative {{ color:#b05050; }}

  .equity-chart {{ width:100%; height:auto; }}
  .chart-label {{ font-size:11px; fill:var(--text-secondary); font-family:"SF Mono","Consolas",monospace; }}

  .suggestion-card {{ padding:16px 20px; border-radius:8px; margin-bottom:12px; }}
  .suggestion-title {{ font-weight:700; font-size:1em; margin-bottom:4px; color:var(--text-heading); }}
  .suggestion-detail {{ font-size:0.9em; color:var(--text-primary); line-height:1.7; }}

  .insight-box {{ background:var(--accent-slate-bg); border-radius:8px; padding:16px 20px;
                  margin-bottom:16px; font-size:0.9em; line-height:1.7; }}
  .insight-box strong {{ color:var(--text-heading); }}

  .footer {{ text-align:center; padding:32px; color:var(--text-secondary); font-size:0.82em; }}
</style>
</head>
<body>

<div class="header">
  <h1>AutoTRD Analysis Report</h1>
  <div class="subtitle">シミュレーション分析 {date_range}</div>
  <div class="meta">Generated: {now_str} | Initial: ${initial:,.2f} | Final: ${final:,.2f} | Return: {total_return:+.2f}%</div>
</div>

<div class="layout">

<nav class="sidebar">
  <div class="sidebar-title">Contents</div>
  <a href="#overview">概要</a>
  <a href="#strategy">戦略別</a>
  <a href="#regime">レジーム別</a>
  <a href="#riskreturn">リスク/リワード</a>
  <a href="#holding">保有期間</a>
  <a href="#monthly">月別</a>
  <a href="#scatter">散布図</a>
  <a href="#suggestions">改善提案</a>
</nav>

<div class="container">

<!-- ===== 概要KPI ===== -->
<div class="section" id="overview">
  <div class="section-header">
    <div class="icon" style="background:var(--accent-slate-bg);">&#x1f50d;</div>
    <div><h2>分析概要</h2></div>
  </div>
  <div class="section-body">
    <div class="kpi-grid">
      <div class="kpi"><div class="label">リターン</div><div class="value {"positive" if total_return>=0 else "negative"}">{total_return:+.2f}%</div></div>
      <div class="kpi"><div class="label">取引回数</div><div class="value">{len(trades)}</div></div>
      <div class="kpi"><div class="label">勝率</div><div class="value {"positive" if win_rate>=50 else "negative"}">{win_rate:.0f}%</div></div>
      <div class="kpi"><div class="label">プロフィットファクター</div><div class="value {"positive" if profit_factor>=1 else "critical"}">{profit_factor:.2f}</div></div>
      <div class="kpi"><div class="label">期待値/トレード</div><div class="value {"positive" if expectancy>=0 else "negative"}">${expectancy:+,.2f}</div></div>
      <div class="kpi"><div class="label">最大DD</div><div class="value negative">{max_dd:.2f}%</div></div>
      <div class="kpi"><div class="label">DD期間</div><div class="value">{dd_start}~{dd_end}</div></div>
      <div class="kpi"><div class="label">稼働率</div><div class="value">{(total_days-no_pos_days)/total_days*100 if total_days > 0 else 0:.0f}%</div></div>
    </div>
    <div class="insight-box">
      <strong>診断:</strong>
      プロフィットファクター{profit_factor:.2f}（1.0未満 = 負けシステム）、
      勝率{win_rate:.0f}%、期待値${expectancy:+,.2f}/トレード。
      平均利益${avg_win:+,.2f} vs 平均損失${avg_loss:+,.2f}で、
      {"損大利小パターン。利確を伸ばすか損切りを早める改善が必要。" if avg_loss != 0 and abs(avg_win) < abs(avg_loss) else "利大損小は維持できているが、勝率の改善が必要。" if avg_loss != 0 else ""}
      最大ドローダウンは{max_dd:.2f}%（{dd_start}～{dd_end}）。
      {"資金の{:.0f}%が遊んでおり、稼働率の改善も課題。".format(no_pos_days/total_days*100) if total_days > 0 and no_pos_days/total_days > 0.4 else ""}
    </div>
  </div>
</div>

<!-- ===== 戦略別 ===== -->
<div class="section" id="strategy">
  <div class="section-header">
    <div class="icon" style="background:var(--accent-sage-bg);">&#x1f3af;</div>
    <div><h2>戦略別パフォーマンス</h2></div>
  </div>
  <div class="section-body">
    <table>
      <tr><th>戦略</th><th>取引数</th><th>勝率</th><th>総損益</th><th>平均損益</th><th>最大損失</th><th>最大利益</th><th>平均保有</th></tr>
      {strat_rows}
    </table>
  </div>
</div>

<!-- ===== レジーム別 ===== -->
<div class="section" id="regime">
  <div class="section-header">
    <div class="icon" style="background:var(--accent-lavender-bg);">&#x1f30d;</div>
    <div><h2>市場レジーム別パフォーマンス</h2></div>
  </div>
  <div class="section-body">
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:24px;">
      <div>
        <h3 style="font-size:0.95em; color:var(--text-secondary); margin-bottom:8px;">エントリー時レジーム別損益</h3>
        <table><tr><th>レジーム</th><th>取引数</th><th>勝率</th><th>総損益</th></tr>{regime_rows}</table>
      </div>
      <div>
        <h3 style="font-size:0.95em; color:var(--text-secondary); margin-bottom:8px;">レジーム日数分布</h3>
        <table><tr><th>レジーム</th><th>日数</th><th>割合</th></tr>{regime_dist_rows}</table>
      </div>
    </div>
  </div>
</div>

<!-- ===== リスク/リワード ===== -->
<div class="section" id="riskreturn">
  <div class="section-header">
    <div class="icon" style="background:var(--accent-dusty-rose-bg);">&#x2696;&#xfe0f;</div>
    <div><h2>リスク / リワード分析</h2></div>
  </div>
  <div class="section-body">
    <div class="kpi-grid">
      <div class="kpi"><div class="label">平均利益</div><div class="value positive">${avg_win:+,.2f}</div></div>
      <div class="kpi"><div class="label">平均損失</div><div class="value negative">${avg_loss:+,.2f}</div></div>
      <div class="kpi"><div class="label">損益比率</div><div class="value {"positive" if avg_loss!=0 and abs(avg_win/avg_loss)>=1.5 else "negative"}">{abs(avg_win/avg_loss) if avg_loss != 0 else 0:.2f}:1</div></div>
      <div class="kpi"><div class="label">勝ちトレード数</div><div class="value">{len(wins_pnl)}</div></div>
      <div class="kpi"><div class="label">負けトレード数</div><div class="value">{len(losses_pnl)}</div></div>
      <div class="kpi"><div class="label">総利益</div><div class="value positive">${sum(wins_pnl):+,.2f}</div></div>
      <div class="kpi"><div class="label">総損失</div><div class="value negative">${sum(losses_pnl):+,.2f}</div></div>
      <div class="kpi"><div class="label">Critic却下数</div><div class="value">{total_rejected}</div></div>
    </div>
    <div class="insight-box">
      <strong>分析:</strong>
      {"損益比率が{:.2f}:1と1.5:1を下回っており、損切りが遅く利確が早い傾向。ストップロスを引き締め、テイクプロフィットを拡大すべき。".format(abs(avg_win/avg_loss) if avg_loss != 0 else 0) if avg_loss != 0 and abs(avg_win/avg_loss) < 1.5 else "損益比率は良好だが、勝率が低い。エントリー精度の改善が必要。"}
      Criticは{total_rejected}件のシグナルを却下し、{total_executed}件を約定。
      {"却下率が高く、Criticが機能しているが、通過したシグナルの質に問題がある。" if total_rejected > total_executed else ""}
    </div>
  </div>
</div>

<!-- ===== 保有期間 ===== -->
<div class="section" id="holding">
  <div class="section-header">
    <div class="icon" style="background:var(--accent-sand-bg);">&#x23f1;&#xfe0f;</div>
    <div><h2>保有期間分析</h2></div>
  </div>
  <div class="section-body">
    <table>
      <tr><th>銘柄</th><th>保有日数</th><th>損益</th><th>損益%</th><th>戦略</th></tr>
      {holding_rows}
    </table>
    <div class="insight-box" style="margin-top:16px;">
      <strong>分析:</strong>
      {"30日以上保有して損失の取引が{}件。時間ベースの強制決済ルール追加が有効。".format(len(long_holds)) if long_holds else "保有期間は適切な範囲。"}
      {"短期（5日以下）の取引は利確も損切りも小さく、手数料負けのリスクあり。" if any(t.get("holding_days",0) <= 5 for t in trades) else ""}
    </div>
  </div>
</div>

<!-- ===== 月別 ===== -->
<div class="section" id="monthly">
  <div class="section-header">
    <div class="icon" style="background:var(--accent-terracotta-bg);">&#x1f4c5;</div>
    <div><h2>月別パフォーマンス</h2></div>
  </div>
  <div class="section-body">
    <table>
      <tr><th>月</th><th>取引数</th><th>勝率</th><th>損益</th></tr>
      {monthly_rows}
    </table>
  </div>
</div>

<!-- ===== 散布図 ===== -->
<div class="section" id="scatter">
  <div class="section-header">
    <div class="icon" style="background:var(--accent-slate-bg);">&#x1f4ca;</div>
    <div><h2>トレード散布図（保有日数 vs 損益%）</h2></div>
  </div>
  <div class="section-body">
    {scatter_svg if scatter_svg else '<div style="color:var(--text-secondary);">トレードデータなし</div>'}
    <div style="margin-top:8px; font-size:0.82em; color:var(--text-secondary);">
      <span style="color:#5a8a6a;">&#x25cf;</span> 利益 &nbsp;
      <span style="color:#b05050;">&#x25cf;</span> 損失 &nbsp;
      円の大きさ = 損益額の大きさ
    </div>
  </div>
</div>

<!-- ===== 改善提案 ===== -->
<div class="section" id="suggestions">
  <div class="section-header">
    <div class="icon" style="background:var(--accent-dusty-rose-bg);">&#x1f4a1;</div>
    <div><h2>改善提案</h2></div>
  </div>
  <div class="section-body">
    {suggestion_cards if suggestion_cards else '<div style="color:var(--text-secondary);">特に改善提案はありません</div>'}
  </div>
</div>

</div>
</div>

<div class="footer">AutoTRD Analysis Report &mdash; Generated by simulate.py</div>

<script>
(function() {{
  const links = document.querySelectorAll('.sidebar a[href^="#"]');
  const sections = Array.from(links).map(a => document.getElementById(a.getAttribute('href').slice(1))).filter(Boolean);
  function onScroll() {{
    let current = '';
    for (const sec of sections) {{
      if (sec.getBoundingClientRect().top <= 120) current = sec.id;
    }}
    links.forEach(a => {{
      a.classList.toggle('active', a.getAttribute('href') === '#' + current);
    }});
  }}
  window.addEventListener('scroll', onScroll, {{ passive: true }});
  onScroll();
}})();
</script>

</body></html>"""
    return html


# ===========================================================================
# メイン
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="過去データでトレーディングシステムをシミュレーションする"
    )
    parser.add_argument(
        "dates", nargs="+",
        help="シミュレーション日付 (YYYY-MM-DD) または範囲 (YYYY-MM-DD:YYYY-MM-DD)",
    )
    parser.add_argument("--capital", type=float, default=3300.0, help="初期資金 (デフォルト: $3,300)")
    parser.add_argument("--output", type=str, default=None, help="HTML出力先パス (デフォルト: doc/simulation_YYYYMMDD.html)")
    args = parser.parse_args()

    sim_dates = parse_dates(args.dates)
    if not sim_dates:
        print("エラー: 有効な日付が指定されていません")
        sys.exit(1)

    print(f"シミュレーション日付: {[str(d) for d in sim_dates]}")
    print(f"初期資金: ${args.capital:,.2f}")

    # 戦略読み込み
    all_strategies = load_strategies()
    print(f"戦略: {[s.name for s in all_strategies]}")

    # 全銘柄のデータ取得
    all_tickers = list(set(DEFAULT_UNIVERSE + [SP500_TICKER, VIX_TICKER]))
    print(f"\n価格データを取得中... ({len(all_tickers)}銘柄)")
    all_data = fetch_all_data(all_tickers)
    print(f"取得成功: {len(all_data)}銘柄")

    sp500_df = all_data.get(SP500_TICKER, pd.DataFrame())
    vix_df = all_data.get(VIX_TICKER, pd.DataFrame())

    if sp500_df.empty:
        print("警告: S&P500データなし — 市場判定が不正確になります")

    # シミュレーション実行
    portfolio = SimulatedPortfolio(initial_cash=args.capital)
    day_reports = []

    print(f"\nシミュレーション開始...")
    for sim_date in sim_dates:
        if not sp500_df.empty:
            available = sp500_df[sp500_df.index <= pd.Timestamp(sim_date)]
            if available.empty:
                print(f"\n  {sim_date}: データなし — スキップ")
                continue

        day_report = simulate_one_day(
            sim_date, portfolio, all_data, sp500_df, vix_df, all_strategies
        )
        day_reports.append(day_report)
        print_report(day_report)

    print_summary(portfolio)

    # HTML レポート出力
    project_root = Path(__file__).parent.parent
    doc_dir = project_root / "doc"
    doc_dir.mkdir(exist_ok=True)

    date_tag = f"{sim_dates[0].strftime('%Y%m%d')}_{sim_dates[-1].strftime('%Y%m%d')}"

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = doc_dir / f"simulation_{date_tag}.html"

    html = generate_html_report(portfolio, day_reports, sim_dates, all_data=all_data)
    output_path.write_text(html, encoding="utf-8")
    print(f"\nシミュレーションレポート: {output_path}")

    # 分析レポート出力
    analysis_path = doc_dir / f"analysis_{date_tag}.html"
    analysis_html = generate_analysis_html(portfolio, day_reports, sim_dates)
    analysis_path.write_text(analysis_html, encoding="utf-8")
    print(f"分析レポート: {analysis_path}")


if __name__ == "__main__":
    main()
