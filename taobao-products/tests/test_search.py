"""Tests for listing mode: selling the search response itself.

A search hit is cheaper per record than a full listing by a wide margin — one
provider call returns a page of them — so the fields it does carry have to be
normalised just as carefully, and the fields it cannot carry have to be
distinguishable from a listing that is genuinely silent about them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import source as source_mod  # noqa: E402
from normalize import normalize_search_entry  # noqa: E402
from source import FixtureSource  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

HIT = {
    "num_iid": "600530677383",
    "title": "无线蓝牙耳机 入耳式降噪",
    "price": "128.00",
    "orginal_price": "358.00",
    "sales": "月销1000+",
    "nick": "数码旗舰店",
    "seller_id": "2200731234567",
    "location": "广东 深圳",
    "pic_url": "//img.alicdn.com/x.jpg",
}


def test_a_search_hit_normalises_the_fields_it_carries():
    record = normalize_search_entry(HIT, platform="taobao").to_item()
    assert record["status"] == "listed"
    assert record["itemId"] == "600530677383"
    assert record["titleCn"] == "无线蓝牙耳机 入耳式降噪"
    assert record["price"]["value"] == 128.0
    assert record["originalPrice"]["value"] == 358.0
    assert record["discountPct"] == 64.2
    assert record["shopNameCn"] == "数码旗舰店"
    assert record["url"] == "https://item.taobao.com/item.htm?id=600530677383"
    assert record["images"] == ["https://img.alicdn.com/x.jpg"]


def test_sales_semantics_survive_the_cheaper_path():
    # The whole pitch is that a sales figure says what it measures. That must
    # hold on the cheap records too, not only the expensive ones.
    record = normalize_search_entry(HIT, platform="taobao").to_item()
    assert record["sales"] == {
        "count": 1000,
        "isApproximate": True,
        "window": "monthly",
        "unit": None,
        "sourceTextCn": "月销1000+",
    }


def test_listed_is_not_ok_so_the_two_are_told_apart():
    # A buyer filtering for complete records needs to see which these are.
    assert normalize_search_entry(HIT, platform="taobao").status == "listed"


def test_the_fields_a_search_hit_cannot_carry_come_back_empty():
    record = normalize_search_entry(HIT, platform="taobao").to_item()
    assert record["skus"] == []
    assert record["skuCount"] is None
    assert record["priceTiers"] == []
    assert record["shopScores"] == []
    assert record["shipping"] is None
    assert record["stock"] is None
    assert record["attributesCn"] == {}


@pytest.mark.parametrize(
    "hit",
    [
        {"title": "无线蓝牙耳机"},                    # no id
        {"num_iid": "123456789"},                     # no title
        {"num_iid": "", "title": ""},
        {},
    ],
)
def test_an_unusable_hit_is_unparsable_not_listed(hit):
    product = normalize_search_entry(hit, platform="taobao")
    assert product.status == "unparsable"
    assert "no id or title" in (product.status_detail or "")


def test_a_search_hit_is_quoted_off_its_flat_price():
    quote = normalize_search_entry(HIT, platform="taobao", quantity=4).to_item()["quote"]
    assert quote["unitPrice"]["value"] == 128.0
    assert quote["subtotal"]["value"] == 512.0
    assert quote["tierSourceTextCn"] is None       # search hits carry no ladder


def test_the_platform_decides_the_url():
    for platform, host in (
        ("taobao", "item.taobao.com"),
        ("tmall", "detail.tmall.com"),
        ("1688", "detail.1688.com"),
    ):
        record = normalize_search_entry(HIT, platform=platform).to_item()
        assert host in record["url"]
        assert record["platform"] == platform


def test_an_unknown_platform_falls_back_rather_than_crashing():
    # main() refuses an unknown platform up front; this is the inner guard.
    record = normalize_search_entry(HIT, platform="nope").to_item()
    assert record["platform"] == "taobao"


def test_fullwidth_and_myriad_input_on_the_cheap_path():
    hit = dict(HIT, price="１２８．００", sales="已售2万+")
    record = normalize_search_entry(hit, platform="taobao").to_item()
    assert record["price"]["value"] == 128.0
    assert record["sales"]["count"] == 20_000
    assert record["sales"]["window"] == "cumulative"


# ------------------------------------------------------------ search_raw

def test_fixture_search_raw_synthesises_hits_from_the_payloads():
    entries = FixtureSource(FIXTURES).search_raw("anything")
    ids = {str(e["num_iid"]) for e in entries}
    # The recorded refusal carries no payload, so it contributes no hit.
    assert "999000111222" not in ids
    assert {"555000111222", "600530677383", "612345678901"} <= ids
    assert all(e.get("title") for e in entries)


def test_fixture_search_raw_has_no_second_page():
    assert FixtureSource(FIXTURES).search_raw("x", page=2) == []


def test_recorded_search_page_wins_over_synthesised_hits(tmp_path):
    (tmp_path / "_search.json").write_text(
        json.dumps({"items": [{"num_iid": "777", "title": "录下来的"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    entries = FixtureSource(tmp_path).search_raw("x")
    assert entries == [{"num_iid": "777", "title": "录下来的"}]


class _Session:
    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))

        class R:
            status_code = 200
            text = self.body

            def raise_for_status(self) -> None:
                pass

            def json(inner):
                return json.loads(self.body)

        return R()


def _client(body: str) -> source_mod.UpstreamSource:
    client = source_mod.UpstreamSource(
        source_mod.SourceConfig(
            base_url="https://provider.example", api_key="k",
            min_interval=0.0, max_retries=0, backoff_base=0.0,
        )
    )
    client.session = _Session(body)  # type: ignore[assignment]
    return client


@pytest.mark.parametrize(
    "body,count",
    [
        ('{"items": {"item": [{"num_iid": "1", "title": "a"}, {"num_iid": "2", "title": "b"}]}}', 2),
        ('{"items": [{"num_iid": "1", "title": "a"}]}', 1),
        ('{"items": {"item": {"num_iid": "1", "title": "a"}}}', 1),   # one hit as a bare dict
        ('{"items": {"item": []}}', 0),
        ('{"nothing": 1}', 0),
        ('{"items": {"item": ["not a dict"]}}', 0),
    ],
)
def test_upstream_search_raw_reads_the_shapes_providers_use(body, count):
    assert len(_client(body).search_raw("蓝牙耳机")) == count


def test_search_raw_and_search_agree_on_the_ids():
    body = '{"items": {"item": [{"num_iid": "11", "title": "a"}, {"item_id": 22, "title": "b"}]}}'
    client = _client(body)
    assert client.search("x") == ["11", "22"]
    assert [e.get("title") for e in _client(body).search_raw("x")] == ["a", "b"]


def test_search_raw_sends_the_keyword_and_page():
    client = _client('{"items": []}')
    client.search_raw("蓝牙耳机", page=3)
    assert client.session.calls[0]["q"] == "蓝牙耳机"
    assert client.session.calls[0]["page"] == 3


def test_an_empty_result_page_is_empty_not_one_garbage_hit():
    # Regression: the `or` chain treated [] as "field missing" and fell back to
    # the container above it, returning [{"item": []}]. In listing mode that
    # counts as a fresh hit, so pagination carries on — and each page is a
    # billed call.
    assert _client('{"items": {"item": []}}').search_raw("x") == []
    assert _client('{"items": []}').search_raw("x") == []
    assert _client('{"items": {"item": []}}').search("x") == []
