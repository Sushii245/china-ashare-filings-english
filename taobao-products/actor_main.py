"""Apify Actor entry point.

Wraps ``source`` -> ``normalize`` in the Actor lifecycle and charges
pay-per-event.

Chargeable event (the price is configured in the Actor's Monetization
settings, not here). The run itself is charged by the platform's built-in
``apify-actor-start``, so no start event is charged here:

``product-normalised``   a listing returned with normalised fields

A listing that upstream refused, that does not exist, or whose payload does not
fit the field map is still written to the dataset with a ``status`` saying which
of those happened, and **is not charged**. Reporting the refusal is worth more
to the buyer than a failed run, and billing for it would not be honest.

Run it locally against the fixtures with::

    ACTOR_SOURCE_FIXTURES=tests/fixtures python -m actor_main
"""

from __future__ import annotations

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from apify import Actor

import normalize as normalize_mod
import source as source_mod

EVENT_PRODUCT = "product-normalised"

#: Operator-side provider credentials, set as Actor secrets. A buyer may
#: override them per run through the input, which is what an organisation with
#: its own provider contract needs.
ENV_BASE_URL = "SOURCE_BASE_URL"
ENV_API_KEY = "SOURCE_API_KEY"

#: Set to a directory of recorded payloads to run without any provider.
ENV_FIXTURES = "ACTOR_SOURCE_FIXTURES"

#: Upper bound on paid search calls per run, whatever the limit asks for.
MAX_SEARCH_PAGES = 20

#: How many listings are fetched at once by default. The provider is a paid
#: API billed per call, so the limit on throughput is its rate limit, not
#: politeness; `min_interval` still spaces every request globally.
DEFAULT_CONCURRENCY = 5

_ID_IN_QUERY = re.compile(r"[?&]id=(\d+)")
_ID_IN_OFFER = re.compile(r"/offer/(\d+)")


def item_id_from_url(url: str) -> str | None:
    """Read the listing id out of a Taobao, Tmall or 1688 URL.

    ``item.taobao.com/item.htm?id=600530677383`` -> ``"600530677383"``
    ``detail.1688.com/offer/612345678901.html``  -> ``"612345678901"``
    """
    if not url:
        return None
    match = _ID_IN_QUERY.search(url) or _ID_IN_OFFER.search(url)
    return match.group(1) if match else None


def _urls_from_input(raw: Any) -> list[str]:
    """Flatten the ``requestListSources`` editor's shape into plain URLs."""
    urls: list[str] = []
    for entry in raw or []:
        if isinstance(entry, str):
            urls.append(entry)
        elif isinstance(entry, dict):
            url = entry.get("url") or entry.get("requestsFromUrl")
            if url:
                urls.append(str(url))
    return urls


def _build_source(
    actor_input: dict[str, Any], platform: str, min_interval: float
) -> source_mod.ProductSource:
    """Pick the data source: fixtures, the buyer's provider, or the operator's."""
    fixtures = os.environ.get(ENV_FIXTURES)
    if fixtures:
        Actor.log.info("Reading recorded payloads from %s.", fixtures)
        return source_mod.FixtureSource(fixtures, platform=platform)

    base_url = (actor_input.get("sourceBaseUrl") or "").strip()
    api_key = (actor_input.get("sourceApiKey") or "").strip()
    if base_url:
        Actor.log.info("Using the product data provider supplied in the input.")
    else:
        base_url = (os.environ.get(ENV_BASE_URL) or "").strip()
        api_key = (os.environ.get(ENV_API_KEY) or "").strip()

    if not base_url:
        raise source_mod.SourceError(
            "no product data provider is configured. Set the Actor secrets "
            f"{ENV_BASE_URL} and {ENV_API_KEY}, or supply them in the input "
            "under Advanced."
        )

    config = source_mod.SourceConfig(
        base_url=base_url, api_key=api_key or None, min_interval=min_interval
    )
    if platform == "1688":
        config.detail_path = "/1688/item_get"
        config.search_path = "/1688/item_search"
    return source_mod.UpstreamSource(config, platform=platform)


def _resolve_ids(
    actor_input: dict[str, Any],
    data_source: source_mod.ProductSource,
    limit: int,
) -> list[str]:
    """Collect ids from ids, URLs and a keyword search, in that order."""
    ids: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        if not candidate or candidate in seen or len(ids) >= limit:
            return
        seen.add(candidate)
        ids.append(candidate)

    for raw in actor_input.get("itemIds") or []:
        add(str(raw).strip() or None)

    for url in _urls_from_input(actor_input.get("itemUrls")):
        found = item_id_from_url(url)
        if found:
            add(found)
        else:
            Actor.log.warning("No listing id found in URL, skipping: %s", url)

    # Stop as soon as a page stops contributing. Providers repeat the first
    # page when a keyword has fewer results than a page holds, and every call
    # is billed, so "the page was not empty" is not enough to keep going.
    keyword = (actor_input.get("keyword") or "").strip()
    page = 1
    while keyword and len(ids) < limit and page <= MAX_SEARCH_PAGES:
        before = len(ids)
        batch = data_source.search(keyword, page=page)
        if not batch:
            break
        for found in batch:
            add(found)
        if len(ids) == before:
            Actor.log.info(
                "Search page %d returned nothing new; stopping at %d id(s).",
                page,
                len(ids),
            )
            break
        page += 1

    return ids


def _fetch_chunk(
    data_source: source_mod.ProductSource,
    item_ids: list[str],
    *,
    quantity: int | None,
    workers: int,
    platform: str,
) -> list[normalize_mod.Product]:
    """Fetch and normalise a group of listings, returned in the order asked.

    Runs in one worker thread of its own, with a small pool inside it. Order is
    preserved because the records are pushed and billed afterwards in sequence,
    and a buyer reading the dataset should see the ids they listed.
    """

    def one(item_id: str) -> normalize_mod.Product:
        try:
            raw = data_source.fetch_product(item_id)
        except Exception as exc:  # one bad listing must not kill the chunk
            raw = source_mod.RawProduct(
                item_id=item_id,
                platform=platform,
                payload=None,
                status="upstream_error",
                status_detail=str(exc),
            )
        try:
            return normalize_mod.normalize(raw, quantity=quantity)
        except Exception as exc:
            return normalize_mod.normalize(
                source_mod.RawProduct(
                    item_id=item_id,
                    platform=platform,
                    payload=None,
                    status="unparsable",
                    status_detail=str(exc),
                )
            )

    if workers <= 1 or len(item_ids) == 1:
        return [one(item_id) for item_id in item_ids]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, item_ids))


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        platform = (actor_input.get("platform") or "taobao").strip()
        limit = int(actor_input.get("maxProducts") or 10)
        include_skus = bool(actor_input.get("includeSkus", True))
        raw_quantity = actor_input.get("quantity")
        quantity = int(raw_quantity) if raw_quantity else None
        concurrency = max(1, min(10, int(actor_input.get("concurrency") or DEFAULT_CONCURRENCY)))
        min_interval = max(0.0, float(actor_input.get("minRequestInterval") or 0.2))

        # A non-monetised Actor (and any non-PPE pricing model) charges nothing,
        # so `charged_count` is always 0 there and must not be read as "limit
        # reached" — that would stop the run after the first item.
        pricing = Actor.get_charging_manager().get_pricing_info()
        is_ppe = bool(pricing.is_pay_per_event)
        Actor.log.info(
            "Pricing model: %s (pay-per-event: %s)",
            pricing.pricing_model or "none",
            is_ppe,
        )

        try:
            data_source = _build_source(actor_input, platform, min_interval)
        except source_mod.SourceError as exc:
            await Actor.fail(status_message=str(exc))
            return

        item_ids = await asyncio.to_thread(_resolve_ids, actor_input, data_source, limit)
        if not item_ids:
            await Actor.set_status_message(
                "No listings to fetch: give Item IDs, Item URLs or a search keyword.",
                is_terminal=True,
            )
            return

        Actor.log.info("Fetching %d listing(s) from %s.", len(item_ids), platform)
        if quantity:
            Actor.log.info("Pricing each listing at %d piece(s).", quantity)
        Actor.log.info(
            "Fetching %d at a time, at least %.2fs apart.", concurrency, min_interval
        )

        returned = skipped = 0
        hit_charge_limit = False
        chunks = [
            item_ids[i : i + concurrency] for i in range(0, len(item_ids), concurrency)
        ]
        done = 0
        for chunk in chunks:
            products = await asyncio.to_thread(
                _fetch_chunk,
                data_source,
                chunk,
                quantity=quantity,
                workers=concurrency,
                platform=platform,
            )

            for product in products:
                done += 1
                Actor.log.info(
                    "[%d/%d] %s %s", done, len(item_ids), product.item_id, product.status
                )

                if not include_skus:
                    product.skus = []
                record = product.to_item()

                if product.status != "ok":
                    await Actor.push_data(record)
                    skipped += 1
                    Actor.log.warning(
                        "  not billed (%s: %s)", product.status, product.status_detail
                    )
                    continue

                charge = await Actor.push_data(record, charged_event_name=EVENT_PRODUCT)
                if is_ppe and charge.charged_count == 0:
                    # The run's charge limit no longer covers another item, so
                    # nothing was stored either. `event_charge_limit_reached`
                    # alone is not enough: it is computed from this event's own
                    # price and ignores the synthetic default-dataset event
                    # added on top, so it can read False while pushes already
                    # store nothing.
                    Actor.log.warning(
                        "Run charge limit reached after %d listing(s); stopping early. "
                        "Raise the run's max total charge to fetch the rest.",
                        returned,
                    )
                    hit_charge_limit = True
                    break
                if charge.charged_count > 0 or not is_ppe:
                    returned += 1
                if is_ppe and charge.event_charge_limit_reached:
                    Actor.log.warning(
                        "Run charge limit reached after %d listing(s); stopping early.",
                        returned,
                    )
                    hit_charge_limit = True
                    break

            if hit_charge_limit:
                break

        parts = [f"Returned {returned} of {len(item_ids)} listing(s)"]
        if skipped:
            parts.append(f"{skipped} unavailable and not billed")
        if hit_charge_limit:
            parts.append("stopped at the run's charge limit")
        await Actor.set_status_message("; ".join(parts) + ".", is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
