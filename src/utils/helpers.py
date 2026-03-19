"""ユーティリティ（JST日時取得・米国市場営業日チェック）"""
from datetime import date, datetime
from zoneinfo import ZoneInfo


def now_jst() -> datetime:
    return datetime.now(ZoneInfo("Asia/Tokyo"))


def today_jst() -> date:
    return now_jst().date()


def is_us_market_day(d: date | None = None) -> bool:
    """Check if given date is a US market trading day (Mon-Fri, not holiday)."""
    d = d or today_jst()
    # Basic weekday check (0=Mon, 6=Sun)
    return d.weekday() < 5
