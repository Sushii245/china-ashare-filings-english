# 1688 Wholesale Products — Tiered Prices & Order Quotes

**Price the order you intend to place, not the headline on the listing.**

On 1688 the price a listing leads with is the **cheapest** rung of its
wholesale ladder — the price at the largest order. A real T-shirt listing
heads at ¥3.14; its ladder starts at 2 pieces for ¥4.30. A scraper that hands
you the headline, or the ladder as text, leaves the arithmetic to you. This
Actor does it: tell it how many pieces you want and each record
comes back with the unit price at that quantity, the subtotal, and whether you
clear the seller's minimum.

---

## The quote

Set **Order quantity to price**. The same real listing at 50 pieces:

```json
"price":            { "value": 3.14, "currency": "CNY" },
"originalPrice":    { "value": 3.3,  "currency": "CNY" },
"minOrderQuantity": { "value": 2 },
"priceTiers": [
  { "minQty": 2,     "maxQty": 99,   "unitPrice": { "value": 4.3 } },
  { "minQty": 100,   "maxQty": 9999, "unitPrice": { "value": 4.0 } },
  { "minQty": 10000, "maxQty": null, "unitPrice": { "value": 3.3 } }
],
"quote": {
  "quantity": 50,
  "unitPrice": { "value": 4.3,   "currency": "CNY" },
  "subtotal":  { "value": 215.0, "currency": "CNY" },
  "meetsMinimumOrder": true
}
```

Priced off the headline, those 50 pieces would have come to ¥157 — 27% short.

- The ladder is sorted, with the top rung left open (`maxQty: null`). Where a
  rung gives only its starting quantity, it ends one below the next rung.
- **Below the seller's minimum** the order is priced at the first rung and
  flagged `meetsMinimumOrder: false` — you still want the number.
- **Between rungs**, the rung below applies: a rung's price holds until the
  next one begins.
- **Overlapping rungs** quote the dearer one. Nothing says which the seller
  means, and understating an order is the harmful way to be wrong.
- **No ladder at all**: the listing price. A listing whose variants are
  priced apart is quoted as a range — `unitPrice` and `subtotal` each carry
  `value` (the cheapest variant) and `valueMax` (the dearest) — rather than
  quietly the low end.
- **A promotion** shows as `price` with the regular price in `originalPrice`
  and the difference in `discountPct`. The quote walks the published ladder,
  which is priced at the regular rate.

## Sales figures that say what they measure

A sales number is only comparable to another if they cover the same window and
count the same thing. Each record says both:

```json
"sales": {
  "count": 412,
  "isApproximate": false,
  "window": "last_30_days",
  "unit": null
}
```

1688 listings carry several "sales" figures that measure different things —
on one real listing, 8 in the last 30 days and 14.6 million cumulative. This
Actor returns the 30-day figure and labels it as such. `window` is `monthly`,
`last_30_days` or `cumulative`; `unit` is `orders`, `buyers` or `items` where
the data says which, and `null` where it does not — as here, rather than a
guess. Where a figure is printed as text with a floor — `已售2万+` — `count` is
the floor, `isApproximate` is true, and `2万` is read as 20,000.

## Chinese and English, never mixed

`titleCn` is the seller's own title. `titleEn` is an English rendering where
one is available. A field named for Chinese never carries English: if the only
title available is a translation, `titleCn` is `null` and the translation is in
`titleEn`.

## What else a full record carries

- **The supplier**: its registered company name (`shopNameCn`) — not the
  login name a listing sometimes shows — and its id.
- **Three seller scores** out of 5: `description_match`, `service`,
  `logistics`. The data source names them in English only, so `aspectCn` is
  `null` rather than an invented Chinese label.
- **Variants** with their axes in the seller's own Chinese
  (`{"颜色": "白色", "尺码": "S"}`), each with a price and stock.
- **Attributes** — material, style, target markets — in the seller's Chinese.
  A property listed several times keeps every value: `"主要销售地区": "欧洲、南美、东南亚、…"`.

Real records are in [`samples/`](samples/), generated from real data-source
responses.

## Two detail levels

**Full detail** fetches each listing: the ladder, the minimum order, the quote,
variants, images, seller. One data-source call per listing.

**Listing level** sells the search results themselves — title, price, sales,
supplier, image — which is what you need to shortlist a hundred candidates.
One call returns a whole page of up to 50, so it costs a fraction. Set
`detailLevel` to `listing` and give a `keyword`. About half of 1688's search
results include the wholesale ladder too, and for those a listing-level record
carries it and quotes your quantity.

The usual shape of the work is both: shortlist wide and cheap at listing level,
then re-run the handful you care about at full detail. Listing-level records
carry `status: "listed"` rather than `"ok"`, and the fields a search result
does not carry — variants, attributes, seller scores, the minimum order — come
back empty, not null, so absent-from-search stays distinguishable from
the-listing-was-silent. Without a stated minimum, a ladder's first rung stands
in for it in `meetsMinimumOrder`.

---

## Input

| Field | Type | Description |
|---|---|---|
| `itemIds` | array | 1688 offer ids. A pasted listing URL works too. |
| `itemUrls` | array | 1688 listing URLs; the offer id is read out of each. |
| `keyword` | string | Search 1688, in Chinese or English. Required for listing level. |
| `detailLevel` | enum | `full` fetches each listing; `listing` sells the search results, far cheaper. |
| `quantity` | integer | Price every listing at this many pieces. |
| `maxProducts` | integer | Hard cap. Each listing returned with data is one billable event. |
| `includeSkus` | boolean | Off keeps records small. `skuCount` still says how many variants there were. |
| `concurrency` | integer | Listings fetched in parallel, 1–10. |

### Example

```json
{
  "keyword": "T恤 批发",
  "quantity": 500,
  "maxProducts": 20
}
```

---

## Pricing

Pay per event — you pay for records actually returned, plus a small fee per
run.

| Event | Price | When it fires |
|---|---|---|
| `product-normalised` | $0.012 ($12 / 1,000) | A listing returned at full detail. |
| `product-listed` | $0.002 ($2 / 1,000) | A record built from a search result, at listing level. |
| `apify-actor-start` | $0.01 | Once per run, per GB of memory (at least once) — one charge at the default 512 MB. |

Platform usage is included; there is no separate compute charge.

**You are not charged** for an id that does not exist, that the data source
refused, or whose data could not be read. Those come back with a `status`
saying which, at no cost. An entry in the ids field with no id in it is skipped
before any call is made. Set a max total charge on the run and the Actor stops
cleanly at the limit.

## Limits — read before you buy

- **1688 only, for now.** Taobao and Tmall are planned. Asking for them today
  is refused before any work is done rather than returning empty records.
- **`sourceTextCn` is often `null`, and that is deliberate.** The field holds
  the Chinese text a value was read from, where there was text. Most figures
  here arrive as typed numbers rather than listing text, and for those there is
  nothing to quote verbatim — so the field is `null` rather than a number
  dressed up as a citation.
- **`titleEn` is the data source's rendering**, not this Actor's translation,
  and may be absent.
- **`stock` is flagged `isApproximate`** unless the data source is known to
  report an exact count. Treat it as an order of magnitude.
- **Price on request (面议) comes back as a null price**, never as zero, so a
  quote can never read as free.
- **A quote is arithmetic on published prices.** It does not include shipping,
  which most 1688 listings settle per order with the seller.
- **Runs on a free Apify plan are limited** to 3 listings at full detail, or
  20 records at listing level. Enough to see what the output looks like; a
  paid Apify plan lifts the limit.
- **A run whose listings mostly come back unavailable stops early** — after
  ten unavailable listings making up more than half of those tried, or twenty
  in a row — and says so in its status. Unavailable listings are never
  billed, so this protects your run time, and usually means the ids are wrong
  or the data source is having a bad moment.
- **Listings are read as published.** The listing remains the authoritative
  source, which is why every record links to it.

## Data source and compliance

Public product and supplier listing data only: titles, prices and ladders,
minimum orders, variants, images, supplier name. **No buyer identities, no
reviewer names, no contact details, no personal data.** Data is obtained
through a commercial product-data API, not by circumventing the platform's
access controls. Requests are rate limited.

---

*Keywords: 1688 scraper, 1688 API, 1688 wholesale price, Alibaba 1688 product
data, 1688 tiered pricing, 阶梯价, 起订量, China wholesale sourcing data, 1688
supplier data, 1688 MOQ, bulk order quote.*

---

Running it locally or contributing? See [DESIGN.md](DESIGN.md) for how the data
path is structured, and [VALIDATION.md](VALIDATION.md) for how the field maps
are checked against the live data source.
