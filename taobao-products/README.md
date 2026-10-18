# Taobao & Tmall Products — Normalised English Fields

**Taobao, Tmall and 1688 listings as typed records, not strings you have to parse yourself.**

Every scraper in this category hands you the listing's own text and stops. You
get `"sales": "月销1000+"` and `"price": "128.00-298.00"` and the actual work —
working out what those mean — is still yours.

This Actor finishes the job. Prices come back as spans with a low and a high.
Sales figures come back with **what they measure**. Wholesale ladders come back
as a sorted list you can price an order against. And every normalised field
keeps the Chinese text it came from, so you can check it without reading
Chinese.

---

## The field that matters most

`月销1000+` and `已售2万+` are both labelled "sales" on the page. They are not
the same measurement. The first is a 30-day floor; the second is a lifetime
floor of 20,000. Sort a shortlist by "sales" across both and the ranking is
meaningless.

```json
"sales": {
  "count": 1000,
  "isApproximate": true,
  "window": "monthly",
  "unit": null,
  "sourceTextCn": "月销1000+"
}
```

`window` is `monthly`, `last_30_days` or `cumulative`. `unit` is `orders`,
`buyers` or `items` — because `100+人付款` counts **people**, not pieces, and a
sourcing model that treats them as units is wrong before it starts.

## What you get per listing

```json
{
  "itemId": "600530677383",
  "platform": "taobao",
  "titleCn": "无线蓝牙耳机 入耳式降噪 长续航 适用苹果安卓",
  "price": { "value": 128.0, "valueMax": 298.0, "currency": "CNY",
             "sourceTextCn": "128.00-298.00" },
  "originalPrice": { "value": 358.0, "sourceTextCn": "￥358.00" },
  "shipping": { "included": true, "fee": { "value": 0.0 },
                "cashOnDelivery": false, "shipsFromCn": "广东 深圳",
                "sourceTextCn": "包邮" },
  "shopScores": [
    { "aspect": "description_match", "aspectCn": "宝贝描述",
      "score": 4.8, "vsPeersPct": 20.15 },
    { "aspect": "service", "aspectCn": "卖家服务",
      "score": 4.7, "vsPeersPct": -3.2 },
    { "aspect": "logistics", "aspectCn": "物流服务",
      "score": 4.9, "vsPeersPct": 0.0 }
  ],
  "stock": { "value": 4532, "isApproximate": true, "sourceTextCn": "4532" },
  "skus": [
    { "skuId": "5001", "optionsCn": { "颜色分类": "黑色", "版本": "标准版" },
      "price": { "value": 128.0 }, "stock": 2100 }
  ],
  "status": "ok"
}
```

On 1688 you also get the wholesale ladder and the minimum order:

```json
"minOrderQuantity": { "value": 2, "unitCn": "件", "sourceTextCn": "起订量：2件" },
"priceTiers": [
  { "minQty": 2,    "maxQty": 99,   "unitPrice": { "value": 12.0 }, "sourceTextCn": "2-99件 12.00" },
  { "minQty": 100,  "maxQty": 999,  "unitPrice": { "value": 10.5 }, "sourceTextCn": "100-999件 10.50" },
  { "minQty": 1000, "maxQty": null, "unitPrice": { "value": 9.0 },  "sourceTextCn": "≥1000件 9.00" }
]
```

`maxQty: null` is the open-ended top step.

### Or just tell it how many you want

Set **Order quantity to price** and the Actor walks the ladder for you, which
is the question you actually had:

```json
"quote": {
  "quantity": 500,
  "unitPrice": { "value": 10.5, "sourceTextCn": "10.50" },
  "subtotal": { "value": 5250.0, "sourceTextCn": null },
  "tierSourceTextCn": "100-999件 10.50",
  "meetsMinimumOrder": true
}
```

`tierSourceTextCn` shows which step it landed on, so the number is checkable.
`meetsMinimumOrder` is false when your quantity is under the seller's 起订量 —
the price is still quoted, because you still want to know it. A listing priced
as a span across variants has no single unit price to resolve to, so
`unitPrice` comes back null rather than quietly using the low end.

---

## How this differs from the other Taobao scrapers

| | Typical Taobao scraper | This Actor |
|---|---|---|
| Price | `"128.00-298.00"` as a string | `value` 128, `valueMax` 298, typed |
| Sales | `"月销1000+"` as a string, or `1000` with the meaning dropped | count, floor flag, window, and what is counted |
| 万 / 亿 | Your problem | Converted in Decimal — `2.3亿` is exactly 230000000 |
| Fullwidth `１２８．００` | Breaks, or parses as something else | NFKC-folded before parsing |
| 阶梯价 | Absent, or a raw string | Sorted `{minQty, maxQty, unitPrice}` array |
| 包邮 / 到付 | One "shipping" string | Seller-pays, buyer-fee, and cash-on-delivery told apart |
| Seller scores | One number, or absent | Three aspects kept apart, each with a signed peer delta |
| SKU axes | `"1627207:28320:颜色分类:黑色"` | `{"颜色分类": "黑色"}` |
| Stock | A bare integer | Flagged `isApproximate`, because the platform publishes a rounded figure |
| "What do 500 cost?" | Work it out yourself | `quote` walks the ladder and names the step it used |
| Discount | Absent | `discountPct` against the struck-through price |
| Verification | None | Every normalised field carries its Chinese source text |

## Typical uses

- **Product sourcing** — compare landed unit cost across 1688 suppliers at your
  actual order quantity, not at the headline price.
- **Amazon and DTC product research** — rank candidates on a sales figure whose
  window you can trust.
- **Price and assortment monitoring** — track a competitor's SKU matrix and
  promotional versus list price over time.
- **Counterfeit and brand monitoring** — listing, shop and seller scores in one
  record, with the Chinese text kept for evidence.
- **Agent and LLM pipelines** — typed JSON, so a model does not have to guess
  what `已售2万+` means.

---

## Input

| Field | Type | Description |
|---|---|---|
| `itemIds` | array | Taobao / Tmall item ids or 1688 offer ids. Pasting a full listing URL here works too — the id is read out of it rather than sent upstream as-is. |
| `itemUrls` | array | Listing URLs instead; the id is read out of the URL. |
| `keyword` | string | Search the catalogue instead of naming ids. |
| `platform` | enum | `taobao`, `tmall` or `1688`. |
| `maxProducts` | integer | Hard cap. Each listing returned with data is one billable event. |
| `quantity` | integer | Price every listing at this many pieces: unit price off the ladder, subtotal, which tier, and whether it clears the MOQ. |
| `concurrency` | integer | Listings fetched in parallel, 1–10. Default 5. |
| `includeSkus` | boolean | Off keeps records small and returns the listing level only. `skuCount` still reports how many variants there were. |

### Example input

```json
{
  "itemIds": ["612345678901"],
  "platform": "1688",
  "quantity": 500,
  "maxProducts": 10,
  "includeSkus": true
}
```

---

## Two detail levels

**Full detail** fetches each listing: SKU matrix, wholesale ladder, shipping
terms, seller scores. One provider call per listing, and it bills accordingly.

**Listing level** sells the search response itself — title, price, sales line
with its window and unit, seller, image, discount — which is what you actually
need to shortlist a hundred candidates. One provider call returns a whole page
of them, so it costs a fraction. Set `detailLevel` to `listing` and give a
`keyword`.

The normal shape of the work is both: shortlist wide and cheap at listing
level, then re-run the handful you care about at full detail.

Records from the cheap path carry `status: "listed"` rather than `"ok"`, so you
can always tell which is which. The fields a search hit cannot carry —
`skus`, `priceTiers`, `shipping`, `shopScores`, `stock` — come back empty
rather than null, because absent-from-this-endpoint is not the same as
the-listing-did-not-say.

## Throughput

About **280 listings a minute** at the defaults (5 in parallel, requests spaced
0.2s apart), measured against a 300 ms provider round-trip. Raise `concurrency`
to 10 and lower the interval if your provider plan allows it; lower both if it
does not. Request spacing is enforced across all workers, not per worker, so
raising concurrency cannot overrun a rate limit on its own.

## Pricing

Pay per event — you pay for listings actually returned.

| Event | When it fires |
|---|---|
| `product-normalised` | A listing returned with full normalised fields. |
| `product-listed` | A record built from a search hit, in listing mode. Far cheaper: one provider call returns a whole page. |

**You are not charged** for a listing that does not exist, that the platform
refused, or whose payload did not fit. Those come back with a `status` saying
which, at no cost. Set a max total charge on the run and the Actor stops
cleanly at the limit instead of running past it.

## Limits — read before you buy

Stated plainly, because they affect whether this fits your workflow:

- **`titleEn` and `optionsEn` are usually `null`.** Listings do not print an
  English title, and this Actor does not invent one. Anything the listing does
  not state comes back `null` rather than guessed.
- **`面议` ("price on request") yields a null price**, not a zero and not a
  made-up number. The source text is kept.
- **`keyword` searches the catalogue's own index**, with its ranking and its
  result cap — not a full-text search of every listing.
- **Not every listing has every field.** Wholesale ladders and minimum order
  quantities are a 1688 concept; seller scores are a Taobao one, and not every
  data source publishes them — when they are absent, `shopScores` is `[]`
  rather than zeros.
- **`stock` is approximate and says so.** The platform publishes a rounded
  figure, not a count, so the field is `{value, isApproximate, sourceTextCn}`
  with `isApproximate` true. Treat it as an order of magnitude.
- **A price span with a null `sourceTextCn` was derived**, not quoted: it is
  the min and max of the listing's own SKU prices, because the listing did not
  print a range. A span with source text was printed as a range.
- **Shipping is read from the listing record**, not fetched separately. Where
  the listing does not carry it, `shipping` is `null` rather than guessed.
- **An entry with no id in it is skipped, not requested.** You get a warning
  naming it. Sending it upstream would be a billed call for a certain miss.
- **An overlapping wholesale ladder quotes the dearer matching rung.** Some
  listings publish rungs that overlap, and then nothing says which the seller
  means; understating what an order costs is the harmful way to be wrong.
  A quantity matching no rung at all falls back to the listing price with
  `tierSourceTextCn` null.
- **Listings the platform refuses are reported, not retried into oblivion.**
  A `risk_control_blocked` status means the platform declined that id for this
  request. It is not billed.
- Figures are read from the listing as published. The listing remains the
  authoritative source, which is why every record links back to it.

## Data source and compliance

Public product and shop listing data only: titles, prices, stock, shipping
terms, SKU options, shop names and the shop's own published scores. **No buyer
identities, no reviewer names, no contact details, no personal data.** Requests
are rate limited by default, and the interval can be raised but not removed.

---

*Keywords: Taobao scraper, Taobao product API, Tmall product data, 1688
wholesale scraper, China product sourcing data, Taobao product details API,
Taobao price monitoring, 1688 supplier data, Chinese e-commerce data,
阶梯价 wholesale tiers, Taobao SKU data.*

---

Running it locally or contributing? See [DESIGN.md](DESIGN.md) for why the
acquisition layer looks the way it does, and [VALIDATION.md](VALIDATION.md)
for how the field map gets checked against a live provider.
