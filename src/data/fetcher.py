"""株価データ取得（yfinance/moomooからOHLCVを取得しDBにキャッシュ、差分更新対応）"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

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
# 米国市場のタイムゾーンと大引け時刻（サマータイムはZoneInfoが吸収する）
US_MARKET_TZ = ZoneInfo("America/New_York")
US_MARKET_CLOSE = dtime(16, 0)


class PriceFetchError(RuntimeError):
    """moomoo OpenD に到達できない（未起動・未インストール等）。

    個別銘柄の取得失敗ではなく「そもそも moomoo 経路が成立しない」状態を表す。
    上位（update_price_cache / update_price_cache_batch）がこれを捕捉して
    yfinance にフォールバックするための内部シグナルであり、日次実行は止めない。"""



def last_completed_us_session(now: datetime | None = None) -> date:
    """直近に大引けを迎えた米国市場の営業日を返す。

    price_cache に「まだ形成途中の日足」を入れないための上限日。実行時刻のローカル
    日付（JST）から前日を引く方式だと、日本時間の 00:00〜05:00 は米国市場が前日の
    日付でまだ取引中のため、場中のスナップショットを確定足として取り込んでしまう。
    save_to_cache は原則として既存日付を上書きしないため、一度混入すると自動修復
    されず、ATR/相対強度/出来高平均/RSI が数ヶ月にわたり歪む
    （2026-08-24 に手動再実行で全50銘柄が汚染された事例）。

    判定は米東部時間で行う。大引け(16:00 ET)前ならその日はまだ確定していないので
    前日に下げ、土日は直前の平日まで巻き戻す。祝日は考慮しない（休場日を上限に
    しても「その日までを取得」の上限が1日ずれるだけで、未確定足は混入しない）。
    """
    now_et = (now or datetime.now(US_MARKET_TZ)).astimezone(US_MARKET_TZ)
    session = now_et.date()
    if now_et.time() < US_MARKET_CLOSE:
        session -= timedelta(days=1)
    while session.weekday() >= 5:  # 5=土, 6=日
        session -= timedelta(days=1)
    return session


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
    """Fetch OHLCV data from yfinance.

    end は yfinance の仕様どおり排他（その日を含まない）。確定済みの最終セッション
    までしか取り込まないよう、その翌日でクランプする。米国場中に呼ぶと当日の
    未確定足が返るため、moomoo 経路と同じ焼き付きが起きる（^GSPC/^VIX のように
    moomoo が扱えずフォールバックする銘柄で実際に発生した）。
    """
    max_end = last_completed_us_session() + timedelta(days=1)
    end = min(end or max_end, max_end)
    if start >= end:
        logger.debug(f"{ticker}: 取得対象期間なし（start={start}, end={end}）")
        return pd.DataFrame()
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

    接続確立に至るまでの失敗は理由を問わず PriceFetchError に包む。SDKの
    バージョン差による TypeError（例: security_firm 未対応の古い moomoo-api）など
    PriceFetchError 以外が漏れると、呼び出し側の yfinance フォールバックを素通りして
    スクリーニングごと落ちるため（2026-08-24 の新規BUY全スキップの原因）。
    """
    try:
        from moomoo import ContextStatus, OpenQuoteContext, SecurityFirm

        ctx = OpenQuoteContext(
            host=settings.moomoo_host,
            port=settings.moomoo_port,
            security_firm=SecurityFirm.FUTUJP,
            is_async_connect=True,
        )
    except Exception as e:
        raise PriceFetchError(
            f"moomoo OpenQuoteContext を生成できませんでした: {type(e).__name__}: {e}"
        ) from e
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
    except PriceFetchError:
        _close_quietly(ctx)
        raise
    except Exception as e:
        _close_quietly(ctx)
        raise PriceFetchError(
            f"moomoo OpenD への接続待機に失敗しました: {type(e).__name__}: {e}"
        ) from e

    # ここから先で出る例外は呼び出し側のもの。包み直さずそのまま通す。
    try:
        yield ctx
    finally:
        _close_quietly(ctx)


def _close_quietly(ctx) -> None:
    """close の失敗でリソース解放以外の流れを壊さないようにする。"""
    try:
        ctx.close()
    except Exception as e:
        logger.warning(f"moomoo OpenQuoteContext の close に失敗: {e}")


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

    moomoo の end は包含であり、場中に呼ぶと形成途中の当日日足が返る。未確定
    OHLCV が price_cache に焼き付くと SL/TP判定やATR/RSを壊すため、
    last_completed_us_session()（直近に大引けを迎えた米国営業日）でクランプする。

    ctx を渡した場合はその接続を使い回し、close は呼び出し側に任せる。None なら
    自前で1本開いて閉じる（単発利用向け）。

    OpenD に到達できない場合は PriceFetchError を送出する（呼び出し側が yfinance へ
    フォールバックできるようにするため、空DataFrameには丸めない）。個別銘柄の
    取得失敗・データ無しは従来どおり空DataFrameを返す。
    """
    session_end = last_completed_us_session()
    end = min(end or session_end, session_end)
    if start > end:
        logger.debug(f"{ticker}: 取得対象期間なし（start={start} > end={end}）")
        return pd.DataFrame()
    logger.info(f"Fetching {ticker} from moomoo: {start} to {end}")
    try:
        if ctx is not None:
            return _request_kline(ctx, ticker, start, end)
        with moomoo_quote_ctx() as own_ctx:
            return _request_kline(own_ctx, ticker, start, end)
    except ImportError as e:
        raise PriceFetchError("moomoo-api が未インストールです") from e
    except PriceFetchError:
        raise
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
    """Save OHLCV DataFrame to DB cache. Returns number of rows written.

    確定済みの過去日は上書きしない（無駄な更新を避けるため）が、キャッシュ内で
    最も新しい日付の行だけは新しい値で上書きする。取得元が未確定足を返した場合に
    自力で修復できないと、誤ったOHLCVが恒久的に残るため（2026-08-24 の事例）。
    最新日以外はこれまでどおりスキップする。
    """
    if df.empty:
        return 0
    df = _drop_nan_rows(ticker, df)
    if df.empty:
        return 0
    # 書き込み前の最新日付。この日付の行だけが上書き対象になる。
    refreshable_date = get_last_cached_date(ticker)
    rows_inserted = 0
    rows_updated = 0
    with get_session() as session:
        for _, row in df.iterrows():
            row_date = pd.Timestamp(row["Date"]).date()
            values = {
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "adj_close": float(row.get("Adj Close", row["Close"])),
                "volume": int(row["Volume"]),
            }
            existing = session.execute(
                select(PriceCache).where(
                    PriceCache.ticker == ticker, PriceCache.date == row_date
                )
            ).scalar_one_or_none()
            if existing is not None:
                # 過去の確定足はそのまま。最新日のみ、値が変わっていれば更新する。
                if row_date != refreshable_date:
                    continue
                if all(getattr(existing, k) == v for k, v in values.items()):
                    continue
                for key, value in values.items():
                    setattr(existing, key, value)
                rows_updated += 1
                continue
            session.add(PriceCache(ticker=ticker, date=row_date, **values))
            rows_inserted += 1
        session.commit()
    if rows_updated:
        logger.info(
            f"Cached {rows_inserted} new rows for {ticker} "
            f"（最新日 {refreshable_date} の{rows_updated}行を確定値で更新）"
        )
    else:
        logger.info(f"Cached {rows_inserted} new rows for {ticker}")
    return rows_inserted + rows_updated


def update_price_cache(ticker: str, ctx=None, source: str | None = None) -> int:
    """Fetch and cache price data. Delta fetch if data already exists.

    ctx は moomoo 経路で使い回す OpenQuoteContext（None なら都度開く）。
    source を省略すると settings.data_source に従う。moomoo 経路で OpenD に
    到達できない場合は yfinance にフォールバックする。

    差分取得の起点はキャッシュ最新日の「翌日」ではなく「その日」にする。最新日の
    足は未確定の状態で保存された可能性があり、翌日以降に取り直さないと誤った
    OHLCV が恒久的に残るため（save_to_cache が最新日だけ上書きする前提）。
    取得本数が1本増えるだけで、リクエスト数は変わらない。
    """
    source = source or settings.data_source
    last_date = get_last_cached_date(ticker)
    if last_date:
        start = last_date
    else:
        start = date.today() - timedelta(days=365 * DEFAULT_HISTORY_YEARS)

    # 「最新の確定足まで入っているか」で判定する。ローカル日付(JST)基準だと、
    # 米国が前日の日付でまだ場中の時間帯に未確定足を取りに行ってしまう。
    session_date = last_completed_us_session()
    if start > session_date:
        logger.debug(f"{ticker} cache is up to date")
        return 0

    if source == "moomoo":
        try:
            df = fetch_from_moomoo(ticker, start, ctx=ctx)
        except PriceFetchError as e:
            logger.warning(f"{ticker}: moomooに接続できないためyfinanceで取得します（{e}）")
            df = fetch_from_yfinance(ticker, start)
        else:
            # fetch_from_moomoo は個別銘柄の取得失敗も空DataFrameに丸めるため、
            # 取得対象期間があるのに0件なら失敗の可能性がある。黙って未更新のまま
            # 進むと古い終値でSL/TP判定をしてしまうので、yfinanceで取り直す。
            if df.empty and start <= session_date:
                logger.warning(
                    f"{ticker}: moomooから0件だったためyfinanceで取得し直します"
                )
                df = fetch_from_yfinance(ticker, start)
    else:
        df = fetch_from_yfinance(ticker, start)
    return save_to_cache(ticker, df)


def _update_each(
    tickers: list[str], ctx=None, source: str | None = None
) -> dict[str, int]:
    """銘柄ごとに順次キャッシュ更新する（レート制限のため間隔を空ける）。"""
    results = {}
    for i, ticker in enumerate(tickers):
        results[ticker] = update_price_cache(ticker, ctx=ctx, source=source)
        if i < len(tickers) - 1:
            time.sleep(FETCH_DELAY_SEC)
    return results


def update_price_cache_batch(tickers: list[str]) -> dict[str, int]:
    """Update cache for multiple tickers with rate limiting.

    moomoo 経路では OpenD 接続を1本だけ張って全銘柄で使い回す。接続が確立できない
    場合はバッチ全体を yfinance に切り替えて続行し、Slackに警告を送る
    （日次実行は止めないが、データ源が変わったことは必ず気付けるようにする）。
    銘柄ごとに接続を試し直すと OpenD 未起動時に銘柄数×タイムアウト待たされるため、
    フォールバックの判断は接続確立の1回だけで行う。
    """
    if settings.data_source != "moomoo":
        return _update_each(tickers)

    try:
        with moomoo_quote_ctx() as ctx:
            log_history_kl_quota(ctx)
            # ctx を渡すので個別銘柄で再接続は起きず、ここから PriceFetchError は出ない
            return _update_each(tickers, ctx=ctx)
    except PriceFetchError as e:
        logger.warning(f"moomooに接続できないためyfinanceにフォールバックします（{e}）")
        _notify_moomoo_fallback(e)
        return _update_each(tickers, source="yfinance")


def _notify_moomoo_fallback(error: Exception) -> None:
    """moomoo→yfinance フォールバック発生をSlackに通知する（通知失敗は握り潰す）。"""
    try:
        from src.notify.notifier import send_notification

        send_notification(
            "価格データ取得元をyfinanceにフォールバック",
            "moomoo OpenD に接続できなかったため、本日の価格キャッシュ更新は "
            "yfinance から行いました。売買判定は継続していますが、OpenD の稼働状況を "
            f"確認してください。\n\n{type(error).__name__}: {error}",
            level="warning",
        )
    except Exception as e:
        logger.warning(f"フォールバック通知の送信に失敗: {e}")


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
