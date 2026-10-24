"""Tests for searching by picture rather than by words.

The buyer's case is a photo with no name attached: a competitor's listing
image, a sample on a desk. What matters here is that the picture is the only
condition sent — a search that silently falls back to words returns a page of
plausible, unrelated hits, and in listing mode each one is billed — and that
an image the data source could never read is refused before a call is spent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import actor_main  # noqa: E402
import source as source_mod  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

IMAGE = "https://cbu01.alicdn.com/img/ibank/O1CN01ha5KYW1knTcju4X9C_!!2813694728-0-cib.jpg"


class _Session:
    """Records the parameters of every call instead of making one."""

    def __init__(self, body: str = '{"Result": {"Items": {"Items": {"Content": []}}}}') -> None:
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


def _otapi_client(body: str | None = None) -> source_mod.UpstreamSource:
    config = source_mod.otapi_1688_config("key")
    config.min_interval = 0.0
    config.max_retries = 0
    config.backoff_base = 0.0
    client = source_mod.UpstreamSource(config, platform="1688")
    client.session = _Session(body) if body else _Session()  # type: ignore[assignment]
    return client


# --------------------------------------------------------------- the wire form


def test_the_image_goes_out_as_the_provider_documents_it():
    client = _otapi_client()
    client.search_raw(image_url=IMAGE)
    sent = client.session.calls[0]
    assert sent["xmlParameters"] == (
        f"<SearchItemsParameters><ImageUrl>{IMAGE}</ImageUrl></SearchItemsParameters>"
    )
    assert sent["framePosition"] == 0
    assert sent["language"] == "en"


def test_the_picture_is_the_only_condition_sent():
    # A keyword alongside it would turn a dropped image parameter into a page
    # of ordinary keyword hits — billable, and nothing to do with the photo.
    client = _otapi_client()
    client.search_raw("T恤 批发", image_url=IMAGE)
    sent = client.session.calls[0]
    assert "ItemTitle" not in sent
    assert IMAGE in sent["xmlParameters"]


def test_an_image_url_with_a_query_string_survives_the_xml():
    # Unescaped, the & ends the element and the provider is handed a document
    # describing a different image, or none.
    url = "https://shop.example/p.jpg?w=800&h=600"
    params = source_mod.image_search_params(source_mod.otapi_1688_config("k"), url)
    assert params["xmlParameters"] == (
        "<SearchItemsParameters><ImageUrl>"
        "https://shop.example/p.jpg?w=800&amp;h=600"
        "</ImageUrl></SearchItemsParameters>"
    )


def test_a_flat_image_parameter_is_used_when_a_provider_has_one():
    config = source_mod.SourceConfig(base_url="https://provider.example", image_param="img_url")
    assert source_mod.image_search_params(config, IMAGE) == {"img_url": IMAGE}


def test_paging_an_image_search_works_like_paging_a_keyword_one():
    client = _otapi_client()
    client.search_raw(image_url=IMAGE, page=3)
    sent = client.session.calls[0]
    assert sent["framePosition"] == 100
    assert sent["frameSize"] == 50


# ------------------------------------------------------- refusing certain misses


@pytest.mark.parametrize(
    "bad",
    [
        "/Users/me/Desktop/sample.jpg",
        "data:image/png;base64,iVBORw0KGgo=",
        "cbu01.alicdn.com/img/x.jpg",
        "",
        "   ",
    ],
)
def test_an_image_the_provider_cannot_read_is_refused_before_any_call(bad):
    client = _otapi_client()
    with pytest.raises(source_mod.SourceError):
        client.search_raw(image_url=bad)
    assert client.session.calls == []


def test_a_provider_without_image_search_says_so():
    config = source_mod.SourceConfig(base_url="https://provider.example")
    assert config.serves_image_search is False
    with pytest.raises(source_mod.SourceError, match="does not serve search by image"):
        source_mod.image_search_params(config, IMAGE)


def test_the_otapi_preset_serves_image_search():
    assert source_mod.otapi_1688_config("k").serves_image_search is True


# ---------------------------------------------------------------------- replay


def test_an_image_search_replays_its_own_capture(tmp_path):
    (tmp_path / "_search.json").write_text(
        json.dumps({"items": [{"num_iid": "111", "title": "by keyword"}]}),
        encoding="utf-8",
    )
    (tmp_path / "_search_image.json").write_text(
        json.dumps({"items": [{"num_iid": "222", "title": "by image"}]}),
        encoding="utf-8",
    )
    fixture = source_mod.FixtureSource(tmp_path, platform="1688")
    assert fixture.search("T恤") == ["111"]
    assert fixture.search(image_url=IMAGE) == ["222"]


def test_replay_falls_back_to_the_keyword_page_when_no_image_page_was_captured(tmp_path):
    (tmp_path / "_search.json").write_text(
        json.dumps({"items": [{"num_iid": "111", "title": "whatever"}]}),
        encoding="utf-8",
    )
    fixture = source_mod.FixtureSource(tmp_path, platform="1688")
    assert fixture.search(image_url=IMAGE) == ["111"]


def test_replay_refuses_an_image_it_could_not_have_fetched(tmp_path):
    fixture = source_mod.FixtureSource(tmp_path, platform="1688")
    with pytest.raises(source_mod.SourceError):
        fixture.search(image_url="sample.jpg")


# -------------------------------------------------------------- the Actor's input


class _RecordingSource:
    """A data source that remembers what each search was asked to look for."""

    def __init__(self) -> None:
        self.config = source_mod.otapi_1688_config("k")
        self.asked: list[tuple[str, str | None]] = []

    def fetch_product(self, item_id):  # pragma: no cover - not used here
        raise AssertionError("not expected")

    def search(self, keyword="", *, page=1, image_url=None):
        self.asked.append((keyword, image_url))
        return ["669724481680"] if page == 1 else []

    def search_raw(self, keyword="", *, page=1, image_url=None):
        self.asked.append((keyword, image_url))
        return [{"Id": "abb-669724481680"}] if page == 1 else []


def test_an_image_in_the_input_is_what_gets_searched():
    data_source = _RecordingSource()
    ids = actor_main._resolve_ids({"imageUrl": IMAGE}, data_source, limit=10)
    assert ids == ["669724481680"]
    assert data_source.asked[0] == ("", IMAGE)


def test_an_image_wins_over_a_keyword_given_with_it():
    data_source = _RecordingSource()
    actor_main._resolve_ids({"imageUrl": IMAGE, "keyword": "T恤"}, data_source, limit=10)
    # The keyword travels along but the image is what the provider is given,
    # and `source` sends one condition only.
    assert data_source.asked[0][1] == IMAGE


def test_a_run_with_neither_searches_nothing():
    data_source = _RecordingSource()
    assert actor_main._resolve_ids({"itemIds": ["612345678901"]}, data_source, limit=10) == [
        "612345678901"
    ]
    assert data_source.asked == []


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        (f"  {IMAGE}  ", IMAGE),
    ],
)
def test_reading_the_image_url_out_of_the_input(value, expected):
    assert actor_main._image_url({"imageUrl": value}) == expected
