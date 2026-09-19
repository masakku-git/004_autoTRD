"""Test account fund field selection."""
import pandas as pd
import pytest

from src.broker.account import _pick_usd_cash


def test_prefers_us_cash_over_obsolete_cash():
    funds = pd.DataFrame([{"cash": 100.0, "us_cash": 788.76}])
    assert _pick_usd_cash(funds) == 788.76


def test_falls_back_to_cash_when_us_cash_is_na():
    funds = pd.DataFrame([{"cash": 100.0, "us_cash": "N/A"}])
    assert _pick_usd_cash(funds) == 100.0


def test_falls_back_to_cash_when_us_cash_missing():
    funds = pd.DataFrame([{"cash": 100.0}])
    assert _pick_usd_cash(funds) == 100.0


def test_raises_when_no_cash_field():
    with pytest.raises(RuntimeError):
        _pick_usd_cash(pd.DataFrame([{"power": 1.0}]))
