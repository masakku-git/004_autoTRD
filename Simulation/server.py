#!/usr/bin/env python3
"""シミュレーション実行サーバー

ブラウザから設定・実行できる HTTP サーバー。
simulate.py・戦略ファイルは一切変更せずに動作する。

使い方:
    python3 Simulation/server.py
    # ブラウザで http://localhost:8765 を開く
"""
from __future__ import annotations

import importlib.util
import json
import queue
import socketserver
import sys
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# パス設定
# ---------------------------------------------------------------------------
SIM_DIR     = Path(__file__).parent          # Simulation/
RESULT_DIR  = SIM_DIR / "Result"             # Simulation/Result/
PROJECT_ROOT = SIM_DIR.parent                # プロジェクトルート

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# ---------------------------------------------------------------------------
# simulate.py から必要な関数をインポート（ファイル変更なし）
# ---------------------------------------------------------------------------
from simulate import (  # noqa: E402
    fetch_all_data,
    assess_market_condition_at,
    run_screening_at,
    evaluate_signal_local,
    approve_trade_local,
    SimulatedPortfolio,
    simulate_one_day,
    select_strategies_for_regime,
    generate_html_report,
    generate_analysis_html,
    parse_dates,
    DEFAULT_UNIVERSE,
    SP500_TICKER,
    VIX_TICKER,
)
from src.strategy.base import BaseStrategy  # noqa: E402

PLUGINS_DIR = PROJECT_ROOT / "src" / "strategy" / "plugins"
PORT = 8765


# ---------------------------------------------------------------------------
# 戦略プラグインの自動探索・ロード
# ---------------------------------------------------------------------------

def discover_strategy_plugins() -> list[dict]:
    """プラグインディレクトリから利用可能な戦略ファイルを探索する。
    戦略名ごとにバージョンをグループ化し、最新バージョンを先頭に返す。

    返り値の各要素:
      {
        "name": "breakout",          # 戦略名
        "versions": [                 # 新しい順
          {"id": "breakout_v3", "version": 3},
          {"id": "breakout_v2", "version": 2},
        ]
      }
    """
    import re

    files = sorted(PLUGINS_DIR.glob("[!_]*_v*.py"))
    groups: dict[str, list[dict]] = {}
    for f in files:
        stem = f.stem  # e.g. "breakout_v3"
        m = re.match(r"^(.+)_v(\d+)$", stem)
        if not m:
            continue
        name = m.group(1)
        version = int(m.group(2))
        groups.setdefault(name, []).append({"id": stem, "version": version})

    result = []
    for name in sorted(groups.keys()):
        versions = sorted(groups[name], key=lambda v: v["version"], reverse=True)
        result.append({"name": name, "versions": versions})
    return result


def load_strategies_by_ids(selected_ids: list[str]) -> list[BaseStrategy]:
    """選択された戦略IDリストからインスタンスを動的にロードして返す。"""
    strategies: list[BaseStrategy] = []
    for sid in selected_ids:
        fpath = PLUGINS_DIR / f"{sid}.py"
        if not fpath.exists():
            print(f"  警告: 戦略ファイルが見つかりません: {fpath}")
            continue
        spec = importlib.util.spec_from_file_location(sid, fpath)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        for attr_name in dir(mod):
            cls = getattr(mod, attr_name)
            if (
                isinstance(cls, type)
                and issubclass(cls, BaseStrategy)
                and cls is not BaseStrategy
            ):
                strategies.append(cls())
                break
    return strategies


# ---------------------------------------------------------------------------
# ジョブ管理
# ---------------------------------------------------------------------------
_jobs: dict[str, dict] = {}


def _run_job(
    job_id: str,
    start_str: str,
    end_str: str,
    capital: float,
    strategy_ids: list[str],
) -> None:
    """バックグラウンドスレッドでシミュレーションを実行する。"""
    import pandas as pd

    job = _jobs[job_id]
    log: queue.Queue = job["log"]

    def emit(msg: str) -> None:
        log.put(msg)

    try:
        emit(f"シミュレーション開始: {start_str} → {end_str}")
        emit(f"初期資金: ${capital:,.2f}")

        # --- 日付解析 ---
        sim_dates = parse_dates([f"{start_str}:{end_str}"])
        if not sim_dates:
            raise ValueError("有効な取引日がありません。日付範囲を確認してください。")
        emit(f"対象取引日: {len(sim_dates)} 日")

        # --- 戦略ロード ---
        strategies = load_strategies_by_ids(strategy_ids)
        if not strategies:
            raise ValueError("戦略が選択されていません。")
        emit(f"使用戦略: {[s.name for s in strategies]}")

        # --- 価格データ取得 ---
        all_tickers = list(set(DEFAULT_UNIVERSE + [SP500_TICKER, VIX_TICKER]))
        emit(f"価格データ取得中... ({len(all_tickers)} 銘柄、数十秒かかります)")
        all_data = fetch_all_data(all_tickers)
        emit(f"取得完了: {len(all_data)} 銘柄")

        sp500_df = all_data.get(SP500_TICKER, pd.DataFrame())
        vix_df   = all_data.get(VIX_TICKER,   pd.DataFrame())

        if sp500_df.empty:
            emit("警告: S&P500 データなし — 市場判定が不正確になります")

        # --- 日次シミュレーション ---
        portfolio  = SimulatedPortfolio(initial_cash=capital)
        day_reports: list[dict] = []
        total = len(sim_dates)

        for i, sim_date in enumerate(sim_dates):
            if not sp500_df.empty:
                available = sp500_df[sp500_df.index <= pd.Timestamp(sim_date)]
                if available.empty:
                    continue

            day_report = simulate_one_day(
                sim_date, portfolio, all_data, sp500_df, vix_df, strategies
            )
            day_reports.append(day_report)

            # 10 日ごと & 最終日に進捗を送信
            if i % 10 == 0 or i == total - 1:
                snap = portfolio.daily_snapshots
                eq   = snap[-1]["total_equity"] if snap else capital
                ret  = (eq / capital - 1) * 100
                n_pos = len(portfolio.positions)
                emit(
                    f"  [{i+1:>3}/{total}] {sim_date}  "
                    f"資産: ${eq:,.2f} ({ret:+.2f}%)  "
                    f"ポジション: {n_pos}"
                )

        # --- レポート保存 ---
        RESULT_DIR.mkdir(exist_ok=True)
        tag = f"{start_str.replace('-','')}_{end_str.replace('-','')}"
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")

        sim_path = RESULT_DIR / f"{tag}_{ts}_sim.html"
        ana_path = RESULT_DIR / f"{tag}_{ts}_analysis.html"

        sim_html = generate_html_report(portfolio, day_reports, sim_dates, all_data=all_data)
        sim_path.write_text(sim_html, encoding="utf-8")

        ana_html = generate_analysis_html(portfolio, day_reports, sim_dates)
        ana_path.write_text(ana_html, encoding="utf-8")

        # --- サマリー ---
        trades = portfolio.closed_trades
        wins   = [t for t in trades if t["pnl"] > 0]
        final_eq = portfolio.daily_snapshots[-1]["total_equity"] if portfolio.daily_snapshots else capital
        ret_pct  = (final_eq / capital - 1) * 100
        win_rate = len(wins) / len(trades) * 100 if trades else 0.0
        real_pnl = sum(t["pnl"] for t in trades)

        emit("")
        emit("=" * 50)
        emit(f"  最終資産:   ${final_eq:,.2f}  ({ret_pct:+.2f}%)")
        emit(f"  取引回数:   {len(trades)} 件")
        emit(f"  勝率:       {len(wins)}/{len(trades)} ({win_rate:.1f}%)")
        emit(f"  実現損益:   ${real_pnl:+,.2f}")
        emit("=" * 50)
        emit(f"レポート保存完了")

        job["result_sim"]      = f"/result/{sim_path.name}"
        job["result_analysis"] = f"/result/{ana_path.name}"
        job["done"] = True

    except Exception as exc:
        emit(f"エラー: {exc}")
        emit(traceback.format_exc())
        job["error"] = str(exc)
        job["done"]  = True


# ---------------------------------------------------------------------------
# HTTP ハンドラー
# ---------------------------------------------------------------------------

class SimHandler(BaseHTTPRequestHandler):

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path
        try:
            if path == "/":
                self._serve_file(SIM_DIR / "index.html", "text/html")
            elif path == "/api/health":
                self._json({"ok": True, "project_root": str(PROJECT_ROOT)})
            elif path == "/api/strategies":
                self._json(discover_strategy_plugins())
            elif path == "/api/results":
                self._serve_results_list()
            elif path.startswith("/result/V2以前/") or path.startswith("/result/V2%E4%BB%A5%E5%89%8D/"):
                # V2以前サブディレクトリのファイル
                if path.startswith("/result/V2以前/"):
                    fname = path[len("/result/V2以前/"):]
                else:
                    fname = path[len("/result/V2%E4%BB%A5%E5%89%8D/"):]
                self._serve_file(RESULT_DIR / "V2以前" / fname, "text/html")
            elif path.startswith("/result/"):
                fname = path[len("/result/"):]
                self._serve_file(RESULT_DIR / fname, "text/html")
            elif path.startswith("/api/stream/"):
                job_id = path[len("/api/stream/"):]
                self._stream_job(job_id)
            else:
                self.send_error(404)
        except Exception as exc:
            print(f"[ERROR] GET {path}: {exc}")
            traceback.print_exc()
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def do_POST(self) -> None:
        if self.path == "/api/run":
            self._start_run()
        else:
            self.send_error(404)

    # --- プライベートメソッド ---

    def _start_run(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length).decode())

        start        = body.get("start", "").strip()
        end          = body.get("end", "").strip()
        capital      = float(body.get("capital", 10000))
        strategy_ids = body.get("strategies", [])

        if not start or not end:
            self._json({"error": "開始日・終了日を指定してください"}, 400)
            return
        if not strategy_ids:
            self._json({"error": "戦略を1つ以上選択してください"}, 400)
            return

        job_id = str(uuid.uuid4())[:8]
        _jobs[job_id] = {
            "log":              queue.Queue(),
            "done":             False,
            "error":            None,
            "result_sim":       None,
            "result_analysis":  None,
        }

        threading.Thread(
            target=_run_job,
            args=(job_id, start, end, capital, strategy_ids),
            daemon=True,
        ).start()

        self._json({"job_id": job_id})

    def _stream_job(self, job_id: str) -> None:
        """Server-Sent Events でジョブ進捗を送信する。"""
        job = _jobs.get(job_id)
        if not job:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            while True:
                try:
                    line = job["log"].get(timeout=0.4)
                    safe = line.replace("\n", " ").replace("\r", "")
                    self.wfile.write(f"data: {safe}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    pass

                if job["done"] and job["log"].empty():
                    if job["result_sim"]:
                        payload = json.dumps(
                            {
                                "sim":      job["result_sim"],
                                "analysis": job["result_analysis"],
                            },
                            ensure_ascii=False,
                        )
                        self.wfile.write(f"event: done\ndata: {payload}\n\n".encode())
                    else:
                        err = job.get("error", "不明なエラー")
                        self.wfile.write(f"event: error\ndata: {err}\n\n".encode())
                    self.wfile.flush()
                    break

        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_results_list(self) -> None:
        """現在の結果とV2以前の結果を分けて返す。"""
        RESULT_DIR.mkdir(exist_ok=True)
        legacy_dir = RESULT_DIR / "V2以前"

        current = self._collect_results(RESULT_DIR, "/result/")
        legacy = self._collect_results(legacy_dir, "/result/V2以前/") if legacy_dir.exists() else []

        self._json({"current": current, "legacy": legacy})

    def _collect_results(self, directory: Path, url_prefix: str) -> list[dict]:
        """指定ディレクトリからシミュレーション結果を収集する。"""
        sim_files = sorted(
            directory.glob("*_sim.html"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        result = []
        for f in sim_files:
            ana_name = f.name.replace("_sim.html", "_analysis.html")
            ana_path = directory / ana_name
            parts = f.stem.split("_")
            period = ""
            run_dt = ""
            if len(parts) >= 4:
                try:
                    d1 = datetime.strptime(parts[0], "%Y%m%d").strftime("%Y-%m-%d")
                    d2 = datetime.strptime(parts[1], "%Y%m%d").strftime("%Y-%m-%d")
                    period = f"{d1} → {d2}"
                except ValueError:
                    period = f.stem
                try:
                    run_dt = datetime.strptime(f"{parts[2]}_{parts[3]}", "%Y%m%d_%H%M%S").strftime(
                        "%Y-%m-%d %H:%M"
                    )
                except (ValueError, IndexError):
                    run_dt = ""
            result.append(
                {
                    "name":         f.name,
                    "period":       period,
                    "run_at":       run_dt,
                    "sim_url":      f"{url_prefix}{f.name}",
                    "analysis_url": f"{url_prefix}{ana_name}" if ana_path.exists() else None,
                    "size_kb":      round(f.stat().st_size / 1024, 1),
                }
            )
        return result

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            content = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, f"File not found: {path.name}")

    def _json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # HTTPアクセスログを抑制


# ---------------------------------------------------------------------------
# ThreadingHTTPServer（SSE とメインリクエストを並列処理）
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    RESULT_DIR.mkdir(exist_ok=True)
    server = ThreadedHTTPServer(("localhost", PORT), SimHandler)
    print(f"╔══════════════════════════════════════╗")
    print(f"║  Auto Trading Simulation Server      ║")
    print(f"╠══════════════════════════════════════╣")
    print(f"║  URL: http://localhost:{PORT}           ║")
    print(f"║  停止: Ctrl+C                        ║")
    print(f"╚══════════════════════════════════════╝")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを停止しました")
