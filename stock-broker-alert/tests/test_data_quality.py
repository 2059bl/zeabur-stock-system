"""
資料品質測試 — Check 1-8 驗證
"""
import pytest
from src.blood_absorption import _data_quality_checks


def make_record(**kwargs):
    base = {
        "trade_date": "2026-07-30",
        "stock_code": "2330",
        "volume": 10000,
        "top_brokers": [
            {"broker_code": "1", "net_volume": 500, "buy_volume": 700, "sell_volume": 200},
            {"broker_code": "2", "net_volume": 300, "buy_volume": 500, "sell_volume": 200},
        ],
    }
    base.update(kwargs)
    return base


def test_check1_negative_volume():
    r = make_record(volume=-1)
    issues = _data_quality_checks(r)
    assert "CHECK1_NEGATIVE_VOLUME" in issues


def test_check1_pass():
    r = make_record(volume=0)
    issues = _data_quality_checks(r)
    assert "CHECK1_NEGATIVE_VOLUME" not in issues


def test_check3_sort():
    r = make_record(top_brokers=[
        {"broker_code": "1", "net_volume": 300, "buy_volume": 400, "sell_volume": 100},
        {"broker_code": "2", "net_volume": 500, "buy_volume": 700, "sell_volume": 200},
    ])
    issues = _data_quality_checks(r)
    assert "CHECK3_BROKER_SORT_ERROR" in issues


def test_check3_pass():
    r = make_record()
    issues = _data_quality_checks(r)
    assert "CHECK3_BROKER_SORT_ERROR" not in issues
