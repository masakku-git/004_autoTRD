"""breakout の「なぜエントリーしなかったか」を集計する。

breakout_v7 が候補銘柄ごとに出す診断ログ（BREAKOUT-DIAG）を集計する。過去分は、
本番の screening_results と価格データから同じ診断を再計算（--replay）して確認できる。

使い方:
    python scripts/summarize_breakout_diag.py                       # logs/ と data/ の診断ログを集計
    python scripts/summarize_breakout_diag.py --logs-dir logs       # ログの場所を指定
    python scripts/summarize_breakout_diag.py --replay 2026-08-01:2026-09-18   # 過去を再計算
    オプション: --near-miss N（ニアミス上位N件を表示、既定15）

reason の意味は src/strategy/plugins/breakout_v7.py の docstring を参照。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

_LINE = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2}) .*BREAKOUT-DIAG ticker=(?P<ticker>\S+) "
    r"result=(?P<result>\S+) reason=(?P<reason>\S+)(?P<rest>.*)$"
)


def parse_log_lines(lines) -> list[dict]:
    """診断ログ行を dict のリストにする（純粋関数）。BREAKOUT-DIAG 以外の行は無視する。"""
    out = []
    for line in lines:
        m = _LINE.match(line)
        if not m:
            continue
        rec = {"day": m["day"], "ticker": m["ticker"], "result": m["result"], "reason": m["reason"]}
        for kv in m["rest"].split():
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    rec[k] = float(v)
                except ValueError:
                    rec[k] = v
        out.append(rec)
    return out


def summarize(records: list[dict]) -> dict:
    """reason別・週別の件数と、ニアミス（あと一歩でBUYだった候補）を返す（純粋関数）。

    ニアミス: volume_low（高値は超えたが出来高不足）と、below_20d_high のうち高値まで1%以内。
    同じ日・銘柄が複数回記録されていても（SELL判定の再呼び出し等）1件に丸める。
    """
    uniq = {}
    for r in records:
        uniq[(r["day"], r["ticker"])] = r
    recs = list(uniq.values())
    by_reason = Counter(r["reason"] for r in recs)
    by_week: dict[str, Counter] = defaultdict(Counter)
    for r in recs:
        y, w, _ = date.fromisoformat(r["day"]).isocalendar()
        by_week[f"{y}-W{w:02d}"][r["reason"]] += 1
    near = [
        r for r in recs
        if r["reason"] == "volume_low"
        or (r["reason"] == "below_20d_high" and 0 <= float(r.get("gap_pct", 99)) <= 1.0)
    ]
    near.sort(key=lambda r: (r["reason"] != "volume_low", float(r.get("gap_pct", 0))))
    return {"total": len(recs), "by_reason": by_reason, "by_week": dict(sorted(by_week.items())), "near": near}


def _print(summary: dict, near_n: int) -> None:
    total = summary["total"]
    print(f"診断件数（日×銘柄）: {total}")
    if not total:
        print("（診断ログがありません。breakout_v7 のデプロイ後に蓄積されます）")
        return
    print("\n== 理由別 ==")
    for reason, n in summary["by_reason"].most_common():
        print(f"  {reason:<18} {n:>5}  ({n/total*100:4.1f}%)")
    reasons = [r for r, _ in summary["by_reason"].most_common()]
    print("\n== 週別 ==")
    print("  週        " + " ".join(f"{r[:12]:>12}" for r in reasons))
    for wk, c in summary["by_week"].items():
        print(f"  {wk}  " + " ".join(f"{c.get(r, 0):>12}" for r in reasons))
    print(f"\n== ニアミス上位{near_n}件（あと一歩でBUY）==")
    for r in summary["near"][:near_n]:
        print(f"  {r['day']} {r['ticker']:<6} {r['reason']:<15} gap_pct={r.get('gap_pct')} vol_ratio={r.get('vol_ratio')}")


def _load_logs(logs_dirs: list[Path]) -> list[dict]:
    lines: list[str] = []
    for d in logs_dirs:
        for p in sorted(d.glob("autotrd.log*")):
            lines.extend(p.read_text(encoding="utf-8", errors="replace").splitlines())
    return parse_log_lines(lines)


def _replay(span: str) -> list[dict]:
    """本番 screening_results の候補と価格データから、過去日の診断を再計算する。"""
    import pandas as pd

    import simulate as sim
    from src.strategy.plugins.breakout_v7 import BreakoutV7

    days = sim.parse_dates([span])
    hist = sim.load_screening_history(Path(__file__).parent.parent / "data" / "csv_export" / "screening_results.csv")
    sim.SCREENING_HISTORY = hist
    tickers = {c["ticker"] for v in hist.values() for c in v} | {sim.SP500_TICKER, sim.VIX_TICKER}
    print(f"価格データを取得中... ({len(tickers)}銘柄)")
    data = sim.fetch_all_data(sorted(tickers), sim_start=days[0])
    sp, vx = data.get(sim.SP500_TICKER, pd.DataFrame()), data.get(sim.VIX_TICKER, pd.DataFrame())
    strat = BreakoutV7()
    out = []
    for d in days:
        mc = sim.assess_market_condition_at(sp, vx, d)
        for c in sim.run_screening_at(data, d):
            df = data.get(c["ticker"])
            if df is None:
                continue
            df = df[df.index <= pd.Timestamp(d)]
            if df.empty:
                continue
            diag = strat.diagnose_entry(df, mc)
            out.append({"day": d.isoformat(), "ticker": c["ticker"], "result": "replay",
                        "reason": diag["reason"], **diag["metrics"]})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="breakout の判定診断を集計する")
    parser.add_argument("--logs-dir", action="append", help="診断ログのディレクトリ（複数指定可。既定: logs と data）")
    parser.add_argument("--replay", help="過去の再計算 YYYY-MM-DD:YYYY-MM-DD（平日のみ）")
    parser.add_argument("--near-miss", type=int, default=15)
    args = parser.parse_args()

    root = Path(__file__).parent.parent
    records = _replay(args.replay) if args.replay else _load_logs(
        [Path(d) for d in args.logs_dir] if args.logs_dir else [root / "logs", root / "data"]
    )
    _print(summarize(records), args.near_miss)


if __name__ == "__main__":
    main()
