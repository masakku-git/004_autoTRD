"""ユーティリティ（JST/UTC日時取得・米国市場営業日チェック）"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


def now_jst() -> datetime:
    return datetime.now(ZoneInfo("Asia/Tokyo"))


def utcnow() -> datetime:
    """Naive UTC now（datetime.utcnow() のPython 3.12非推奨対応）。

    DBのtimestamp列（timezoneなし）および既存のnaive datetime演算と互換を
    保つため、tzinfoを外したUTC時刻を返す。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_jst() -> date:
    return now_jst().date()


def is_us_market_day(d: date | None = None) -> bool:
    """Check if given date is a US market trading day (Mon-Fri, not holiday)."""
    d = d or today_jst()
    # Basic weekday check (0=Mon, 6=Sun)
    return d.weekday() < 5
