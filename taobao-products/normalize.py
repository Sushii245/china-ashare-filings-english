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
from models import Money, Product, PriceTier, ShopScore, Sku, Status, Stock
from source import RawProduct, SourceConfig, dig

#: 1688 item ids live under a different host than Taobao's.
_URL_TEMPLATES = {
    "taobao": "https://item.taobao.com/item.htm?id={item_id}",
    "tmall": "https://detail.tmall.com/item.htm?id={item_id}",
    "1688": "https://detail.1688.com/offer/{item_id}.html",
}


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


def _normalize_skus(raw: Any) -> list[Sku]:
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
        url = _first_str(entry)
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http"):
            images.append(url)
    return images


def _normalize_attributes(raw: Any) -> dict[str, str]:
    """Flatten a property list into ``{axis: value}``.

    Providers send this as ``{"1627207:28320": "颜色分类:黑色"}``, as
    ``[{"name": …, "value": …}]``, or as a plain ``{axis: value}`` map.
    """
    attributes: dict[str, str] = {}
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


def _normalize_shop_scores(raw: Any) -> list[ShopScore]:
    """Build the three seller scores from whatever shape arrived."""
    scores: list[ShopScore] = []
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


def _normalize_tiers(raw: Any) -> list[PriceTier]:
    """Build a 阶梯价 ladder, sorted by the quantity it starts at."""
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


def _platform_for(raw: RawProduct, title: str | None) -> str:
    if raw.platform in _URL_TEMPLATES:
        return raw.platform
    return "taobao"


def normalize(
    raw: RawProduct,
    *,
    config: SourceConfig | None = None,
) -> Product:
    """Turn one provider payload into the record the buyer receives.

    A payload that is missing or that upstream refused still produces a record,
    carrying ``status`` and ``statusDetail``. Those are not charged.
    """
    field_map = (config or SourceConfig(base_url="")).field_map
    platform = _platform_for(raw, None)
    url = _URL_TEMPLATES[platform].format(item_id=raw.item_id)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    if raw.payload is None or raw.status != "ok":
        return Product(
            item_id=raw.item_id,
            platform=platform,  # type: ignore[arg-type]
            url=url,
            status=raw.status if raw.status != "ok" else "unparsable",  # type: ignore[arg-type]
            status_detail=raw.status_detail or "no payload returned",
            fetched_at=now,
        )

    def field(name: str) -> Any:
        path = field_map.get(name)
        return dig(raw.payload, path) if path else None

    title_cn = _first_str(field("title_cn"))
    if not title_cn:
        return Product(
            item_id=raw.item_id,
            platform=platform,  # type: ignore[arg-type]
            url=url,
            status="unparsable",
            status_detail="payload carried no title; field map may not match this provider",
            fetched_at=now,
        )

    # A range field, when the provider sends one, is more informative than the
    # single price, which is usually just the low end of the same span.
    skus = _normalize_skus(field("skus"))

    # A provider that states the span outright is best. Most do not — the
    # OneBound docs define no price_range field — so the span is taken from the
    # SKU prices instead. It carries no source_text_cn, because the listing
    # never printed it as a range: a null there means "derived, not quoted".
    price = units.parse_price(_scalar(field("price_range")))
    if price is None:
        price = units.parse_price(_scalar(field("price")))
        sku_prices = [s.price.value for s in skus if s.price]
        if price and len(sku_prices) > 1 and min(sku_prices) != max(sku_prices):
            price = Money(value=min(sku_prices), value_max=max(sku_prices))

    stock_text = _first_str(field("stock"))
    stock_value = units.parse_cn_number(stock_text)
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

    return Product(
        item_id=str(_first_str(field("item_id")) or raw.item_id),
        platform=platform,  # type: ignore[arg-type]
        url=url,
        status="ok",
        title_cn=title_cn,
        price=price,
        original_price=units.parse_price(_scalar(field("original_price"))),
        price_tiers=_normalize_tiers(field("tiers")),
        min_order_quantity=units.parse_quantity(_scalar(field("min_order_quantity"))),
        sales=units.parse_sales(_first_str(field("sales"))),
        stock=(
            Stock(
                value=int(stock_value),
                # Taobao publishes a rounded figure, not a count.
                is_approximate=True,
                source_text_cn=stock_text,
            )
            if stock_value is not None
            else None
        ),
        shipping=shipping,
        is_presale=is_presale,
        presale_ships_within_days=units.parse_presale_days(presale_text),
        shop_name_cn=_first_str(field("shop_name_cn")),
        shop_id=_first_str(field("shop_id")),
        shop_scores=_normalize_shop_scores(field("shop_scores")),
        skus=skus,
        images=_normalize_images(field("images")),
        attributes_cn=_normalize_attributes(field("properties")),
        fetched_at=now,
    )
