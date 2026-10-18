"""End-to-end tests over recorded payloads.

These run against :class:`FixtureSource`, so they never touch the network and
every branch of the normaliser is reachable. The fixtures cover the four
outcomes that matter: a full Taobao listing, a 1688 listing with a wholesale
ladder, a listing whose fields arrive in the awkward single-value shapes, and a
refusal from upstream.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from normalize import normalize  # noqa: E402
from source import FixtureSource  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def source() -> FixtureSource:
    return FixtureSource(FIXTURES)


def item(source: FixtureSource, item_id: str) -> dict:
    return normalize(source.fetch_product(item_id)).to_item()


# ------------------------------------------------------------------- taobao

def test_taobao_listing_prices_and_sales(source):
    record = item(source, "600530677383")
    assert record["status"] == "ok"
    assert record["platform"] == "taobao"
    assert record["url"].startswith("https://item.taobao.com/item.htm?id=")

    # The span across SKUs wins over the single price, which is its low end.
    assert record["price"]["value"] == 128.0
    assert record["price"]["valueMax"] == 298.0
    assert record["originalPrice"]["value"] == 358.0

    assert record["sales"] == {
        "count": 1000,
        "isApproximate": True,
        "window": "monthly",
        "unit": None,
        "sourceTextCn": "月销1000+",
    }
    assert record["stock"]["value"] == 4532


def test_taobao_listing_shipping_and_origin(source):
    record = item(source, "600530677383")
    assert record["shipping"]["included"] is True
    assert record["shipping"]["fee"]["value"] == 0.0
    assert record["shipping"]["cashOnDelivery"] is False
    assert record["shipping"]["shipsFromCn"] == "广东 深圳"


def test_taobao_skus_decode_the_packed_axis_string(source):
    record = item(source, "600530677383")
    assert len(record["skus"]) == 2
    first = record["skus"][0]
    assert first["skuId"] == "5001"
    assert first["optionsCn"] == {"颜色分类": "黑色", "版本": "标准版"}
    assert first["optionsEn"] is None          # nothing is transliterated
    assert first["price"]["value"] == 128.0
    assert first["stock"] == 2100
    assert record["skus"][1]["optionsCn"] == {"颜色分类": "白色", "版本": "降噪版"}


def test_taobao_shop_scores_carry_signed_peer_deltas(source):
    record = item(source, "600530677383")
    by_aspect = {s["aspect"]: s for s in record["shopScores"]}
    assert by_aspect["description_match"]["score"] == 4.8
    assert by_aspect["description_match"]["vsPeersPct"] == 20.15
    assert by_aspect["service"]["vsPeersPct"] == -3.2      # 低于 is negative
    assert by_aspect["logistics"]["vsPeersPct"] == 0.0     # 持平
    assert by_aspect["logistics"]["aspectCn"] == "物流服务"


def test_protocol_relative_images_become_absolute(source):
    record = item(source, "600530677383")
    assert record["images"] == [
        "https://img.alicdn.com/imgextra/i1/abc/O1CN01.jpg",
        "https://img.alicdn.com/imgextra/i2/abc/O1CN02.jpg",
    ]


def test_attributes_drop_the_numeric_property_ids(source):
    record = item(source, "600530677383")
    assert record["attributesCn"] == {
        "颜色分类": "黑色",
        "版本": "标准版",
        "品牌": "Soundcore",
    }


def test_in_stock_listing_is_not_marked_presale(source):
    record = item(source, "600530677383")
    assert record["isPresale"] is False
    assert record["presaleShipsWithinDays"] is None


# --------------------------------------------------------------------- 1688

def test_1688_listing_uses_the_offer_url_and_platform(source):
    record = item(source, "612345678901")
    assert record["platform"] == "1688"
    assert record["url"] == "https://detail.1688.com/offer/612345678901.html"


def test_1688_wholesale_ladder_is_sorted_and_open_topped(source):
    record = item(source, "612345678901")
    tiers = record["priceTiers"]
    assert [(t["minQty"], t["maxQty"]) for t in tiers] == [
        (2, 99),
        (100, 999),
        (1000, None),
    ]
    assert [t["unitPrice"]["value"] for t in tiers] == [12.0, 10.5, 9.0]
    assert tiers[0]["sourceTextCn"] == "2-99件 12.00"


def test_1688_min_order_quantity(source):
    record = item(source, "612345678901")
    assert record["minOrderQuantity"]["value"] == 2
    assert record["minOrderQuantity"]["unitCn"] == "件"


def test_1688_cumulative_sales_are_not_monthly(source):
    record = item(source, "612345678901")
    assert record["sales"]["count"] == 20_000
    assert record["sales"]["window"] == "cumulative"
    assert record["sales"]["isApproximate"] is True


def test_1688_buyer_paid_shipping(source):
    record = item(source, "612345678901")
    assert record["shipping"]["included"] is False
    assert record["shipping"]["fee"]["value"] == 10.0


def test_single_sku_arriving_as_a_bare_dict_is_still_a_list(source):
    # Providers send one SKU as a dict and many as a list. A parser that
    # assumes a list silently drops every single-variant listing.
    record = item(source, "612345678901")
    assert len(record["skus"]) == 1
    assert record["skus"][0]["optionsCn"] == {"颜色": "白色"}


def test_single_image_arriving_as_a_bare_string(source):
    record = item(source, "612345678901")
    assert record["images"] == ["https://cbu01.alicdn.com/img/ibank/x.jpg"]


def test_attribute_list_of_name_value_pairs(source):
    record = item(source, "612345678901")
    assert record["attributesCn"] == {"材质": "纯棉", "克重": "180g"}


def test_shop_scores_given_as_a_flat_chinese_keyed_map(source):
    record = item(source, "612345678901")
    by_aspect = {s["aspect"]: s["score"] for s in record["shopScores"]}
    assert by_aspect == {
        "description_match": 4.6,
        "service": 4.5,
        "logistics": 4.4,
    }


# ------------------------------------------------------------- awkward shapes

def test_fullwidth_price_and_comma_grouped_original(source):
    record = item(source, "555000111222")
    assert record["price"]["value"] == 128.0
    assert record["originalPrice"]["value"] == 1280.0


def test_payer_count_is_not_an_item_count(source):
    record = item(source, "555000111222")
    assert record["sales"]["unit"] == "buyers"
    assert record["sales"]["window"] is None
    assert record["sales"]["count"] == 100


def test_cash_on_delivery_leaves_included_unknown(source):
    record = item(source, "555000111222")
    assert record["shipping"]["cashOnDelivery"] is True
    assert record["shipping"]["included"] is None
    assert record["shipping"]["fee"] is None


def test_presale_dispatch_promise(source):
    record = item(source, "555000111222")
    assert record["isPresale"] is True
    assert record["presaleShipsWithinDays"] == 15


def test_numeric_min_num_does_not_invent_a_source_string(source):
    # min_num arrives as the integer 1 here. Stringifying it and storing "1" as
    # sourceTextCn would claim the listing said something it did not.
    record = item(source, "555000111222")
    assert record["minOrderQuantity"]["value"] == 1
    assert record["minOrderQuantity"]["sourceTextCn"] is None


def test_empty_containers_normalise_to_empty_not_null(source):
    record = item(source, "555000111222")
    assert record["skus"] == []
    assert record["images"] == []
    assert record["attributesCn"] == {}
    assert record["shopScores"] == []


def test_zero_stock_is_zero_not_missing(source):
    record = item(source, "555000111222")
    assert record["stock"]["value"] == 0


def test_stock_is_flagged_approximate_and_keeps_its_source(source):
    # The platform publishes a rounded figure, not a count. Returning a bare
    # integer would claim a precision nobody gave us.
    record = item(source, "600530677383")
    assert record["stock"]["isApproximate"] is True
    assert record["stock"]["sourceTextCn"] == "4532"


# ------------------------------------------------------------------ refusals

def test_risk_control_refusal_is_a_record_not_an_exception(source):
    record = item(source, "999000111222")
    assert record["status"] == "risk_control_blocked"
    assert "RGV587" in record["statusDetail"]
    assert record["titleCn"] is None
    assert record["price"] is None
    assert record["url"].endswith("999000111222")


def test_missing_item_reports_not_found(source):
    record = item(source, "000000000000")
    assert record["status"] == "not_found"
    assert record["titleCn"] is None


def test_payload_the_field_map_does_not_fit_is_unparsable(tmp_path):
    (tmp_path / "123.json").write_text('{"unexpected": {"shape": 1}}', encoding="utf-8")
    record = normalize(FixtureSource(tmp_path).fetch_product("123")).to_item()
    assert record["status"] == "unparsable"
    assert "field map" in record["statusDetail"]


def test_every_record_is_json_serialisable(source):
    import json

    for item_id in source.search(""):
        json.dumps(item(source, item_id), ensure_ascii=False)


# ------------------------------------------------- values that have no number

def test_negotiable_price_is_null_and_the_record_still_returns(tmp_path):
    # 面议 ("price on request") is common on 1688. There is no number to parse,
    # and guessing one would be worse than returning null.
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "定制五金件", "price": "面议", '
        '"sales": "面议", "post_fee": "面议"}}',
        encoding="utf-8",
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert record["status"] == "ok"
    assert record["titleCn"] == "定制五金件"
    assert record["price"] is None
    assert record["sales"] is None
    # Shipping keeps the source text even with nothing to type off it.
    assert record["shipping"]["included"] is None
    assert record["shipping"]["sourceTextCn"] == "面议"


def test_a_tier_without_a_parsable_price_is_dropped_not_half_built(tmp_path):
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "批发件", "price_ranges": ['
        '{"range": "1-99件", "price": "12.00"},'
        '{"range": "100-999件", "price": "面议"},'
        '{"range": "面议", "price": "9.00"}]}}',
        encoding="utf-8",
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert [(t["minQty"], t["maxQty"]) for t in record["priceTiers"]] == [(1, 99)]


def test_whitespace_only_title_is_unparsable_not_ok(tmp_path):
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "   ", "price": "9.90"}}', encoding="utf-8"
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert record["status"] == "unparsable"


def test_sku_axis_string_with_an_unexpected_arity_is_skipped(tmp_path):
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "商品", "skus": {"sku": ['
        '{"sku_id": "a", "properties_name": "broken:three:parts", "price": "1"},'
        '{"sku_id": "b", "properties_name": "1:2:颜色:红色", "price": "2"}]}}}',
        encoding="utf-8",
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert record["skus"][0]["optionsCn"] == {}        # kept, with no axes
    assert record["skus"][1]["optionsCn"] == {"颜色": "红色"}


def test_image_entries_without_a_url_are_dropped(tmp_path):
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "商品", "item_imgs": ['
        '{"url": "https://a.example/1.jpg"}, {"size": 3}, "", '
        '"not-a-url", "//b.example/2.jpg"]}}',
        encoding="utf-8",
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert record["images"] == ["https://a.example/1.jpg", "https://b.example/2.jpg"]


def test_a_shop_score_without_a_number_is_dropped(tmp_path):
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "商品", "shop_score": ['
        '{"title": "宝贝描述", "score": "暂无"},'
        '{"title": "卖家服务", "score": "4.5"}]}}',
        encoding="utf-8",
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert [s["aspect"] for s in record["shopScores"]] == ["service"]


def test_price_span_is_derived_from_the_skus_when_absent(tmp_path):
    # Most providers define no price_range field, so a listing whose variants
    # differ still has to come back as a span.
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "商品", "price": "128.00", "skus": {"sku": ['
        '{"sku_id": "a", "properties_name": "1:2:颜色:黑", "price": "128.00"},'
        '{"sku_id": "b", "properties_name": "1:3:颜色:白", "price": "298.00"}]}}}',
        encoding="utf-8",
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert record["price"]["value"] == 128.0
    assert record["price"]["valueMax"] == 298.0
    # Derived, not quoted: the listing never printed "128.00-298.00".
    assert record["price"]["sourceTextCn"] is None


def test_uniform_sku_prices_do_not_become_a_span(tmp_path):
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "商品", "price": "128.00", "skus": {"sku": ['
        '{"sku_id": "a", "properties_name": "1:2:颜色:黑", "price": "128.00"},'
        '{"sku_id": "b", "properties_name": "1:3:颜色:白", "price": "128.00"}]}}}',
        encoding="utf-8",
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert record["price"]["valueMax"] is None
    assert record["price"]["sourceTextCn"] == "128.00"


def test_an_explicit_range_wins_over_the_derived_one(tmp_path):
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "商品", "price": "1.00", '
        '"price_range": "128.00-298.00", "skus": {"sku": ['
        '{"sku_id": "a", "properties_name": "1:2:颜色:黑", "price": "9.00"}]}}}',
        encoding="utf-8",
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert (record["price"]["value"], record["price"]["valueMax"]) == (128.0, 298.0)
    assert record["price"]["sourceTextCn"] == "128.00-298.00"


def test_numeric_property_ids_never_become_attribute_names(tmp_path):
    # props_list keys are pids. When the value carries no "axis:value" pair
    # there is no axis name to be had, and emitting the pid as the name puts
    # "1627207:28320": "黑色" in front of the buyer.
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "商品", "props_list": {'
        '"1627207:28320": "颜色分类:黑色",'
        '"20509:28314": "黑色",'
        '"品牌": "Soundcore"}}}',
        encoding="utf-8",
    )
    record = normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()
    assert record["attributesCn"] == {"颜色分类": "黑色", "品牌": "Soundcore"}


# ------------------------------------------------- quantity-aware quoting

@pytest.mark.parametrize(
    "quantity,unit,subtotal,tier",
    [
        (2,    12.0,  24.0,    "2-99件 12.00"),
        (99,   12.0,  1188.0,  "2-99件 12.00"),
        (100,  10.5,  1050.0,  "100-999件 10.50"),
        (999,  10.5,  10489.5, "100-999件 10.50"),
        (1000, 9.0,   9000.0,  "≥1000件 9.00"),
        (50_000, 9.0, 450_000.0, "≥1000件 9.00"),   # the top step is open-ended
    ],
)
def test_the_ladder_is_walked_for_the_asked_quantity(source, quantity, unit, subtotal, tier):
    product = normalize(source.fetch_product("612345678901"), quantity=quantity)
    quote = product.to_item()["quote"]
    assert quote["quantity"] == quantity
    assert quote["unitPrice"]["value"] == unit
    assert quote["subtotal"]["value"] == subtotal
    assert quote["tierSourceTextCn"] == tier
    assert quote["meetsMinimumOrder"] is True


def test_a_quantity_below_the_minimum_is_flagged_but_still_priced(source):
    # The MOQ on this listing is 2, and the ladder starts at 2, so one piece
    # matches no step. Refusing to price it would be less useful than pricing
    # it off the headline figure and flagging that the order cannot be placed
    # as asked — which is what meetsMinimumOrder is for. tierSourceTextCn
    # stays null, so the buyer can tell this did not come from a ladder step.
    quote = normalize(source.fetch_product("612345678901"), quantity=1).to_item()["quote"]
    assert quote["meetsMinimumOrder"] is False
    assert quote["unitPrice"]["value"] == 12.0
    assert quote["subtotal"]["value"] == 12.0
    assert quote["tierSourceTextCn"] is None


def test_a_flat_priced_listing_quotes_off_the_single_price(source):
    # No ladder: the Taobao case. 555000111222 is ¥128 flat with MOQ 1.
    quote = normalize(source.fetch_product("555000111222"), quantity=3).to_item()["quote"]
    assert quote["unitPrice"]["value"] == 128.0
    assert quote["subtotal"]["value"] == 384.0
    assert quote["tierSourceTextCn"] is None
    assert quote["meetsMinimumOrder"] is True


def test_a_price_span_cannot_be_quoted_and_says_so(source):
    # 600530677383 is 128-298 across SKUs. There is no single unit price to
    # resolve to, and picking the low end would understate the order.
    quote = normalize(source.fetch_product("600530677383"), quantity=10).to_item()["quote"]
    assert quote["quantity"] == 10
    assert quote["unitPrice"] is None
    assert quote["subtotal"] is None


def test_no_quantity_means_no_quote(source):
    assert normalize(source.fetch_product("612345678901")).to_item()["quote"] is None


def test_a_refused_listing_is_not_quoted(source):
    product = normalize(source.fetch_product("999000111222"), quantity=100)
    assert product.to_item()["quote"] is None


def test_subtotal_is_rounded_to_cents(source):
    quote = normalize(source.fetch_product("612345678901"), quantity=333).to_item()["quote"]
    assert quote["subtotal"]["value"] == 3496.5      # 333 * 10.50


def test_a_derived_subtotal_carries_no_source_text(source):
    quote = normalize(source.fetch_product("612345678901"), quantity=100).to_item()["quote"]
    assert quote["subtotal"]["sourceTextCn"] is None


# ----------------------------------------------------------------- discount

def test_discount_is_computed_from_the_struck_through_price(source):
    # 128.00 against an original of 358.00.
    record = item(source, "600530677383")
    assert record["discountPct"] == 64.2


def test_no_original_price_means_no_discount(source):
    assert item(source, "612345678901")["discountPct"] is None


def test_an_original_at_or_below_the_current_price_is_not_a_discount(tmp_path):
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "商品", "price": "100", '
        '"orginal_price": "100"}}', encoding="utf-8",
    )
    assert normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()["discountPct"] is None


def test_sku_count_survives_dropping_the_sku_array(source):
    # With includeSkus off the array is emptied, so skuCount is the only thing
    # left that says the listing has variants. Deriving it from len(skus) made
    # it vanish with them, which defeats the point of having it.
    product = normalize(source.fetch_product("600530677383"))
    assert product.to_item()["skuCount"] == 2
    product.skus = []
    assert product.to_item()["skuCount"] == 2


def test_a_listing_with_no_variants_reports_no_sku_count(tmp_path):
    (tmp_path / "1.json").write_text(
        '{"item": {"num_iid": "1", "title": "商品", "price": "9.90"}}', encoding="utf-8"
    )
    assert normalize(FixtureSource(tmp_path).fetch_product("1")).to_item()["skuCount"] is None
