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

    for var in (actor_main.ENV_FIXTURES, actor_main.ENV_BASE_URL,
                actor_main.ENV_API_KEY, actor_main.ENV_PROVIDER):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(source_mod.SourceError) as caught:
        actor_main._build_source({}, "1688", 1.0)
    assert "SOURCE_PROVIDER" in str(caught.value)
    assert "SOURCE_API_KEY" in str(caught.value)


def test_the_operator_preset_is_built_with_the_operator_key(monkeypatch):
    monkeypatch.delenv(actor_main.ENV_FIXTURES, raising=False)
    monkeypatch.setenv(actor_main.ENV_PROVIDER, "otapi-1688")
    monkeypatch.setenv(actor_main.ENV_API_KEY, "operator-key")
    built = actor_main._build_source({}, "1688", 0.5)
    assert built.config.detail_path == "/BatchGetItemFullInfo"
    assert built.session.headers["X-RapidAPI-Key"] == "operator-key"
    assert built.config.min_interval == 0.5
    assert built.platform == "1688"


def test_a_buyer_key_is_used_with_the_operator_preset(monkeypatch):
    monkeypatch.delenv(actor_main.ENV_FIXTURES, raising=False)
    monkeypatch.setenv(actor_main.ENV_PROVIDER, "otapi-1688")
    monkeypatch.setenv(actor_main.ENV_API_KEY, "operator-key")
    built = actor_main._build_source({"sourceApiKey": "buyer-key"}, "1688", 0.5)
    assert built.session.headers["X-RapidAPI-Key"] == "buyer-key"


def test_a_preset_without_any_key_fails_clearly(monkeypatch):
    import source as source_mod

    monkeypatch.delenv(actor_main.ENV_FIXTURES, raising=False)
    monkeypatch.delenv(actor_main.ENV_API_KEY, raising=False)
    monkeypatch.setenv(actor_main.ENV_PROVIDER, "otapi-1688")
    with pytest.raises(source_mod.SourceError) as caught:
        actor_main._build_source({}, "1688", 0.5)
    assert "no key" in str(caught.value)


def test_an_unknown_provider_name_lists_the_known_ones(monkeypatch):
    import source as source_mod

    monkeypatch.delenv(actor_main.ENV_FIXTURES, raising=False)
    monkeypatch.setenv(actor_main.ENV_PROVIDER, "nope")
    monkeypatch.setenv(actor_main.ENV_API_KEY, "k")
    with pytest.raises(source_mod.SourceError) as caught:
        actor_main._build_source({}, "1688", 0.5)
    assert "otapi-1688" in str(caught.value)


def test_fixtures_replay_through_the_named_preset(monkeypatch, tmp_path):
    # Phase 5 of VALIDATION.md: a capture must be read exactly as live data
    # would be, or the replay proves nothing.
    monkeypatch.setenv(actor_main.ENV_FIXTURES, str(tmp_path))
    monkeypatch.setenv(actor_main.ENV_PROVIDER, "otapi-1688")
    built = actor_main._build_source({}, "1688", 0.5)
    assert isinstance(built, source_mod.FixtureSource)
    assert built.config.id_prefix == "abb-"
    assert built.config.platforms == ("1688",)


def test_a_platform_the_provider_does_not_serve_is_refused(monkeypatch):
    monkeypatch.delenv(actor_main.ENV_FIXTURES, raising=False)
    monkeypatch.setenv(actor_main.ENV_PROVIDER, "otapi-1688")
    monkeypatch.setenv(actor_main.ENV_API_KEY, "k")
    built = actor_main._build_source({}, "taobao", 0.5)
    with pytest.raises(ValueError) as caught:
        actor_main._check_platform_served("taobao", built)
    assert "1688 only" in str(caught.value)
    actor_main._check_platform_served("1688", built)   # does not raise


@pytest.mark.parametrize(
    "given,expected",
    [("abb-612345678901", "612345678901"), (" abb-612345678901 ", "612345678901")],
)
def test_a_pasted_wire_id_is_read_as_the_platform_id(given, expected):
    source = PageIgnoringSource([])
    assert actor_main._resolve_ids({"itemIds": [given]}, source, limit=5) == [expected]


def test_the_chunk_normalises_with_the_sources_own_map():
    # Regression: _fetch_chunk called normalize() without the source's config,
    # so every source was read with the default query-string map and a new
    # provider's records all came back unparsable.
    class StructuredSource(CountingSource):
        def __init__(self):
            super().__init__()
            self.config = source_mod.SourceConfig(
                base_url="", id_prefix="abb-",
                field_map={"title_cn": "R.T", "price": "R.P"},
            )

        def fetch_product(self, item_id):
            return source_mod.RawProduct(
                item_id=item_id, platform="1688",
                payload={"R": {"T": "直线导轨滑块", "P": 38.5}},
            )

    [product] = actor_main._fetch_chunk(
        StructuredSource(), ["abb-123456789"], quantity=None, workers=1, platform="1688"
    )
    assert product.status == "ok"
    assert product.title_cn == "直线导轨滑块"
    assert product.price.value == 38.5
    assert product.item_id == "123456789"


class PageIgnoringSource:
    """A provider that returns the same results whatever page you ask for.

    Real ones do this when a keyword has fewer results than a page holds, and
    every call is billed, so the loop has to notice.
    """

    def __init__(self, ids: list[str]) -> None:
        self.config = source_mod.SourceConfig(base_url="")
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
        self.config = source_mod.SourceConfig(base_url="")
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


# ------------------------------------------------------------ request spacing

@pytest.mark.parametrize(
    "given,expected",
    [
        ({}, 0.2),                                   # unset: the default
        ({"minRequestInterval": None}, 0.2),
        ({"minRequestInterval": 0.2}, 0.2),
        ({"minRequestInterval": 0.05}, 0.05),        # decimals are allowed
        ({"minRequestInterval": 1}, 1.0),
        ({"minRequestInterval": 0}, 0.0),            # 0 means 0, not the default
        ({"minRequestInterval": 0.0}, 0.0),
        ({"minRequestInterval": -3}, 0.0),           # clamped, not an error
        ({"minRequestInterval": "0.5"}, 0.5),
        ({"minRequestInterval": "fast"}, 0.2),       # unreadable: the default
    ],
)
def test_min_interval_reads_the_input(given, expected):
    # Regression: the value was read as `x or 0.2`, so 0 became 0.2; and the
    # schema typed it as an integer defaulting to 1, which held every run to
    # one request a second whatever the concurrency.
    assert actor_main._min_interval(given) == expected


def test_the_schema_default_matches_the_code_default():
    import json

    schema = json.loads((ROOT / ".actor" / "input_schema.json").read_text(encoding="utf-8"))
    field = schema["properties"]["minRequestInterval"]
    assert field["type"] == "number"
    assert field["default"] == actor_main.DEFAULT_MIN_INTERVAL
    assert field["minimum"] == 0


def test_the_chosen_interval_reaches_the_client(monkeypatch):
    monkeypatch.delenv(actor_main.ENV_FIXTURES, raising=False)
    monkeypatch.setenv(actor_main.ENV_PROVIDER, "otapi-1688")
    monkeypatch.setenv(actor_main.ENV_API_KEY, "k")
    built = actor_main._build_source({}, "1688", actor_main._min_interval({"minRequestInterval": 0}))
    assert built.config.min_interval == 0.0


# ------------------------------------------------------------- short pages

class SizedPages:
    """A provider that serves `total` ids, `size` to a page, and counts calls."""

    def __init__(self, total: int, size: int, *, sized: bool) -> None:
        self.config = source_mod.SourceConfig(
            base_url="", page_size=size,
            page_size_param="frameSize" if sized else None,
            offset_param="framePosition" if sized else None,
        )
        self.ids = [str(100000000 + n) for n in range(total)]
        self.size = size
        self.calls = 0

    def fetch_product(self, item_id):  # pragma: no cover
        raise AssertionError("not expected")

    def search(self, keyword, *, page=1):
        self.calls += 1
        return self.ids[(page - 1) * self.size : page * self.size]


def test_a_short_page_ends_the_search_when_the_page_size_is_ours():
    # 5 results at 50 a page: one call. Asking for page 2 would be a billed
    # call for an empty page — on every run whose results fit on one page.
    source = SizedPages(total=5, size=50, sized=True)
    assert len(actor_main._resolve_ids({"keyword": "x"}, source, limit=100)) == 5
    assert source.calls == 1


def test_full_pages_keep_going():
    source = SizedPages(total=120, size=50, sized=True)
    assert len(actor_main._resolve_ids({"keyword": "x"}, source, limit=1000)) == 120
    assert source.calls == 3        # 50, 50, 20 — the short third page ends it


def test_a_short_page_is_not_trusted_when_the_provider_picks_its_size():
    # Without a page-size parameter we do not know the page size, so a short
    # page proves nothing and the search runs until a page comes back empty.
    source = SizedPages(total=5, size=50, sized=False)
    assert len(actor_main._resolve_ids({"keyword": "x"}, source, limit=100)) == 5
    assert source.calls == 2
