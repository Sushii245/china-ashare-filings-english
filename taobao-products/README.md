# 1688 Wholesale Products — Tiered Prices & Order Quotes

**Price the order you intend to place, not the headline on the listing.**

On 1688 the price shown first is only the bottom rung of a wholesale ladder —
`2-99件 ¥12.00 / 100-999件 ¥10.50 / ≥1000件 ¥9.00` — and the seller has a
minimum order besides. Every other scraper hands you that headline, or the
ladder as text, and leaves the arithmetic to you. This Actor does it: tell it
how many pieces you want and each record comes back with the unit price at that
quantity, the subtotal, which rung it landed on, and whether you clear the
seller's minimum.

---

## The quote

Set **Order quantity to price**:

```json
"quote": {
  "quantity": 500,
  "unitPrice": { "value": 10.5, "currency": "CNY" },
  "subtotal": { "value": 5250.0, "currency": "CNY" },
  "meetsMinimumOrder": true
}
```

The ladder it walked is on the record too, as a sorted list with the top rung
left open:

```json
"priceTiers": [
  { "minQty": 2,    "maxQty": 99,   "unitPrice": { "value": 12.0 } },
  { "minQty": 100,  "maxQty": 999,  "unitPrice": { "value": 10.5 } },
  { "minQty": 1000, "maxQty": null, "unitPrice": { "value": 9.0 } }
],
"minOrderQuantity": { "value": 2 }
```

- Below the seller's minimum the quote is still priced, with
  `meetsMinimumOrder: false` — you still want the number.
- A ladder whose rungs overlap quotes the **dearer** matching rung. Nothing
  says which the seller means, and understating an order is the harmful way to
  be wrong.
- A quantity that falls between rungs, or a listing with no ladder, is priced
  off the listing price.
- A listing priced as a span across variants has no single unit price to
  resolve to, so `unitPrice` is `null` rather than quietly the low end.

## Sales figures that say what they measure

A sales number is only comparable to another if they cover the same window and
count the same thing. Each record says both:

```json
"sales": {
  "count": 860,
  "isApproximate": false,
  "window": "last_30_days",
  "unit": "orders"
}
```

`window` is `monthly`, `last_30_days` or `cumulative`; `unit` is `orders`,
`buyers` or `items`. Where a figure is printed as text with a floor — `已售2万+`
— `count` is the floor, `isApproximate` is true, and `2万` is read as 20,000.

## Chinese and English, never mixed

`titleCn` is the seller's own title. `titleEn` is an English rendering where
one is available. A field named for Chinese never carries English: if the only
title available is a translation, `titleCn` is `null` and the translation is in
`titleEn`.

## Two detail levels

**Full detail** fetches each listing: the ladder, the minimum order, the quote,
variants, images, seller. One data-source call per listing.

**Listing level** sells the search results themselves — title, price, sales,
image — which is what you need to shortlist a hundred candidates. One call
returns a whole page, so it costs a fraction. Set `detailLevel` to `listing`
and give a `keyword`.

The usual shape of the work is both: shortlist wide and cheap at listing level,
then re-run the handful you care about at full detail. Listing-level records
carry `status: "listed"` rather than `"ok"`, and the fields a search result
cannot carry — the ladder, variants, minimum order — come back empty, not
null, so absent-from-search stays distinguishable from the-listing-was-silent.

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

Pay per event — you pay for records actually returned.

| Event | When it fires |
|---|---|
| `product-normalised` | A listing returned at full detail. |
| `product-listed` | A record built from a search result, at listing level. |

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
- **A quote is arithmetic on published prices.** It does not include shipping,
  which most 1688 listings settle per order with the seller.
- **Listings are read as published.** The listing remains the authoritative
  source, which is why every record links to it.

## Data source and compliance

Public product and supplier listing data only: titles, prices and ladders,
minimum orders, variants, images, supplier name. **No buyer identities, no
reviewer names, no contact details, no personal data.** Data is obtained
through a licensed commercial product-data API, not by circumventing the
platform's access controls. Requests are rate limited.

---

*Keywords: 1688 scraper, 1688 API, 1688 wholesale price, Alibaba 1688 product
data, 1688 tiered pricing, 阶梯价, 起订量, China wholesale sourcing data, 1688
supplier data, 1688 MOQ, bulk order quote.*

---

Running it locally or contributing? See [DESIGN.md](DESIGN.md) for how the data
path is structured, and [VALIDATION.md](VALIDATION.md) for how the field maps
are checked against the live data source.
