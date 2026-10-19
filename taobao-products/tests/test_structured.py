"""The normaliser against a structured provider: typed numbers, PascalCase
keys, name/value lists, prefixed ids and minimum-only wholesale rungs.

The config here is a *hypothetical* structured provider written for these
tests. It is not a claim about OTAPI's response shape — that gets written from
a real response in Phase 3 of VALIDATION.md. What these pin down is that once
such a map exists, nothing in the normaliser silently drops what it points at.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import source as source_mod  # noqa: E402
from normalize import normalize, normalize_search_entry  # noqa: E402
from source import RawProduct  # noqa: E402


def structured_config(**hints) -> source_mod.SourceConfig:
    config = source_mod.otapi_1688_config("k")
    config.field_map = {
        "item_id": "Result.Item.Id",
        "title_cn": "Result.Item.OriginalTitle",
        "title_en": "Result.Item.Title",
        "price": "Result.Item.Price.OriginalPrice",
        "tiers": "Result.Item.QuantityRanges",
        "min_order_quantity": "Result.Item.FirstLotQuantity",
        "sales": "Result.Item.FeaturedValues[Name=SalesInLast30Days].Value",
        "stock": "Result.Item.MasterQuantity",
        "shop_name_cn": "Result.Item.VendorName",
        "images": "Result.Item.Pictures",
    }
    config.search_field_map = {
        "item_id": "Id",
        "title_cn": "OriginalTitle",
        "title_en": "Title",
        "price": "Price.OriginalPrice",
        "sales": "FeaturedValues[Name=TotalSales].Value",
        "image": "MainPictureUrl",
    }
    config.hints = {
        "tiers": {"min_qty": "MinQuantity", "price": "Price.OriginalPrice"},
        "sales": {"window": "last_30_days", "unit": "orders"},
        "stock": {"approximate": False},
        **hints,
    }
    return config


def item(**overrides):
    base = {
        "Id": "abb-612345678901",
        "OriginalTitle": "厂家直销 纯棉T恤 空白打底衫",
        "Title": "Factory direct cotton blank T-shirt",
        "Price": {"OriginalPrice": 12.0, "CurrencyCode": "CNY"},
        "QuantityRanges": [
            {"MinQuantity": 100, "Price": {"OriginalPrice": 10.5}},
            {"MinQuantity": 2, "Price": {"OriginalPrice": 12.0}},
            {"MinQuantity": 1000, "Price": {"OriginalPrice": 9.0}},
        ],
        "FirstLotQuantity": 2,
        "FeaturedValues": [
            {"Name": "TotalSales", "Value": "20000"},
            {"Name": "SalesInLast30Days", "Value": "860"},
        ],
        "MasterQuantity": 99999,
        "VendorName": "义乌服装厂",
        "Pictures": [{"Url": "https://cbu01.alicdn.com/a.jpg"}],
    }
    base.update(overrides)
    return {"ErrorCode": "Ok", "Result": {"Item": base}}


def run(payload, *, quantity=None, **hints):
    raw = RawProduct(item_id="612345678901", platform="1688", payload=payload)
    return normalize(raw, config=structured_config(**hints), quantity=quantity).to_item()


# ----------------------------------------------------------------- ids/urls

def test_the_record_carries_the_platform_id_not_the_wire_id():
    record = run(item())
    assert record["itemId"] == "612345678901"
    assert record["url"] == "https://detail.1688.com/offer/612345678901.html"


def test_a_wire_id_on_the_raw_product_never_reaches_the_url():
    raw = RawProduct(item_id="abb-612345678901", platform="1688", payload=item())
    record = normalize(raw, config=structured_config()).to_item()
    assert "abb-" not in record["url"]
    assert record["itemId"] == "612345678901"


def test_a_refused_record_also_drops_the_prefix():
    raw = RawProduct(item_id="abb-1", platform="1688", payload=None, status="not_found")
    record = normalize(raw, config=structured_config()).to_item()
    assert record["itemId"] == "1"
    assert record["url"].endswith("/offer/1.html")


# -------------------------------------------------------------------- titles

def test_chinese_and_english_titles_land_in_their_own_fields():
    record = run(item())
    assert record["titleCn"] == "厂家直销 纯棉T恤 空白打底衫"
    assert record["titleEn"] == "Factory direct cotton blank T-shirt"


def test_an_english_title_never_lands_in_title_cn():
    # A provider asked for English may put the translation where the map
    # expects Chinese. A field named for Chinese holding English is worse
    # than a null.
    record = run(item(OriginalTitle="Factory direct cotton T-shirt", Title=None))
    assert record["titleCn"] is None
    assert record["titleEn"] == "Factory direct cotton T-shirt"
    assert record["status"] == "ok"


def test_chinese_in_the_english_slot_is_moved_back():
    record = run(item(OriginalTitle=None, Title="厂家直销 纯棉T恤"))
    assert record["titleCn"] == "厂家直销 纯棉T恤"
    assert record["titleEn"] is None


def test_no_title_at_all_is_unparsable():
    record = run(item(OriginalTitle=None, Title=None))
    assert record["status"] == "unparsable"


# ------------------------------------------------------------- the ladder

def test_minimum_only_rungs_are_closed_by_the_next_rung():
    # Regression: a structured ladder used to be dropped whole, because the
    # rung parser only read "2-99件" text. With no ladder, the quote — the
    # main differentiator on 1688 — had nothing to walk.
    tiers = run(item())["priceTiers"]
    assert [(t["minQty"], t["maxQty"], t["unitPrice"]["value"]) for t in tiers] == [
        (2, 99, 12.0),
        (100, 999, 10.5),
        (1000, None, 9.0),
    ]


def test_typed_rungs_quote_no_listing_text():
    for tier in run(item())["priceTiers"]:
        assert tier["sourceTextCn"] is None


@pytest.mark.parametrize("quantity,unit", [(2, 12.0), (500, 10.5), (5000, 9.0)])
def test_the_quote_walks_a_structured_ladder(quantity, unit):
    quote = run(item(), quantity=quantity)["quote"]
    assert quote["unitPrice"]["value"] == unit
    assert quote["meetsMinimumOrder"] is True


def test_below_the_typed_minimum_order_is_flagged():
    quote = run(item(), quantity=1)["quote"]
    assert quote["meetsMinimumOrder"] is False


def test_an_explicit_max_wins_over_the_derived_one():
    ladder = [
        {"MinQuantity": 1, "MaxQuantity": 49, "Price": {"OriginalPrice": 5.0}},
        {"MinQuantity": 100, "Price": {"OriginalPrice": 4.0}},
    ]
    record = run(
        item(QuantityRanges=ladder),
        tiers={"min_qty": "MinQuantity", "max_qty": "MaxQuantity", "price": "Price.OriginalPrice"},
    )
    assert [(t["minQty"], t["maxQty"]) for t in record["priceTiers"]] == [(1, 49), (100, None)]


def test_a_rung_without_a_price_is_skipped_not_half_built():
    ladder = [{"MinQuantity": 2, "Price": {}}, {"MinQuantity": 50, "Price": {"OriginalPrice": 8.0}}]
    tiers = run(item(QuantityRanges=ladder))["priceTiers"]
    assert [(t["minQty"], t["maxQty"]) for t in tiers] == [(50, None)]


# --------------------------------------------------------------------- sales

def test_a_structured_sales_figure_takes_its_window_from_the_hint():
    sales = run(item())["sales"]
    assert sales == {
        "count": 860,
        "isApproximate": False,
        "window": "last_30_days",
        "unit": "orders",
        # Typed by the provider: there is no listing text to cite.
        "sourceTextCn": None,
    }


def test_a_typed_integer_sales_figure_cites_nothing():
    # Same bug class as the min_num one fixed earlier: stringifying a number
    # would put a sourceTextCn on the record that the listing never printed.
    fv = [{"Name": "SalesInLast30Days", "Value": 860}]
    assert run(item(FeaturedValues=fv))["sales"]["sourceTextCn"] is None


def test_the_hint_never_overrides_what_the_text_says():
    fv = [{"Name": "SalesInLast30Days", "Value": "已售2万+"}]
    sales = run(item(FeaturedValues=fv))["sales"]
    assert sales["window"] == "cumulative"      # from the text, not the hint
    assert sales["isApproximate"] is True


def test_a_missing_name_value_entry_is_null_not_another_entry():
    fv = [{"Name": "TotalSales", "Value": "20000"}]
    assert run(item(FeaturedValues=fv))["sales"] is None


# --------------------------------------------------------------------- stock

def test_stock_precision_comes_from_the_provider_hint():
    assert run(item())["stock"] == {"value": 99999, "isApproximate": False, "sourceTextCn": None}


def test_stock_defaults_to_approximate_when_the_provider_says_nothing():
    record = run(item(), stock={})
    assert record["stock"]["isApproximate"] is True


# ------------------------------------------------------------ search entries

def search_hit(**overrides):
    hit = {
        "Id": "abb-444555666",
        "OriginalTitle": "蓝牙耳机 批发",
        "Title": "Bluetooth earphones wholesale",
        "Price": {"OriginalPrice": 18.5},
        "FeaturedValues": [{"Name": "TotalSales", "Value": "3200"}],
        "MainPictureUrl": "https://cbu01.alicdn.com/b.jpg",
    }
    hit.update(overrides)
    return hit


def test_a_structured_search_hit_is_listed_with_the_platform_id():
    record = normalize_search_entry(
        search_hit(), platform="1688", config=structured_config(sales={})
    ).to_item()
    assert record["status"] == "listed"
    assert record["itemId"] == "444555666"
    assert record["url"] == "https://detail.1688.com/offer/444555666.html"
    assert record["titleCn"] == "蓝牙耳机 批发"
    assert record["titleEn"] == "Bluetooth earphones wholesale"
    assert record["price"]["value"] == 18.5
    assert record["sales"]["count"] == 3200
    assert record["images"] == ["https://cbu01.alicdn.com/b.jpg"]


def test_an_english_only_search_hit_keeps_title_cn_empty():
    record = normalize_search_entry(
        search_hit(OriginalTitle=None), platform="1688", config=structured_config()
    ).to_item()
    assert record["titleCn"] is None
    assert record["titleEn"] == "Bluetooth earphones wholesale"
    assert record["status"] == "listed"


def test_a_number_serialised_as_a_string_is_not_cited():
    fv = [{"Name": "SalesInLast30Days", "Value": " 860 "}]
    sales = run(item(FeaturedValues=fv))["sales"]
    assert sales["count"] == 860 and sales["sourceTextCn"] is None
