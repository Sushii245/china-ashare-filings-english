"""Provider payload -> typed record.

No I/O here, so every branch is reachable from a fixture. The functions are
defensive about shape on purpose: providers in this space return the same field
as a string, a number, a dict or a one-element list depending on the listing,
and a listing that happens to have one SKU is a frequent cause of a parser
that works on the sample and fails in production.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import units
from models import Money, Product, PriceTier, Quote, Sales, ShopScore, Sku, Status, Stock
from source import RawProduct, SourceConfig, dig

#: 1688 item ids live under a different host than Taobao's.
_URL_TEMPLATES = {
    "taobao": "https://item.taobao.com/item.htm?id={item_id}",
    "tmall": "https://detail.tmall.com/item.htm?id={item_id}",
    "1688": "https://detail.1688.com/offer/{item_id}.html",
}


#: A property-id key, e.g. "1627207:28320" or "20509". Digits and colons
#: only, so a real axis name like 品牌 or "Ships from" never matches.
_PROPERTY_ID = re.compile(r"[\d:]+")

#: A plain decimal with nothing around it: a typed value serialised as a string.
_BARE_NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: Any CJK ideograph. A provider asked for English may hand back a translated
#: title; a field named titleCn must never carry it.
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _split_titles(primary: str | None, english: str | None) -> tuple[str | None, str | None]:
    """Sort a provider's titles into (Chinese, English).

    ``primary`` is what the map calls the Chinese title. If it carries no
    Chinese at all it is a translation, and goes to the English slot instead —
    a field named for Chinese holding English is worse than a null.
    """
    title_cn = primary if primary and _CJK.search(primary) else None
    title_en = english
    if primary and not title_cn and not title_en:
        title_en = primary
    if title_en and not title_cn and _mostly_cjk(title_en):
        # The "English" field is in fact Chinese: put it where it belongs.
        # Mostly, not merely containing: a translated title often keeps the
        # brand in Chinese ("策马 (Wholesale), camouflage T-shirt ..."), and
        # one brand name does not make a title Chinese.
        title_cn, title_en = title_en, None
    return title_cn, title_en


def _mostly_cjk(text: str) -> bool:
    """True when CJK ideographs outnumber Latin letters."""
    cjk = len(_CJK.findall(text))
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return cjk > latin


def _as_list(value: Any) -> list[Any]:
    """Coerce the one-or-many shapes providers use into a list.

    A single SKU may arrive as a bare dict, and an empty one as ``""``.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _first_str(value: Any) -> str | None:
    """Reduce a scalar-or-container to the first usable string."""
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or None
    if isinstance(value, dict):
        for key in ("value", "text", "title", "name", "url"):
            if key in value:
                return _first_str(value[key])
        return None
    if isinstance(value, (list, tuple)):
        for entry in value:
            found = _first_str(entry)
            if found:
                return found
    return None


def _scalar(value: Any) -> str | float | int | None:
    """Keep a number a number, reduce anything else to its first string.

    Matters because the parsers record the Chinese text a value came from. A
    provider that types ``min_num`` as the integer 1 did not say "1" anywhere,
    so stringifying it first would put a ``sourceTextCn`` on the record that
    the listing never contained.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return _first_str(value)


def _parse_sku_options(properties_name: str | None) -> dict[str, str]:
    """Decode Taobao's packed SKU axis string.

    The format is ``pid:vid:axis:value`` joined by ``;``, e.g.
    ``"1627207:28320:颜色分类:黑色;20509:28314:尺码:XL"`` -> ``{"颜色分类":
    "黑色", "尺码": "XL"}``. Ids are discarded; the axis names are what a buyer
    can act on.
    """
    text = units.normalize_text(properties_name)
    if not text:
        return {}
    options: dict[str, str] = {}
    for chunk in text.split(";"):
        if not chunk.strip():
            continue
        parts = chunk.split(":")
        if len(parts) >= 4:
            axis, value = parts[-2].strip(), parts[-1].strip()
        elif len(parts) == 2:
            axis, value = parts[0].strip(), parts[1].strip()
        else:
            continue
        if axis and value:
            options[axis] = value
    return options


def _structured_skus(raw: Any, hint: dict[str, Any]) -> list[Sku]:
    """Variants given as typed objects, axes as a list of name/value pairs.

    OTAPI writes a 1688 variant as ``{"Id", "Quantity", "Price": {...},
    "Configurators": [{"Pid": "颜色", "Vid": "白色"}, ...]}`` — the axis names
    and values are the seller's own Chinese, so they go in ``optionsCn``.
    """
    skus: list[Sku] = []
    for entry in _as_list(raw):
        if not isinstance(entry, dict):
            continue
        options: dict[str, str] = {}
        for axis in _as_list(dig(entry, hint["options"]) if hint.get("options") else None):
            if not isinstance(axis, dict):
                continue
            name = _first_str(axis.get(hint.get("option_name", "name")))
            value = _first_str(axis.get(hint.get("option_value", "value")))
            if name and value:
                options[name] = value
        stock_raw = _scalar(dig(entry, hint["stock"])) if hint.get("stock") else None
        stock = (
            float(stock_raw) if isinstance(stock_raw, (int, float))
            else units.parse_cn_number(stock_raw)
        )
        skus.append(
            Sku(
                sku_id=_first_str(dig(entry, hint["id"])) if hint.get("id") else None,
                options_cn=options,
                price=units.parse_price(_scalar(dig(entry, hint["price"])))
                if hint.get("price")
                else None,
                stock=int(stock) if stock is not None else None,
            )
        )
    return skus


def _normalize_skus(raw: Any, hint: dict[str, Any] | None = None) -> list[Sku]:
    if hint:
        return _structured_skus(raw, hint)
    skus: list[Sku] = []
    for entry in _as_list(raw):
        if not isinstance(entry, dict):
            continue
        options = _parse_sku_options(
            entry.get("properties_name") or entry.get("propertiesName")
        )
        if not options and isinstance(entry.get("options"), dict):
            options = {str(k): str(v) for k, v in entry["options"].items()}
        stock_raw = entry.get("quantity", entry.get("stock"))
        stock = units.parse_cn_number(_first_str(stock_raw))
        skus.append(
            Sku(
                sku_id=_first_str(entry.get("sku_id") or entry.get("skuId")),
                options_cn=options,
                price=units.parse_price(_scalar(entry.get("price"))),
                stock=int(stock) if stock is not None else None,
            )
        )
    return skus


def _normalize_images(raw: Any) -> list[str]:
    images: list[str] = []
    for entry in _as_list(raw):
        if isinstance(entry, dict):
            # Key case varies by provider: OTAPI's Pictures[] use "Url".
            entry = next(
                (value for key, value in entry.items() if key.lower() == "url"),
                entry,
            )
        url = _first_str(entry)
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http"):
            images.append(url)
    return images


def _normalize_attributes(raw: Any, hint: dict[str, Any] | None = None) -> dict[str, str]:
    """Flatten a property list into ``{axis: value}``.

    Providers send this as ``{"1627207:28320": "颜色分类:黑色"}``, as
    ``[{"name": …, "value": …}]``, or as a plain ``{axis: value}`` map.

    The first shape keys by property id, so the axis name has to come out of
    the value. When it does not carry one, there is no axis name to be had and
    the entry is dropped: emitting the id would put ``"1627207:28320": "黑色"``
    in front of a buyer who cannot act on it.
    """
    attributes: dict[str, str] = {}
    if hint and isinstance(raw, list):
        # Typed name/value records. Entries flagged by the hint's "skip" key
        # (OTAPI's IsConfigurator) are variant axes, already in the SKUs. A
        # property listed several times keeps every value: a plain dict would
        # silently keep the last of seven 主要销售地区.
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if hint.get("skip") and entry.get(hint["skip"]):
                continue
            axis = _first_str(entry.get(hint.get("name", "name")))
            value = _first_str(entry.get(hint.get("value", "value")))
            if not axis or not value:
                continue
            if axis in attributes:
                if value not in attributes[axis].split("、"):
                    attributes[axis] = f"{attributes[axis]}、{value}"
            else:
                attributes[axis] = value
        return attributes
    if isinstance(raw, dict):
        for key, value in raw.items():
            text = _first_str(value)
            if not text:
                continue
            # Keys are numeric property ids ("1627207:28320") and the value
            # carries the axis name, so the axis comes from the value.
            if ":" in text:
                axis, _, val = text.partition(":")
                axis, val = axis.strip(), val.strip()
                if axis and val:
                    attributes[axis] = val
                    continue
            if not _PROPERTY_ID.fullmatch(str(key).strip()):
                attributes[str(key)] = text
        return attributes
    for entry in _as_list(raw):
        if not isinstance(entry, dict):
            continue
        axis = _first_str(entry.get("name") or entry.get("title") or entry.get("key"))
        value = _first_str(entry.get("value") or entry.get("text"))
        if axis and value:
            attributes[axis] = value
    return attributes


def _normalize_shop_scores(raw: Any, hint: dict[str, Any] | None = None) -> list[ShopScore]:
    """Build the three seller scores from whatever shape arrived.

    With an ``aspects`` hint the source is a dict of typed scores keyed by the
    provider's own names, e.g. OTAPI's ``{"ItemScore": 3.0, "ServiceScore":
    4.0, "DeliveryScore": 3.4}``. Those carry no Chinese label, so
    ``aspect_cn`` stays null rather than having one invented.
    """
    scores: list[ShopScore] = []
    if hint and hint.get("aspects") and isinstance(raw, dict):
        for key, aspect in hint["aspects"].items():
            score = units.parse_cn_number(_first_str(raw.get(key)))
            if score is None:
                continue
            scores.append(ShopScore(aspect=aspect, aspect_cn=None, score=score))
        return scores
    entries = _as_list(raw)
    if len(entries) == 1 and isinstance(entries[0], dict):
        single = entries[0]
        # A flat map keyed by the Chinese aspect name.
        if any(aspect_cn in single for _, aspect_cn in units.SHOP_SCORE_ASPECTS):
            entries = [
                {"title": aspect_cn, "score": single.get(aspect_cn)}
                for _, aspect_cn in units.SHOP_SCORE_ASPECTS
                if single.get(aspect_cn) is not None
            ]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = _first_str(entry.get("title") or entry.get("name")) or ""
        aspect = next(
            (en for en, cn in units.SHOP_SCORE_ASPECTS if cn in title),
            None,
        )
        score_text = _first_str(entry.get("score") or entry.get("value"))
        score = units.parse_cn_number(score_text)
        if score is None:
            continue
        delta_text = _first_str(
            entry.get("compare") or entry.get("level") or entry.get("delta")
        )
        scores.append(
            ShopScore(
                aspect=aspect or "unknown",
                aspect_cn=title or "",
                score=score,
                vs_peers_pct=units.parse_peer_delta(delta_text),
                source_text_cn=f"{title} {score_text or ''}".strip() or None,
            )
        )
    return scores


def _normalize_tiers(raw: Any, hint: dict[str, Any] | None = None) -> list[PriceTier]:
    """Build a 阶梯价 ladder, sorted by the quantity it starts at.

    Two shapes. Text rungs ("2-99件" / "12.00") carry their own span and are
    parsed as written. Structured rungs carry numbers under keys the provider
    chooses, named by the ``tiers`` hint — and usually only a *minimum*
    quantity, the top of each rung being one below the next rung's start.
    Without deriving that, a structured ladder used to be dropped whole, and
    the quote had nothing to walk.
    """
    if hint and (hint.get("min_qty") or hint.get("price")):
        return _structured_tiers(raw, hint)
    tiers: list[PriceTier] = []
    entries = _as_list(raw)
    if isinstance(raw, dict):
        entries = [{"range": k, "price": v} for k, v in raw.items()]
    for entry in entries:
        if isinstance(entry, dict):
            span_text = _first_str(
                entry.get("range")
                or entry.get("quantity")
                or entry.get("begin")
                or entry.get("qty")
            )
            price_text = _first_str(entry.get("price") or entry.get("unit_price"))
        else:
            span_text = _first_str(entry)
            price_text = None
        span = units.parse_tier_range(span_text)
        price = units.parse_price(price_text)
        if span is None or price is None:
            continue
        tiers.append(
            PriceTier(
                min_qty=span[0],
                max_qty=span[1],
                unit_price=price,
                source_text_cn=f"{span_text} {price_text}".strip(),
            )
        )
    tiers.sort(key=lambda t: t.min_qty)
    return tiers


def _structured_tiers(raw: Any, hint: dict[str, Any]) -> list[PriceTier]:
    """Rungs given as numbers, possibly with only a starting quantity each."""
    rungs: list[tuple[int, int | None, Money]] = []
    for entry in _as_list(raw):
        if not isinstance(entry, dict):
            continue
        min_raw = dig(entry, hint["min_qty"]) if hint.get("min_qty") else None
        max_raw = dig(entry, hint["max_qty"]) if hint.get("max_qty") else None
        price_raw = dig(entry, hint["price"]) if hint.get("price") else None
        low = units.parse_quantity(_scalar(min_raw))
        price = units.parse_price(_scalar(price_raw))
        if low is None or price is None:
            continue
        high = units.parse_quantity(_scalar(max_raw)) if max_raw is not None else None
        rungs.append((low.value, high.value if high else None, price))

    rungs.sort(key=lambda r: r[0])
    tiers: list[PriceTier] = []
    for index, (low, high, price) in enumerate(rungs):
        if high is None and index + 1 < len(rungs):
            # Only a start was given: the rung ends where the next begins.
            high = rungs[index + 1][0] - 1
        tiers.append(
            PriceTier(
                min_qty=low,
                max_qty=high,
                unit_price=price,
                # Typed numbers, not listing text: nothing to quote verbatim.
                source_text_cn=None,
            )
        )
    return tiers


def _platform_for(raw: RawProduct) -> str:
    """Trust the source's platform when it names one this project knows."""
    if raw.platform in _URL_TEMPLATES:
        return raw.platform
    return "taobao"


def normalize(
    raw: RawProduct,
    *,
    config: SourceConfig | None = None,
    quantity: int | None = None,
) -> Product:
    """Turn one provider payload into the record the buyer receives.

    A payload that is missing or that upstream refused still produces a record,
    carrying ``status`` and ``statusDetail``. Those are not charged.

    ``quantity``, when given, adds a :class:`Quote`: the unit price and
    subtotal for that many pieces, resolved against the wholesale ladder.
    """
    config = config or SourceConfig(base_url="")
    field_map = config.field_map
    hints = config.hints or {}
    platform = _platform_for(raw)
    # The record and its URL always carry the platform's own id. A provider
    # prefix such as OTAPI's "abb-" stays on the wire.
    platform_id = config.from_wire_id(raw.item_id)
    url = _URL_TEMPLATES[platform].format(item_id=platform_id)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    if raw.payload is None or raw.status != "ok":
        return Product(
            item_id=platform_id,
            platform=platform,  # type: ignore[arg-type]
            url=url,
            status=raw.status if raw.status != "ok" else "unparsable",  # type: ignore[arg-type]
            status_detail=raw.status_detail or "no payload returned",
            fetched_at=now,
        )

    def field(name: str) -> Any:
        path = field_map.get(name)
        return dig(raw.payload, path) if path else None

    title_cn, title_en = _split_titles(
        _first_str(field("title_cn")), _first_str(field("title_en"))
    )
    if not title_cn and not title_en:
        return Product(
            item_id=platform_id,
            platform=platform,  # type: ignore[arg-type]
            url=url,
            status="unparsable",
            status_detail="payload carried no title; field map may not match this provider",
            fetched_at=now,
        )

    # A range field, when the provider sends one, is more informative than the
    # single price, which is usually just the low end of the same span.
    skus = _normalize_skus(field("skus"), hints.get("skus"))

    # A provider that states the span outright is best. Most do not — the
    # OneBound docs define no price_range field — so the span is taken from the
    # SKU prices instead. It carries no source_text_cn, because the listing
    # never printed it as a range: a null there means "derived, not quoted".
    original_price = units.parse_price(_scalar(field("original_price")))
    price = units.parse_price(_scalar(field("price_range")))
    if price is None:
        price = units.parse_price(_scalar(field("price")))
        # The listing price counts as a candidate alongside the variants. A
        # listing at 9.90 with one variant priced 19.90 is a span, even though
        # only one variant carries a price of its own. But not when it is a
        # promotion: variants carry the regular price, and mixing the two
        # turned "3.14 on promotion, normally 3.30" into a 3.14-3.30 span.
        candidates = [s.price.value for s in skus if s.price]
        on_promotion = (
            price is not None
            and original_price is not None
            and price.value < original_price.value
        )
        if price is not None and not on_promotion:
            candidates.append(price.value)
        if price is not None and len(set(candidates)) > 1:
            price = Money(value=min(candidates), value_max=max(candidates))

    stock_raw = _scalar(field("stock"))
    stock_text = stock_raw if isinstance(stock_raw, str) else None
    stock_value = (
        float(stock_raw) if isinstance(stock_raw, (int, float))
        else units.parse_cn_number(stock_text)
    )
    stock_approximate = bool((hints.get("stock") or {}).get("approximate", True))
    shipping = units.parse_shipping(_first_str(field("shipping")))
    ships_from = _first_str(field("ships_from"))
    if shipping and ships_from:
        shipping.ships_from_cn = ships_from
    elif ships_from:
        shipping = units.parse_shipping(ships_from)
        if shipping:
            shipping.included = None
            shipping.ships_from_cn = ships_from

    presale_text = _first_str(field("presale"))
    is_presale = None
    if presale_text is not None:
        is_presale = "预售" in units.normalize_text(presale_text)

    mapped_id = _first_str(field("item_id"))
    product = Product(
        item_id=config.from_wire_id(mapped_id) if mapped_id else platform_id,
        platform=platform,  # type: ignore[arg-type]
        url=url,
        status="ok",
        title_cn=title_cn,
        title_en=title_en,
        price=price,
        original_price=original_price,
        price_tiers=_normalize_tiers(field("tiers"), hints.get("tiers")),
        min_order_quantity=units.parse_quantity(_scalar(field("min_order_quantity"))),
        sales=_sales(field("sales"), hints.get("sales")),
        stock=(
            Stock(
                value=int(stock_value),
                # Whether the figure is a count or a rounded display value is
                # a fact about the provider, so it comes from its hints.
                is_approximate=stock_approximate,
                source_text_cn=stock_text,
            )
            if stock_value is not None
            else None
        ),
        shipping=shipping,
        is_presale=is_presale,
        presale_ships_within_days=units.parse_presale_days(presale_text),
        shop_name_cn=_first_str(field("shop_name_cn")),
        shop_id=(
            config.from_wire_id(_first_str(field("shop_id")))
            if _first_str(field("shop_id"))
            else None
        ),
        shop_scores=_normalize_shop_scores(field("shop_scores"), hints.get("shop_scores")),
        skus=skus,
        sku_count=len(skus) or None,
        images=_normalize_images(field("images")),
        attributes_cn=_normalize_attributes(field("properties"), hints.get("properties")),
        fetched_at=now,
    )

    product.discount_pct = _discount_pct(product.price, product.original_price)
    if quantity is not None and quantity > 0:
        product.quote = _build_quote(product, quantity)
    return product


def _sales(raw: Any, hint: dict[str, Any] | None) -> Sales | None:
    """Parse a sales figure, filling in what the data cannot say for itself.

    A text figure like "月销1000+" states its own window. A structured one —
    a bare number under a field called SalesInLast30Days — does not, and the
    window lives in the field's *name*; the provider's ``sales`` hint carries
    it. A hint never overrides what the text itself says.
    """
    value = _scalar(raw)
    if isinstance(value, str) and _BARE_NUMBER.fullmatch(value.strip()):
        # A number that merely arrived as a string ("860") is not listing
        # text. Citing it as sourceTextCn beside a window taken from the hint
        # would imply the text supports a claim it never made.
        value = float(value.strip())
    sales = units.parse_sales(value)
    if sales is None or not hint:
        return sales
    if sales.window is None and hint.get("window"):
        sales.window = hint["window"]
    if sales.unit is None and hint.get("unit"):
        sales.unit = hint["unit"]
    return sales


def _build_quote(product: Product, quantity: int) -> Quote:
    """Price ``quantity`` pieces against the ladder, or the flat price.

    On 1688 the headline price is the *cheapest* rung of the ladder — the price
    at the largest order. So whenever a ladder exists, the quote comes from the
    ladder and never falls back to the headline: doing that for an order below
    the first rung, or between rungs, quoted a 2-piece order at the
    10,000-piece price (4.3 元 a piece quoted as 3.3 on a real listing). The
    buyer budgets off this number, and understating it is the harmful error.
    """
    quote = Quote(quantity=quantity)
    tiers = product.price_tiers  # sorted by min_qty

    moq = product.min_order_quantity
    if moq is not None:
        # Below the seller's minimum the order cannot be placed as asked. The
        # price is still worth showing, so this is a flag, not a refusal.
        quote.meets_minimum_order = quantity >= moq.value
    elif tiers:
        # No stated minimum, but the ladder's first rung is the smallest
        # quantity the seller prices at all.
        quote.meets_minimum_order = quantity >= tiers[0].min_qty

    # A ladder should not overlap, but some do, and then there is no way to
    # know which rung the seller means. The dearest matching rung wins:
    # understating an order is the harmful direction to be wrong in.
    matching = [
        tier
        for tier in tiers
        if quantity >= tier.min_qty
        and (tier.max_qty is None or quantity <= tier.max_qty)
    ]
    if matching:
        rung = max(matching, key=lambda t: t.unit_price.value)
    elif tiers:
        # No rung contains the quantity: either it is below the ladder, where
        # the first rung is what the smallest order the seller accepts costs,
        # or it falls in a gap, where the rung below still applies — a rung's
        # price holds until the next one begins.
        below = [tier for tier in tiers if tier.min_qty <= quantity]
        rung = below[-1] if below else tiers[0]
    else:
        rung = None

    if rung is not None:
        quote.unit_price = rung.unit_price
        quote.tier_source_text_cn = rung.source_text_cn
    elif product.price is not None and not product.price.is_range:
        # No ladder at all: the flat price. A span across variants has no
        # single unit price to quote.
        quote.unit_price = product.price

    if quote.unit_price is not None:
        # Derived, so it carries no source text: the listing never printed it.
        quote.subtotal = Money(value=round(quote.unit_price.value * quantity, 2))

    return quote


def _discount_pct(price: Money | None, original: Money | None) -> float | None:
    """How far below the struck-through price the current one sits.

    Compares the low end of each, so a listing that discounts a whole span is
    still comparable. Returns ``None`` unless the original is genuinely higher
    — a listing with the two equal is not on sale, and one where the "original"
    is lower is not something to editorialise about.
    """
    if price is None or original is None or original.value <= 0:
        return None
    if original.value <= price.value:
        return None
    return round((original.value - price.value) / original.value * 100, 1)


def normalize_search_entry(
    entry: dict[str, Any],
    *,
    platform: str,
    quantity: int | None = None,
    config: SourceConfig | None = None,
) -> Product:
    """Turn one search hit into a ``listed`` record.

    A search response already carries the title, price, sales line, seller and
    an image, so one billed call yields a whole page of usable records where
    fetching each listing in full costs a call each. The record says
    ``status: "listed"`` rather than ``ok``, because the fields a search hit
    cannot carry — usually the SKU matrix, shipping terms, shop scores — are
    absent rather than null-because-the-listing-is-silent.

    What a hit *does* carry is used, whatever it is: OTAPI's 1688 search
    returns the wholesale ladder on about half its hits, and a ladder read for
    free from the search page lets the cheap tier quote orders too.
    """
    config = config or SourceConfig(base_url="")
    hints = config.hints or {}
    platform = platform if platform in _URL_TEMPLATES else "taobao"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def field(name: str) -> Any:
        path = config.search_field_map.get(name)
        return dig(entry, path) if path else None

    wire_id = _first_str(field("item_id"))
    item_id = config.from_wire_id(wire_id) if wire_id else None
    title_cn, title_en = _split_titles(
        _first_str(field("title_cn")), _first_str(field("title_en"))
    )
    if not item_id or not (title_cn or title_en):
        return Product(
            item_id=item_id or "",
            platform=platform,  # type: ignore[arg-type]
            url=_URL_TEMPLATES[platform].format(item_id=item_id or ""),
            status="unparsable",
            status_detail="search hit carried no id or title",
            fetched_at=now,
        )

    price = units.parse_price(_scalar(field("price")))
    stock_raw = _scalar(field("stock"))
    stock_value = (
        float(stock_raw) if isinstance(stock_raw, (int, float))
        else units.parse_cn_number(stock_raw)
    )
    shop_id = _first_str(field("shop_id"))
    product = Product(
        item_id=item_id,
        platform=platform,  # type: ignore[arg-type]
        url=_URL_TEMPLATES[platform].format(item_id=item_id),
        status="listed",
        title_cn=title_cn,
        title_en=title_en,
        price=price,
        original_price=units.parse_price(_scalar(field("original_price"))),
        sales=_sales(field("sales"), hints.get("sales")),
        price_tiers=_normalize_tiers(field("tiers"), hints.get("tiers")),
        stock=(
            Stock(
                value=int(stock_value),
                is_approximate=bool((hints.get("stock") or {}).get("approximate", True)),
                source_text_cn=stock_raw if isinstance(stock_raw, str) else None,
            )
            if stock_value is not None
            else None
        ),
        shop_name_cn=_first_str(field("shop_name_cn")),
        shop_id=config.from_wire_id(shop_id) if shop_id else None,
        images=_normalize_images(field("image")),
        fetched_at=now,
    )
    product.discount_pct = _discount_pct(product.price, product.original_price)
    if quantity is not None and quantity > 0:
        product.quote = _build_quote(product, quantity)
    return product
