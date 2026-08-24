"""Tests for src.data.fetcher NaN handling and moomoo fetch path."""
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import pytest

from src.data.fetcher import (
    MAX_KLINE_PAGES,
    PriceFetchError,
    _drop_nan_rows,
    _moomoo_code_to_ticker,
    _ticker_to_moomoo_code,
    fetch_from_moomoo,
    moomoo_quote_ctx,
    update_price_cache,
    update_price_cache_batch,
)


def _ready_ctx() -> MagicMock:
    """接続確立済み（READY）として振る舞う OpenQuoteContext のモック"""
    ctx = MagicMock()
    ctx.status = "READY"  # moomoo.ContextStatus.READY は文字列 "READY"
    return ctx


def _make_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.bdate_range("2026-07-01", periods=5),
            "Open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "High": [10.5, 11.5, 12.5, 13.5, 14.5],
            "Low": [9.5, 10.5, 11.5, 12.5, 13.5],
            "Close": [10.2, 11.2, 12.2, 13.2, 14.2],
            "Adj Close": [10.2, 11.2, 12.2, 13.2, 14.2],
            "Volume": [1_000_000] * 5,
        }
    )


def test_drop_nan_rows_keeps_clean_df():
    df = _make_df()
    cleaned = _drop_nan_rows("TEST", df)
    assert len(cleaned) == 5


def test_drop_nan_rows_removes_nan_volume():
    df = _make_df()
    df.loc[2, "Volume"] = np.nan
    cleaned = _drop_nan_rows("TEST", df)
    assert len(cleaned) == 4
    # int(row["Volume"]) must be safe on every remaining row
    assert all(int(v) > 0 for v in cleaned["Volume"])


def test_drop_nan_rows_removes_nan_close():
    df = _make_df()
    df.loc[4, "Close"] = np.nan
    cleaned = _drop_nan_rows("TEST", df)
    assert len(cleaned) == 4
    assert not cleaned["Close"].isna().any()


def test_drop_nan_rows_all_nan_returns_empty():
    df = _make_df()
    df["Close"] = np.nan
    cleaned = _drop_nan_rows("TEST", df)
    assert cleaned.empty


def test_ticker_to_moomoo_code():
    assert _ticker_to_moomoo_code("AAPL") == "US.AAPL"


def test_ticker_to_moomoo_code_class_share_uses_dot():
    """universe_builderのハイフン表記(BRK-B)をmoomooのドット表記に戻す"""
    assert _ticker_to_moomoo_code("BRK-B") == "US.BRK.B"


def test_moomoo_code_to_ticker():
    assert _moomoo_code_to_ticker("US.AAPL") == "AAPL"


def test_moomoo_code_to_ticker_class_share_uses_hyphen():
    assert _moomoo_code_to_ticker("US.BRK.B") == "BRK-B"


def test_moomoo_code_to_ticker_no_prefix():
    assert _moomoo_code_to_ticker("AAPL") == "AAPL"


def _make_moomoo_kline_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["US.AAPL"] * 3,
            "time_key": ["2026-07-01", "2026-07-02", "2026-07-03"],
            "open": [10.0, 11.0, 12.0],
            "high": [10.5, 11.5, 12.5],
            "low": [9.5, 10.5, 11.5],
            "close": [10.2, 11.2, 12.2],
            "volume": [1_000_000, 1_100_000, 1_200_000],
        }
    )


def test_fetch_from_moomoo_normalizes_columns_and_passes_nan_guard():
    mock_ctx = _ready_ctx()
    mock_ctx.request_history_kline.return_value = (0, _make_moomoo_kline_df(), None)

    with patch("moomoo.OpenQuoteContext", return_value=mock_ctx):
        from datetime import date

        df = fetch_from_moomoo("AAPL", date(2026, 7, 1), date(2026, 7, 3))

    mock_ctx.close.assert_called_once()
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert len(df) == 3
    assert df["Adj Close"].tolist() == df["Close"].tolist()

    cleaned = _drop_nan_rows("AAPL", df)
    assert len(cleaned) == 3


def test_fetch_from_moomoo_paginates_until_key_is_none():
    """page_req_keyがNoneになるまで追加ページを取得して結合する"""
    page1 = _make_moomoo_kline_df()
    page2 = _make_moomoo_kline_df()
    page2["time_key"] = ["2026-07-06", "2026-07-07", "2026-07-08"]

    mock_ctx = _ready_ctx()
    mock_ctx.request_history_kline.side_effect = [
        (0, page1, "KEY1"),
        (0, page2, None),
    ]

    with patch("moomoo.OpenQuoteContext", return_value=mock_ctx):
        from datetime import date

        df = fetch_from_moomoo("AAPL", date(2026, 7, 1), date(2026, 7, 8))

    assert len(df) == 6
    assert mock_ctx.request_history_kline.call_count == 2
    # 2回目の呼び出しに1回目のkeyが渡されている
    assert mock_ctx.request_history_kline.call_args_list[1].kwargs["page_req_key"] == "KEY1"


def test_fetch_from_moomoo_stops_at_page_limit():
    """page_req_keyが返り続けても上限で打ち切り、無限ループしない"""
    mock_ctx = _ready_ctx()
    mock_ctx.request_history_kline.return_value = (0, _make_moomoo_kline_df(), "KEY")

    with patch("moomoo.OpenQuoteContext", return_value=mock_ctx):
        from datetime import date

        df = fetch_from_moomoo("AAPL", date(2026, 7, 1), date(2026, 7, 3))

    assert mock_ctx.request_history_kline.call_count == MAX_KLINE_PAGES
    assert len(df) == 3 * MAX_KLINE_PAGES


def test_fetch_from_moomoo_clamps_end_to_yesterday():
    """当日の未確定足を取り込まないよう、endは前日にクランプされる"""
    from datetime import date, timedelta

    mock_ctx = _ready_ctx()
    mock_ctx.request_history_kline.return_value = (0, _make_moomoo_kline_df(), None)

    with patch("moomoo.OpenQuoteContext", return_value=mock_ctx):
        fetch_from_moomoo("AAPL", date.today() - timedelta(days=10))

    passed_end = mock_ctx.request_history_kline.call_args.kwargs["end"]
    assert passed_end == str(date.today() - timedelta(days=1))


def test_fetch_from_moomoo_skips_when_start_after_end():
    """start が前日より後なら API を呼ばずに空を返す"""
    from datetime import date

    mock_ctx = _ready_ctx()
    with patch("moomoo.OpenQuoteContext", return_value=mock_ctx):
        df = fetch_from_moomoo("AAPL", date.today())

    assert df.empty
    mock_ctx.request_history_kline.assert_not_called()


def test_fetch_from_moomoo_returns_empty_on_error():
    mock_ctx = _ready_ctx()
    mock_ctx.request_history_kline.return_value = (-1, "error", None)

    with patch("moomoo.OpenQuoteContext", return_value=mock_ctx):
        from datetime import date

        df = fetch_from_moomoo("AAPL", date(2026, 7, 1), date(2026, 7, 3))

    assert df.empty
    mock_ctx.close.assert_called_once()


def test_fetch_from_moomoo_reuses_given_ctx_without_closing():
    """ctxを渡した場合は新規接続せず、closeも呼び出し側に任せる"""
    from datetime import date

    shared = _ready_ctx()
    shared.request_history_kline.return_value = (0, _make_moomoo_kline_df(), None)

    with patch("moomoo.OpenQuoteContext") as mock_cls:
        df = fetch_from_moomoo("AAPL", date(2026, 7, 1), date(2026, 7, 3), ctx=shared)

    mock_cls.assert_not_called()
    shared.close.assert_not_called()
    assert len(df) == 3


def test_moomoo_quote_ctx_times_out_when_never_ready(monkeypatch):
    """READYにならない接続はタイムアウトしてPriceFetchErrorになる（無限ハングしない）"""
    monkeypatch.setattr("src.data.fetcher.MOOMOO_CONNECT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("src.data.fetcher.MOOMOO_CONNECT_POLL_SEC", 0.01)

    never_ready = MagicMock()
    never_ready.status = "CONNECTING"

    with patch("moomoo.OpenQuoteContext", return_value=never_ready):
        with pytest.raises(PriceFetchError):
            with moomoo_quote_ctx():
                pass

    # タイムアウトしても接続は必ず閉じる
    never_ready.close.assert_called_once()


def test_moomoo_quote_ctx_passes_async_connect():
    """is_async_connect=True でないとOpenD未起動時にコンストラクタがハングする"""
    with patch("moomoo.OpenQuoteContext", return_value=_ready_ctx()) as mock_cls:
        with moomoo_quote_ctx():
            pass

    assert mock_cls.call_args.kwargs["is_async_connect"] is True


def test_update_price_cache_batch_opens_one_ctx_for_all_tickers(monkeypatch):
    """moomoo経路では銘柄数によらず接続は1本だけ"""
    monkeypatch.setattr("src.data.fetcher.settings.data_source", "moomoo")
    monkeypatch.setattr("src.data.fetcher.FETCH_DELAY_SEC", 0)
    monkeypatch.setattr("src.data.fetcher.log_history_kl_quota", lambda ctx=None: None)
    monkeypatch.setattr(
        "src.data.fetcher.update_price_cache",
        lambda ticker, ctx=None, source=None: 0,
    )

    with patch("moomoo.OpenQuoteContext", return_value=_ready_ctx()) as mock_cls:
        results = update_price_cache_batch(["AAPL", "MSFT", "NVDA"])

    assert mock_cls.call_count == 1
    assert set(results) == {"AAPL", "MSFT", "NVDA"}


def test_update_price_cache_batch_falls_back_to_yfinance(monkeypatch):
    """OpenDに接続できないときは中断せずyfinanceで全銘柄を取得し直す"""
    monkeypatch.setattr("src.data.fetcher.settings.data_source", "moomoo")
    monkeypatch.setattr("src.data.fetcher.MOOMOO_CONNECT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("src.data.fetcher.MOOMOO_CONNECT_POLL_SEC", 0.01)
    monkeypatch.setattr("src.data.fetcher.FETCH_DELAY_SEC", 0)

    notified = []
    monkeypatch.setattr(
        "src.data.fetcher._notify_moomoo_fallback", lambda e: notified.append(e)
    )

    used_sources = {}

    def fake_update(ticker, ctx=None, source=None):
        used_sources[ticker] = source
        return 0

    monkeypatch.setattr("src.data.fetcher.update_price_cache", fake_update)

    never_ready = MagicMock()
    never_ready.status = "CONNECTING"

    with patch("moomoo.OpenQuoteContext", return_value=never_ready):
        results = update_price_cache_batch(["AAPL", "MSFT"])

    assert set(results) == {"AAPL", "MSFT"}
    assert used_sources == {"AAPL": "yfinance", "MSFT": "yfinance"}
    assert len(notified) == 1
    # フォールバック時も接続は閉じる
    never_ready.close.assert_called_once()


def test_update_price_cache_falls_back_per_ticker(monkeypatch):
    """単発取得でもmoomoo接続不可ならyfinanceにフォールバックする"""
    monkeypatch.setattr("src.data.fetcher.settings.data_source", "moomoo")
    monkeypatch.setattr("src.data.fetcher.get_last_cached_date", lambda t: None)
    monkeypatch.setattr("src.data.fetcher.save_to_cache", lambda t, df: len(df))

    def boom(ticker, start, end=None, ctx=None):
        raise PriceFetchError("OpenD unreachable")

    monkeypatch.setattr("src.data.fetcher.fetch_from_moomoo", boom)
    monkeypatch.setattr(
        "src.data.fetcher.fetch_from_yfinance",
        lambda ticker, start, end=None: _make_df(),
    )

    assert update_price_cache("AAPL") == len(_make_df())


def test_fetch_from_moomoo_raises_on_connect_timeout(monkeypatch):
    """接続不能は空DataFrameに丸めず、上位がフォールバック判断できるよう送出する"""
    from datetime import date

    monkeypatch.setattr("src.data.fetcher.MOOMOO_CONNECT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("src.data.fetcher.MOOMOO_CONNECT_POLL_SEC", 0.01)

    never_ready = MagicMock()
    never_ready.status = "CONNECTING"

    with patch("moomoo.OpenQuoteContext", return_value=never_ready):
        with pytest.raises(PriceFetchError):
            fetch_from_moomoo("AAPL", date(2026, 7, 1), date(2026, 7, 3))


def test_moomoo_quote_ctx_wraps_sdk_typeerror():
    """SDKの引数不一致（古いmoomoo-api）もPriceFetchErrorに包む

    2026-08-24 はここで TypeError が素通りし、yfinanceフォールバックに乗らないまま
    スクリーニングごと落ちて新規BUYが全スキップになった。
    """

    def old_sdk(**kwargs):
        raise TypeError(
            "OpenQuoteContext.__init__() got an unexpected keyword argument "
            "'security_firm'"
        )

    with patch("moomoo.OpenQuoteContext", side_effect=old_sdk):
        with pytest.raises(PriceFetchError):
            with moomoo_quote_ctx():
                pass


def test_update_price_cache_batch_falls_back_on_sdk_typeerror(monkeypatch):
    """SDK不整合でもバッチは中断せずyfinanceで継続する"""
    monkeypatch.setattr("src.data.fetcher.settings.data_source", "moomoo")
    monkeypatch.setattr("src.data.fetcher.FETCH_DELAY_SEC", 0)
    monkeypatch.setattr("src.data.fetcher._notify_moomoo_fallback", lambda e: None)

    used_sources = {}

    def fake_update(ticker, ctx=None, source=None):
        used_sources[ticker] = source
        return 0

    monkeypatch.setattr("src.data.fetcher.update_price_cache", fake_update)

    with patch("moomoo.OpenQuoteContext", side_effect=TypeError("bad kwarg")):
        results = update_price_cache_batch(["AAPL", "MSFT"])

    assert set(results) == {"AAPL", "MSFT"}
    assert used_sources == {"AAPL": "yfinance", "MSFT": "yfinance"}


def test_update_price_cache_retries_with_yfinance_when_moomoo_empty(monkeypatch):
    """moomooが0件を返したら黙って未更新にせずyfinanceで取り直す

    fetch_from_moomoo は個別銘柄の取得失敗も空DataFrameに丸めるため、そのままだと
    価格が古いままSL/TP判定に使われてしまう。
    """
    from datetime import date, timedelta

    monkeypatch.setattr("src.data.fetcher.settings.data_source", "moomoo")
    monkeypatch.setattr(
        "src.data.fetcher.get_last_cached_date",
        lambda t: date.today() - timedelta(days=10),
    )
    monkeypatch.setattr("src.data.fetcher.save_to_cache", lambda t, df: len(df))
    monkeypatch.setattr(
        "src.data.fetcher.fetch_from_moomoo",
        lambda ticker, start, end=None, ctx=None: pd.DataFrame(),
    )
    monkeypatch.setattr(
        "src.data.fetcher.fetch_from_yfinance",
        lambda ticker, start, end=None: _make_df(),
    )

    assert update_price_cache("AAPL") == len(_make_df())


def test_update_price_cache_skips_yfinance_retry_without_fetch_window(monkeypatch):
    """取得対象期間が無い（前日分まで取得済み）ときは再取得しない"""
    from datetime import date, timedelta

    monkeypatch.setattr("src.data.fetcher.settings.data_source", "moomoo")
    monkeypatch.setattr(
        "src.data.fetcher.get_last_cached_date",
        lambda t: date.today() - timedelta(days=1),
    )
    monkeypatch.setattr("src.data.fetcher.save_to_cache", lambda t, df: len(df))
    monkeypatch.setattr(
        "src.data.fetcher.fetch_from_moomoo",
        lambda ticker, start, end=None, ctx=None: pd.DataFrame(),
    )

    def unexpected(ticker, start, end=None):
        raise AssertionError("yfinanceを呼んではいけない")

    monkeypatch.setattr("src.data.fetcher.fetch_from_yfinance", unexpected)

    assert update_price_cache("AAPL") == 0
