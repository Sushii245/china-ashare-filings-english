"""Tests for the upstream client, with the HTTP layer stubbed out.

This is the only part of the project that touches the network, so it is also
the part whose failure modes are worth pinning down without going near it: a
refusal that arrives inside a 200 body, a provider that reports its own error
the same way, a transient 500 that should be retried, and a non-JSON response.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import source as source_mod  # noqa: E402


class FakeResponse:
    def __init__(self, *, status_code: int = 200, body: str = "{}") -> None:
        self.status_code = status_code
        self.text = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Returns queued responses in order, recording the calls."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if not self._responses:
            raise AssertionError("FakeSession ran out of queued responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def build(responses, **overrides):
    config = source_mod.SourceConfig(
        base_url="https://provider.example",
        api_key="secret",
        min_interval=0.0,
        max_retries=2,
        backoff_base=0.0,
        **overrides,
    )
    client = source_mod.UpstreamSource(config)
    client.session = FakeSession(responses)  # type: ignore[assignment]
    return client


OK_BODY = json.dumps(
    {"item": {"num_iid": "1", "title": "测试商品", "price": "9.90"}}, ensure_ascii=False
)


def test_successful_fetch_returns_the_payload():
    client = build([FakeResponse(body=OK_BODY)])
    raw = client.fetch_product("1")
    assert raw.status == "ok"
    assert raw.payload["item"]["title"] == "测试商品"


def test_the_api_key_is_sent_as_a_parameter():
    client = build([FakeResponse(body=OK_BODY)])
    client.fetch_product("600530677383")
    _, params = client.session.calls[0]
    assert params["key"] == "secret"
    assert params["num_iid"] == "600530677383"


@pytest.mark.parametrize(
    "body",
    [
        '{"ret": ["RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!"]}',
        '{"data": {"url": "https://login.taobao.com/member/login.jhtml"}}',
        '{"html": "punish:resource:template cloud_ip_bl"}',
        '{"msg": "滑块验证"}',
    ],
)
def test_risk_control_in_a_200_body_becomes_a_status(body):
    client = build([FakeResponse(body=body)])
    raw = client.fetch_product("1")
    assert raw.status == "risk_control_blocked"
    assert raw.payload is None


def test_risk_control_is_not_retried():
    # Retrying a refusal costs the buyer time and the provider a call, and the
    # answer will not change within one run.
    client = build([FakeResponse(body='{"ret":["RGV587_ERROR"]}')])
    client.fetch_product("1")
    assert len(client.session.calls) == 1


def test_provider_error_inside_a_200_body_is_an_upstream_error():
    client = build([FakeResponse(body='{"error": "invalid num_iid", "error_code": "2000"}')])
    raw = client.fetch_product("1")
    assert raw.status == "upstream_error"
    assert "invalid num_iid" in (raw.status_detail or "")


def test_provider_error_that_quotes_risk_control_is_classified_as_blocked():
    client = build([FakeResponse(body='{"error": "item fetch failed: RGV587"}')])
    raw = client.fetch_product("1")
    assert raw.status == "risk_control_blocked"


def test_a_transient_500_is_retried_then_succeeds():
    client = build([FakeResponse(status_code=500), FakeResponse(body=OK_BODY)])
    raw = client.fetch_product("1")
    assert raw.status == "ok"
    assert len(client.session.calls) == 2


def test_retries_are_bounded_and_end_in_a_status_not_an_exception():
    client = build([FakeResponse(status_code=503)] * 3)
    raw = client.fetch_product("1")
    assert raw.status == "upstream_error"
    assert len(client.session.calls) == 3          # 1 try + 2 retries


def test_non_json_response_is_reported_not_retried():
    client = build([FakeResponse(body="<html>gateway</html>")])
    raw = client.fetch_product("1")
    assert raw.status == "upstream_error"
    assert "non-JSON" in (raw.status_detail or "")


def test_connection_error_is_retried_then_reported():
    client = build([requests.ConnectionError("reset"), requests.ConnectionError("reset"), requests.ConnectionError("reset")])
    raw = client.fetch_product("1")
    assert raw.status == "upstream_error"


@pytest.mark.parametrize(
    "body,expected",
    [
        ('{"items": {"item": [{"num_iid": "11"}, {"num_iid": "22"}]}}', ["11", "22"]),
        ('{"items": [{"item_id": 33}]}', ["33"]),
        ('{"items": {"item": {"num_iid": "44"}}}', ["44"]),
        ('{"items": {"item": []}}', []),
        ('{"nothing": 1}', []),
    ],
)
def test_search_reads_the_id_shapes_providers_use(body, expected):
    client = build([FakeResponse(body=body)])
    assert client.search("蓝牙耳机") == expected


def test_search_sends_the_keyword_and_page():
    client = build([FakeResponse(body='{"items": []}')])
    client.search("蓝牙耳机", page=3)
    _, params = client.session.calls[0]
    assert params["q"] == "蓝牙耳机"
    assert params["page"] == 3


def test_build_source_refuses_a_config_with_no_base_url():
    with pytest.raises(source_mod.SourceError):
        source_mod.build_source()


def test_fixtures_win_over_a_configured_provider():
    built = source_mod.build_source(
        fixtures_dir=Path(__file__).parent / "fixtures",
        config=source_mod.SourceConfig(base_url="https://provider.example"),
    )
    assert isinstance(built, source_mod.FixtureSource)


@pytest.mark.parametrize(
    "payload,path,expected",
    [
        ({"a": {"b": "c"}}, "a.b", "c"),
        ({"a": [{"b": 1}]}, "a[0].b", 1),
        ({"a": {"b": [{"c": 2}, {"c": 3}]}}, "a.b[1].c", 3),
        ({"a": [1]}, "a[5]", None),
        ({"a": None}, "a.b.c", None),
        ({"a": "scalar"}, "a.b", None),
        ({}, "missing", None),
    ],
)
def test_dig(payload, path, expected):
    assert source_mod.dig(payload, path) == expected


def test_the_zero_success_code_is_not_mistaken_for_an_error():
    # Providers signal success with error_code "0000", which is a truthy
    # string. Testing the field for truth would reject every good response.
    body = json.dumps(
        {"error": "", "error_code": "0000",
         "item": {"num_iid": "1", "title": "测试商品", "price": "9.90"}},
        ensure_ascii=False,
    )
    client = build([FakeResponse(body=body)])
    raw = client.fetch_product("1")
    assert raw.status == "ok"
    assert raw.payload["item"]["title"] == "测试商品"


def test_a_real_error_code_beside_a_missing_title_is_an_error():
    body = '{"error": "item-not-found", "error_code": "2000"}'
    client = build([FakeResponse(body=body)])
    assert client.fetch_product("1").status == "upstream_error"


def test_secret_cache_and_lang_are_sent_when_configured():
    config = source_mod.SourceConfig(
        base_url="https://provider.example",
        api_key="k",
        api_secret="s",
        cache="no",
        lang="en",
        min_interval=0.0,
        backoff_base=0.0,
    )
    client = source_mod.UpstreamSource(config)
    client.session = FakeSession([FakeResponse(body=OK_BODY)])  # type: ignore[assignment]
    client.fetch_product("1")
    _, params = client.session.calls[0]
    assert params["key"] == "k"
    assert params["secret"] == "s"
    assert params["cache"] == "no"      # a cached copy is wrong for monitoring
    assert params["lang"] == "en"


def test_optional_credentials_are_omitted_rather_than_sent_empty():
    client = build([FakeResponse(body=OK_BODY)])
    client.fetch_product("1")
    _, params = client.session.calls[0]
    assert "secret" not in params
    assert "cache" not in params
    assert "lang" not in params


class ThreadSafeSession:
    """Returns the same good body to any number of concurrent callers."""

    def __init__(self) -> None:
        import threading

        self.times: list[float] = []
        self._lock = threading.Lock()
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        import time

        with self._lock:
            self.times.append(time.monotonic())
        return FakeResponse(body=OK_BODY)


def test_the_interval_is_enforced_across_threads_not_per_thread():
    # Five workers each honouring the interval on their own would be five
    # times the intended request rate, which is how a provider rate limit gets
    # tripped. The spacing has to be global.
    from concurrent.futures import ThreadPoolExecutor

    interval = 0.05
    config = source_mod.SourceConfig(
        base_url="https://provider.example",
        api_key="k",
        min_interval=interval,
        max_retries=0,
        backoff_base=0.0,
    )
    client = source_mod.UpstreamSource(config)
    session = ThreadSafeSession()
    client.session = session  # type: ignore[assignment]

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(client.fetch_product, [str(i) for i in range(10)]))

    assert len(session.times) == 10
    gaps = [b - a for a, b in zip(sorted(session.times), sorted(session.times)[1:])]
    # Allow a little slack for scheduling, but nothing like a 5x burst.
    assert min(gaps) >= interval * 0.8, f"requests bunched up: {gaps}"


def test_concurrent_fetches_all_succeed():
    from concurrent.futures import ThreadPoolExecutor

    config = source_mod.SourceConfig(
        base_url="https://provider.example", api_key="k",
        min_interval=0.0, max_retries=0, backoff_base=0.0,
    )
    client = source_mod.UpstreamSource(config)
    client.session = ThreadSafeSession()  # type: ignore[assignment]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(client.fetch_product, [str(i) for i in range(24)]))
    assert all(r.status == "ok" for r in results)


# ------------------------------------------------- header auth / offset paging

def test_header_credentials_never_reach_the_query_string():
    config = source_mod.SourceConfig(
        base_url="https://provider.example",
        auth_headers={"X-RapidAPI-Key": "secret-key", "X-RapidAPI-Host": "h"},
        min_interval=0.0, max_retries=0, backoff_base=0.0,
    )
    client = source_mod.UpstreamSource(config)
    sent = FakeSession([FakeResponse(body=OK_BODY)])
    sent.headers = dict(client.session.headers)
    client.session = sent  # type: ignore[assignment]
    client.fetch_product("1")
    _, params = sent.calls[0]
    assert "X-RapidAPI-Key" not in params
    assert "secret-key" not in str(params)


def test_the_preset_sets_the_headers_on_the_session():
    config = source_mod.otapi_1688_config("my-key")
    client = source_mod.UpstreamSource(config)
    assert client.session.headers["X-RapidAPI-Key"] == "my-key"
    assert client.session.headers["X-RapidAPI-Host"] == "otapi-1688.p.rapidapi.com"


@pytest.mark.parametrize(
    "page,offset",
    [(1, 0), (2, 50), (3, 100), (10, 450)],
)
def test_offset_paging_multiplies_by_the_frame_size(page, offset):
    # framePosition is a zero-based item offset. Sending the page number
    # instead returns the second item onwards, not the second page, and
    # nothing about the response says so.
    config = source_mod.otapi_1688_config("k", page_size=50)
    config.min_interval = 0.0
    config.max_retries = 0
    config.backoff_base = 0.0
    client = source_mod.UpstreamSource(config)
    client.session = FakeSession([FakeResponse(body='{"items": []}')])  # type: ignore[assignment]
    client.search_raw("耳机", page=page)
    _, params = client.session.calls[0]
    assert params["framePosition"] == offset
    assert params["frameSize"] == 50
    assert "page" not in params


def test_the_preset_sends_the_required_language_and_keyword_names():
    config = source_mod.otapi_1688_config("k")
    config.min_interval = 0.0
    config.max_retries = 0
    config.backoff_base = 0.0
    client = source_mod.UpstreamSource(config)
    client.session = FakeSession([FakeResponse(body='{"items": []}')])  # type: ignore[assignment]
    client.search_raw("蓝牙耳机")
    _, params = client.session.calls[0]
    assert params["language"] == "en"        # required on every OTAPI call
    assert params["ItemTitle"] == "蓝牙耳机"  # not the generic "q"


def test_the_preset_leaves_the_field_map_empty_on_purpose():
    # Guessing it would produce fields that read as present and are not. It is
    # written from a real response by capture.py + audit.py.
    assert source_mod.otapi_1688_config("k").field_map == {}


def test_a_page_param_provider_still_pages_by_page():
    client = build([FakeResponse(body='{"items": []}')])
    client.search_raw("x", page=4)
    _, params = client.session.calls[0]
    assert params["page"] == 4
    assert "framePosition" not in params
