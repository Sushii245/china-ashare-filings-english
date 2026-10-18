"""Tests for the input-resolution helpers in the Actor entry point.

These are the parts that run before any network call and decide what the run
will charge for, so they are worth pinning down.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import actor_main  # noqa: E402
import normalize as normalize_mod  # noqa: E402
import source as source_mod  # noqa: E402
from source import FixtureSource  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://item.taobao.com/item.htm?id=600530677383", "600530677383"),
        ("https://item.taobao.com/item.htm?spm=a1z10&id=600530677383&ns=1", "600530677383"),
        ("https://detail.tmall.com/item.htm?id=555000111222", "555000111222"),
        ("https://detail.1688.com/offer/612345678901.html", "612345678901"),
        ("https://m.1688.com/offer/612345678901.html?spm=x", "612345678901"),
        ("https://item.taobao.com/item.htm", None),
        ("https://example.com/nothing", None),
        ("", None),
    ],
)
def test_item_id_from_url(url, expected):
    assert actor_main.item_id_from_url(url) == expected


def test_request_list_editor_shape_is_flattened():
    raw = [
        "https://item.taobao.com/item.htm?id=1",
        {"url": "https://item.taobao.com/item.htm?id=2"},
        {"requestsFromUrl": "https://detail.1688.com/offer/3.html"},
        {"nothing": "useful"},
    ]
    assert actor_main._urls_from_input(raw) == [
        "https://item.taobao.com/item.htm?id=1",
        "https://item.taobao.com/item.htm?id=2",
        "https://detail.1688.com/offer/3.html",
    ]


def test_ids_urls_and_keyword_are_deduplicated_and_capped():
    source = FixtureSource(FIXTURES)
    ids = actor_main._resolve_ids(
        {
            "itemIds": ["600530677383", "600530677383"],   # duplicate
            "itemUrls": ["https://item.taobao.com/item.htm?id=555000111222"],
            "keyword": "anything",                          # fixture search adds all
        },
        source,
        limit=3,
    )
    assert ids[:2] == ["600530677383", "555000111222"]
    assert len(ids) == 3
    assert len(set(ids)) == 3


def test_explicit_ids_come_before_search_results():
    source = FixtureSource(FIXTURES)
    ids = actor_main._resolve_ids(
        {"itemIds": ["999000111222"], "keyword": "x"}, source, limit=2
    )
    assert ids[0] == "999000111222"


def test_no_input_resolves_to_nothing():
    source = FixtureSource(FIXTURES)
    assert actor_main._resolve_ids({}, source, limit=10) == []


def test_keyword_search_stops_when_a_page_is_empty():
    # FixtureSource returns everything on page 1 and nothing after, so an
    # unbounded loop here would hang the run rather than fail it. A recorded
    # refusal carries no payload and so contributes no search entry, which is
    # why it is not in the expected set.
    import json as _json

    source = FixtureSource(FIXTURES)
    with_payload = sorted(
        p.stem
        for p in FIXTURES.glob("*.json")
        if not p.name.startswith("_")
        and (lambda d: d.get("payload", d) is not None)(
            _json.loads(p.read_text(encoding="utf-8"))
        )
    )
    ids = actor_main._resolve_ids({"keyword": "x"}, source, limit=1000)
    assert ids == with_payload


def test_missing_provider_fails_with_an_actionable_message(monkeypatch):
    import source as source_mod

    monkeypatch.delenv(actor_main.ENV_FIXTURES, raising=False)
    monkeypatch.delenv(actor_main.ENV_BASE_URL, raising=False)
    monkeypatch.delenv(actor_main.ENV_API_KEY, raising=False)
    with pytest.raises(source_mod.SourceError) as caught:
        actor_main._build_source({}, "taobao", 1.0)
    assert "SOURCE_BASE_URL" in str(caught.value)


def test_1688_platform_selects_its_own_endpoints(monkeypatch):
    monkeypatch.delenv(actor_main.ENV_FIXTURES, raising=False)
    built = actor_main._build_source(
        {"sourceBaseUrl": "https://provider.example", "sourceApiKey": "k"},
        "1688",
        1.0,
    )
    assert built.config.detail_path == "/1688/item_get"
    assert built.platform == "1688"


class PageIgnoringSource:
    """A provider that returns the same results whatever page you ask for.

    Real ones do this when a keyword has fewer results than a page holds, and
    every call is billed, so the loop has to notice.
    """

    def __init__(self, ids: list[str]) -> None:
        self._ids = ids
        self.calls = 0

    def fetch_product(self, item_id: str):  # pragma: no cover - not used here
        raise AssertionError("not expected")

    def search(self, keyword: str, *, page: int = 1) -> list[str]:
        self.calls += 1
        return list(self._ids)


def test_a_page_that_adds_nothing_new_stops_the_search():
    # Otherwise this spends 20 paid upstream calls collecting the same 3 ids.
    source = PageIgnoringSource(["1", "2", "3"])
    ids = actor_main._resolve_ids({"keyword": "x"}, source, limit=100)
    assert ids == ["1", "2", "3"]
    assert source.calls == 2, f"made {source.calls} search calls, expected 2"


class CountingSource:
    """Records the order it was asked in, and can be told to fail on an id."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.asked: list[str] = []
        self.fail = fail or set()
        self._lock = threading.Lock()

    def fetch_product(self, item_id: str):
        with self._lock:
            self.asked.append(item_id)
        if item_id in self.fail:
            raise RuntimeError(f"boom on {item_id}")
        return source_mod.RawProduct(
            item_id=item_id,
            platform="taobao",
            payload={"item": {"num_iid": item_id, "title": f"商品{item_id}", "price": "9.90"}},
        )

    def search(self, keyword: str, *, page: int = 1):  # pragma: no cover
        return []


def test_a_chunk_comes_back_in_the_order_it_was_asked():
    # The dataset should list the ids the buyer gave, in that order, even
    # though the fetches themselves overlap.
    ids = [str(i) for i in range(10)]
    products = actor_main._fetch_chunk(
        CountingSource(), ids, quantity=None, workers=5, platform="taobao"
    )
    assert [p.item_id for p in products] == ids


def test_one_failing_listing_does_not_sink_the_chunk():
    source = CountingSource(fail={"3"})
    products = actor_main._fetch_chunk(
        source, ["1", "2", "3", "4"], quantity=None, workers=4, platform="taobao"
    )
    by_id = {p.item_id: p for p in products}
    assert by_id["3"].status == "upstream_error"
    assert "boom on 3" in (by_id["3"].status_detail or "")
    assert [by_id[i].status for i in ("1", "2", "4")] == ["ok", "ok", "ok"]


def test_the_quantity_reaches_every_listing_in_a_chunk():
    products = actor_main._fetch_chunk(
        CountingSource(), ["1", "2"], quantity=7, workers=2, platform="taobao"
    )
    assert [p.quote.quantity for p in products] == [7, 7]


def test_a_single_listing_skips_the_pool():
    source = CountingSource()
    products = actor_main._fetch_chunk(
        source, ["1"], quantity=None, workers=5, platform="taobao"
    )
    assert len(products) == 1 and products[0].status == "ok"


@pytest.mark.parametrize(
    "given,expected",
    [
        ("600530677383", "600530677383"),
        (" 600530677383 ", "600530677383"),
        ("600530677383\n", "600530677383"),
        # A pasted URL in the ids field used to be sent upstream verbatim,
        # which is one billed call per paste for a guaranteed miss.
        ("https://item.taobao.com/item.htm?id=600530677383", "600530677383"),
        ("https://detail.1688.com/offer/612345678901.html", "612345678901"),
        ("id=600530677383", "600530677383"),
    ],
)
def test_ids_are_salvaged_rather_than_spent_on(given, expected):
    source = PageIgnoringSource([])
    assert actor_main._resolve_ids({"itemIds": [given]}, source, limit=5) == [expected]


@pytest.mark.parametrize("junk", ["abc", "", "   ", "商品", "https://example.com/x"])
def test_an_unusable_id_is_dropped_before_it_costs_a_call(junk):
    source = PageIgnoringSource([])
    assert actor_main._resolve_ids({"itemIds": [junk]}, source, limit=5) == []


def test_an_unknown_platform_is_refused_rather_than_relabelled():
    # Silently calling a JD id "taobao" hands the buyer a taobao URL for a
    # listing that is not there.
    with pytest.raises(ValueError) as caught:
        actor_main._validate_platform("jd")
    assert "jd" in str(caught.value)


@pytest.mark.parametrize("ok", ["taobao", "tmall", "1688"])
def test_the_supported_platforms_pass(ok):
    assert actor_main._validate_platform(ok) == ok


def test_every_dataset_view_field_exists_on_a_record():
    # The dataset views name fields by string. Renaming one in models.py
    # without touching the schema breaks the view silently in Console.
    import json

    from source import RawProduct

    schema = json.loads((ROOT / ".actor" / "dataset_schema.json").read_text(encoding="utf-8"))
    record = normalize_mod.normalize(
        RawProduct(item_id="1", platform="taobao",
                   payload={"item": {"num_iid": "1", "title": "商品", "price": "9.90"}}),
        quantity=10,
    ).to_item()
    for view, body in schema["views"].items():
        for field in body["transformation"]["fields"]:
            assert field in record, f"view {view!r} names {field!r}, which no record has"
