"""Against a real OTAPI 1688 search response.

tests/fixtures/otapi-1688/_search.json is a BatchSearchItemsFrame response
captured on 2026-10-19 for "T恤 批发", trimmed to five hits (see its "note").
Unlike every other fixture in this suite, nobody wrote it: these tests pin the
preset's search map to what the provider actually returns, so a change on
either side shows up here first.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import source as source_mod  # noqa: E402
from normalize import normalize_search_entry  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "otapi-1688"
RAW = json.loads((FIXTURE_DIR / "_search.json").read_text(encoding="utf-8"))
CONFIG = source_mod.otapi_1688_config("")
SOURCE = source_mod.FixtureSource(FIXTURE_DIR, platform="1688", config=CONFIG)
HITS = SOURCE.search_raw("T恤 批发")
CJK = re.compile(r"[一-鿿]")


def listed(item_id: str, quantity: int | None = None) -> dict:
    hit = next(h for h in HITS if h["Id"] == f"abb-{item_id}")
    return normalize_search_entry(hit, platform="1688", config=CONFIG, quantity=quantity).to_item()


def featured(item_id: str, name: str):
    hit = next(h for h in HITS if h["Id"] == f"abb-{item_id}")
    return next((e["Value"] for e in hit["FeaturedValues"] if e["Name"] == name), None)


def test_the_hits_are_where_the_preset_looks():
    assert len(HITS) == len(RAW["ids"]) == 5
    assert SOURCE.search("x") == RAW["ids"]
    assert all(not i.startswith("abb-") for i in SOURCE.search("x"))


@pytest.mark.parametrize("item_id", RAW["ids"])
def test_every_hit_becomes_a_listed_record(item_id):
    record = listed(item_id)
    assert record["status"] == "listed"
    assert record["itemId"] == item_id
    assert record["url"] == f"https://detail.1688.com/offer/{item_id}.html"
    # The provider's own URL for the listing matches the one we build.
    hit = next(h for h in HITS if h["Id"] == f"abb-{item_id}")
    assert hit["ExternalItemUrl"] == record["url"]


@pytest.mark.parametrize("item_id", RAW["ids"])
def test_titles_are_split_by_language(item_id):
    record = listed(item_id)
    assert record["titleCn"] and CJK.search(record["titleCn"])
    assert record["titleEn"] and not CJK.search(record["titleEn"])


@pytest.mark.parametrize("item_id", RAW["ids"])
def test_sales_is_the_30_day_figure_and_says_so(item_id):
    sales = listed(item_id)["sales"]
    assert sales["count"] == int(featured(item_id, "SalesInLast30Days"))
    assert sales["window"] == "last_30_days"
    assert sales["isApproximate"] is False
    # Unit unstated on purpose: nothing says pieces rather than orders.
    assert sales["unit"] is None
    # A typed number, so nothing is quoted as listing text.
    assert sales["sourceTextCn"] is None


@pytest.mark.parametrize("item_id", RAW["ids"])
def test_prices_are_cny_and_shop_ids_carry_no_prefix(item_id):
    record = listed(item_id)
    assert record["price"]["currency"] == "CNY"
    assert record["shopNameCn"]
    assert record["shopId"] and not record["shopId"].startswith("abb-")


def test_a_promotion_price_is_the_price_and_the_regular_one_is_struck_through():
    record = listed("669724481680")
    assert record["price"]["value"] == 3.14
    assert record["originalPrice"]["value"] == 3.3
    assert record["discountPct"] == pytest.approx(4.8, abs=0.1)


def test_without_a_promotion_there_is_no_discount():
    record = listed("623219903269")
    assert record["price"]["value"] == record["originalPrice"]["value"] == 24.9
    assert record["discountPct"] is None


def test_the_ladder_comes_through_the_search_page():
    tiers = listed("623219903269")["priceTiers"]
    assert [(t["minQty"], t["maxQty"], t["unitPrice"]["value"]) for t in tiers] == [
        (1, 99, 26.9),
        (100, 4999, 25.9),
        (5000, None, 24.9),
    ]


def test_the_headline_is_the_cheapest_rung():
    # The fact the quote exists for: 1688's headline is the price at the
    # largest order.
    record = listed("623219903269")
    assert record["price"]["value"] == min(t["unitPrice"]["value"] for t in record["priceTiers"])


@pytest.mark.parametrize(
    "item_id,quantity,unit,meets",
    [
        ("623219903269", 50, 26.9, True),
        ("623219903269", 500, 25.9, True),
        ("623219903269", 8000, 24.9, True),
        # Below the first rung (2 pieces): priced at that rung, 4.3 — not the
        # 3.3 headline or the 3.14 promotion, which would understate by 30%.
        ("669724481680", 1, 4.3, False),
        ("669724481680", 2, 4.3, True),
        ("672715568844", 10, 40.0, True),
    ],
)
def test_real_ladders_are_quoted_without_understating(item_id, quantity, unit, meets):
    quote = listed(item_id, quantity=quantity)["quote"]
    assert quote["unitPrice"]["value"] == unit
    assert quote["subtotal"]["value"] == round(unit * quantity, 2)
    assert quote["meetsMinimumOrder"] is meets


def test_a_hit_without_a_ladder_quotes_its_flat_price():
    quote = listed("44153134578", quantity=10)["quote"]
    assert quote["unitPrice"]["value"] == 13.0
    assert quote["meetsMinimumOrder"] is None      # nothing states a minimum


def test_stock_is_read_and_flagged():
    stock = listed("623219903269")["stock"]
    hit = next(h for h in HITS if h["Id"] == "abb-623219903269")
    assert stock["value"] == hit["MasterQuantity"]
    assert stock["isApproximate"] is True
