"""
動的ユニバース構築モジュール

S&P500構成銘柄リスト（Wikipedia）から時価総額・セクター分散を考慮して
スクリーニング対象ユニバースを自動生成する。

キャッシュ仕様:
  - data/universe_cache.json にキャッシュを保存
  - CACHE_DAYS 日以内なら再取得しない（デフォルト7日）
  - キャッシュが古い or 存在しない場合のみ Wikipedia + yfinance へアクセス

設定:
  - TOP_N_UNIVERSE        : ユニバースの最終銘柄数（デフォルト50）
  - MAX_PER_SECTOR        : セクターあたりの最大銘柄数（デフォルト8）
  - MIN_MARKET_CAP_B      : 最小時価総額（十億ドル、デフォルト30B）
  - EXCLUDED_TICKERS      : 強制除外銘柄（手動管理）
  - CACHE_DAYS            : キャッシュ有効日数（デフォルト7）

使い方:
  from src.data.universe_builder import get_universe
  tickers = get_universe()          # キャッシュ優先
  tickers = get_universe(force_refresh=True)  # 強制再取得
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from src.utils.logger import logger

# ── 設定 ───────────────────────────────────────────────────────────
TOP_N_UNIVERSE   = 50   # ユニバースの最終銘柄数
MAX_PER_SECTOR   = 8    # セクターあたりの最大銘柄数
MIN_MARKET_CAP_B = 30.0 # 最小時価総額（十億ドル）
CACHE_DAYS       = 7    # キャッシュ有効日数

# 手動除外リスト（上場廃止・流動性問題・取引不可銘柄など）
EXCLUDED_TICKERS: set[str] = {
    "BRK-B",  # yfinanceでの時価総額取得が不安定なため除外
    "BRK-A",
}

# キャッシュファイルの保存先（プロジェクトルート直下の data/ ディレクトリ）
_CACHE_PATH = Path(__file__).parent.parent.parent / "data" / "universe_cache.json"

# S&P500リストのWikipedia URL
_SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# yfinance の連続アクセスを避けるウェイト（秒）
_FETCH_DELAY = 0.3


# ── パブリックAPI ────────────────────────────────────────────────────

def get_universe(force_refresh: bool = False) -> list[str]:
    """
    スクリーニング対象ユニバースを返す。

    キャッシュが有効ならキャッシュを返す。
    古い or 存在しない場合は Wikipedia + yfinance で再構築してキャッシュする。

    Args:
        force_refresh: True の場合キャッシュを無視して再取得

    Returns:
        ティッカーシンボルのリスト（例: ["AAPL", "MSFT", ...]）
    """
    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            logger.info(
                f"[universe_builder] キャッシュからユニバースを読み込み "
                f"({len(cached['tickers'])}銘柄, "
                f"作成日: {cached['built_at'][:10]})"
            )
            return cached["tickers"]

    logger.info("[universe_builder] ユニバースを再構築します（Wikipedia + yfinance）")
    tickers = _build_universe()
    _save_cache(tickers)
    return tickers


def get_universe_metadata() -> dict | None:
    """
    キャッシュされたユニバースのメタデータを返す。
    キャッシュがなければ None。

    Returns:
        {
            "tickers": [...],
            "built_at": "2026-03-20T10:00:00",
            "sector_breakdown": {"Technology": 8, ...},
            "source": "S&P500 Wikipedia + yfinance",
            "criteria": {...}
        }
    """
    return _load_cache()


# ── 内部処理 ─────────────────────────────────────────────────────────

def _build_universe() -> list[str]:
    """S&P500リストを取得し、時価総額・セクター分散でユニバースを構築する。"""

    # 1. S&P500構成銘柄リストを取得
    sp500_df = _fetch_sp500_list()
    if sp500_df is None or sp500_df.empty:
        logger.warning("[universe_builder] S&P500リストの取得に失敗。フォールバックリストを使用します")
        return _fallback_universe()

    logger.info(f"[universe_builder] S&P500構成銘柄: {len(sp500_df)}銘柄取得")

    # 2. 除外銘柄を除く
    sp500_df = sp500_df[~sp500_df["ticker"].isin(EXCLUDED_TICKERS)].copy()

    # 3. 各銘柄の時価総額を取得してフィルタリング
    sp500_df = _enrich_with_market_cap(sp500_df)
    before = len(sp500_df)
    sp500_df = sp500_df[sp500_df["market_cap_b"] >= MIN_MARKET_CAP_B].copy()
    logger.info(
        f"[universe_builder] 時価総額フィルタ（>={MIN_MARKET_CAP_B}B）: "
        f"{before}→{len(sp500_df)}銘柄"
    )

    if sp500_df.empty:
        logger.warning("[universe_builder] フィルタ後に銘柄がゼロ。フォールバックを使用")
        return _fallback_universe()

    # 4. 時価総額で降順ソート
    sp500_df = sp500_df.sort_values("market_cap_b", ascending=False)

    # 5. セクター分散を考慮して上位 TOP_N_UNIVERSE 銘柄を選定
    selected = _select_with_sector_balance(sp500_df)

    logger.info(
        f"[universe_builder] ユニバース構築完了: {len(selected)}銘柄\n"
        + _format_sector_summary(selected)
    )

    return selected["ticker"].tolist()


def _fetch_sp500_list() -> pd.DataFrame | None:
    """WikipediaからS&P500構成銘柄リストを取得する。"""
    try:
        tables = pd.read_html(_SP500_WIKI_URL)
        df = tables[0]

        # カラム名を正規化
        df.columns = [c.strip() for c in df.columns]
        ticker_col  = _find_column(df, ["Symbol", "Ticker"])
        sector_col  = _find_column(df, ["GICS Sector", "Sector"])
        name_col    = _find_column(df, ["Security", "Name", "Company"])

        if ticker_col is None or sector_col is None:
            logger.error("[universe_builder] Wikipediaのテーブル構造が想定外です")
            return None

        result = pd.DataFrame({
            "ticker":  df[ticker_col].str.replace(".", "-", regex=False).str.strip(),
            "sector":  df[sector_col].str.strip(),
            "name":    df[name_col].str.strip() if name_col else "",
        })
        return result

    except Exception as e:
        logger.error(f"[universe_builder] Wikipedia取得エラー: {e}")
        return None


def _enrich_with_market_cap(df: pd.DataFrame) -> pd.DataFrame:
    """yfinanceで時価総額を取得してDataFrameに追加する。"""
    market_caps: dict[str, float] = {}
    tickers = df["ticker"].tolist()
    total = len(tickers)

    logger.info(f"[universe_builder] 時価総額を取得中（{total}銘柄）...")

    for i, ticker in enumerate(tickers, 1):
        try:
            info = yf.Ticker(ticker).fast_info
            # fast_info は market_cap を持つ場合が多い
            cap = getattr(info, "market_cap", None)
            if cap is None:
                # fallback: info dict
                full_info = yf.Ticker(ticker).info
                cap = full_info.get("marketCap")
            market_caps[ticker] = float(cap) / 1e9 if cap else 0.0  # 十億ドルに変換
        except Exception as e:
            logger.debug(f"[universe_builder] {ticker} 時価総額取得失敗: {e}")
            market_caps[ticker] = 0.0

        if i % 50 == 0:
            logger.info(f"[universe_builder] 進捗: {i}/{total}")

        time.sleep(_FETCH_DELAY)

    df = df.copy()
    df["market_cap_b"] = df["ticker"].map(market_caps).fillna(0.0)
    return df


def _select_with_sector_balance(df: pd.DataFrame) -> pd.DataFrame:
    """
    時価総額降順に並んだDataFrameから、セクター上限を守りながら
    TOP_N_UNIVERSE 銘柄を選定する。
    """
    sector_counts: dict[str, int] = {}
    selected_rows = []

    for _, row in df.iterrows():
        sector = row["sector"]
        count = sector_counts.get(sector, 0)

        if count >= MAX_PER_SECTOR:
            continue  # このセクターは上限に達している

        selected_rows.append(row)
        sector_counts[sector] = count + 1

        if len(selected_rows) >= TOP_N_UNIVERSE:
            break

    return pd.DataFrame(selected_rows)


def _format_sector_summary(df: pd.DataFrame) -> str:
    """セクター別内訳をログ用に整形する。"""
    counts = df["sector"].value_counts()
    lines = [f"  セクター別内訳:"]
    for sector, count in counts.items():
        lines.append(f"    {sector}: {count}銘柄")
    return "\n".join(lines)


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """候補カラム名のうち実際に存在するものを返す。"""
    for name in candidates:
        if name in df.columns:
            return name
    return None


# ── キャッシュ ────────────────────────────────────────────────────────

def _load_cache() -> dict | None:
    """キャッシュを読み込む。有効期限切れ or 存在しない場合は None を返す。"""
    if not _CACHE_PATH.exists():
        return None
    try:
        with _CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)

        built_at = datetime.fromisoformat(data["built_at"])
        if datetime.now() - built_at > timedelta(days=CACHE_DAYS):
            logger.info(
                f"[universe_builder] キャッシュが{CACHE_DAYS}日を超えています。再取得します"
            )
            return None

        return data
    except Exception as e:
        logger.warning(f"[universe_builder] キャッシュ読み込みエラー: {e}")
        return None


def _save_cache(tickers: list[str]) -> None:
    """ユニバースをキャッシュファイルに保存する。"""
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # セクター情報を再取得（キャッシュに記録するため）
    cached = _load_cache()
    sector_breakdown: dict[str, int] = {}
    if cached and "sector_breakdown" in cached:
        sector_breakdown = cached["sector_breakdown"]

    payload = {
        "tickers": tickers,
        "built_at": datetime.now().isoformat(),
        "ticker_count": len(tickers),
        "sector_breakdown": sector_breakdown,
        "source": "S&P500 Wikipedia + yfinance",
        "criteria": {
            "top_n_universe": TOP_N_UNIVERSE,
            "max_per_sector": MAX_PER_SECTOR,
            "min_market_cap_b": MIN_MARKET_CAP_B,
            "cache_days": CACHE_DAYS,
            "excluded_tickers": list(EXCLUDED_TICKERS),
        },
    }

    with _CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"[universe_builder] キャッシュを保存しました: {_CACHE_PATH}")


# ── フォールバック ────────────────────────────────────────────────────

def _fallback_universe() -> list[str]:
    """
    Wikipedia / yfinance 取得失敗時のフォールバック。
    最後に手動で確認した時価総額上位銘柄リスト（2025年時点）。
    """
    logger.warning("[universe_builder] フォールバックユニバースを使用します（手動リスト）")
    return [
        # Technology
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL",
        "AMD", "QCOM", "TXN", "AMAT", "INTC",
        # Consumer Discretionary
        "AMZN", "TSLA", "MCD", "NKE", "SBUX",
        "HD", "LOW",
        # Communication Services
        "GOOGL", "META",
        # Health Care
        "LLY", "UNH", "JNJ", "ABBV", "MRK",
        "TMO", "ABT", "DHR", "ISRG",
        # Financials
        "JPM", "V", "MA", "GS", "MS", "BLK",
        # Consumer Staples
        "WMT", "COST", "PG", "KO", "PEP",
        # Energy
        "XOM", "CVX",
        # Industrials
        "UNP", "UPS", "ACN", "ADP",
        # Information Technology / Other
        "CRM", "CSCO",
        # Utilities / Real Estate
        "NEE",
        # Other Large Cap
        "PM",
    ]
