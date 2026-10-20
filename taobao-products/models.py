"""Typed records returned by this Actor.

The output contract lives here, and nowhere else. Two rules it follows
everywhere:

* A field the source does not state comes back ``None``. Nothing is guessed.
* A normalised value keeps the Chinese string it was derived from, in a
  ``source_text_cn`` sibling, so a buyer who cannot read Chinese can still hand
  the record to someone who can.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Literal

#: What a sales figure counts. Taobao and 1688 are not consistent about this and
#: the distinction changes what the number means.
SalesUnit = Literal["orders", "buyers", "items"]

#: The window a sales figure covers. ``cumulative`` is since the listing opened.
SalesWindow = Literal["monthly", "last_30_days", "cumulative"]

#: Why a product has no data. Every one of these is returned to the buyer
#: instead of failing the run, and none of them is charged.
Status = Literal[
    "ok",
    #: Built from a search hit rather than a listing record: the fields a
    #: search response cannot carry are absent, not null.
    "listed",
    "not_found",
    "risk_control_blocked",
    "upstream_error",
    "unparsable",
]


@dataclasses.dataclass(slots=True)
class Money:
    """One price point, normalised to CNY.

    ``value`` is yuan as a float. Listings that show a span across SKUs set
    ``value_max`` as well, and ``value`` is then the low end.
    """

    value: float
    value_max: float | None = None
    currency: str = "CNY"
    source_text_cn: str | None = None

    @property
    def is_range(self) -> bool:
        return self.value_max is not None and self.value_max != self.value


@dataclasses.dataclass(slots=True)
class Sales:
    """A sales figure with its meaning attached.

    ``月销1000+`` and ``已售2万+`` are both "sales" on the page and mean
    different things. Returning ``count`` alone is how a buyer ends up sorting
    a monthly floor against a lifetime total.
    """

    count: int
    is_approximate: bool
    window: SalesWindow | None = None
    unit: SalesUnit | None = None
    source_text_cn: str | None = None


@dataclasses.dataclass(slots=True)
class Stock:
    """An inventory level, with whether the source means it exactly.

    Providers reading Taobao return a deliberately fuzzy number here — the
    OneBound docs label the field 库存(没有精确，是模糊值) — so handing it over
    as a bare integer claims a precision the platform never gave. The whole
    point of this Actor is not doing that.
    """

    value: int
    is_approximate: bool = False
    source_text_cn: str | None = None


@dataclasses.dataclass(slots=True)
class Quantity:
    """A piece count, e.g. a minimum order quantity or a stock level."""

    value: int
    unit_cn: str | None = None
    source_text_cn: str | None = None


@dataclasses.dataclass(slots=True)
class PriceTier:
    """One step of a 阶梯价 wholesale ladder.

    ``max_qty`` is ``None`` for the open-ended top step (``≥1000件``).
    """

    min_qty: int
    max_qty: int | None
    unit_price: Money
    source_text_cn: str | None = None


@dataclasses.dataclass(slots=True)
class Quote:
    """What this listing costs at a stated order quantity.

    The question a sourcing buyer actually has is not "what is the price" but
    "what does 500 pieces cost me". On 1688 that means walking the 阶梯价
    ladder; the headline price is only the first step. Nothing else in this
    category answers it, so the Actor does the walk and shows which step it
    landed on.

    When the listing gives a span across variants and no ladder, ``unit_price``
    and ``subtotal`` are that span: the cost depends on which variants are
    ordered, so both ends are shown rather than the low one alone.
    """

    quantity: int
    unit_price: Money | None = None
    subtotal: Money | None = None
    tier_source_text_cn: str | None = None
    meets_minimum_order: bool | None = None


@dataclasses.dataclass(slots=True)
class Shipping:
    """Delivery terms.

    ``included`` true means the seller pays (包邮). ``fee`` is the buyer's cost
    when stated. ``cash_on_delivery`` covers 到付, where neither applies.
    """

    included: bool | None = None
    fee: Money | None = None
    cash_on_delivery: bool = False
    ships_from_cn: str | None = None
    source_text_cn: str | None = None


@dataclasses.dataclass(slots=True)
class ShopScore:
    """One of Taobao's three seller scores, with its peer comparison.

    The page shows these as 宝贝描述 / 卖家服务 / 物流服务, each with a score out
    of 5 and a "高于同行 20.15%" style delta. The delta is signed: 低于 is
    negative.
    """

    aspect: str
    #: The label the listing prints, when the source gives one. A provider
    #: that only names the score in English leaves this null rather than
    #: having a Chinese label invented for it.
    aspect_cn: str | None
    score: float
    vs_peers_pct: float | None = None
    source_text_cn: str | None = None


@dataclasses.dataclass(slots=True)
class Sku:
    """One concrete purchasable variant.

    ``options_cn`` is the raw axis map as the listing states it, e.g.
    ``{"颜色分类": "黑色", "尺码": "XL"}``. ``options_en`` is filled only when a
    translation layer runs; it is ``None`` otherwise rather than transliterated.
    """

    sku_id: str | None
    options_cn: dict[str, str]
    options_en: dict[str, str] | None = None
    price: Money | None = None
    stock: int | None = None


@dataclasses.dataclass(slots=True)
class Product:
    """One Taobao / Tmall / 1688 listing."""

    item_id: str
    platform: Literal["taobao", "tmall", "1688"]
    url: str
    status: Status = "ok"
    status_detail: str | None = None

    title_cn: str | None = None
    title_en: str | None = None

    price: Money | None = None
    original_price: Money | None = None
    price_tiers: list[PriceTier] = dataclasses.field(default_factory=list)
    min_order_quantity: Quantity | None = None

    sales: Sales | None = None
    stock: Stock | None = None

    shipping: Shipping | None = None
    quote: Quote | None = None
    discount_pct: float | None = None
    is_presale: bool | None = None
    presale_ships_within_days: int | None = None

    shop_name_cn: str | None = None
    shop_id: str | None = None
    shop_scores: list[ShopScore] = dataclasses.field(default_factory=list)

    skus: list[Sku] = dataclasses.field(default_factory=list)
    #: How many variants the listing had, recorded before the array can be
    #: dropped. With includeSkus off this is the only thing left that says the
    #: listing has variants at all, so it must not be derived from `skus`.
    sku_count: int | None = None
    images: list[str] = dataclasses.field(default_factory=list)
    attributes_cn: dict[str, str] = dataclasses.field(default_factory=dict)

    fetched_at: str | None = None

    def to_item(self) -> dict[str, Any]:
        """Flatten to the camelCase dataset item the buyer sees."""
        return {
            "itemId": self.item_id,
            "platform": self.platform,
            "url": self.url,
            "status": self.status,
            "statusDetail": self.status_detail,
            "titleCn": self.title_cn,
            "titleEn": self.title_en,
            "price": _money(self.price),
            "originalPrice": _money(self.original_price),
            "priceTiers": [
                {
                    "minQty": t.min_qty,
                    "maxQty": t.max_qty,
                    "unitPrice": _money(t.unit_price),
                    "sourceTextCn": t.source_text_cn,
                }
                for t in self.price_tiers
            ],
            "minOrderQuantity": (
                {
                    "value": self.min_order_quantity.value,
                    "unitCn": self.min_order_quantity.unit_cn,
                    "sourceTextCn": self.min_order_quantity.source_text_cn,
                }
                if self.min_order_quantity
                else None
            ),
            "sales": (
                {
                    "count": self.sales.count,
                    "isApproximate": self.sales.is_approximate,
                    "window": self.sales.window,
                    "unit": self.sales.unit,
                    "sourceTextCn": self.sales.source_text_cn,
                }
                if self.sales
                else None
            ),
            "stock": (
                {
                    "value": self.stock.value,
                    "isApproximate": self.stock.is_approximate,
                    "sourceTextCn": self.stock.source_text_cn,
                }
                if self.stock
                else None
            ),
            "shipping": (
                {
                    "included": self.shipping.included,
                    "fee": _money(self.shipping.fee),
                    "cashOnDelivery": self.shipping.cash_on_delivery,
                    "shipsFromCn": self.shipping.ships_from_cn,
                    "sourceTextCn": self.shipping.source_text_cn,
                }
                if self.shipping
                else None
            ),
            "quote": (
                {
                    "quantity": self.quote.quantity,
                    "unitPrice": _money(self.quote.unit_price),
                    "subtotal": _money(self.quote.subtotal),
                    "tierSourceTextCn": self.quote.tier_source_text_cn,
                    "meetsMinimumOrder": self.quote.meets_minimum_order,
                }
                if self.quote
                else None
            ),
            "discountPct": self.discount_pct,
            "skuCount": self.sku_count,
            "isPresale": self.is_presale,
            "presaleShipsWithinDays": self.presale_ships_within_days,
            "shopNameCn": self.shop_name_cn,
            "shopId": self.shop_id,
            "shopScores": [
                {
                    "aspect": s.aspect,
                    "aspectCn": s.aspect_cn,
                    "score": s.score,
                    "vsPeersPct": s.vs_peers_pct,
                    "sourceTextCn": s.source_text_cn,
                }
                for s in self.shop_scores
            ],
            "skus": [
                {
                    "skuId": s.sku_id,
                    "optionsCn": s.options_cn,
                    "optionsEn": s.options_en,
                    "price": _money(s.price),
                    "stock": s.stock,
                }
                for s in self.skus
            ],
            "images": self.images,
            "attributesCn": self.attributes_cn,
            "fetchedAt": self.fetched_at,
        }


def _money(m: Money | None) -> dict[str, Any] | None:
    if m is None:
        return None
    return {
        "value": m.value,
        "valueMax": m.value_max,
        "currency": m.currency,
        "sourceTextCn": m.source_text_cn,
    }
