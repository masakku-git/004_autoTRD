"""株価データ取得（yfinance/moomooからOHLCVを取得しDBにキャッシュ、差分更新対応）"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from sqlalchemy import func, select

from config.settings import settings
from src.models.base import get_session
from src.models.price import PriceCache
from src.utils.logger import logger

# Default history length for first fetch
DEFAULT_HISTORY_YEARS = 2
# Rate limit between yfinance calls
FETCH_DELAY_SEC = 0.5
# Columns that must be non-NaN for a row to be cached
_REQUIRED_COLS = ["Open", "High", "Low", "Close", "Volume"]
# moomoo履歴K線のページング取得回数の上限（無限ループ防止）
MAX_KLINE_PAGES = 20
# moomoo OpenD への接続確立を待つ上限秒数
MOOMOO_CONNECT_TIMEOUT_SEC = 15
# 接続状態のポーリング間隔
MOOMOO_CONNECT_POLL_SEC = 0.2


class PriceFetchError(RuntimeError):
    """価格データ取得基盤に到達できない（OpenD未起動・未インストール等）。

    個別銘柄の取得失敗ではなく「そもそも一括更新が成立しない」状態を表す。
    update_price_cache_batch から送出され、run_screening 経由で main.run_daily の
    スクリーニング失敗ハンドラに拾われる（Slack通知 + 新規BUYスキップ、
    損切り・売却判定は継続）。"""



def get_last_cached_date(ticker: str) -> date | None:
    """Get the most recent date cached in DB for a ticker."""
    with get_session() as session:
        result = session.execute(
            select(func.max(PriceCache.date)).where(PriceCache.ticker == ticker)
        ).scalar()
        return result


def fetch_from_yfinance(
    ticker: str, start: date, end: date | None = None
) -> pd.DataFrame:
    """Fetch OHLCV data from yfinance."""
    end = end or date.today()
    logger.info(f"Fetching {ticker} from yfinance: {start} to {end}")
    try:
        df = yf.download(
            ticker, start=str(start), end=str(end), progress=False, auto_adjust=False
        )
        if df.empty:
            logger.warning(f"No data returned for {ticker}")
            return pd.DataFrame()
        # Flatten multi-level columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        return df
    except Exception as e:
        logger.error(f"Failed to fetch {ticker}: {e}")
        return pd.DataFrame()


def _ticker_to_moomoo_code(ticker: str) -> str:
    """米国株ティッカーをmoomooのマーケット接頭辞付きコードに変換（例: "AAPL" -> "US.AAPL"）。

    クラス株の表記が両者で異なる点に注意。universe_builder は Wikipedia の
    "BRK.B" を yfinance 表記の "BRK-B" に正規化して保持しているが、moomoo は
    ドット区切り（"US.BRK.B"）を要求するため、ここでハイフンをドットに戻す。
    変換を忘れると該当銘柄だけ静かに取得失敗する。
    """
    return f"US.{ticker.replace('-', '.')}"


def _moomoo_code_to_ticker(code: str) -> str:
    """moomooコードを社内表記のティッカーに変換（例: "US.BRK.B" -> "BRK-B"）"""
    body = code.split(".", 1)[1] if "." in code else code
    return body.replace(".", "-")


@contextmanager
def moomoo_quote_ctx():
    """moomoo OpenQuoteContext を1本開き、抜けるときに必ず close する。

    OpenD への接続確立は安くない（TCP接続 + InitConnect の同期往復 +
    CallbackExecutor スレッドの生成/破棄）。K線リクエスト本体よりオーバーヘッドが
    重いため、複数銘柄を処理するときは銘柄ごとに開き直さずこれで使い回すこと。

    is_async_connect=True は必須。既定の False では OpenD 未起動時に
    コンストラクタが `while True: ... sleep(6)` で永久リトライし、例外も投げずに
    ハングする（呼び出し側の except では捕捉できない）。非同期接続にしたうえで
    READY をタイムアウト付きで待ち、駄目なら PriceFetchError を送出する。
    """
    from moomoo import ContextStatus, OpenQuoteContext, SecurityFirm

    ctx = OpenQuoteContext(
        host=settings.moomoo_host,
        port=settings.moomoo_port,
        security_firm=SecurityFirm.FUTUJP,
        is_async_connect=True,
    )
    try:
        deadline = time.monotonic() + MOOMOO_CONNECT_TIMEOUT_SEC
        while ctx.status != ContextStatus.READY:
            if time.monotonic() >= deadline:
                raise PriceFetchError(
                    "moomoo OpenD への接続がタイムアウトしました "
                    f"({settings.moomoo_host}:{settings.moomoo_port}, "
                    f"{MOOMOO_CONNECT_TIMEOUT_SEC}秒, status={ctx.status})"
                )
            time.sleep(MOOMOO_CONNECT_POLL_SEC)
        yield ctx
    finally:
        ctx.close()


def _request_kline(ctx, ticker: str, start: date, end: date) -> pd.DataFrame:
    """開かれた ctx で日足K線を全ページ取得し、社内スキーマに正規化して返す。"""
    from moomoo import AuType, KLType, RET_OK

    code = _ticker_to_moomoo_code(ticker)
    frames = []
    page_req_key = None
    # サーバが page_req_key を返し続けた場合に無限ループしないための上限。
    # 1ページ1000本・日足なので MAX_KLINE_PAGES ページあれば十分に足りる。
    for _page in range(MAX_KLINE_PAGES):
        ret, data, page_req_key = ctx.request_history_kline(
            code,
            start=str(start),
            end=str(end),
            ktype=KLType.K_DAY,
            autype=AuType.QFQ,
            max_count=1000,
            page_req_key=page_req_key,
        )
        if ret != RET_OK:
            logger.error(f"moomoo request_history_kline failed for {ticker}: {data}")
            return pd.DataFrame()
        frames.append(data)
        if page_req_key is None:
            break
    else:
        logger.warning(
            f"{ticker}: moomooページング上限({MAX_KLINE_PAGES})に到達。"
            "取得済み分のみ使用します"
        )

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        logger.warning(f"No data returned for {ticker}")
        return pd.DataFrame()

    df = df.rename(
        columns={
            "time_key": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    # moomooのK線APIは調整済み終値を別列で返さない（autypeで調整方式を指定する方式）。
    # QFQ（順方向調整）済みのCloseをAdj Closeとしてもそのまま使う。
    df["Adj Close"] = df["Close"]
    df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
    return df[["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]]


def fetch_from_moomoo(
    ticker: str, start: date, end: date | None = None, ctx=None
) -> pd.DataFrame:
    """Fetch OHLCV data from moomoo OpenD (request_history_kline).

    end は yfinance と揃えて「当日を含まない」意味で扱う。yfinance の end は排他
    だが moomoo の end は包含であり、場中に呼ぶと形成途中の当日日足が返る。
    save_to_cache は既存日付の行を更新しないため、未確定OHLCVがそのまま
    price_cache に焼き付き、SL/TP判定やATR/RSを壊す。よって前日でクランプする。

    ctx を渡した場合はその接続を使い回し、close は呼び出し側に任せる。None なら
    自前で1本開いて閉じる（単発利用向け）。単発利用では接続失敗も空DataFrameに
    丸めて返す。一括更新で接続失敗を検知したい場合は update_price_cache_batch を使う。
    """
    end = min(end or date.today(), date.today() - timedelta(days=1))
    if start > end:
        logger.debug(f"{ticker}: 取得対象期間なし（start={start} > end={end}）")
        return pd.DataFrame()
    logger.info(f"Fetching {ticker} from moomoo: {start} to {end}")
    try:
        if ctx is not None:
            return _request_kline(ctx, ticker, start, end)
        with moomoo_quote_ctx() as own_ctx:
            return _request_kline(own_ctx, ticker, start, end)
    except ImportError:
        logger.warning("moomoo-api not installed, cannot fetch from moomoo")
        return pd.DataFrame()
    except PriceFetchError as e:
        logger.error(f"{ticker}: {e}")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"Failed to fetch {ticker} from moomoo: {e}")
        return pd.DataFrame()


def log_history_kl_quota(ctx=None) -> None:
    """moomoo履歴K線APIのクォータ使用状況をログ出力する（バッチ先頭で1回のみ呼ぶ想定）。

    ctx を渡した場合はその接続を使い回す。診断用途なので失敗しても例外は投げない。
    """
    try:
        from moomoo import RET_OK

        if ctx is not None:
            ret, data = ctx.get_history_kl_quota()
        else:
            with moomoo_quote_ctx() as own_ctx:
                ret, data = own_ctx.get_history_kl_quota()
        if ret != RET_OK:
            logger.warning(f"get_history_kl_quota failed: {data}")
            return
        used_quota, remain_quota, _detail_list = data
        logger.info(f"moomoo履歴K線クォータ: used={used_quota} remain={remain_quota}")
    except ImportError:
        logger.warning("moomoo-api not installed, skipping quota check")
    except Exception as e:
        logger.warning(f"moomoo quota check failed: {e}")


def _drop_nan_rows(ticker: str, df: pd.DataFrame) -> pd.DataFrame:
    """OHLCVにNaNを含む行を除外する（NaNがprice_cacheに永続化されるのを防ぐ）。

    ソース非依存のガード。yfinance/moomooいずれも取得失敗時に部分的なNaN行を
    返すことがあり、そのままDBに入るとint(NaN)でのクラッシュや、NaN比較
    （常にFalse）によるSL/TP判定の無効化を招く。
    """
    cols = [c for c in _REQUIRED_COLS if c in df.columns]
    cleaned = df.dropna(subset=cols)
    dropped = len(df) - len(cleaned)
    if dropped:
        logger.warning(f"{ticker}: NaNを含む{dropped}行をキャッシュ保存から除外")
    return cleaned


def save_to_cache(ticker: str, df: pd.DataFrame) -> int:
    """Save OHLCV DataFrame to DB cache. Returns number of rows inserted."""
    if df.empty:
        return 0
    df = _drop_nan_rows(ticker, df)
    if df.empty:
        return 0
    rows_inserted = 0
    with get_session() as session:
        for _, row in df.iterrows():
            row_date = pd.Timestamp(row["Date"]).date()
            # Skip if already cached
            existing = session.execute(
                select(PriceCache.id).where(
                    PriceCache.ticker == ticker, PriceCache.date == row_date
                )
            ).scalar()
            if existing:
                continue
            record = PriceCache(
                ticker=ticker,
                date=row_date,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                adj_close=float(row.get("Adj Close", row["Close"])),
                volume=int(row["Volume"]),
            )
            session.add(record)
            rows_inserted += 1
        session.commit()
    logger.info(f"Cached {rows_inserted} new rows for {ticker}")
    return rows_inserted


def update_price_cache(ticker: str, ctx=None) -> int:
    """Fetch and cache price data. Delta fetch if data already exists.

    ctx は moomoo 経路で使い回す OpenQuoteContext（None なら都度開く）。
    """
    last_date = get_last_cached_date(ticker)
    if last_date:
        start = last_date + timedelta(days=1)
    else:
        start = date.today() - timedelta(days=365 * DEFAULT_HISTORY_YEARS)

    if start >= date.today():
        logger.debug(f"{ticker} cache is up to date")
        return 0

    if settings.data_source == "moomoo":
        df = fetch_from_moomoo(ticker, start, ctx=ctx)
    else:
        df = fetch_from_yfinance(ticker, start)
    return save_to_cache(ticker, df)


def _update_each(tickers: list[str], ctx=None) -> dict[str, int]:
    """銘柄ごとに順次キャッシュ更新する（レート制限のため間隔を空ける）。"""
    results = {}
    for i, ticker in enumerate(tickers):
        results[ticker] = update_price_cache(ticker, ctx=ctx)
        if i < len(tickers) - 1:
            time.sleep(FETCH_DELAY_SEC)
    return results


def update_price_cache_batch(tickers: list[str]) -> dict[str, int]:
    """Update cache for multiple tickers with rate limiting.

    moomoo 経路では OpenD 接続を1本だけ張って全銘柄で使い回す。接続自体が
    確立できない場合は PriceFetchError を送出して一括更新を打ち切る
    （古い price_cache のまま黙ってスクリーニングが進むのを防ぐ）。
    """
    if settings.data_source != "moomoo":
        return _update_each(tickers)

    try:
        with moomoo_quote_ctx() as ctx:
            log_history_kl_quota(ctx)
            return _update_each(tickers, ctx=ctx)
    except ImportError as e:
        raise PriceFetchError(
            "moomoo-api が未インストールのため価格キャッシュを更新できません"
        ) from e


def get_ohlcv(
    ticker: str,
    start: date | None = None,
    end: date | None = None,
    ensure_updated: bool = True,
) -> pd.DataFrame:
    """Get OHLCV data from DB cache, optionally updating first."""
    if ensure_updated:
        update_price_cache(ticker)

    with get_session() as session:
        query = select(PriceCache).where(PriceCache.ticker == ticker)
        if start:
            query = query.where(PriceCache.date >= start)
        if end:
            query = query.where(PriceCache.date <= end)
        query = query.order_by(PriceCache.date)

        rows = session.execute(query).scalars().all()
        if not rows:
            return pd.DataFrame()

        data = [
            {
                "Date": r.date,
                "Open": r.open,
                "High": r.high,
                "Low": r.low,
                "Close": r.close,
                "Adj Close": r.adj_close,
                "Volume": r.volume,
            }
            for r in rows
        ]
        df = pd.DataFrame(data)
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        return df
