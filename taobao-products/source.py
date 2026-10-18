"""Acquiring raw product payloads.

There is no login-free Taobao endpoint (see DESIGN.md for what each entry point
actually returns), so this module does not try to be clever about it. It defines
one protocol and two adapters:

* :class:`FixtureSource`  replays recorded payloads, and drives the tests.
* :class:`UpstreamSource` calls one commercial product-data provider over HTTP.

Switching provider is a :class:`SourceConfig` change — base URL, key, endpoint
paths and a field map — not a rewrite, because everything downstream works on
the provider-neutral shape that :func:`UpstreamSource.fetch_product` returns.

What this module deliberately does not do: sign requests, carry a login
session, or answer a captcha. When upstream reports that risk control refused a
request, that becomes a ``risk_control_blocked`` status on the record. The
buyer sees which ids were refused; the run still succeeds.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Protocol

import requests

LOG = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """Upstream could not be queried successfully."""


class RiskControlBlocked(SourceError):
    """Upstream reached the platform, and the platform refused.

    Separate from :class:`SourceError` because it is a per-item outcome worth
    reporting rather than a transport failure worth retrying.
    """


#: Markers that mean the platform's risk control answered instead of the
#: catalogue. ``RGV587`` is Alibaba's rejection code; ``baxia`` and ``punish``
#: are the interstitials it serves; ``cloud_ip_bl`` is the datacentre-IP block.
_BLOCK_MARKERS = (
    "RGV587",
    "baxia",
    "punish",
    "cloud_ip_bl",
    "login.taobao.com",
    "哎哟喂",
    "被挤爆",
    "滑块",
    "访问受限",
)


@dataclasses.dataclass(slots=True)
class RawProduct:
    """A payload as the provider returned it, plus how the fetch went."""

    item_id: str
    platform: str
    payload: dict[str, Any] | None
    status: str = "ok"
    status_detail: str | None = None


@dataclasses.dataclass(slots=True)
class SourceConfig:
    """Everything provider-specific, in one place.

    ``field_map`` maps this project's field names to dotted paths into the
    provider's JSON. ``a.b[0].c`` is supported. The default map follows the
    response shape most Chinese product-data aggregators use, where the item
    sits under an ``item`` key with ``num_iid`` / ``title`` / ``price`` fields.
    """

    base_url: str
    api_key: str | None = None
    key_param: str = "key"
    #: Some providers want a second credential beside the key.
    api_secret: str | None = None
    secret_param: str = "secret"
    #: Providers serve a cached copy by default, which is wrong for price
    #: monitoring. Set to "no" to force a live read where the provider allows it.
    cache: str | None = None
    #: Where supported, asks the provider for an English rendering of the
    #: listing text. Costs nothing extra and fills titleEn when it works.
    lang: str | None = None
    detail_path: str = "/taobao/item_get"
    search_path: str = "/taobao/item_search"
    item_id_param: str = "num_iid"
    keyword_param: str = "q"
    page_param: str = "page"
    extra_params: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Minimum seconds between requests, enforced across every worker. This
    #: is a paid provider API, not someone's public website — the floor exists
    #: to stay inside a rate limit, not out of politeness to a site owner.
    min_interval: float = 0.2
    max_retries: int = 3
    #: Seconds for the first retry; doubles per attempt. Zero in tests.
    backoff_base: float = 1.0
    timeout: float = 30.0
    field_map: dict[str, str] = dataclasses.field(
        default_factory=lambda: {
            "item_id": "item.num_iid",
            "title_cn": "item.title",
            "price": "item.price",
            "original_price": "item.orginal_price",
            "price_range": "item.price_range",
            "sales": "item.sales",
            "stock": "item.num",
            "shipping": "item.post_fee",
            "ships_from": "item.location",
            "shop_name_cn": "item.nick",
            "shop_id": "item.seller_id",
            "images": "item.item_imgs",
            "skus": "item.skus.sku",
            "properties": "item.props_list",
            "tiers": "item.price_ranges",
            "min_order_quantity": "item.min_num",
            "presale": "item.presale_text",
            # Not every provider publishes these two. An absent path digs
            # to None and the normaliser falls back, so mapping them costs
            # nothing and a provider that does have them is still read.
            "shop_scores": "item.shop_score",
        }
    )


class ProductSource(Protocol):
    """What the pipeline needs from a data source."""

    def fetch_product(self, item_id: str) -> RawProduct: ...

    def search(self, keyword: str, *, page: int = 1) -> list[str]: ...


def dig(payload: Any, path: str) -> Any:
    """Follow a dotted path with optional list indices into nested JSON.

    Returns ``None`` for any missing link rather than raising, because a
    provider leaving a field out is normal and must not fail the item.
    """
    current = payload
    for part in path.split("."):
        if current is None:
            return None
        match = re.fullmatch(r"([^\[\]]*)((?:\[\d+\])*)", part)
        if not match:
            return None
        name, indices = match.group(1), match.group(2)
        if name:
            if not isinstance(current, dict):
                return None
            current = current.get(name)
        for index in re.findall(r"\[(\d+)\]", indices):
            if not isinstance(current, (list, tuple)):
                return None
            position = int(index)
            current = current[position] if position < len(current) else None
    return current


def looks_blocked(text: str) -> str | None:
    """Return the risk-control marker found in ``text``, if any."""
    for marker in _BLOCK_MARKERS:
        if marker in text:
            return marker
    return None


class FixtureSource:
    """Replays payloads from a directory of JSON files.

    A fixture is ``<item_id>.json`` holding either a bare provider payload or
    ``{"payload": …, "status": …}`` when the recorded outcome was a refusal.
    This is what lets the normalisation layer be finished and tested before any
    upstream credential exists.
    """

    def __init__(self, directory: str | Path, *, platform: str = "taobao") -> None:
        self.directory = Path(directory)
        self.platform = platform

    def fetch_product(self, item_id: str) -> RawProduct:
        path = self.directory / f"{item_id}.json"
        if not path.exists():
            return RawProduct(
                item_id=item_id,
                platform=self.platform,
                payload=None,
                status="not_found",
                status_detail=f"no fixture at {path.name}",
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "payload" in data:
            return RawProduct(
                item_id=item_id,
                platform=data.get("platform", self.platform),
                payload=data.get("payload"),
                status=data.get("status", "ok"),
                status_detail=data.get("statusDetail"),
            )
        return RawProduct(item_id=item_id, platform=self.platform, payload=data)

    def search(self, keyword: str, *, page: int = 1) -> list[str]:
        """Fixture search returns every recorded id, ignoring the keyword."""
        if page > 1:
            return []
        return sorted(p.stem for p in self.directory.glob("*.json"))


class UpstreamSource:
    """Rate-limited, retrying client for one commercial provider."""

    def __init__(self, config: SourceConfig, *, platform: str = "taobao") -> None:
        self.config = config
        self.platform = platform
        self._last_request = 0.0
        # Shared by every worker thread, so the spacing is global rather than
        # per-thread: N workers at one request each per interval would be N
        # times the intended rate.
        self._throttle_lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "apify-taobao-products/0.1",
            }
        )

    # ------------------------------------------------------------------ plumbing

    def _throttle(self) -> None:
        """Space requests globally, holding the slot while sleeping.

        The sleep happens inside the lock on purpose. Releasing it first lets
        every waiting worker read the same ``_last_request`` and wake together,
        which is the bug this is written to avoid.
        """
        with self._throttle_lock:
            wait = self.config.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _params(self, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = dict(self.config.extra_params)
        if self.config.api_key:
            params[self.config.key_param] = self.config.api_key
        if self.config.api_secret:
            params[self.config.secret_param] = self.config.api_secret
        if self.config.cache:
            params["cache"] = self.config.cache
        if self.config.lang:
            params["lang"] = self.config.lang
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = self.config.base_url.rstrip("/") + path
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(
                    url, params=params, timeout=self.config.timeout
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    raise SourceError(f"HTTP {response.status_code} from {path}")
                response.raise_for_status()
                body = response.text
                marker = looks_blocked(body)
                if marker:
                    raise RiskControlBlocked(marker)
                return response.json()
            except RiskControlBlocked:
                raise
            except json.JSONDecodeError as exc:
                raise SourceError(f"{path} returned non-JSON: {exc}") from exc
            except (requests.RequestException, SourceError) as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    break
                backoff = self.config.backoff_base * ((2**attempt) + random.uniform(0, 1.0))
                LOG.warning(
                    "GET %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                    path,
                    exc,
                    backoff,
                    attempt + 1,
                    self.config.max_retries,
                )
                time.sleep(backoff)
        raise SourceError(f"GET {path} failed after retries: {last_error}")

    # --------------------------------------------------------------------- api

    def fetch_product(self, item_id: str) -> RawProduct:
        """Fetch one listing. Refusals become a status, not an exception."""
        params = self._params(**{self.config.item_id_param: item_id})
        try:
            payload = self._get(self.config.detail_path, params)
        except RiskControlBlocked as exc:
            return RawProduct(
                item_id=item_id,
                platform=self.platform,
                payload=None,
                status="risk_control_blocked",
                status_detail=f"upstream reported {exc}",
            )
        except SourceError as exc:
            return RawProduct(
                item_id=item_id,
                platform=self.platform,
                payload=None,
                status="upstream_error",
                status_detail=str(exc),
            )

        # Providers in this space report their own errors inside a 200 body,
        # and the success code is itself a string of zeros ("0000"), which is
        # truthy. Testing the field for truth would call every good response an
        # error, so the sentinel values have to be named.
        error = payload.get("error")
        code = payload.get("error_code")
        if code is not None and str(code).strip("0") == "":
            code = None
        error = error or code
        if error and not dig(payload, self.config.field_map["title_cn"]):
            blocked = looks_blocked(json.dumps(payload, ensure_ascii=False))
            return RawProduct(
                item_id=item_id,
                platform=self.platform,
                payload=payload,
                status="risk_control_blocked" if blocked else "upstream_error",
                status_detail=str(error),
            )
        return RawProduct(item_id=item_id, platform=self.platform, payload=payload)

    def search(self, keyword: str, *, page: int = 1) -> list[str]:
        """Return item ids for one page of results."""
        params = self._params(
            **{self.config.keyword_param: keyword, self.config.page_param: page}
        )
        payload = self._get(self.config.search_path, params)
        items = dig(payload, "items.item") or dig(payload, "items") or []
        if isinstance(items, dict):
            items = [items]
        ids: list[str] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("num_iid") or entry.get("item_id") or entry.get("id")
            if raw_id:
                ids.append(str(raw_id))
        return ids


def build_source(
    *,
    fixtures_dir: str | Path | None = None,
    config: SourceConfig | None = None,
    platform: str = "taobao",
) -> ProductSource:
    """Pick an adapter. Fixtures win, so tests never reach the network."""
    if fixtures_dir:
        return FixtureSource(fixtures_dir, platform=platform)
    if config and config.base_url:
        return UpstreamSource(config, platform=platform)
    raise SourceError(
        "no data source configured: pass fixtures_dir, or a SourceConfig with a "
        "base_url and api_key for your product-data provider"
    )
