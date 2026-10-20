"""Apify Actor entry point.

Wraps ``source`` -> ``normalize`` in the Actor lifecycle and charges
pay-per-event.

Chargeable event (the price is configured in the Actor's Monetization
settings, not here). The run itself is charged by the platform's built-in
``apify-actor-start``, so no start event is charged here:

``product-normalised``   a listing returned with normalised fields
``product-listed``       a record built from a search hit, in listing mode

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

#: A record built from a search hit. One billed provider call returns a whole
#: page of these, where a full listing is one call each, so it prices well
#: below ``product-normalised``.
EVENT_LISTED = "product-listed"

#: Operator-side provider credentials, set as Actor secrets. A buyer may
#: override them per run through the input, which is what an organisation with
#: its own provider contract needs.
ENV_BASE_URL = "SOURCE_BASE_URL"
ENV_API_KEY = "SOURCE_API_KEY"
#: Name of a built-in provider preset, e.g. "otapi-1688". A preset knows its
#: provider's paths, auth style, paging, id prefix and field maps; a bare base
#: URL knows none of that and falls back to the query-string defaults.
ENV_PROVIDER = "SOURCE_PROVIDER"

#: Set to a directory of recorded payloads to run without any provider.
ENV_FIXTURES = "ACTOR_SOURCE_FIXTURES"

#: Upper bound on paid search calls per run, whatever the limit asks for.
MAX_SEARCH_PAGES = 20

#: How many listings are fetched at once by default. The provider is a paid
#: API billed per call, so the limit on throughput is its rate limit, not
#: politeness; `min_interval` still spaces every request globally.
DEFAULT_CONCURRENCY = 5

#: What a run started by a user on a free Apify plan may fetch. Apify pays
#: nothing to the developer for those runs but the data-source calls are still
#: ours, so they are capped: three listings at full detail, and at listing level
#: fewer records than one search page holds, i.e. exactly one call. Enough to
#: try the Actor on, too little to farm.
FREE_PLAN_MAX_PRODUCTS = 3
FREE_PLAN_MAX_LISTED = 20

#: The environment variable Apify sets with the starting user's plan.
ENV_USER_IS_PAYING = "APIFY_USER_IS_PAYING"

#: Circuit breaker for runs whose listings mostly fail. Failed listings are not
#: billed but each still costs a data-source call, so a run fed thousands of
#: made-up ids would be all cost and no revenue. Trips on whichever comes
#: first: at least BREAKER_MIN_UNBILLED failures making up more than half of
#: what was attempted, or BREAKER_CONSECUTIVE failures in a row (which catches
#: a long good run that turns bad, where the ratio would be slow to move).
BREAKER_MIN_UNBILLED = 10
BREAKER_CONSECUTIVE = 20

#: Seconds between requests when the input leaves it unset. Matches the input
#: schema default; kept here too for runs started through the API without it.
DEFAULT_MIN_INTERVAL = 0.2

_ID_IN_QUERY = re.compile(r"[?&]id=(\d+)")
_ID_IN_OFFER = re.compile(r"/offer/(\d+)")

#: A listing id on its own. Taobao, Tmall and 1688 ids are long numerics.
_BARE_ID = re.compile(r"\d{6,}")
#: Last resort: any long digit run inside whatever was pasted.
_ANY_LONG_ID = re.compile(r"(\d{6,})")

#: Platforms this Actor builds URLs for. Anything else is refused rather than
#: quietly relabelled, because a record carrying a taobao URL for a listing
#: that is not on Taobao is worse than a failed run.
SUPPORTED_PLATFORMS = ("taobao", "tmall", "1688")


def item_id_from_url(url: str) -> str | None:
    """Read the listing id out of a Taobao, Tmall or 1688 URL.

    ``item.taobao.com/item.htm?id=600530677383`` -> ``"600530677383"``
    ``detail.1688.com/offer/612345678901.html``  -> ``"612345678901"``
    """
    if not url:
        return None
    match = _ID_IN_QUERY.search(url) or _ID_IN_OFFER.search(url)
    return match.group(1) if match else None


def _min_interval(actor_input: dict[str, Any]) -> float:
    """The request spacing the buyer asked for, in seconds.

    Read with an explicit None check, not ``or``: 0 is a real choice — no
    spacing, parallelism as the only limit — and ``0 or default`` silently
    turned it back into the default. Negative values clamp to 0, and a value
    that is not a number falls back to the default rather than failing the run.
    """
    raw = actor_input.get("minRequestInterval")
    if raw is None or raw == "":
        return DEFAULT_MIN_INTERVAL
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_MIN_INTERVAL


def _plan_limit(
    limit: int,
    detail_level: str,
    *,
    at_home: bool,
    plan_known: bool,
    paying: bool,
) -> tuple[int, bool]:
    """Cap a run started on a free Apify plan. Returns (limit, capped).

    Enforced on the platform always — a missing plan flag there counts as
    free, so the check fails closed. Off the platform it is enforced only when
    the flag is set explicitly, which leaves local development alone and lets
    the end-to-end tests exercise it; the SDK reads "unset" and "free" alike,
    so presence is the only way to tell them apart.
    """
    if not (at_home or plan_known) or paying:
        return limit, False
    cap = FREE_PLAN_MAX_LISTED if detail_level == "listing" else FREE_PLAN_MAX_PRODUCTS
    return min(limit, cap), limit > cap


def _breaker_reason(attempted: int, unbilled: int, consecutive: int) -> str | None:
    """Why the run should stop fetching, or None to carry on."""
    if consecutive >= BREAKER_CONSECUTIVE:
        return f"{consecutive} listings in a row came back unavailable"
    if unbilled >= BREAKER_MIN_UNBILLED and unbilled * 2 > attempted:
        return f"{unbilled} of {attempted} listings came back unavailable"
    return None


def _validate_platform(platform: str) -> str:
    """Return ``platform`` if this Actor knows it, else raise."""
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(
            f"unsupported platform {platform!r}; expected one of "
            + ", ".join(SUPPORTED_PLATFORMS)
        )
    return platform


def clean_item_id(raw: Any) -> str | None:
    """Get a listing id out of whatever was typed, or give up before paying.

    Every id handed to the provider is a billed call, so a pasted URL or a
    stray newline must not become a request that is certain to miss. Returns
    ``None`` when there is no id to be found.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if _BARE_ID.fullmatch(text):
        return text
    from_url = item_id_from_url(text)
    if from_url:
        return from_url
    found = _ANY_LONG_ID.search(text)
    return found.group(1) if found else None


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


def _preset_config(name: str, api_key: str) -> source_mod.SourceConfig:
    """Build a named provider preset, or explain which names exist."""
    factory = source_mod.PROVIDER_PRESETS.get(name)
    if factory is None:
        raise source_mod.SourceError(
            f"unknown provider {name!r}; known: "
            + ", ".join(sorted(source_mod.PROVIDER_PRESETS))
        )
    return factory(api_key)


def _build_source(
    actor_input: dict[str, Any], platform: str, min_interval: float
) -> source_mod.ProductSource:
    """Pick the data source.

    In order: recorded payloads (``ACTOR_SOURCE_FIXTURES``, replayed through
    the named preset's maps when ``SOURCE_PROVIDER`` is set, so a capture is
    read exactly as live data would be); the operator's preset with the
    buyer's key; the operator's preset with the operator's key; a bare
    operator base URL with the default query-string maps.
    """
    provider = (os.environ.get(ENV_PROVIDER) or "").strip()

    fixtures = os.environ.get(ENV_FIXTURES)
    if fixtures:
        config = _preset_config(provider, "") if provider else None
        Actor.log.info(
            "Reading recorded payloads from %s%s.",
            fixtures,
            f" through the {provider} maps" if provider else "",
        )
        return source_mod.FixtureSource(fixtures, platform=platform, config=config)

    # A buyer can bring their own key for the operator's provider, but not a
    # bare provider URL: without that provider's paths, auth style and field
    # maps a URL alone produces nothing but unparsable records.
    buyer_key = (actor_input.get("sourceApiKey") or "").strip()
    operator_key = (os.environ.get(ENV_API_KEY) or "").strip()

    if provider:
        if buyer_key:
            Actor.log.info("Using the %s provider with the key supplied in the input.", provider)
        key = buyer_key or operator_key
        if not key:
            raise source_mod.SourceError(
                f"provider {provider!r} is selected but no key is set: add the "
                f"Actor secret {ENV_API_KEY}, or supply a key in the input under "
                "Advanced."
            )
        config = _preset_config(provider, key)
    else:
        base_url = (os.environ.get(ENV_BASE_URL) or "").strip()
        if not base_url:
            raise source_mod.SourceError(
                "no product data provider is configured. Set the Actor "
                f"environment variable {ENV_PROVIDER} (e.g. otapi-1688) and the "
                f"secret {ENV_API_KEY}."
            )
        config = source_mod.SourceConfig(base_url=base_url, api_key=operator_key or None)

    config.min_interval = min_interval
    return source_mod.UpstreamSource(config, platform=platform)


def _check_platform_served(platform: str, data_source: source_mod.ProductSource) -> None:
    """Refuse a platform the configured provider does not serve.

    Without this, asking an OTAPI-1688 source for Taobao spends a billed call
    per id on certain misses before anyone notices.
    """
    served = data_source.config.platforms
    if platform not in served:
        raise ValueError(
            f"this Actor's data source serves {', '.join(served)} only; "
            f"{platform!r} was requested"
        )


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
        cleaned = clean_item_id(raw)
        if cleaned is None:
            Actor.log.warning(
                "No listing id found in %r; skipping it rather than spending a "
                "call on it.",
                raw,
            )
            continue
        if cleaned != str(raw).strip():
            Actor.log.info("Read %s out of %r.", cleaned, raw)
        add(cleaned)

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
        # A page the provider did not fill is the last one; asking for the
        # next would be a billed call for an empty page.
        page_size = data_source.config.known_page_size
        if page_size and len(batch) < page_size:
            break
        page += 1

    return ids


async def _fail_rejected_key(exc: Exception) -> None:
    """End the run on a rejected key, saying so plainly.

    Without this every listing reads as "unavailable", the breaker trips, and
    the run reports success — pointing at the ids instead of the key.
    """
    await Actor.fail(
        status_message=(
            f"The data source rejected the API key ({exc}). Check the key, and "
            "that its subscription is active."
        )
    )


async def _push_and_charge(
    record: dict[str, Any], *, event: str, is_ppe: bool, so_far: int
) -> tuple[bool, bool]:
    """Store one record and bill it. Returns ``(counted, stop)``.

    One place for this, because the condition below is subtle enough that a
    second copy would drift away from it.
    """
    charge = await Actor.push_data(record, charged_event_name=event)
    if is_ppe and charge.charged_count == 0:
        # The run's charge limit no longer covers another item, so nothing was
        # stored either. `event_charge_limit_reached` alone is not enough: it
        # is computed from this event's own price and ignores the synthetic
        # default-dataset event added on top, so it can read False while
        # pushes already store nothing. Continuing would pay for work that
        # cannot be delivered.
        Actor.log.warning(
            "Run charge limit reached after %d record(s); stopping early. "
            "Raise the run's max total charge to fetch the rest.",
            so_far,
        )
        return False, True
    counted = charge.charged_count > 0 or not is_ppe
    if is_ppe and charge.event_charge_limit_reached:
        Actor.log.warning(
            "Run charge limit reached after %d record(s); stopping early.", so_far
        )
        return counted, True
    return counted, False


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
        except source_mod.AuthenticationFailed:
            # Not one bad listing: every call will fail the same way.
            raise
        except Exception as exc:  # one bad listing must not kill the chunk
            raw = source_mod.RawProduct(
                item_id=item_id,
                platform=platform,
                payload=None,
                status="upstream_error",
                status_detail=str(exc),
            )
        # The source's own maps, always. Normalising with anything else is how
        # every record from a new provider comes out "unparsable".
        config = data_source.config
        try:
            return normalize_mod.normalize(raw, config=config, quantity=quantity)
        except Exception as exc:
            return normalize_mod.normalize(
                source_mod.RawProduct(
                    item_id=item_id,
                    platform=platform,
                    payload=None,
                    status="unparsable",
                    status_detail=str(exc),
                ),
                config=config,
            )

    if workers <= 1 or len(item_ids) == 1:
        return [one(item_id) for item_id in item_ids]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, item_ids))


async def _run_listing_mode(
    data_source: source_mod.ProductSource,
    *,
    keyword: str,
    platform: str,
    limit: int,
    quantity: int | None,
    is_ppe: bool,
) -> tuple[int, int, bool]:
    """Sell the search response itself: one call per page, not per listing.

    A search hit carries the title, price, sales line, seller and an image —
    enough to shortlist on — and a page of them costs one provider call where
    fetching each listing in full costs a call each. That is why these bill at
    their own, much lower rate.
    """
    returned = skipped = 0
    stop = False
    seen: set[str] = set()
    page = 1

    charging = Actor.get_charging_manager()
    while not stop and returned + skipped < limit and page <= MAX_SEARCH_PAGES:
        # A search page is one billed data-source call. If the run cannot pay
        # for even one more record, that call buys nothing deliverable.
        if is_ppe and charging.compute_push_data_limit(
            1, EVENT_LISTED, is_default_dataset=True
        ) <= 0:
            Actor.log.warning(
                "Run charge limit covers no more records; not requesting page %d.",
                page,
            )
            stop = True
            break
        entries = await asyncio.to_thread(data_source.search_raw, keyword, page=page)
        if not entries:
            break

        fresh = 0
        for entry in entries:
            if returned + skipped >= limit:
                break
            product = normalize_mod.normalize_search_entry(
                entry, platform=platform, quantity=quantity, config=data_source.config
            )
            if product.item_id and product.item_id in seen:
                continue
            if product.item_id:
                seen.add(product.item_id)
            fresh += 1

            record = product.to_item()
            if product.status != "listed":
                await Actor.push_data(record)
                skipped += 1
                Actor.log.warning(
                    "  not billed (%s: %s)", product.status, product.status_detail
                )
                continue

            counted, stop = await _push_and_charge(
                record, event=EVENT_LISTED, is_ppe=is_ppe, so_far=returned
            )
            if counted:
                returned += 1
            if stop:
                break

        Actor.log.info(
            "Search page %d: %d hit(s), %d new, %d record(s) so far.",
            page,
            len(entries),
            fresh,
            returned,
        )
        # A provider that repeats a page when results run short would otherwise
        # keep costing a call per loop.
        if fresh == 0:
            break
        # A page the provider did not fill is the last one; asking for the
        # next would be a billed call for an empty page.
        page_size = data_source.config.known_page_size
        if page_size and len(entries) < page_size:
            break
        page += 1

    return returned, skipped, stop


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        try:
            platform = _validate_platform(
                (actor_input.get("platform") or "1688").strip()
            )
        except ValueError as exc:
            await Actor.fail(status_message=str(exc))
            return
        limit = int(actor_input.get("maxProducts") or 10)
        detail_level = (actor_input.get("detailLevel") or "full").strip()
        limit, plan_capped = _plan_limit(
            limit,
            detail_level,
            at_home=Actor.is_at_home(),
            plan_known=os.environ.get(ENV_USER_IS_PAYING) is not None,
            paying=Actor.configuration.user_is_paying,
        )
        if plan_capped:
            Actor.log.warning(
                "Free Apify plan: this run is limited to %d %s. A paid Apify plan "
                "lifts the limit.",
                limit,
                "records" if detail_level == "listing" else "listings",
            )
        include_skus = bool(actor_input.get("includeSkus", True))
        raw_quantity = actor_input.get("quantity")
        quantity = int(raw_quantity) if raw_quantity else None
        concurrency = max(1, min(10, int(actor_input.get("concurrency") or DEFAULT_CONCURRENCY)))
        min_interval = _min_interval(actor_input)

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
            _check_platform_served(platform, data_source)
        except (source_mod.SourceError, ValueError) as exc:
            await Actor.fail(status_message=str(exc))
            return

        if detail_level == "listing":
            keyword = (actor_input.get("keyword") or "").strip()
            if not keyword:
                await Actor.set_status_message(
                    "Listing mode sells the search response itself, so it needs "
                    "a search keyword. Give one, or switch to full detail.",
                    is_terminal=True,
                )
                return
            if actor_input.get("itemIds") or actor_input.get("itemUrls"):
                Actor.log.warning(
                    "Listing mode reads search results, so the ids and URLs in "
                    "this input are ignored. Switch to full detail to fetch them."
                )
            Actor.log.info("Listing mode: selling search hits for %r.", keyword)
            try:
                returned, skipped, hit_limit = await _run_listing_mode(
                    data_source,
                    keyword=keyword,
                    platform=platform,
                    limit=limit,
                    quantity=quantity,
                    is_ppe=is_ppe,
                )
            except source_mod.AuthenticationFailed as exc:
                await _fail_rejected_key(exc)
                return
            parts = [f"Returned {returned} listing-level record(s)"]
            if plan_capped:
                parts.append(f"free Apify plan, limited to {limit}")
            if skipped:
                parts.append(f"{skipped} unusable and not billed")
            if hit_limit:
                parts.append("stopped at the run's charge limit")
            await Actor.set_status_message("; ".join(parts) + ".", is_terminal=True)
            return

        try:
            item_ids = await asyncio.to_thread(_resolve_ids, actor_input, data_source, limit)
        except source_mod.AuthenticationFailed as exc:
            await _fail_rejected_key(exc)
            return
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
        attempted = consecutive_unbilled = 0
        tripped: str | None = None
        charging = Actor.get_charging_manager()
        pending = list(item_ids)
        done = 0
        while pending:
            # Ask what the run can still pay for *before* fetching, not after.
            # A batch is fetched in parallel and only then pushed and billed,
            # so a limit discovered mid-batch would already have paid the data
            # source for listings that can never be delivered. The SDK's
            # figure counts this event and the synthetic default-dataset item
            # event together; a non-PPE run gets the whole batch.
            wanted = min(concurrency, len(pending))
            size = (
                charging.compute_push_data_limit(
                    wanted, EVENT_PRODUCT, is_default_dataset=True
                )
                if is_ppe
                else wanted
            )
            if size <= 0:
                Actor.log.warning(
                    "Run charge limit covers no more listings; stopping before "
                    "fetching the remaining %d.",
                    len(pending),
                )
                hit_charge_limit = True
                break
            chunk, pending = pending[:size], pending[size:]
            Actor.log.info(
                "Fetching a batch of %d%s.",
                size,
                " (all the charge limit still covers)" if size < wanted else "",
            )
            try:
                products = await asyncio.to_thread(
                    _fetch_chunk,
                    data_source,
                    chunk,
                    quantity=quantity,
                    workers=concurrency,
                    platform=platform,
                )
            except source_mod.AuthenticationFailed as exc:
                await _fail_rejected_key(exc)
                return

            for product in products:
                done += 1
                attempted += 1
                consecutive_unbilled = 0 if product.status == "ok" else consecutive_unbilled + 1
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

                counted, hit_charge_limit = await _push_and_charge(
                    record, event=EVENT_PRODUCT, is_ppe=is_ppe, so_far=returned
                )
                if counted:
                    returned += 1
                if hit_charge_limit:
                    break

            if hit_charge_limit:
                break

            # Checked between batches: a batch is the unit of fetching, so this
            # is the earliest point at which stopping saves calls.
            tripped = _breaker_reason(attempted, skipped, consecutive_unbilled)
            if tripped and pending:
                Actor.log.warning(
                    "Stopping early: %s. Not fetching the remaining %d — check "
                    "the ids, or whether the data source is having trouble.",
                    tripped,
                    len(pending),
                )
                break
            tripped = None

        parts = [f"Returned {returned} of {len(item_ids)} listing(s)"]
        if skipped:
            parts.append(f"{skipped} unavailable and not billed")
        if hit_charge_limit:
            unfetched = len(pending)
            parts.append(
                "stopped at the run's charge limit"
                + (f", {unfetched} not fetched" if unfetched else "")
            )
        if tripped:
            parts.append(f"stopped early because {tripped}, {len(pending)} not fetched")
        if plan_capped:
            parts.append(f"free Apify plan, limited to {limit}")
        await Actor.set_status_message("; ".join(parts) + ".", is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
