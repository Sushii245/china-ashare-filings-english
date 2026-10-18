"""Tests for the input-resolution helpers in the Actor entry point.

These are the parts that run before any network call and decide what the run
will charge for, so they are worth pinning down.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import actor_main  # noqa: E402
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
    # unbounded loop here would hang the run rather than fail it.
    source = FixtureSource(FIXTURES)
    ids = actor_main._resolve_ids({"keyword": "x"}, source, limit=1000)
    assert ids == sorted(p.stem for p in FIXTURES.glob("*.json"))


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
