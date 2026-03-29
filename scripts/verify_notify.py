"""DBから過去データを読み取り、修正後のSlack通知内容を検証するスクリプト。

使い方:
    python scripts/verify_notify.py 2026-03-26
    python scripts/verify_notify.py 2026-03-27
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from src.models.base import get_session
from src.models.market import MarketCondition, ScreeningResult
from src.models.portfolio import PortfolioSnapshot
from src.models.trade import Order, TradeLog


@dataclass
class _FakeAccount:
    total_equity: float
    cash: float
    positions: list
    market_value: float = 0.0


def _load_data(target_date: date) -> dict:
    """指定日のDB記録を取得する。"""
    with get_session() as session:
        # 市場環境
        mc = session.execute(
            select(MarketCondition).where(MarketCondition.date == target_date)
        ).scalar_one_or_none()

        # ポートフォリオスナップショット
        ps = session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.date == target_date)
        ).scalar_one_or_none()

        # スクリーニング結果（selected=Trueのもの）
        sr_rows = session.execute(
            select(ScreeningResult)
            .where(ScreeningResult.run_date == target_date)
            .where(ScreeningResult.selected == True)  # noqa: E712
            .order_by(ScreeningResult.score.desc())
        ).scalars().all()

        # 当日の注文履歴
        orders = session.execute(
            select(Order)
            .where(Order.created_at >= f"{target_date} 00:00:00")
            .where(Order.created_at < f"{date(target_date.year, target_date.month, target_date.day + 1)} 00:00:00")
            .order_by(Order.created_at)
        ).scalars().all()

        # 当日クローズされたトレードログ
        closed_trades = session.execute(
            select(TradeLog)
            .where(TradeLog.exit_date == target_date)
        ).scalars().all()

        # 当日エントリーしたトレードログ
        open_trades = session.execute(
            select(TradeLog)
            .where(TradeLog.entry_date == target_date)
        ).scalars().all()

        return {
            "market_condition": mc,
            "portfolio_snapshot": ps,
            "screening_results": sr_rows,
            "orders": orders,
            "closed_trades": closed_trades,
            "open_trades": open_trades,
        }


def _build_market_condition_dict(mc: MarketCondition | None) -> dict:
    if mc is None:
        return {"regime": "range", "sp500_trend": "neutral", "vix_level": 0.0}
    return {
        "regime": mc.regime,
        "sp500_trend": mc.sp500_trend,
        "vix_level": mc.vix_level,
    }


def _build_candidates(sr_rows: list) -> list:
    candidates = []
    for row in sr_rows:
        cj = row.criteria_json or {}
        candidates.append({
            "ticker": row.ticker,
            "last_close": cj.get("last_close", 0.0),
            "atr_pct": cj.get("atr_pct", 0.0),
            "relative_strength": cj.get("relative_strength", 0.0),
            "score": row.score,
        })
    return candidates


def _build_executed_orders(orders: list) -> tuple[list, int, int]:
    executed = []
    buy_count = 0
    sell_count = 0
    for o in orders:
        status_label = f"[{o.status}]"
        if o.side == "BUY":
            buy_count += 1
            executed.append(f"BUY {o.quantity}x {o.ticker} @${o.price or 0:.2f} {status_label} strategy={o.strategy_name}")
        elif o.side == "SELL":
            sell_count += 1
            executed.append(f"SELL {o.quantity}x {o.ticker} @${o.price or 0:.2f} {status_label} strategy={o.strategy_name}")
    return executed, buy_count, sell_count


def _build_summary_from_db(target_date: date, data: dict) -> str:
    """修正後の _build_summary と同等のロジックでサマリーを生成する。"""
    mc_dict = _build_market_condition_dict(data["market_condition"])
    ps = data["portfolio_snapshot"]
    candidates = _build_candidates(data["screening_results"])
    executed_orders, buy_count, sell_count = _build_executed_orders(data["orders"])

    regime_raw = mc_dict.get("regime", "")
    trend_raw = mc_dict.get("sp500_trend", "")
    vix = mc_dict.get("vix_level", 0.0)

    regime_ja = {"trending": "トレンド相場", "range": "レンジ相場", "volatile": "高ボラ相場"}.get(regime_raw, regime_raw)
    trend_ja = {"bull": "強気(上昇)", "bear": "弱気(下落)", "neutral": "中立(横ばい)"}.get(trend_raw, trend_raw)

    regime_desc = {
        "trending": "明確なトレンドが出ている相場（ブレイクアウト・モメンタム戦略が有効）",
        "range":    "方向感のない横ばい相場（逆張り・レンジ戦略が有効）",
        "volatile": "VIX>30 の不安定な相場（新規エントリーを縮小・慎重モード）",
    }.get(regime_raw, "")

    lines = [
        f"日付: {target_date}",
        "",
        "【市場環境】",
        f"  S&P500トレンド : {trend_ja}",
        f"  VIX           : {vix:.1f}",
        f"  レジーム       : {regime_ja}",
        f"  └ {regime_desc}",
    ]

    if trend_raw == "bear":
        lines.append("  ⚠ ベア相場のため全戦略がBUYシグナルを生成しません（新規買いなし）")
    if vix >= 30:
        lines.append(f"  ⚠ VIX={vix:.1f} (>=30) のためポジションサイズを50%縮小")
    elif vix >= 25:
        lines.append(f"  ⚠ VIX={vix:.1f} (>=25) のためポジションサイズを75%に縮小")

    total_equity = ps.total_equity if ps else 0.0
    cash = ps.cash if ps else 0.0
    positions = ps.positions_json if ps else []
    num_positions = ps.num_positions if ps else 0

    lines += [
        "",
        "【資産状況】",
        f"  総資産        : ${total_equity:.2f}",
        f"  現金          : ${cash:.2f}",
        f"  保有ポジション : {num_positions}件",
        "",
        "【シグナル診断】",
        f"  スクリーニング通過 : {len(candidates)}銘柄（最大15銘柄）",
        f"  BUYシグナル       : {buy_count}件",
        f"  SELLシグナル      : {sell_count}件",
        f"  Criticに却下      : (DBに保存なし・当日実行ログ参照)",
        f"  リスク管理で却下  : (DBに保存なし・当日実行ログ参照)",
        f"  約定注文          : {len(executed_orders)}件",
    ]

    for order in executed_orders:
        lines.append(f"    - {order}")

    if candidates:
        lines.append("")
        lines.append(f"【スクリーニング候補 上位{min(5, len(candidates))}銘柄】")
        lines.append(f"  {'銘柄':<8} {'株価':>7} {'ATR%':>6} {'相対強度':>8} {'スコア':>7}")
        lines.append(f"  {'-'*8} {'-'*7} {'-'*6} {'-'*8} {'-'*7}")
        for c in candidates[:5]:
            lines.append(
                f"  {c['ticker']:<8} "
                f"${c['last_close']:>6.2f} "
                f"{c['atr_pct']:>5.1f}% "
                f"{c['relative_strength']:>+7.1f}% "
                f"{c['score']:>7.2f}"
            )

    # トレードログの詳細（エントリー/クローズ）
    if data["open_trades"] or data["closed_trades"]:
        lines.append("")
        lines.append("【当日のトレードログ】")
        for t in data["open_trades"]:
            lines.append(
                f"  ENTRY  {t.ticker} x{t.quantity} @${t.entry_price:.2f}"
                f"  SL=${t.stop_loss or 0:.2f}  TP=${t.take_profit or 0:.2f}"
                f"  strategy={t.strategy_name}"
            )
        for t in data["closed_trades"]:
            pnl_str = f"  PnL=${t.pnl:+.2f} ({t.pnl_pct:+.1f}%)" if t.pnl is not None else ""
            lines.append(
                f"  EXIT   {t.ticker} x{t.quantity} @${t.exit_price or 0:.2f}"
                f"{pnl_str}  reason in notes"
            )

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("使い方: python scripts/verify_notify.py <YYYY-MM-DD> [<YYYY-MM-DD> ...]")
        print("例:     python scripts/verify_notify.py 2026-03-26 2026-03-27")
        sys.exit(1)

    dates = [date.fromisoformat(d) for d in sys.argv[1:]]

    for target_date in dates:
        print(f"\n{'='*60}")
        print(f"検証日: {target_date}")
        print("=" * 60)

        data = _load_data(target_date)

        # DB記録の有無を確認
        if data["market_condition"] is None:
            print(f"  ⚠ market_conditions に {target_date} のレコードなし")
        if data["portfolio_snapshot"] is None:
            print(f"  ⚠ portfolio_snapshots に {target_date} のレコードなし")
        if not data["screening_results"]:
            print(f"  ⚠ screening_results に {target_date} の候補なし")

        summary = _build_summary_from_db(target_date, data)
        print("\n--- Slack通知プレビュー ---")
        print(summary)
        print("-" * 40)


if __name__ == "__main__":
    main()
