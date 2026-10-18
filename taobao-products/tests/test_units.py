"""Table-driven tests for the Chinese commerce parsers.

Every case here is a shape seen on a real listing or a shape a provider is
known to send. The width and myriad cases are the ones that silently produce a
wrong number rather than an error, so they are the bulk of the table.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import units  # noqa: E402


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2万", 20_000.0),
        ("2万+", 20_000.0),
        ("1.5万", 15_000.0),
        ("2.3亿", 230_000_000.0),
        ("3千", 3_000.0),
        ("1,280", 1_280.0),
        ("１２８", 128.0),          # fullwidth digits
        ("￥1,280.50", 1_280.5),
        ("128.00", 128.0),
        ("0", 0.0),
        ("", None),
        (None, None),
        ("无", None),
    ],
)
def test_parse_cn_number(text, expected):
    assert units.parse_cn_number(text) == expected


def test_thousands_separator_does_not_swallow_prose():
    # A comma that is not a thousands group must not merge two numbers.
    assert units.parse_cn_number("共1,2件") == 1.0


@pytest.mark.parametrize(
    "text,low,high",
    [
        ("¥128.00", 128.0, None),
        ("￥128.00", 128.0, None),          # fullwidth yen
        ("128元", 128.0, None),
        ("128.00-298.00", 128.0, 298.0),
        ("￥128.00-￥298.00", 128.0, 298.0),
        ("128~298", 128.0, 298.0),
        ("128～298", 128.0, 298.0),         # fullwidth tilde
        ("128—298", 128.0, 298.0),          # em dash
        ("298-128", 128.0, 298.0),          # reversed span is sorted
        ("1.5万-2万", 15_000.0, 20_000.0),
        ("１２８．００", 128.0, None),
    ],
)
def test_parse_price(text, low, high):
    money = units.parse_price(text)
    assert money is not None
    assert money.value == low
    assert money.value_max == high
    assert money.source_text_cn == text


def test_parse_price_keeps_numeric_input_without_inventing_source_text():
    money = units.parse_price(128)
    assert money is not None and money.value == 128.0
    assert money.source_text_cn is None


@pytest.mark.parametrize(
    "text,count,approx,window,unit",
    [
        ("月销1000+", 1000, True, "monthly", None),
        ("月销 1000", 1000, False, "monthly", None),
        ("已售2万+", 20_000, True, "cumulative", None),
        ("累计销量 5万", 50_000, False, "cumulative", None),
        ("30天销量1200", 1200, False, "last_30_days", None),
        ("100+人付款", 100, True, None, "buyers"),
        ("成交1203笔", 1203, False, None, "orders"),
        ("已售500件", 500, False, "cumulative", "items"),
        ("1000以上", 1000, True, None, None),
        ("", None, None, None, None),
    ],
)
def test_parse_sales(text, count, approx, window, unit):
    sales = units.parse_sales(text)
    if count is None:
        assert sales is None
        return
    assert sales is not None
    assert sales.count == count
    assert sales.is_approximate is approx
    assert sales.window == window
    assert sales.unit == unit
    assert sales.source_text_cn == text


def test_monthly_and_cumulative_are_not_interchangeable():
    monthly = units.parse_sales("月销1000+")
    cumulative = units.parse_sales("已售1000+")
    assert monthly and cumulative
    assert monthly.count == cumulative.count
    assert monthly.window != cumulative.window


@pytest.mark.parametrize(
    "text,value,unit",
    [
        ("起订量：2件", 2, "件"),
        ("100件起批", 100, "件"),
        ("≥1000", 1000, None),
        ("1", 1, None),
        ("2个", 2, "个"),
        ("", None, None),
    ],
)
def test_parse_quantity(text, value, unit):
    quantity = units.parse_quantity(text)
    if value is None:
        assert quantity is None
        return
    assert quantity is not None
    assert quantity.value == value
    assert quantity.unit_cn == unit


def test_parse_quantity_numeric_input_has_no_source_text():
    quantity = units.parse_quantity(2)
    assert quantity is not None and quantity.value == 2
    assert quantity.source_text_cn is None


@pytest.mark.parametrize(
    "text,included,fee,cod",
    [
        ("包邮", True, 0.0, False),
        ("免运费", True, 0.0, False),
        ("卖家承担运费", True, 0.0, False),
        ("运费10元", False, 10.0, False),
        ("快递：12.50", False, 12.5, False),
        ("到付", None, None, True),
        ("货到付款", None, None, True),
        ("0", True, 0.0, False),
        ("8.00", False, 8.0, False),
    ],
)
def test_parse_shipping(text, included, fee, cod):
    shipping = units.parse_shipping(text)
    assert shipping is not None
    assert shipping.included is included
    assert shipping.cash_on_delivery is cod
    if fee is None:
        assert shipping.fee is None
    else:
        assert shipping.fee is not None and shipping.fee.value == fee


def test_unrecognised_shipping_still_keeps_the_source():
    shipping = units.parse_shipping("请咨询客服")
    assert shipping is not None
    assert shipping.included is None
    assert shipping.fee is None
    assert shipping.source_text_cn == "请咨询客服"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1-99件", (1, 99)),
        ("2-99件", (2, 99)),
        ("100-999件", (100, 999)),
        ("≥1000件", (1000, None)),
        (">=1000", (1000, None)),
        ("1000件以上", (1000, None)),
        ("1000起", (1000, None)),
        ("", None),
        ("面议", None),
    ],
)
def test_parse_tier_range(text, expected):
    assert units.parse_tier_range(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("高于同行20.15%", 20.15),
        ("低于同行3.2%", -3.2),
        ("持平", 0.0),
        ("高于20%", 20.0),
        ("", None),
        ("暂无", None),
    ],
)
def test_parse_peer_delta(text, expected):
    assert units.parse_peer_delta(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("付款后15天内发货", 15),
        ("7天内发货", 7),
        ("预售商品,付款后15天内发货", 15),
        ("现货,48小时内发货", None),
        ("", None),
    ],
)
def test_parse_presale_days(text, expected):
    assert units.parse_presale_days(text) == expected


def test_normalize_text_folds_width_and_space():
    assert units.normalize_text("　￥１２８　") == "¥128"
    assert units.normalize_text("a  b") == "a b"
    assert units.normalize_text(None) == ""


@pytest.mark.parametrize(
    "text,count,window",
    [
        ("30天销量1200", 1200, "last_30_days"),
        ("近30日成交 860", 860, "last_30_days"),
        ("月销2万+", 20_000, "monthly"),
    ],
)
def test_window_marker_digits_are_not_read_as_the_count(text, count, window):
    # Regression: "30天销量1200" used to parse as 30, because the window marker
    # carries digits and the number scan stopped at the first one it saw.
    sales = units.parse_sales(text)
    assert sales is not None
    assert (sales.count, sales.window) == (count, window)


def test_myriad_multiplication_is_exact():
    # Regression: float arithmetic gave 229999999.99999997 for 2.3亿.
    assert units.parse_cn_number("2.3亿") == 230_000_000.0
    assert units.parse_cn_number("1.1万") == 11_000.0
    assert units.parse_cn_number("0.7万") == 7_000.0
