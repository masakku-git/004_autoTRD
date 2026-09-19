"""reconcile_fills.build_probe_line: 売却代金が買付余力として使えているかを判定できる1行を組み立てる"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from reconcile_fills import build_probe_line  # noqa: E402

FUNDS = {"cash": 788.76, "us_cash": 788.76, "avl_withdrawal_cash": 788.76, "frozen_cash": 0.0, "power": 788.76}


def test_line_shows_usable_when_max_buy_matches_cash():
    line = build_probe_line("post_fill", FUNDS, ("T", 25.4, 31), [("V", 1, 369.0)])
    assert line.startswith("BUYPOWER-PROBE label=post_fill")
    assert "sell_proceeds=369.00" in line and "sells_today=['Vx1@369.00']" in line
    # 788.76/25.4=31.05→31 が cash_implied。max_cash_buy=31 と一致 → 売却代金は使えている
    assert "max_cash_buy=31 cash_implied=31 if_proceeds_unusable=16" in line


def test_line_without_ref_and_without_sells():
    line = build_probe_line("manual", FUNDS, None, [])
    assert "sell_proceeds=0.00" in line and "max_cash_buy" not in line


def test_us_cash_none_falls_back_to_cash():
    funds = {**FUNDS, "us_cash": None}
    assert "cash_implied=31" in build_probe_line("x", funds, ("T", 25.4, 31), [])
