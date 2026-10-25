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


def test_the_image_goes_out_under_the_parameter_the_provider_documents():
    # ImageUrl as a flat query parameter, per this host's own playground and a
    # real run. A hand-written xmlParameters document — OTAPI's generic form —
    # was refused: "Search parameters must have at least one of these values:
    # CategoryId, VendorName, VendorId, ItemTitle, BrandId, Configurators".
    client = _otapi_client()
    client.search_raw(image_url=IMAGE)
    sent = client.session.calls[0]
    assert sent["ImageUrl"] == IMAGE
    assert "xmlParameters" not in sent
    assert sent["framePosition"] == 0
    assert sent["language"] == "en"


def test_the_picture_is_the_only_condition_sent():
    # A keyword alongside it would turn a dropped image parameter into a page
    # of ordinary keyword hits — billable, and nothing to do with the photo.
    client = _otapi_client()
    client.search_raw("T恤 批发", image_url=IMAGE)
    sent = client.session.calls[0]
    assert "ItemTitle" not in sent
    assert sent["ImageUrl"] == IMAGE


def test_the_url_is_sent_as_given():
    # requests encodes the query string, so nothing here may pre-escape it.
    url = "https://shop.example/p.jpg?w=800&h=600"
    params = source_mod.image_search_params(source_mod.otapi_1688_config("k"), url)
    assert params == {"ImageUrl": url}


def test_a_provider_naming_the_parameter_differently_is_followed():
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


# ------------------------------------------------- is the picture readable at all


class _ImageHost:
    """Stands in for whatever is serving the reference image."""

    def __init__(self, status=200, content_type="image/jpeg", error=None) -> None:
        self.status, self.content_type, self.error = status, content_type, error
        self.asked: list[str] = []
        self.closed = 0

    def get(self, url, timeout=None, stream=None, headers=None):
        self.asked.append(url)
        if self.error:
            raise self.error
        host = self

        class R:
            status_code = host.status
            headers = {"Content-Type": host.content_type}

            def close(inner):
                host.closed += 1

        return R()


def test_a_readable_image_is_no_problem():
    host = _ImageHost()
    assert source_mod.image_unreachable(IMAGE, session=host) is None
    assert host.asked == [IMAGE]
    assert host.closed == 1


def test_an_upload_the_data_source_cannot_read_is_named_as_such():
    # The case this exists for: a key-value store record that is not public.
    problem = source_mod.image_unreachable(
        "https://api.apify.com/v2/key-value-stores/x/records/photo.jpg",
        session=_ImageHost(status=403),
    )
    assert problem is not None and "HTTP 403" in problem


def test_an_address_serving_a_web_page_is_not_an_image():
    problem = source_mod.image_unreachable(
        "https://detail.1688.com/offer/672715568844.html",
        session=_ImageHost(content_type="text/html; charset=utf-8"),
    )
    assert problem is not None and "not an image" in problem


def test_a_host_that_cannot_be_reached_at_all_is_reported():
    import requests

    problem = source_mod.image_unreachable(
        IMAGE, session=_ImageHost(error=requests.ConnectionError("no route"))
    )
    assert problem is not None and "could not be fetched" in problem


@pytest.mark.parametrize("content_type", ["", "application/octet-stream", "binary/image"])
def test_an_odd_content_type_is_let_through(content_type):
    # Refusing a picture the data source could have read would cost the buyer
    # the search they came for; only certainties are refused.
    assert (
        source_mod.image_unreachable(IMAGE, session=_ImageHost(content_type=content_type))
        is None
    )


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


# ------------------------------------------- a refused search is not an empty one


def test_a_search_the_provider_refused_is_raised_not_read_as_empty():
    # The first live image search came back as a successful run with no
    # records in 0.3s: the body carried the provider's error and the hit
    # scan simply found no hits at the configured paths. Details were always
    # checked this way; search was not.
    body = json.dumps(
        {"ErrorCode": "InvalidParameters", "ErrorDescription": "xmlParameters is required"}
    )
    client = _otapi_client(body)
    with pytest.raises(source_mod.SourceError, match="refused the search"):
        client.search_raw(image_url=IMAGE)


def test_a_successful_search_body_still_reads_as_success():
    # OTAPI writes ErrorCode "Ok" on a good response; that must not read as
    # an error, or every keyword search fails.
    body = json.dumps(
        {
            "ErrorCode": "Ok",
            "Result": {"Items": {"Items": {"Content": [{"Id": "abb-1"}]}}},
        }
    )
    assert _otapi_client(body).search(image_url=IMAGE) == ["1"]


def test_the_raw_response_is_still_returned_for_a_capture():
    # capture.py saves search_response(): a refusal is exactly the payload
    # worth keeping, so the raising belongs in search_raw, not there.
    body = json.dumps({"ErrorCode": "InvalidParameters"})
    assert _otapi_client(body).search_response(image_url=IMAGE) == {
        "ErrorCode": "InvalidParameters"
    }


# --------------------------------------------- an image search is slow work


class _SlowSession:
    """Records the timeout each call was given."""

    def __init__(self, body='{"ErrorCode": "Ok", "Result": {"Items": {"Items": {"Content": []}}}}'):
        self.body = body
        self.timeouts: list[float | None] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.timeouts.append(timeout)
        host = self

        class R:
            status_code = 200
            text = host.body

            def raise_for_status(inner):
                pass

        return R()


def test_an_image_search_is_given_minutes_where_a_keyword_gets_seconds():
    # A real 30s keyword-sized budget timed out: the provider fetches the
    # picture and runs a visual match before it can answer.
    client = _otapi_client()
    session = _SlowSession()
    client.session = session  # type: ignore[assignment]
    client.search_raw("T恤 批发")
    client.search_raw(image_url=IMAGE)
    keyword_budget, image_budget = session.timeouts
    assert keyword_budget == 30.0
    assert image_budget == 180.0


def test_a_timed_out_image_search_is_retried_once_not_three_times():
    # Each attempt is a billed minute, and a visual search that did not
    # answer in three minutes rarely answers on the next try.
    import requests

    class _Timeouts:
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            raise requests.ConnectTimeout("read timed out")

    client = _otapi_client()
    client.config.backoff_base = 0.0
    client.session = _Timeouts()  # type: ignore[assignment]
    with pytest.raises(source_mod.SourceError):
        client.search_raw(image_url=IMAGE)
    assert client.session.calls == 2


# ------------------------------------------- a hung provider cannot bill four times


def test_a_timed_out_keyword_search_is_retried_once_not_three_times():
    # 2026-10-25: the provider hung, and every run spent four billed attempts
    # — 30s each — on the same answer. A timeout means the request reached
    # them, so it is charged; a hung provider must not cost four charges.
    import requests

    class _Hung:
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            raise requests.ReadTimeout("read timed out")

    client = _otapi_client()
    client.config.backoff_base = 0.0
    client.config.max_retries = 3
    client.session = _Hung()  # type: ignore[assignment]
    with pytest.raises(source_mod.SourceError):
        client.search_raw("T恤 批发")
    assert client.session.calls == 2


def test_an_error_that_is_not_a_timeout_keeps_the_full_retry_budget():
    # A reset connection costs nothing and may well succeed next time.
    import requests

    class _Flaky:
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            raise requests.ConnectionError("reset")

    client = _otapi_client()
    client.config.backoff_base = 0.0
    client.config.max_retries = 3
    client.session = _Flaky()  # type: ignore[assignment]
    with pytest.raises(source_mod.SourceError):
        client.search_raw("T恤 批发")
    assert client.session.calls == 4
