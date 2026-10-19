"""Acquiring raw product payloads.

There is no login-free Taobao endpoint (see DESIGN.md for what each entry point
actually returns), so this module does not try to be clever about it. It defines
one protocol and two adapters:

* :class:`FixtureSource`  replays recorded payloads, and drives the tests.
* :class:`UpstreamSource` calls one commercial product-data provider over HTTP.

Switching provider is a :class:`SourceConfig` change — base URL, key, endpoint
paths and a field map — not a rewrite, because everything downstream works on
the provider-neutral shape that :func:`UpstreamSource.fetch_product` returns.

Everything provider-specific lives on :class:`SourceConfig` and travels with
the source: the field maps, where a search response keeps its hits, how errors
are reported, which platforms are served and how ids are written on the wire.
The normaliser reads all of it from ``source.config``; nothing downstream may
assume one provider's shape.

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
#:
#: These are only ever matched against a non-JSON body or against a JSON
#: response's *error* fields — never against a whole product record. 滑块 is
#: also a large 1688 category (直线导轨滑块, linear-guide sliders), and an
#: English description can say "punish"; scanning the full body classed real
#: listings as refusals.
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
    #: Credentials that belong in headers rather than the query string.
    #: RapidAPI-fronted providers authenticate this way.
    auth_headers: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Some providers page by a zero-based offset rather than a page number
    #: (OTAPI's framePosition). When set, the offset is sent under this name,
    #: computed as (page - 1) * page_size, and page_param is not sent.
    offset_param: str | None = None
    page_size_param: str | None = None
    page_size: int = 50
    #: Extra query parameters every call needs, e.g. OTAPI's required
    #: ``language``. Kept apart from extra_params so a provider preset can set
    #: them without clobbering a caller's own additions.
    required_params: dict[str, str] = dataclasses.field(default_factory=dict)
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
    #: Platforms this provider actually serves. A run asking for anything else
    #: fails up front rather than spending calls on certain misses.
    platforms: tuple[str, ...] = ("taobao", "tmall", "1688")
    #: Prefix the provider puts on ids, e.g. OTAPI writes a 1688 offer as
    #: "abb-612345678901". Added before a call, stripped from what comes back,
    #: so the buyer only ever sees and types the platform's own id.
    id_prefix: str = ""
    #: Where an in-body error is reported, and the codes that mean success.
    #: Codes are compared case-insensitively, and an all-zero code ("0000")
    #: counts as success.
    error_code_paths: tuple[str, ...] = ("error_code",)
    error_message_paths: tuple[str, ...] = ("error",)
    ok_codes: tuple[str, ...] = ("ok", "success")
    #: Candidate locations of the hit list in a search response, tried in
    #: order; the first that resolves to a list (or a single dict) wins.
    search_items_paths: tuple[str, ...] = ("items.item", "items")
    #: Keys a search hit may carry its id under.
    search_id_keys: tuple[str, ...] = ("num_iid", "item_id", "id")
    #: Per-field facts the data cannot state for itself, e.g. that a mapped
    #: sales number is a 30-day figure, that stock is exact, or which keys
    #: inside a ladder rung hold the quantity and the price.
    hints: dict[str, dict[str, Any]] = dataclasses.field(
        default_factory=lambda: {"stock": {"approximate": True}}
    )
    search_field_map: dict[str, str] = dataclasses.field(
        default_factory=lambda: {
            "item_id": "num_iid",
            "title_cn": "title",
            "price": "price",
            "original_price": "orginal_price",
            "sales": "sales",
            "shop_name_cn": "nick",
            "shop_id": "seller_id",
            "ships_from": "location",
            "image": "pic_url",
        }
    )
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


    def to_wire_id(self, item_id: str) -> str:
        """The id as the provider wants it on a request."""
        if self.id_prefix and not item_id.startswith(self.id_prefix):
            return self.id_prefix + item_id
        return item_id

    def from_wire_id(self, wire_id: str) -> str:
        """The id as the platform writes it, for the record and the URL."""
        if self.id_prefix and wire_id.startswith(self.id_prefix):
            return wire_id[len(self.id_prefix):]
        return wire_id

    def in_body_error(self, payload: Any) -> str | None:
        """The provider's own error, if the 200 body carries one.

        The success code in this space is often truthy — "Ok", or a string of
        zeros — so it has to be recognised by name, not by truthiness.
        """
        if not isinstance(payload, dict):
            return None
        code = None
        for path in self.error_code_paths:
            value = dig(payload, path)
            if value not in (None, ""):
                code = str(value).strip()
                break
        if code is not None:
            if code.strip("0") == "" or code.lower() in {c.lower() for c in self.ok_codes}:
                code = None
        message = None
        for path in self.error_message_paths:
            value = dig(payload, path)
            if value not in (None, ""):
                message = str(value).strip()
                break
        if code and message:
            return f"{code}: {message}"
        return code or message


class ProductSource(Protocol):
    """What the pipeline needs from a data source."""

    config: SourceConfig

    def fetch_product(self, item_id: str) -> RawProduct: ...

    def search(self, keyword: str, *, page: int = 1) -> list[str]: ...

    def search_raw(self, keyword: str, *, page: int = 1) -> list[dict[str, Any]]: ...


_PATH_PART = re.compile(r"([^\[\]]*)((?:\[[^\]]*\])*)")
_SELECTOR = re.compile(r"\[([^\]]*)\]")


def dig(payload: Any, path: str) -> Any:
    """Follow a dotted path into nested JSON.

    ``a.b[0].c`` indexes a list. ``a[Name=TotalSales].Value`` picks the first
    dict in a list whose ``Name`` equals ``TotalSales`` — the shape providers
    use for name/value property lists, which a positional index cannot address
    because the order is not stable.

    Returns ``None`` for any missing link rather than raising, because a
    provider leaving a field out is normal and must not fail the item.
    """
    current = payload
    for part in path.split("."):
        if current is None:
            return None
        match = _PATH_PART.fullmatch(part)
        if not match:
            return None
        name, selectors = match.group(1), match.group(2)
        if name:
            if not isinstance(current, dict):
                return None
            current = current.get(name)
        for selector in _SELECTOR.findall(selectors):
            if not isinstance(current, (list, tuple)):
                return None
            if "=" in selector:
                key, _, wanted = selector.partition("=")
                current = next(
                    (
                        entry
                        for entry in current
                        if isinstance(entry, dict) and str(entry.get(key)) == wanted
                    ),
                    None,
                )
            elif selector.isdigit():
                position = int(selector)
                current = current[position] if position < len(current) else None
            else:
                return None
    return current


def looks_blocked(text: str) -> str | None:
    """Return the risk-control marker found in ``text``, if any.

    Only call this on text that is not a product record: a non-JSON body, or
    a JSON response's error fields. See ``_BLOCK_MARKERS``.
    """
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

    def __init__(
        self,
        directory: str | Path,
        *,
        platform: str = "taobao",
        config: SourceConfig | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.platform = platform
        # Replaying a provider's capture needs that provider's maps, or every
        # record reads as unparsable. Defaults to the query-string shape the
        # handwritten fixtures use.
        self.config = config or SourceConfig(base_url="")

    def fetch_product(self, item_id: str) -> RawProduct:
        """One listing — the fixture equivalent of one billed call."""
        return self._read(item_id)

    def _read(self, item_id: str) -> RawProduct:
        """Load a recorded payload without counting as a call.

        Kept apart from fetch_product so that synthesising a search page out
        of stored listings does not look, to anything watching the call
        boundary, like a fetch per listing — the live client's search makes
        exactly one call.
        """
        path = self.directory / f"{item_id}.json"
        if not path.exists():
            wire = self.directory / f"{self.config.to_wire_id(item_id)}.json"
            if wire.exists():
                path = wire
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
        return _ids_from_hits(self.search_raw(keyword, page=page), self.config)

    def search_raw(self, keyword: str, *, page: int = 1) -> list[dict[str, Any]]:
        """Replay a recorded search page, or synthesise one from the payloads.

        A ``_search.json`` captured by ``capture.py`` is used when present;
        otherwise each stored payload contributes the fields a search response
        would have carried, so listing mode is testable without one.
        """
        if page > 1:
            return []
        recorded = self.directory / "_search.json"
        if recorded.exists():
            data = json.loads(recorded.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "payload" in data:
                # A raw response saved by capture.py: read it exactly the way
                # the live client would.
                return _hits_from_search_payload(data["payload"], self.config)
            entries = data.get("items") or data.get("entries") or []
            if entries:
                return [e for e in entries if isinstance(e, dict)]
        entries: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            if path.name.startswith("_"):
                continue
            raw = self._read(path.stem)
            item = (raw.payload or {}).get("item") if raw.payload else None
            if not isinstance(item, dict):
                continue
            entries.append(
                {
                    "num_iid": item.get("num_iid", path.stem),
                    "title": item.get("title"),
                    "price": item.get("price"),
                    "orginal_price": item.get("orginal_price"),
                    "sales": item.get("sales"),
                    "nick": item.get("nick"),
                    "seller_id": item.get("seller_id"),
                    "location": item.get("location"),
                    "pic_url": (item.get("item_imgs") or [{}])[0].get("url")
                    if isinstance(item.get("item_imgs"), list)
                    and item["item_imgs"]
                    and isinstance(item["item_imgs"][0], dict)
                    else None,
                }
            )
        return entries


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
        # Header credentials are set once on the session so they are never at
        # risk of being logged with the query string.
        if config.auth_headers:
            self.session.headers.update(config.auth_headers)

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
        params: dict[str, Any] = dict(self.config.required_params)
        params.update(self.config.extra_params)
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

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = self.config.base_url.rstrip("/") + path
        last_error: Exception | None = None
        body: str | None = None
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
                break
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
        if body is None:
            raise SourceError(f"GET {path} failed after retries: {last_error}")

        # Parsing happens outside the retry loop on purpose: a body that is not
        # JSON will not become JSON on a second billed call.
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            # A non-JSON body is not a product record, so it is safe to scan
            # for the platform's interstitial markers.
            marker = looks_blocked(body)
            if marker:
                raise RiskControlBlocked(marker) from exc
            raise SourceError(f"{path} returned non-JSON: {exc}") from exc

    # --------------------------------------------------------------------- api

    def fetch_product(self, item_id: str) -> RawProduct:
        """Fetch one listing. Refusals become a status, not an exception.

        ``item_id`` is the platform's own id; the provider prefix, if any, is
        added here and nowhere else.
        """
        params = self._params(
            **{self.config.item_id_param: self.config.to_wire_id(item_id)}
        )
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

        if not isinstance(payload, dict):
            return RawProduct(
                item_id=item_id,
                platform=self.platform,
                payload=None,
                status="upstream_error",
                status_detail=f"expected a JSON object, got {type(payload).__name__}",
            )

        # A record whose title resolves is a product, full stop — even beside a
        # warning code, since some providers attach notices to good responses.
        # And a product's text is never scanned for risk-control markers: 滑块
        # is a large 1688 category and "punish" turns up in descriptions.
        title_path = self.config.field_map.get("title_cn")
        if title_path and dig(payload, title_path):
            return RawProduct(item_id=item_id, platform=self.platform, payload=payload)

        error = self.config.in_body_error(payload)
        # Only when the map *expects* a title and none came back is the body
        # known not to be a listing, and so safe to scan whole: a platform
        # refusal relayed in some field like `ret` or `msg` is caught there.
        # With no map yet (a provider still being validated) there is no way
        # to tell a refusal from a listing, so only the error fields count.
        if title_path:
            marker = looks_blocked(json.dumps(payload, ensure_ascii=False))
        else:
            marker = looks_blocked(error) if error else None
        if marker:
            return RawProduct(
                item_id=item_id,
                platform=self.platform,
                payload=payload,
                status="risk_control_blocked",
                status_detail=f"upstream reported {marker}" + (f" ({error})" if error else ""),
            )
        if error:
            squashed = error.lower().replace(" ", "").replace("-", "").replace("_", "")
            return RawProduct(
                item_id=item_id,
                platform=self.platform,
                payload=payload,
                status="not_found" if "notfound" in squashed else "upstream_error",
                status_detail=error,
            )
        # No title and no error: hand it on, and the normaliser will mark it
        # unparsable with a note that the field map may not fit.
        return RawProduct(item_id=item_id, platform=self.platform, payload=payload)

    def _paging_params(self, page: int) -> dict[str, Any]:
        """Page or offset, whichever the provider speaks.

        OTAPI's framePosition is a zero-based item offset, not a page number,
        so asking for "page 2" without multiplying by the frame size silently
        returns the second item onwards rather than the second page.
        """
        if self.config.offset_param:
            params: dict[str, Any] = {
                self.config.offset_param: (page - 1) * self.config.page_size
            }
            if self.config.page_size_param:
                params[self.config.page_size_param] = self.config.page_size
            return params
        return {self.config.page_param: page}

    def search(self, keyword: str, *, page: int = 1) -> list[str]:
        """Return platform item ids for one page of results."""
        return _ids_from_hits(self.search_raw(keyword, page=page), self.config)

    def search_response(self, keyword: str, *, page: int = 1) -> Any:
        """One page of search, exactly as the provider returned it.

        ``capture.py`` saves this rather than the parsed hits: the first
        search against a new provider is the call that shows where its hits
        live, and keeping only the ids would throw that answer away.
        """
        params = self._params(
            **{self.config.keyword_param: keyword, **self._paging_params(page)}
        )
        return self._get(self.config.search_path, params)

    def search_raw(self, keyword: str, *, page: int = 1) -> list[dict[str, Any]]:
        """One page of search results, as the provider returned them.

        The search response already carries a title, a price and an image per
        hit. Listing mode sells those directly: one billed call yields a whole
        page of records, where fetching each listing in full is one call each.
        """
        return _hits_from_search_payload(
            self.search_response(keyword, page=page), self.config
        )


def _hits_from_search_payload(payload: Any, config: SourceConfig) -> list[dict[str, Any]]:
    """Find the hit list in a search response, per the provider's config.

    Each candidate path is tested for *presence*, not truth: an empty result
    page is an empty list, and treating it as "field missing" falls through to
    the container above it and yields one garbage hit. In listing mode that
    hit looks like progress, so pagination keeps going and keeps billing.
    """
    for path in config.search_items_paths:
        items = dig(payload, path)
        if items is None:
            continue
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        return [entry for entry in items if isinstance(entry, dict)]
    return []


def _ids_from_hits(hits: list[dict[str, Any]], config: SourceConfig) -> list[str]:
    """Platform ids from search hits, with the provider prefix removed."""
    ids: list[str] = []
    for entry in hits:
        for key in config.search_id_keys:
            raw_id = entry.get(key)
            if raw_id not in (None, ""):
                ids.append(config.from_wire_id(str(raw_id)))
                break
    return ids


#: Every OTAPI host serves exactly one platform; this one is 1688.
OTAPI_1688_HOST = "otapi-1688.p.rapidapi.com"


def otapi_1688_config(api_key: str, *, page_size: int = 50) -> SourceConfig:
    """Preset for the RapidAPI-fronted OTAPI 1688 service.

    Confirmed from the provider's own request samples (2026-10-19):

    * ``GET /BatchGetItemFullInfo?language=en&itemId=abb-1243920753``
    * ``GET /BatchSearchItemsFrame?language=en&framePosition=0&frameSize=50&ItemTitle=…``
    * credentials in ``x-rapidapi-key`` / ``x-rapidapi-host`` headers;
    * 1688 ids carry an ``abb-`` prefix on the wire (categories too:
      ``CategoryId=abb-156``).

    Each of those breaks a call silently when wrong — a display name used as a
    path 404s, a page number sent as ``framePosition`` returns the second item
    onwards, a bare offer id is a miss — so they are fixed here once.

    Deliberately *not* filled in: the item and search field maps. No sample
    response is published, and guessing them would manufacture fields that
    read as present and are not; ``capture.py`` + ``audit.py`` write them from
    a real response. The error and hit-list paths below are OTAPI's documented
    conventions offered as candidates — each is only used if it resolves.
    """
    return SourceConfig(
        base_url=f"https://{OTAPI_1688_HOST}",
        api_key=None,
        auth_headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": OTAPI_1688_HOST},
        detail_path="/BatchGetItemFullInfo",
        search_path="/BatchSearchItemsFrame",
        item_id_param="itemId",
        keyword_param="ItemTitle",
        offset_param="framePosition",
        page_size_param="frameSize",
        page_size=page_size,
        required_params={"language": "en"},
        platforms=("1688",),
        id_prefix="abb-",
        error_code_paths=("ErrorCode", "error_code"),
        error_message_paths=("ErrorDescription", "ErrorMessage", "error"),
        ok_codes=("ok", "success"),
        search_items_paths=(
            "Result.Items.Items.Content",
            "Result.Items.Content",
            "Items.Items.Content",
            "Items.Content",
        ),
        search_id_keys=("Id", "id", "ItemId"),
        # Unknown until audited. Stock stays flagged approximate until a real
        # response says otherwise — claiming less precision is the safe error.
        hints={"stock": {"approximate": True}},
        # RapidAPI lists this API's latency at ~2.5s, so the spacing can be
        # small: the round-trip is the limit, not the rate.
        min_interval=0.1,
        field_map={},
        search_field_map={},
    )


#: Presets by name, for ``capture.py --provider``.
PROVIDER_PRESETS = {"otapi-1688": otapi_1688_config}


def build_source(
    *,
    fixtures_dir: str | Path | None = None,
    config: SourceConfig | None = None,
    platform: str = "taobao",
) -> ProductSource:
    """Pick an adapter. Fixtures win, so tests never reach the network."""
    if fixtures_dir:
        return FixtureSource(fixtures_dir, platform=platform, config=config)
    if config and config.base_url:
        return UpstreamSource(config, platform=platform)
    raise SourceError(
        "no data source configured: pass fixtures_dir, or a SourceConfig with a "
        "base_url and api_key for your product-data provider"
    )
