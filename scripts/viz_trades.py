#!/usr/bin/env python3
"""DBエクスポートCSVから取引ダッシュボードHTMLを生成。

使い方:
    python3 scripts/viz_trades.py
    python3 scripts/viz_trades.py --start 2026-04-01 --end 2026-05-15
    open data/dashboard.html

CLI で渡した --start / --end は、ブラウザの日付ピッカーの初期値に反映される。
画面上で日付を変更すれば、その場でカード/表/グラフがリフィルタされる
（クライアント側で再計算するため再生成は不要）。

依存: pandas のみ（Plotly は CDN 経由で読込）
入力: data/csv_export/*.csv
出力: data/dashboard.html
"""
from __future__ import annotations

import argparse
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
    """trade_log.notes から段階決済を抽出（exit_date は orders から補完）。"""
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
    return events


def _recompute_snapshot(row: pd.Series) -> dict:
    """qty=0 の残骸（端株）を除外して total_equity / num_positions を再計算。

    端株 (株式分割や配当再投資の残り) は moomoo API の total_assets には含まれるが
    画面の「資産総額」では除外されているため、記録済みスナップショットも同じ基準で
    補正する。新規記録は account.py 側で既に補正済みなので本処理は冪等。
    """
    raw_total = _safe_num(row.get("total_equity"))
    cash = _safe_num(row.get("cash"))
    raw_num = int(_safe_num(row.get("num_positions")))
    pj = row.get("positions_json")
    if pd.isna(pj) or not str(pj).strip():
        return {"total_equity": raw_total, "cash": cash, "num_positions": raw_num}
    try:
        positions = json.loads(pj)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"total_equity": raw_total, "cash": cash, "num_positions": raw_num}
    if not positions:
        # 空配列は「ポジション詳細なし」を意味する（backfill 由来）。
        # raw_total をそのまま返し、cash / num_positions は既存値を保持。
        return {"total_equity": raw_total, "cash": cash, "num_positions": raw_num}
    real_count = 0
    residual_mv = 0.0
    for p in positions:
        try:
            qty = int(p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            real_count += 1
        else:
            residual_mv += _safe_num(p.get("market_value"))
    return {
        "total_equity": round(raw_total - residual_mv, 2),
        "cash": round(cash, 2),
        "num_positions": real_count,
    }


def _extract_held_positions(pj_raw) -> list[dict]:
    """positions_json から qty>0 の銘柄のみ抽出（端株を除外、ticker は US. プレフィックス除去）。"""
    if pd.isna(pj_raw) or not str(pj_raw).strip():
        return []
    try:
        positions = json.loads(pj_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    held: list[dict] = []
    for pos in positions:
        try:
            qty = int(pos.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        ticker = str(pos.get("ticker", "")).replace("US.", "")
        held.append({
            "ticker": ticker,
            "qty": qty,
            "avg_price": _safe_num(pos.get("avg_price")),
            "market_value": _safe_num(pos.get("market_value")),
        })
    return held


def build_raw_data(dfs: dict[str, pd.DataFrame], events: list[dict]) -> dict:
    """ブラウザに渡す全データ（フィルタ前）を組み立てる。"""
    portfolio_rows: list[dict] = []
    if not dfs["portfolio_snapshots"].empty:
        p = dfs["portfolio_snapshots"].sort_values("date").reset_index(drop=True)
        for _, r in p.iterrows():
            corr = _recompute_snapshot(r)
            portfolio_rows.append({
                "date": str(r["date"]),
                "total_equity": corr["total_equity"],
                "cash": corr["cash"],
                "num_positions": corr["num_positions"],
                "positions": _extract_held_positions(r.get("positions_json")),
            })

    market_rows: list[dict] = []
    if not dfs["market_conditions"].empty:
        m = dfs["market_conditions"].sort_values("date").reset_index(drop=True)
        for _, r in m.iterrows():
            market_rows.append({
                "date": str(r["date"]),
                "vix": round(_safe_num(r.get("vix_level")), 2),
                "regime": str(r.get("regime", "-")),
                "trend": str(r.get("sp500_trend", "-")),
            })

    # trade_log の status=OPEN 行は「現在保有中の銘柄リスト」としては信頼できない
    # (close_trade_log の更新漏れで古いゴミ行が残ることがあるため)。
    # ここでは銘柄ごとに SL/TP/戦略/建玉日をルックアップする辞書として保持し、
    # 実際の保有銘柄リストは portfolio_snapshot.positions_json を信頼源とする (JS 側で結合)。
    open_trade_lookup: list[dict] = []
    trades = dfs["trade_log"]
    if not trades.empty:
        open_df = trades[trades["status"] == "OPEN"]
        for _, r in open_df.iterrows():
            entry_d = pd.to_datetime(r.get("entry_date"), errors="coerce")
            entry_date = entry_d.strftime("%Y-%m-%d") if pd.notna(entry_d) else "-"
            open_trade_lookup.append({
                "ticker": str(r["ticker"]).replace("US.", ""),
                "stop_loss": _safe_num(r.get("stop_loss")),
                "take_profit": _safe_num(r.get("take_profit")),
                "strategy_name": str(r.get("strategy_name", "-")),
                "entry_date": entry_date,
            })

    all_dates: list[str] = []
    all_dates += [r["date"] for r in portfolio_rows]
    all_dates += [r["date"] for r in market_rows]
    all_dates += [e["exit_date"] for e in events if e.get("exit_date") and e["exit_date"] != "-"]
    all_dates_sorted = sorted({d for d in all_dates if d})

    return {
        "portfolio": portfolio_rows,
        "market": market_rows,
        "events": events,
        "open_trade_lookup": open_trade_lookup,
        "data_min": all_dates_sorted[0] if all_dates_sorted else None,
        "data_max": all_dates_sorted[-1] if all_dates_sorted else None,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>autoTRD 取引ダッシュボード</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{--bg:#f5efe6;--card:#faf6ed;--border:#e6dcc8;--text:#3a3530;--sub:#8a8170;--head:#4a4238;--accent:#7a8b5c;--pos:#6b8e5a;--neg:#b06848;--gold:#c4a253}
  body{background:var(--bg);color:var(--text);font-family:'SF Pro Text','Hiragino Sans','Meiryo',system-ui,sans-serif;line-height:1.6;padding:24px;font-size:14px}
  h1{font-size:1.6rem;color:var(--head);margin-bottom:12px;font-weight:600}
  h2{font-size:1.15rem;color:var(--head);margin:0 0 12px;padding-left:10px;border-left:3px solid var(--accent);font-weight:600}
  .controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:18px;box-shadow:0 1px 2px rgba(74,66,56,.04)}
  .controls label{color:var(--sub);font-size:.82rem;letter-spacing:.04em}
  .controls input[type="date"]{padding:5px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-family:inherit;font-size:.9rem;color-scheme:light}
  .controls input[type="date"]:focus{outline:none;border-color:var(--accent)}
  .controls .sep{color:var(--sub)}
  .controls button{padding:5px 12px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-family:inherit;font-size:.82rem;cursor:pointer;transition:background .12s,color .12s,border-color .12s}
  .controls button:hover{background:var(--accent);color:#faf6ed;border-color:var(--accent)}
  .controls .meta-info{color:var(--sub);font-size:.82rem;margin-left:auto}
  .section{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:20px;box-shadow:0 1px 2px rgba(74,66,56,.04)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;box-shadow:0 1px 2px rgba(74,66,56,.04)}
  .card .label{color:var(--sub);font-size:.78rem;letter-spacing:.04em;margin-bottom:6px}
  .card .label .asof{font-size:.72rem;letter-spacing:0;opacity:.85}
  .card .value{font-size:1.5rem;font-weight:600;color:var(--head)}
  .card .sub{color:var(--sub);font-size:.78rem;margin-top:4px}
  .positive{color:var(--pos)}
  .negative{color:var(--neg)}
  .tbl{width:100%;border-collapse:collapse;font-size:.85rem}
  .tbl th,.tbl td{padding:8px 12px;border-bottom:1px solid var(--border);text-align:left}
  .tbl th{color:var(--sub);font-weight:500;font-size:.78rem;letter-spacing:.04em;text-transform:uppercase}
  .tbl td.num{text-align:right;font-variant-numeric:tabular-nums}
  .tbl tbody tr:hover{background:rgba(122,139,92,.08)}
  .empty{color:var(--sub);padding:12px;text-align:center;font-style:italic}
  .chart{width:100%;height:340px}
  .kind{display:inline-block;padding:1px 8px;border-radius:4px;font-size:.72rem;letter-spacing:.04em}
  .kind-full{background:rgba(122,139,92,.18);color:#5e6e44}
  .kind-partial{background:rgba(196,162,83,.22);color:#8a6f30}
</style>
</head>
<body>
  <h1>autoTRD 取引ダッシュボード</h1>

  <div class="controls">
    <label for="start-date">期間</label>
    <input type="date" id="start-date" />
    <span class="sep">～</span>
    <input type="date" id="end-date" />
    <button type="button" id="reset-btn" title="全期間に戻す">全期間</button>
    <span class="meta-info" id="meta-info"></span>
  </div>

  <div class="cards" id="summary-cards"></div>

  <div class="section">
    <h2>エクイティカーブ</h2>
    <div id="equity-chart" class="chart"></div>
  </div>

  <div class="section">
    <h2>保有ポジション (OPEN)</h2>
    <div id="open-table-wrap"></div>
  </div>

  <div class="section">
    <h2>決済履歴 (CLOSED)</h2>
    <div id="closed-table-wrap"></div>
  </div>

  <div class="section">
    <h2>銘柄別 累計損益</h2>
    <div id="ticker-chart" class="chart"></div>
  </div>

  <div class="section">
    <h2>戦略別パフォーマンス</h2>
    <div id="strategy-table-wrap"></div>
  </div>

  <div class="section">
    <h2>市場環境推移 (VIX)</h2>
    <div id="market-chart" class="chart"></div>
  </div>

<script>
const RAW = __RAW_JSON__;
const INITIAL = __INITIAL_JSON__;
const GEN_AT = "__GEN_AT__";

const COLORS = {
  bg: '#faf6ed', text: '#3a3530', grid: '#e6dcc8', line: '#d6c9b0',
  accent: '#7a8b5c', gold: '#c4a253', pos: '#6b8e5a', neg: '#b06848',
};

const layoutBase = {
  paper_bgcolor: COLORS.bg, plot_bgcolor: COLORS.bg,
  font: { color: COLORS.text, family: 'SF Pro Text, system-ui, sans-serif', size: 12 },
  margin: { t: 20, r: 30, b: 50, l: 60 },
  xaxis: { gridcolor: COLORS.grid, linecolor: COLORS.line, zerolinecolor: COLORS.line },
  yaxis: { gridcolor: COLORS.grid, linecolor: COLORS.line, zerolinecolor: COLORS.line },
  legend: { orientation: 'h', y: -0.2 },
};

function safeNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}
function fmtMoney(v, sign = false) {
  if (v == null || !Number.isFinite(Number(v))) return '-';
  const n = Number(v);
  const s = (sign && n >= 0) ? '+' : '';
  return s + '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPct(v) {
  if (v == null || !Number.isFinite(Number(v))) return '-';
  const n = Number(v);
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}
function cls(v) {
  if (v == null || !Number.isFinite(Number(v))) return '';
  const n = Number(v);
  return n > 0 ? 'positive' : (n < 0 ? 'negative' : '');
}
function inRange(d, start, end) {
  if (!d || d === '-') return false;
  if (start && d < start) return false;
  if (end && d > end) return false;
  return true;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}

function computeSummary(portfolio, events, openCount) {
  const s = {
    totalEquity: 0, cash: 0, numPositions: 0,
    startEquity: 0, returnAbs: 0, returnPct: 0,
    openCount: openCount, closedCount: 0, totalPnl: 0,
    winRate: 0, avgWin: 0, avgLoss: 0,
    latestDate: '-', firstDate: '-',
  };
  if (portfolio.length) {
    const sorted = portfolio.slice().sort((a, b) => a.date.localeCompare(b.date));
    const last = sorted[sorted.length - 1];
    s.firstDate = sorted[0].date;
    s.latestDate = last.date;
    s.totalEquity = safeNum(last.total_equity);
    s.cash = safeNum(last.cash);
    s.numPositions = safeNum(last.num_positions);
    s.startEquity = safeNum(sorted[0].total_equity);
    if (s.startEquity > 0) {
      s.returnAbs = s.totalEquity - s.startEquity;
      s.returnPct = (s.totalEquity / s.startEquity - 1) * 100;
    }
  }
  if (events.length) {
    s.closedCount = events.length;
    s.totalPnl = events.reduce((a, e) => a + safeNum(e.pnl), 0);
    const wins = events.filter(e => safeNum(e.pnl) > 0).map(e => safeNum(e.pnl));
    const losses = events.filter(e => safeNum(e.pnl) <= 0).map(e => safeNum(e.pnl));
    s.winRate = wins.length / events.length * 100;
    s.avgWin = wins.length ? wins.reduce((a, v) => a + v, 0) / wins.length : 0;
    s.avgLoss = losses.length ? losses.reduce((a, v) => a + v, 0) / losses.length : 0;
  }
  return s;
}

function renderCards(s) {
  const asOf = s.latestDate && s.latestDate !== '-' ? `${s.latestDate}時点` : '-';
  const diffSub = (s.firstDate !== '-' && s.latestDate !== '-')
    ? `期首 ${fmtMoney(s.startEquity)} → 期末 ${fmtMoney(s.totalEquity)}`
    : '-';
  document.getElementById('summary-cards').innerHTML = `
    <div class="card"><div class="label">総資産 <span class="asof">(${asOf})</span></div><div class="value">${fmtMoney(s.totalEquity)}</div><div class="sub">現金 ${fmtMoney(s.cash)}</div></div>
    <div class="card"><div class="label">期間差額 (期首→期末)</div><div class="value ${cls(s.returnAbs)}">${fmtMoney(s.returnAbs, true)}</div><div class="sub">${diffSub}</div></div>
    <div class="card"><div class="label">期間リターン</div><div class="value ${cls(s.returnPct)}">${fmtPct(s.returnPct)}</div><div class="sub ${cls(s.returnPct)}">${fmtMoney(s.returnAbs, true)}</div></div>
    <div class="card"><div class="label">保有ポジション <span class="asof">(${asOf})</span></div><div class="value">${s.numPositions}</div><div class="sub">OPEN件数 ${s.openCount}</div></div>
    <div class="card"><div class="label">累計実現損益</div><div class="value ${cls(s.totalPnl)}">${fmtMoney(s.totalPnl, true)}</div><div class="sub">決済 ${s.closedCount}件</div></div>
    <div class="card"><div class="label">勝率</div><div class="value">${s.closedCount ? s.winRate.toFixed(1) + '%' : '-'}</div><div class="sub">平均勝 ${s.avgWin ? fmtMoney(s.avgWin, true) : '-'} / 平均負 ${s.avgLoss ? fmtMoney(s.avgLoss, true) : '-'}</div></div>
  `;
}

function buildOpenFromSnapshot(snapshot) {
  // snapshot.positions (qty>0、実際の保有) を正本にし、trade_log から
  // SL/TP/戦略/建玉日を ticker 単位でルックアップして合成する。
  // 同一 ticker の OPEN trade_log 行が複数ある場合は最新の建玉日を採用。
  if (!snapshot || !snapshot.positions || !snapshot.positions.length) return [];
  const lookup = {};
  for (const t of (RAW.open_trade_lookup || [])) {
    const key = t.ticker;
    if (!lookup[key] || (t.entry_date || '') > (lookup[key].entry_date || '')) {
      lookup[key] = t;
    }
  }
  return snapshot.positions.map(pos => {
    const t = lookup[pos.ticker] || {};
    return {
      ticker: pos.ticker,
      quantity: pos.qty,
      entry_price: pos.avg_price,
      stop_loss: t.stop_loss != null ? t.stop_loss : 0,
      take_profit: t.take_profit != null ? t.take_profit : 0,
      strategy_name: t.strategy_name || '-',
      entry_date: t.entry_date || '-',
    };
  });
}

function renderOpenTable(rows, latestDate) {
  const wrap = document.getElementById('open-table-wrap');
  if (!rows.length) {
    wrap.innerHTML = "<p class='empty'>保有中のポジションはありません</p>";
    return;
  }
  const trs = rows.map(r => {
    const cost = safeNum(r.entry_price) * safeNum(r.quantity);
    let holdDays = '-';
    if (latestDate && latestDate !== '-' && r.entry_date && r.entry_date !== '-') {
      const e = new Date(r.entry_date);
      const l = new Date(latestDate);
      if (!Number.isNaN(e.getTime()) && !Number.isNaN(l.getTime())) {
        holdDays = Math.round((l - e) / 86400000);
      }
    }
    return `<tr>
      <td>${esc(r.ticker)}</td>
      <td class='num'>${r.quantity}</td>
      <td class='num'>${fmtMoney(r.entry_price)}</td>
      <td class='num'>${fmtMoney(cost)}</td>
      <td class='num'>${fmtMoney(r.stop_loss)}</td>
      <td class='num'>${fmtMoney(r.take_profit)}</td>
      <td>${esc(r.strategy_name || '-')}</td>
      <td>${esc(r.entry_date)}</td>
      <td class='num'>${holdDays}</td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `<table class='tbl'><thead><tr>
    <th>銘柄</th><th>株数</th><th>取得単価</th><th>取得額</th>
    <th>SL</th><th>TP</th><th>戦略</th><th>建玉日</th><th>保有日数</th>
  </tr></thead><tbody>${trs}</tbody></table>`;
}

function renderClosedTable(events) {
  const wrap = document.getElementById('closed-table-wrap');
  if (!events.length) {
    wrap.innerHTML = "<p class='empty'>決済済みトレードはありません</p>";
    return;
  }
  const sorted = events.slice().sort((a, b) => (b.exit_date || '').localeCompare(a.exit_date || ''));
  const trs = sorted.map(e => {
    const kindCls = e.kind === '部分' ? 'kind-partial' : 'kind-full';
    return `<tr>
      <td>${esc(e.ticker)}</td>
      <td><span class='kind ${kindCls}'>${esc(e.kind)}</span></td>
      <td class='num'>${e.quantity}</td>
      <td class='num'>${fmtMoney(e.entry_price)}</td>
      <td class='num'>${fmtMoney(e.exit_price)}</td>
      <td class='num ${cls(e.pnl)}'>${fmtMoney(e.pnl, true)}</td>
      <td class='num ${cls(e.pnl_pct)}'>${fmtPct(e.pnl_pct)}</td>
      <td>${esc(e.strategy_name)}</td>
      <td>${esc(e.entry_date)}</td>
      <td>${esc(e.exit_date)}</td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `<table class='tbl'><thead><tr>
    <th>銘柄</th><th>区分</th><th>株数</th><th>建値</th><th>決済値</th>
    <th>損益</th><th>損益率</th><th>戦略</th><th>建玉日</th><th>決済日</th>
  </tr></thead><tbody>${trs}</tbody></table>`;
}

function renderStrategyTable(events) {
  const wrap = document.getElementById('strategy-table-wrap');
  if (!events.length) {
    wrap.innerHTML = "<p class='empty'>戦略別の決済データはまだありません</p>";
    return;
  }
  const byStrat = {};
  for (const e of events) {
    const key = e.strategy_name || '-';
    if (!byStrat[key]) byStrat[key] = [];
    byStrat[key].push(e);
  }
  const rows = Object.entries(byStrat).map(([strat, items]) => {
    const wins = items.filter(e => safeNum(e.pnl) > 0).length;
    const totalPnl = items.reduce((a, e) => a + safeNum(e.pnl), 0);
    const avgPnl = totalPnl / items.length;
    const avgPnlPct = items.reduce((a, e) => a + safeNum(e.pnl_pct), 0) / items.length;
    return { strat, count: items.length, wins, winRate: wins / items.length * 100, totalPnl, avgPnl, avgPnlPct };
  }).sort((a, b) => b.totalPnl - a.totalPnl);
  const trs = rows.map(r => `<tr>
    <td>${esc(r.strat)}</td>
    <td class='num'>${r.count}</td>
    <td class='num'>${r.wins}</td>
    <td class='num'>${r.winRate.toFixed(1)}%</td>
    <td class='num ${cls(r.totalPnl)}'>${fmtMoney(r.totalPnl, true)}</td>
    <td class='num ${cls(r.avgPnl)}'>${fmtMoney(r.avgPnl, true)}</td>
    <td class='num ${cls(r.avgPnlPct)}'>${fmtPct(r.avgPnlPct)}</td>
  </tr>`).join('');
  wrap.innerHTML = `<table class='tbl'><thead><tr>
    <th>戦略</th><th>件数</th><th>勝</th><th>勝率</th>
    <th>累計損益</th><th>平均損益</th><th>平均損益率</th>
  </tr></thead><tbody>${trs}</tbody></table>`;
}

function renderEquityChart(rows) {
  const el = document.getElementById('equity-chart');
  if (!rows.length) {
    el.innerHTML = '<p class="empty">対象期間に portfolio_snapshots データがありません</p>';
    return;
  }
  const sorted = rows.slice().sort((a, b) => a.date.localeCompare(b.date));
  const x = sorted.map(r => r.date);
  const y = sorted.map(r => safeNum(r.total_equity));

  const yMin = Math.min(...y);
  const yMax = Math.max(...y);
  const span = yMax - yMin;
  const pad = span > 0 ? span * 0.08 : Math.max(yMax * 0.01, 10);
  const yRange = [yMin - pad, yMax + pad];

  const startVal = y[0];
  const endVal = y[y.length - 1];
  const startDate = x[0];
  const endDate = x[x.length - 1];
  const fmt = v => '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  Plotly.newPlot(el, [
    { x, y, name: '総資産', type: 'scatter', mode: 'lines+markers',
      line: { color: COLORS.accent, width: 2, shape: 'linear' },
      marker: { color: COLORS.accent, size: 5 },
      fill: 'tozeroy',
      fillcolor: 'rgba(122,139,92,0.12)',
      hovertemplate: '%{x|%Y-%m-%d}<br>%{y:$,.2f}<extra></extra>' },
  ], {
    ...layoutBase,
    xaxis: { ...layoutBase.xaxis, type: 'date' },
    yaxis: { ...layoutBase.yaxis, type: 'linear', tickformat: '$,.0f', range: yRange },
    showlegend: false,
    annotations: [
      { x: startDate, y: startVal, text: `期首: ${fmt(startVal)}`,
        showarrow: false, xanchor: 'left', yanchor: 'bottom', xshift: 6, yshift: 6,
        font: { size: 11, color: COLORS.text },
        bgcolor: 'rgba(250,246,237,0.85)', borderpad: 3 },
      { x: endDate, y: endVal, text: `期末: ${fmt(endVal)}`,
        showarrow: false, xanchor: 'right', yanchor: 'top', xshift: -6, yshift: -6,
        font: { size: 11, color: COLORS.text },
        bgcolor: 'rgba(250,246,237,0.85)', borderpad: 3 },
    ],
  }, { displayModeBar: false, responsive: true });
}

function renderTickerChart(events) {
  const el = document.getElementById('ticker-chart');
  if (!events.length) {
    el.innerHTML = '<p class="empty">対象期間に決済済みトレードがありません</p>';
    return;
  }
  const agg = {};
  for (const e of events) agg[e.ticker] = (agg[e.ticker] || 0) + safeNum(e.pnl);
  const sorted = Object.entries(agg).sort((a, b) => a[1] - b[1]);
  const tickers = sorted.map(x => x[0]);
  const pnls = sorted.map(x => x[1]);
  const colors = pnls.map(v => v >= 0 ? COLORS.pos : COLORS.neg);
  Plotly.newPlot(el, [
    { x: pnls, y: tickers, type: 'bar', orientation: 'h', marker: { color: colors } },
  ], {
    ...layoutBase,
    xaxis: { ...layoutBase.xaxis, type: 'linear', tickformat: '$,.0f' },
    yaxis: { ...layoutBase.yaxis, type: 'category' },
  }, { displayModeBar: false, responsive: true });
}

function renderMarketChart(rows) {
  const el = document.getElementById('market-chart');
  if (!rows.length) {
    el.innerHTML = '<p class="empty">対象期間に market_conditions データがありません</p>';
    return;
  }
  const sorted = rows.slice().sort((a, b) => a.date.localeCompare(b.date));
  Plotly.newPlot(el, [
    { x: sorted.map(r => r.date), y: sorted.map(r => r.vix), name: 'VIX',
      type: 'scatter', mode: 'lines+markers',
      line: { color: COLORS.neg, width: 1.8 }, marker: { color: COLORS.neg, size: 5 },
      text: sorted.map(r => r.regime),
      hovertemplate: '%{x}<br>VIX %{y:.1f}<br>regime %{text}<extra></extra>' },
  ], {
    ...layoutBase,
    xaxis: { ...layoutBase.xaxis, type: 'date' },
    yaxis: { ...layoutBase.yaxis, type: 'linear' },
  }, { displayModeBar: false, responsive: true });
}

function refresh() {
  const startEl = document.getElementById('start-date');
  const endEl = document.getElementById('end-date');
  let start = startEl.value || null;
  let end = endEl.value || null;
  if (start && end && start > end) {
    [start, end] = [end, start];
    startEl.value = start;
    endEl.value = end;
  }

  const portfolio = RAW.portfolio.filter(r => inRange(r.date, start, end));
  const market = RAW.market.filter(r => inRange(r.date, start, end));
  const events = RAW.events.filter(e => inRange(e.exit_date, start, end));
  // OPEN ポジションは「対象期間末端のスナップショット」を信頼源にする
  // (trade_log の status=OPEN は更新漏れがあるため使わない)
  const sortedPort = portfolio.slice().sort((a, b) => a.date.localeCompare(b.date));
  const lastSnap = sortedPort.length ? sortedPort[sortedPort.length - 1] : null;
  const openPos = buildOpenFromSnapshot(lastSnap);

  const summary = computeSummary(portfolio, events, openPos.length);
  renderCards(summary);

  const periodLabel = `${start || RAW.data_min || '-'} ～ ${end || RAW.data_max || '-'}`;
  document.getElementById('meta-info').textContent = `期間: ${periodLabel} ／ 生成: ${GEN_AT}`;

  renderOpenTable(openPos, summary.latestDate);
  renderClosedTable(events);
  renderStrategyTable(events);
  renderEquityChart(portfolio);
  renderTickerChart(events);
  renderMarketChart(market);
}

function init() {
  const startEl = document.getElementById('start-date');
  const endEl = document.getElementById('end-date');
  if (RAW.data_min) { startEl.min = RAW.data_min; endEl.min = RAW.data_min; }
  if (RAW.data_max) { startEl.max = RAW.data_max; endEl.max = RAW.data_max; }
  startEl.value = INITIAL.start || RAW.data_min || '';
  endEl.value = INITIAL.end || RAW.data_max || '';

  startEl.addEventListener('change', refresh);
  endEl.addEventListener('change', refresh);
  document.getElementById('reset-btn').addEventListener('click', () => {
    startEl.value = RAW.data_min || '';
    endEl.value = RAW.data_max || '';
    refresh();
  });
  refresh();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
</script>
</body>
</html>
"""


def render_html(raw: dict, initial: dict) -> str:
    html = HTML_TEMPLATE
    html = html.replace("__RAW_JSON__", json.dumps(raw, ensure_ascii=False))
    html = html.replace("__INITIAL_JSON__", json.dumps(initial, ensure_ascii=False))
    html = html.replace("__GEN_AT__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    return html


def _parse_date(s: str | None) -> str | None:
    """YYYY-MM-DD 形式を検証し、そのまま返す（不正なら例外）。"""
    if not s:
        return None
    try:
        pd.Timestamp(s)
    except Exception as e:
        raise SystemExit(f"日付の形式が不正です: {s} ({e})")
    return s


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="autoTRD 取引ダッシュボードを生成")
    p.add_argument("--start", type=str, default=None, help="期間の開始日 YYYY-MM-DD（画面の日付ピッカー初期値）")
    p.add_argument("--end", type=str, default=None, help="期間の終了日 YYYY-MM-DD（画面の日付ピッカー初期値）")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    start = _parse_date(args.start)
    end = _parse_date(args.end)
    if start is not None and end is not None and start > end:
        raise SystemExit(f"--start ({start}) は --end ({end}) 以前である必要があります")

    if not CSV_DIR.exists():
        raise SystemExit(f"CSV ディレクトリが見つかりません: {CSV_DIR}\n先に `bash scripts/sync_db_csv.sh` を実行してください。")

    dfs = load_csvs()
    if all(df.empty for df in dfs.values()):
        raise SystemExit(f"CSV が空です: {CSV_DIR}\nVPS で `bash scripts/export_db_csv.sh` を実行してから同期してください。")

    partials = extract_partial_exits(dfs["trade_log"], dfs["orders"])
    events = build_realized_events(dfs["trade_log"], partials)
    raw = build_raw_data(dfs, events)
    initial = {"start": start, "end": end}

    html = render_html(raw, initial)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")

    print(f"✅ ダッシュボード生成完了: {OUTPUT}")
    if start or end:
        print(f"   初期表示期間: {start or '(最古)'} ～ {end or '(最新)'}")
    print(f"   ブラウザで開く: open {OUTPUT}")


if __name__ == "__main__":
    main()
