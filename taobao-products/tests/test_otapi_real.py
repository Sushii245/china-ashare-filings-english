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


# --------------------------------------------------------- detail responses
#
# tests/fixtures/otapi-1688/<id>.json are real BatchGetItemFullInfo responses
# captured 2026-10-20 for three ids from the search above, plus a bogus id,
# trimmed as each file's "note" says.

from normalize import normalize  # noqa: E402


def detail(item_id: str, quantity: int | None = None) -> dict:
    return normalize(SOURCE.fetch_product(item_id), config=CONFIG, quantity=quantity).to_item()


DETAIL_IDS = ["623219903269", "669724481680", "672715568844"]


@pytest.mark.parametrize("item_id", DETAIL_IDS)
def test_every_real_detail_normalises(item_id):
    record = detail(item_id)
    assert record["status"] == "ok"
    assert record["itemId"] == item_id
    assert record["url"] == f"https://detail.1688.com/offer/{item_id}.html"
    assert CJK.search(record["titleCn"]) and not CJK.search(record["titleEn"])


@pytest.mark.parametrize(
    "item_id,moq,tiers",
    [
        ("623219903269", 1, [(1, 99, 26.9), (100, 4999, 25.9), (5000, None, 24.9)]),
        ("669724481680", 2, [(2, 99, 4.3), (100, 9999, 4.0), (10000, None, 3.3)]),
        ("672715568844", 1, [(1, 998, 40.0), (999, None, 35.0)]),
    ],
)
def test_the_detail_carries_the_minimum_order_and_ladder(item_id, moq, tiers):
    record = detail(item_id)
    assert record["minOrderQuantity"]["value"] == moq
    assert [(t["minQty"], t["maxQty"], t["unitPrice"]["value"]) for t in record["priceTiers"]] == tiers


@pytest.mark.parametrize(
    "item_id,unit,headline",
    [("623219903269", 26.9, 24.9), ("669724481680", 4.3, 3.14), ("672715568844", 40.0, 33.25)],
)
def test_a_50_piece_order_is_never_quoted_at_the_headline(item_id, unit, headline):
    record = detail(item_id, quantity=50)
    assert record["price"]["value"] == headline
    assert record["quote"]["unitPrice"]["value"] == unit > headline
    assert record["quote"]["subtotal"]["value"] == round(unit * 50, 2)
    assert record["quote"]["meetsMinimumOrder"] is True


def test_one_piece_under_a_minimum_of_two_is_flagged():
    quote = detail("669724481680", quantity=1)["quote"]
    assert quote["meetsMinimumOrder"] is False
    assert quote["unitPrice"]["value"] == 4.3


@pytest.mark.parametrize("item_id,promo,regular", [("669724481680", 3.14, 3.3), ("672715568844", 33.25, 35.0)])
def test_a_promotion_is_not_turned_into_a_price_span(item_id, promo, regular):
    # The variants carry the regular price. Mixing it with the promotion used
    # to read as a 3.14-3.30 span.
    record = detail(item_id)
    assert record["price"]["value"] == promo
    assert record["price"]["valueMax"] is None
    assert record["originalPrice"]["value"] == regular
    assert record["discountPct"] == round((regular - promo) / regular * 100, 1)


def test_the_shop_is_the_registered_company_not_the_login():
    # The item's VendorName is "tfih666" here; the vendor's ShopName is the
    # company a buyer would contract with.
    record = detail("623219903269")
    assert record["shopNameCn"] == "青岛圣英针纺服饰有限公司"
    assert record["shopId"] and not record["shopId"].startswith("abb-")


def test_three_seller_scores_without_invented_chinese_labels():
    scores = {s["aspect"]: s for s in detail("623219903269")["shopScores"]}
    assert set(scores) == {"description_match", "service", "logistics"}
    assert (scores["description_match"]["score"], scores["service"]["score"],
            scores["logistics"]["score"]) == (3.0, 4.0, 3.4)
    assert all(s["aspectCn"] is None for s in scores.values())


def test_variant_axes_are_the_sellers_own_chinese():
    record = detail("623219903269")
    raw = json.loads((FIXTURE_DIR / "623219903269.json").read_text(encoding="utf-8"))
    kept = raw["payload"]["Result"]["Item"]["ConfiguredItems"]
    assert record["skuCount"] == len(kept)
    first = record["skus"][0]
    assert first["optionsCn"] == {"颜色": "白色", "尺码": "S"}
    assert first["optionsEn"] is None
    assert first["stock"] == kept[0]["Quantity"]


def test_attributes_leave_out_the_variant_axes_and_keep_every_value():
    attributes = detail("672715568844")["attributesCn"]
    # Variant axes (IsConfigurator) are in the SKUs, not here.
    assert "颜色" not in attributes and "尺码" not in attributes
    # Listed seven times in the response; a plain dict would keep only one.
    assert attributes["主要销售地区"].count("、") == 6


def test_images_are_read_from_capitalised_keys():
    assert detail("623219903269")["images"][0].startswith("https://cbu01.alicdn.com/")


def test_the_bogus_id_is_not_found():
    record = detail("1")
    assert record["status"] == "not_found"
    assert record["statusDetail"] == "NotFound: Item with id='1' not found."


# ------------------------------------------------- a zero is not a price

import copy  # noqa: E402


def zeroed(item_id: str, mutate) -> dict:
    hit = copy.deepcopy(next(h for h in HITS if h["Id"] == f"abb-{item_id}"))
    mutate(hit)
    return hit


def test_a_zero_listing_price_is_unknown_not_free():
    # 1688 shows "面议" (price on request) as a zero. Quoting it would tell
    # the buyer the order is free — the most understated quote possible.
    hit = zeroed("44153134578", lambda h: h["Price"].__setitem__("OriginalPrice", 0.0))
    record = normalize_search_entry(hit, platform="1688", config=CONFIG, quantity=100).to_item()
    assert record["price"] is None
    assert record["quote"]["unitPrice"] is None
    assert record["quote"]["subtotal"] is None


def test_a_zero_rung_is_dropped_not_quoted():
    hit = zeroed("623219903269", lambda h: h["QuantityRanges"][2]["Price"].__setitem__("OriginalPrice", 0.0))
    record = normalize_search_entry(hit, platform="1688", config=CONFIG, quantity=8000).to_item()
    assert [t["unitPrice"]["value"] for t in record["priceTiers"]] == [26.9, 25.9]
    assert record["quote"]["unitPrice"]["value"] == 25.9


def test_a_zero_promotion_falls_back_to_nothing_rather_than_free():
    hit = zeroed("669724481680", lambda h: h["PromotionPrice"].__setitem__("OriginalPrice", 0.0))
    record = normalize_search_entry(hit, platform="1688", config=CONFIG).to_item()
    # The zero promotion is skipped and the regular price stands, not a null:
    # each alternative of the price path is read on its own.
    assert record["price"]["value"] == 3.3
    assert record["discountPct"] is None
