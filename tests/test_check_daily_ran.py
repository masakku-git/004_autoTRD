"""日次実行の死活監視: ログから今日の実行状況を判定する"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from check_daily_ran import daily_run_status  # noqa: E402

TODAY = date(2026, 9, 18)
STARTED = "2026-09-18 22:00:02 [INFO] autotrd: Daily run started at 2026-09-18T13:00:02"
COMPLETED = "2026-09-18 22:01:03 [INFO] autotrd: Daily run completed"


def test_completed_run_is_ok():
    assert daily_run_status("\n".join([STARTED, COMPLETED]), TODAY) == "ok"


def test_market_holiday_skip_is_ok():
    log = STARTED + "\n2026-09-18 22:00:02 [INFO] autotrd: Not a US market day, skipping"
    assert daily_run_status(log, TODAY) == "ok"


def test_started_but_not_completed_is_crashed():
    assert daily_run_status(STARTED, TODAY) == "crashed"


def test_no_log_for_today_is_not_run():
    yesterday = "2026-09-17 22:00:02 [INFO] autotrd: Daily run started at x\n2026-09-17 22:01:00 [INFO] autotrd: Daily run completed"
    assert daily_run_status(yesterday, TODAY) == "not_run"
    assert daily_run_status("", TODAY) == "not_run"


def test_yesterdays_completion_does_not_hide_todays_crash():
    log = "2026-09-17 22:01:00 [INFO] autotrd: Daily run completed\n" + STARTED
    assert daily_run_status(log, TODAY) == "crashed"
