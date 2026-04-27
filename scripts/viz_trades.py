#!/usr/bin/env python3
"""DBエクスポートCSVから取引ダッシュボードHTMLを生成。

使い方:
    python3 scripts/viz_trades.py
    open data/dashboard.html

依存: pandas のみ（Plotly は CDN 経由で読込）
入力: data/csv_export/*.csv
出力: data/dashboard.html
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

PARTIAL_RE = re.compile(
    r"段階決済:\s*(\d+)株\s*@\s*\$([\d,\.]+),\s*PnL=\$(-?[\d,\.]+)"
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CSV_DIR = PROJECT_ROOT / "data" / "csv_export"
OUTPUT = PROJECT_ROOT / "data" / "dashboard.html"

TABLES = [
    "orders",
    "trade_log",
    "portfolio_snapshots",
    "market_conditions",
    "screening_results",
    "strategy_metadata",
]


def load_csvs() -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for name in TABLES:
        path = CSV_DIR / f"{name}.csv"
        if path.exists() and path.stat().st_size > 0:
            try:
                out[name] = pd.read_csv(path)
            except pd.errors.EmptyDataError:
                out[name] = pd.DataFrame()
        else:
            out[name] = pd.DataFrame()
    return out


def _safe_num(v, default=0.0):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def extract_partial_exits(trades_df: pd.DataFrame, orders_df: pd.DataFrame) -> list[dict]:
    """trade_log の notes フィールドから段階決済を抽出。

    DBスキーマ上、段階決済の実現損益は trade_log.notes にテキストで記録される
    （pnl 列は全数決済時のみ書き込み）。本関数はそれを構造化データに変換する。
    可能であれば orders.csv の SELL 注文と銘柄+数量で突き合わせて exit_date を補完する。
    """
    if trades_df.empty:
        return []

    partials: list[dict] = []
    for _, r in trades_df.iterrows():
        notes = r.get("notes")
        if pd.isna(notes) or not str(notes).strip():
            continue
        entry_price = _safe_num(r.get("entry_price"))
        for line in str(notes).splitlines():
            m = PARTIAL_RE.search(line)
            if not m:
                continue
            sold_qty = int(m.group(1))
            exit_price = float(m.group(2).replace(",", ""))
            pnl = float(m.group(3).replace(",", ""))
            cost = entry_price * sold_qty
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
            partials.append({
                "ticker": r["ticker"],
                "quantity": sold_qty,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "strategy_name": r.get("strategy_name", "-"),
                "entry_date": str(r["entry_date"]) if pd.notna(r.get("entry_date")) else "-",
                "exit_date": "-",
                "kind": "部分",
            })

    if partials and not orders_df.empty and "side" in orders_df.columns:
        sells = orders_df[orders_df["side"] == "SELL"].copy()
        if not sells.empty:
            sells["_ts"] = pd.to_datetime(sells.get("created_at"), errors="coerce")
            sells = sells.sort_values("_ts")
            used: set = set()
            for p in partials:
                cands = sells[sells["ticker"] == p["ticker"]]
                for idx, row in cands.iterrows():
                    if idx in used:
                        continue
                    if int(_safe_num(row.get("quantity"))) == p["quantity"]:
                        ts = row.get("_ts")
                        if pd.notna(ts):
                            p["exit_date"] = ts.strftime("%Y-%m-%d")
                        used.add(idx)
                        break

    return partials


def build_realized_events(trades_df: pd.DataFrame, partials: list[dict]) -> list[dict]:
    """status=CLOSED のトレードと部分決済を統合した実現損益イベント一覧を返す。"""
    events: list[dict] = list(partials)
    if not trades_df.empty:
        closed = trades_df[trades_df["status"] == "CLOSED"]
        for _, r in closed.iterrows():
            events.append({
                "ticker": r["ticker"],
                "quantity": int(_safe_num(r.get("quantity"))),
                "entry_price": _safe_num(r.get("entry_price")),
                "exit_price": _safe_num(r.get("exit_price")),
                "pnl": _safe_num(r.get("pnl")),
                "pnl_pct": _safe_num(r.get("pnl_pct")),
                "strategy_name": r.get("strategy_name", "-"),
                "entry_date": str(r["entry_date"]) if pd.notna(r.get("entry_date")) else "-",
                "exit_date": str(r["exit_date"]) if pd.notna(r.get("exit_date")) else "-",
                "kind": "全数",
            })
    events.sort(key=lambda e: (e["exit_date"] or "0000-00-00"), reverse=True)
    return events


def compute_summary(dfs: dict[str, pd.DataFrame], events: list[dict]) -> dict:
    portfolio = dfs["portfolio_snapshots"]
    trades = dfs["trade_log"]

    s: dict = {
        "total_equity": 0.0, "cash": 0.0, "num_positions": 0,
        "start_equity": 0.0, "return_abs": 0.0, "return_pct": 0.0,
        "open_count": 0, "closed_count": 0, "total_pnl": 0.0,
        "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "latest_date": "-", "first_date": "-",
    }

    if not portfolio.empty:
        p = portfolio.sort_values("date").reset_index(drop=True)
        s["latest_date"] = str(p.iloc[-1]["date"])
        s["first_date"] = str(p.iloc[0]["date"])
        s["total_equity"] = _safe_num(p.iloc[-1]["total_equity"])
        s["cash"] = _safe_num(p.iloc[-1]["cash"])
        s["num_positions"] = int(_safe_num(p.iloc[-1]["num_positions"]))
        s["start_equity"] = _safe_num(p.iloc[0]["total_equity"])
        if s["start_equity"] > 0:
            s["return_abs"] = s["total_equity"] - s["start_equity"]
            s["return_pct"] = (s["total_equity"] / s["start_equity"] - 1) * 100

    if not trades.empty:
        s["open_count"] = int((trades["status"] == "OPEN").sum())

    if events:
        s["closed_count"] = len(events)
        s["total_pnl"] = float(sum(e["pnl"] for e in events))
        wins = [e["pnl"] for e in events if e["pnl"] > 0]
        losses = [e["pnl"] for e in events if e["pnl"] <= 0]
        s["win_rate"] = len(wins) / len(events) * 100
        s["avg_win"] = sum(wins) / len(wins) if wins else 0.0
        s["avg_loss"] = sum(losses) / len(losses) if losses else 0.0

    return s


def compute_strategy_perf(events: list[dict]) -> list[dict]:
    if not events:
        return []
    by_strat: dict[str, list[dict]] = {}
    for e in events:
        by_strat.setdefault(e["strategy_name"] or "-", []).append(e)
    rows: list[dict] = []
    for strat, items in by_strat.items():
        wins = sum(1 for e in items if e["pnl"] > 0)
        total_pnl = sum(e["pnl"] for e in items)
        avg_pnl = total_pnl / len(items)
        avg_pnl_pct = sum(e["pnl_pct"] for e in items) / len(items)
        rows.append({
            "strategy": strat,
            "count": len(items),
            "wins": wins,
            "win_rate": wins / len(items) * 100,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "avg_pnl_pct": avg_pnl_pct,
        })
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return rows


def _fmt_money(v: float, sign: bool = False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    sign_part = "+" if (sign and v >= 0) else ""
    return f"{sign_part}${v:,.2f}"


def _cls(v: float) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return "positive" if v > 0 else ("negative" if v < 0 else "")


def render_open_table(open_df: pd.DataFrame, latest_date: str) -> str:
    if open_df.empty:
        return "<p class='empty'>保有中のポジションはありません</p>"
    rows = []
    for _, r in open_df.iterrows():
        entry_d = pd.to_datetime(r["entry_date"]).date() if pd.notna(r["entry_date"]) else None
        try:
            latest = pd.to_datetime(latest_date).date()
            hold_days = (latest - entry_d).days if entry_d else "-"
        except Exception:
            hold_days = "-"
        entry_price = _safe_num(r.get("entry_price"))
        qty = int(_safe_num(r.get("quantity")))
        cost = entry_price * qty
        rows.append(
            f"<tr>"
            f"<td>{r['ticker']}</td>"
            f"<td class='num'>{qty}</td>"
            f"<td class='num'>${entry_price:,.2f}</td>"
            f"<td class='num'>${cost:,.2f}</td>"
            f"<td class='num'>${_safe_num(r.get('stop_loss')):,.2f}</td>"
            f"<td class='num'>${_safe_num(r.get('take_profit')):,.2f}</td>"
            f"<td>{r.get('strategy_name', '-')}</td>"
            f"<td>{r['entry_date']}</td>"
            f"<td class='num'>{hold_days}</td>"
            f"</tr>"
        )
    return (
        "<table class='tbl'>"
        "<thead><tr>"
        "<th>銘柄</th><th>株数</th><th>取得単価</th><th>取得額</th>"
        "<th>SL</th><th>TP</th><th>戦略</th><th>建玉日</th><th>保有日数</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_closed_table(events: list[dict]) -> str:
    if not events:
        return "<p class='empty'>決済済みトレードはありません</p>"
    rows = []
    for e in events:
        pnl = e["pnl"]
        pnl_pct = e["pnl_pct"]
        kind_cls = "kind-partial" if e["kind"] == "部分" else "kind-full"
        rows.append(
            f"<tr>"
            f"<td>{e['ticker']}</td>"
            f"<td><span class='kind {kind_cls}'>{e['kind']}</span></td>"
            f"<td class='num'>{e['quantity']}</td>"
            f"<td class='num'>${e['entry_price']:,.2f}</td>"
            f"<td class='num'>${e['exit_price']:,.2f}</td>"
            f"<td class='num {_cls(pnl)}'>{_fmt_money(pnl, sign=True)}</td>"
            f"<td class='num {_cls(pnl_pct)}'>{pnl_pct:+.2f}%</td>"
            f"<td>{e['strategy_name']}</td>"
            f"<td>{e['entry_date']}</td>"
            f"<td>{e['exit_date']}</td>"
            f"</tr>"
        )
    return (
        "<table class='tbl'>"
        "<thead><tr>"
        "<th>銘柄</th><th>区分</th><th>株数</th><th>建値</th><th>決済値</th>"
        "<th>損益</th><th>損益率</th><th>戦略</th><th>建玉日</th><th>決済日</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_strategy_table(perf: list[dict]) -> str:
    if not perf:
        return "<p class='empty'>戦略別の決済データはまだありません</p>"
    rows = []
    for r in perf:
        rows.append(
            f"<tr>"
            f"<td>{r['strategy']}</td>"
            f"<td class='num'>{r['count']}</td>"
            f"<td class='num'>{r['wins']}</td>"
            f"<td class='num'>{r['win_rate']:.1f}%</td>"
            f"<td class='num {_cls(r['total_pnl'])}'>{_fmt_money(r['total_pnl'], sign=True)}</td>"
            f"<td class='num {_cls(r['avg_pnl'])}'>{_fmt_money(r['avg_pnl'], sign=True)}</td>"
            f"<td class='num {_cls(r['avg_pnl_pct'])}'>{r['avg_pnl_pct']:+.2f}%</td>"
            f"</tr>"
        )
    return (
        "<table class='tbl'>"
        "<thead><tr>"
        "<th>戦略</th><th>件数</th><th>勝</th><th>勝率</th>"
        "<th>累計損益</th><th>平均損益</th><th>平均損益率</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def build_chart_data(dfs: dict[str, pd.DataFrame], events: list[dict]) -> dict:
    portfolio = dfs["portfolio_snapshots"]
    market = dfs["market_conditions"]

    chart: dict = {"equity": None, "market": None, "ticker_pnl": None}

    if not portfolio.empty:
        p = portfolio.sort_values("date").reset_index(drop=True)
        chart["equity"] = {
            "x": p["date"].astype(str).tolist(),
            "equity": [_safe_num(v) for v in p["total_equity"].tolist()],
            "cash": [_safe_num(v) for v in p["cash"].tolist()],
        }

    if not market.empty:
        m = market.sort_values("date").reset_index(drop=True)
        chart["market"] = {
            "x": m["date"].astype(str).tolist(),
            "vix": [_safe_num(v) for v in m["vix_level"].tolist()],
            "regime": m["regime"].astype(str).tolist(),
            "trend": m["sp500_trend"].astype(str).tolist(),
        }

    if events:
        agg: dict[str, float] = {}
        for e in events:
            agg[e["ticker"]] = agg.get(e["ticker"], 0.0) + e["pnl"]
        sorted_items = sorted(agg.items(), key=lambda kv: kv[1])
        chart["ticker_pnl"] = {
            "tickers": [k for k, _ in sorted_items],
            "pnl": [float(v) for _, v in sorted_items],
        }

    return chart


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>autoTRD 取引ダッシュボード</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{--bg:#1a1c1e;--card:#22262b;--border:#2e333a;--text:#d4cfc8;--sub:#8a8a8a;--head:#c8c0b4;--accent:#5fa3d0;--pos:#5a8a6a;--neg:#a05050;--gold:#c8a84a}
  body{background:var(--bg);color:var(--text);font-family:'SF Pro Text','Hiragino Sans','Meiryo',system-ui,sans-serif;line-height:1.6;padding:24px;font-size:14px}
  h1{font-size:1.6rem;color:var(--head);margin-bottom:4px}
  h2{font-size:1.15rem;color:var(--head);margin:0 0 12px;padding-left:10px;border-left:3px solid var(--accent)}
  .meta{color:var(--sub);font-size:.85rem;margin-bottom:24px}
  .section{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:20px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
  .card .label{color:var(--sub);font-size:.78rem;letter-spacing:.04em;margin-bottom:6px}
  .card .value{font-size:1.5rem;font-weight:600;color:var(--head)}
  .card .sub{color:var(--sub);font-size:.78rem;margin-top:4px}
  .positive{color:var(--pos)}
  .negative{color:var(--neg)}
  .tbl{width:100%;border-collapse:collapse;font-size:.85rem}
  .tbl th,.tbl td{padding:8px 12px;border-bottom:1px solid var(--border);text-align:left}
  .tbl th{color:var(--sub);font-weight:500;font-size:.78rem;letter-spacing:.04em;text-transform:uppercase}
  .tbl td.num{text-align:right;font-variant-numeric:tabular-nums}
  .tbl tbody tr:hover{background:rgba(95,163,208,.06)}
  .empty{color:var(--sub);padding:12px;text-align:center;font-style:italic}
  .chart{width:100%;height:340px}
  .kind{display:inline-block;padding:1px 8px;border-radius:4px;font-size:.72rem;letter-spacing:.04em}
  .kind-full{background:rgba(95,163,208,.18);color:#9ec6e0}
  .kind-partial{background:rgba(200,168,74,.18);color:#d6bb6c}
</style>
</head>
<body>
  <h1>autoTRD 取引ダッシュボード</h1>
  <div class="meta">期間: __FIRST_DATE__ ～ __LATEST_DATE__ ／ 生成: __GEN_AT__</div>

  <div class="cards">
    <div class="card"><div class="label">総資産</div><div class="value">__TOTAL_EQUITY__</div><div class="sub">現金 __CASH__</div></div>
    <div class="card"><div class="label">期間リターン</div><div class="value __RETURN_CLS__">__RETURN_PCT__</div><div class="sub __RETURN_CLS__">__RETURN_ABS__</div></div>
    <div class="card"><div class="label">保有ポジション</div><div class="value">__NUM_POS__</div><div class="sub">OPEN件数 __OPEN_COUNT__</div></div>
    <div class="card"><div class="label">累計実現損益</div><div class="value __PNL_CLS__">__TOTAL_PNL__</div><div class="sub">決済 __CLOSED_COUNT__件</div></div>
    <div class="card"><div class="label">勝率</div><div class="value">__WIN_RATE__</div><div class="sub">平均勝 __AVG_WIN__ / 平均負 __AVG_LOSS__</div></div>
  </div>

  <div class="section">
    <h2>エクイティカーブ</h2>
    <div id="equity-chart" class="chart"></div>
  </div>

  <div class="section">
    <h2>保有ポジション (OPEN)</h2>
    __OPEN_TABLE__
  </div>

  <div class="section">
    <h2>決済履歴 (CLOSED)</h2>
    __CLOSED_TABLE__
  </div>

  <div class="section">
    <h2>銘柄別 累計損益</h2>
    <div id="ticker-chart" class="chart"></div>
  </div>

  <div class="section">
    <h2>戦略別パフォーマンス</h2>
    __STRATEGY_TABLE__
  </div>

  <div class="section">
    <h2>市場環境推移 (VIX)</h2>
    <div id="market-chart" class="chart"></div>
  </div>

<script>
const CHART_DATA = __CHART_JSON__;

const layoutBase = {
  paper_bgcolor: '#22262b',
  plot_bgcolor: '#22262b',
  font: { color: '#d4cfc8', family: 'SF Pro Text, system-ui, sans-serif', size: 12 },
  margin: { t: 20, r: 30, b: 50, l: 60 },
  xaxis: { gridcolor: '#2e333a', linecolor: '#2e333a' },
  yaxis: { gridcolor: '#2e333a', linecolor: '#2e333a' },
  legend: { orientation: 'h', y: -0.2 },
};

if (CHART_DATA.equity) {
  Plotly.newPlot('equity-chart', [
    { x: CHART_DATA.equity.x, y: CHART_DATA.equity.equity, name: '総資産', type: 'scatter', mode: 'lines+markers', line: { color: '#5fa3d0', width: 2 } },
    { x: CHART_DATA.equity.x, y: CHART_DATA.equity.cash, name: '現金', type: 'scatter', mode: 'lines', line: { color: '#c8a84a', width: 1, dash: 'dot' } },
  ], { ...layoutBase, yaxis: { ...layoutBase.yaxis, tickprefix: '$' } }, { displayModeBar: false, responsive: true });
} else {
  document.getElementById('equity-chart').innerHTML = '<p class="empty">portfolio_snapshots データがありません</p>';
}

if (CHART_DATA.ticker_pnl) {
  const colors = CHART_DATA.ticker_pnl.pnl.map(v => v >= 0 ? '#5a8a6a' : '#a05050');
  Plotly.newPlot('ticker-chart', [
    { x: CHART_DATA.ticker_pnl.pnl, y: CHART_DATA.ticker_pnl.tickers, type: 'bar', orientation: 'h', marker: { color: colors } },
  ], { ...layoutBase, xaxis: { ...layoutBase.xaxis, tickprefix: '$' } }, { displayModeBar: false, responsive: true });
} else {
  document.getElementById('ticker-chart').innerHTML = '<p class="empty">決済済みトレードがまだありません</p>';
}

if (CHART_DATA.market) {
  Plotly.newPlot('market-chart', [
    { x: CHART_DATA.market.x, y: CHART_DATA.market.vix, name: 'VIX', type: 'scatter', mode: 'lines+markers', line: { color: '#c8a84a' }, text: CHART_DATA.market.regime, hovertemplate: '%{x}<br>VIX %{y:.1f}<br>regime %{text}<extra></extra>' },
  ], layoutBase, { displayModeBar: false, responsive: true });
} else {
  document.getElementById('market-chart').innerHTML = '<p class="empty">market_conditions データがありません</p>';
}
</script>
</body>
</html>
"""


def render_html(
    dfs: dict[str, pd.DataFrame],
    summary: dict,
    strategy_perf: list[dict],
    events: list[dict],
) -> str:
    trades = dfs["trade_log"]
    open_df = trades[trades["status"] == "OPEN"].copy() if not trades.empty else pd.DataFrame()

    chart_json = json.dumps(build_chart_data(dfs, events), ensure_ascii=False)

    html = HTML_TEMPLATE
    replacements = {
        "__FIRST_DATE__": summary["first_date"],
        "__LATEST_DATE__": summary["latest_date"],
        "__GEN_AT__": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "__TOTAL_EQUITY__": _fmt_money(summary["total_equity"]),
        "__CASH__": _fmt_money(summary["cash"]),
        "__RETURN_PCT__": f"{summary['return_pct']:+.2f}%",
        "__RETURN_ABS__": _fmt_money(summary["return_abs"], sign=True),
        "__RETURN_CLS__": _cls(summary["return_pct"]),
        "__NUM_POS__": str(summary["num_positions"]),
        "__OPEN_COUNT__": str(summary["open_count"]),
        "__TOTAL_PNL__": _fmt_money(summary["total_pnl"], sign=True),
        "__PNL_CLS__": _cls(summary["total_pnl"]),
        "__CLOSED_COUNT__": str(summary["closed_count"]),
        "__WIN_RATE__": f"{summary['win_rate']:.1f}%" if summary["closed_count"] else "-",
        "__AVG_WIN__": _fmt_money(summary["avg_win"], sign=True) if summary["avg_win"] else "-",
        "__AVG_LOSS__": _fmt_money(summary["avg_loss"], sign=True) if summary["avg_loss"] else "-",
        "__OPEN_TABLE__": render_open_table(open_df, summary["latest_date"]),
        "__CLOSED_TABLE__": render_closed_table(events),
        "__STRATEGY_TABLE__": render_strategy_table(strategy_perf),
        "__CHART_JSON__": chart_json,
    }
    for key, val in replacements.items():
        html = html.replace(key, val)
    return html


def main() -> None:
    if not CSV_DIR.exists():
        raise SystemExit(f"CSV ディレクトリが見つかりません: {CSV_DIR}\n先に `bash scripts/sync_db_csv.sh` を実行してください。")

    dfs = load_csvs()
    if all(df.empty for df in dfs.values()):
        raise SystemExit(f"CSV が空です: {CSV_DIR}\nVPS で `bash scripts/export_db_csv.sh` を実行してから同期してください。")

    partials = extract_partial_exits(dfs["trade_log"], dfs["orders"])
    events = build_realized_events(dfs["trade_log"], partials)
    summary = compute_summary(dfs, events)
    strategy_perf = compute_strategy_perf(events)
    html = render_html(dfs, summary, strategy_perf, events)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"✅ ダッシュボード生成完了: {OUTPUT}")
    print(f"   ブラウザで開く: open {OUTPUT}")


if __name__ == "__main__":
    main()
