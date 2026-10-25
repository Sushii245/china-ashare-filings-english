# Changelog

## 0.1 — unreleased

First build. Not published yet: the data source's field maps are written from
a real response first (VALIDATION.md).

- The picture for an image search can be uploaded in the input form, not only
  pasted as a link, and either way the address is read once — free, on the
  image's own host — before any billed search: a link that 404s, sits behind a
  login or serves a web page fails the run with that reason, where before it
  would have been a paid search that matched nothing.
- Search by picture. `imageUrl` takes the web address of a product photo and
  the data source matches it visually, instead of matching a keyword; both
  detail levels, the quote, the minimum-order filter and the per-record price
  are the same either way, at one call per page of results. The picture is the
  only condition sent: pairing it with a keyword would turn a data source that
  ignored the image into a page of plausible, billable, unrelated hits. An
  image URL the data source could not fetch — a local path, a `data:` URI, a
  bare host — and a data source that serves words only are both refused before
  a call is spent, and an image given with a keyword searches the image and
  says so in the log.
- `maxMinOrderQuantity` keeps only listings whose minimum order is at most
  the number given. With a keyword, search hits whose ladder already starts
  above it are dropped before any detail call; ids given directly are checked
  once fetched. Left-out listings are never billed and do not count towards
  `maxProducts`. Screening pages for it stops after five pages, one on a free
  plan, since every page is a call.
- The README leads with what the Store's 1688 Issues show buyers want most:
  runs that do not stall on captchas or risk-control blocks.
- Priced to the market before publishing: full detail $0.005 (was $0.012),
  run start $0.005 (was $0.01), listing level unchanged at $0.002. Renamed
  "1688 Scraper — Wholesale Prices, MOQ & Order Quotes", the term the category
  is searched by.
- A listing whose variants are priced apart, with no ladder, is quoted as a
  range — cheapest variant to dearest, on both the unit price and the
  subtotal. It used to be quoted as null, which on the first live run left two
  of three listings with no quote at all.
- Off promotion, `originalPrice` spans the same variants as `price`. It was
  the single listing figure, which read as one price beside a range.

- 1688 listings as typed records, through the OTAPI 1688 data source on
  RapidAPI. Input by offer id, by listing URL, or by search keyword. Taobao and
  Tmall are planned through a second data source; asking for them today is
  refused before any work is done.
- Everything specific to a data source — paths, auth style, paging, id format,
  error reporting, where search hits live, field maps — travels with the
  source, and every stage reads it from there.
- 1688 ids are written `abb-…` by the data source. The prefix is added to each
  request and stripped from everything returned, so records, URLs and inputs
  only ever carry the platform's own offer id.
- A listing is never mistaken for a platform refusal. Refusal markers used to
  be matched against the whole response, and 滑块 is also a large 1688
  category, so linear-guide slider listings came back as refused and empty.
- Typed wholesale ladders are read, including rungs that give only a starting
  quantity, each closed by the next rung's start. Values in name/value lists
  are addressed by name. Facts a typed value cannot state — a sales figure's
  window, whether stock is exact — come from the data source's hints.
- The quote never falls back to the headline price while a ladder exists. On
  1688 the headline is the cheapest rung — the price at the largest order —
  so quoting a 2-piece order off it understated a real listing by 30% (4.3 元
  a piece quoted as 3.3). Below the first rung the first rung's price applies
  and the order is flagged as under the minimum; in a gap, the rung below.
- Listing-level records carry the wholesale ladder, stock and a quote when the
  search result includes them, which on 1688 is about half the time.
- A search page the data source did not fill ends the search, saving a billed
  call per run for an empty next page. Only applied where this Actor sets the
  page size itself.
- A field path may name alternatives (`a|b`), used for a promotion price that
  falls back to the regular one.
- Full-detail records are read from the data source's real item responses:
  minimum order, ladder, promotion, the registered company as the shop, three
  typed seller scores, variants with their Chinese axes, and the seller's
  attributes with variant axes left out and repeated properties kept whole.
- A promotion price is no longer mixed with the regular variant prices into a
  false price span.
- Runs started on a free Apify plan are capped at 3 listings, or 20
  listing-level records (one search call), since those runs earn nothing
  while their data-source calls still cost.
- A run stops early when its listings mostly come back unavailable: after ten
  failures making up more than half of those tried, or twenty in a row.
- A rejected API key (HTTP 401/403) fails the run at once with the data
  source's own reason. It used to be retried per listing, each listing then
  read as "unavailable", and the run reported success — blaming the ids for an
  expired key. Other client errors (a wrong path, 404) are no longer retried
  either; rate limits and server errors still are.
- A translated title that keeps a brand in Chinese stays in `titleEn`; only a
  mostly-Chinese title is moved to `titleCn`.
- A search the data source fails to answer no longer crashes the run with a
  traceback. With nothing in hand the run fails with the data source's reason;
  with records already delivered, or ids given alongside the keyword, it keeps
  those and says in its status that the search stopped.
- A zero is not a price: 1688 shows 面议 (price on request) as zero, and a
  zero quoted as an order cost would read as free. A zero listing price is
  null, a zero rung is dropped, and a zero promotion falls back to the regular
  price.
- `titleCn` and `titleEn` are kept apart, and a title with no Chinese in it is
  never put in `titleCn`.
- `sourceTextCn` is filled where a value was read from listing text and null
  where it arrived as a typed number, including a number that merely arrived
  as a string.
- Prices parsed as spans, not strings: a listing showing `128.00-298.00`
  returns `value` 128 and `valueMax` 298, with the original text kept.
- Sales figures carry their meaning. `月销1000+` and `已售2万+` are both
  "sales" on the page and are not the same measurement, so each record states
  the count, whether it is a floor, the window (monthly, last 30 days,
  cumulative) and what is counted (orders, buyers, items).
- 阶梯价 wholesale ladders as a sorted list of `{minQty, maxQty, unitPrice}`,
  with the top step left open-ended. Minimum order quantity typed separately.
- A second, much cheaper detail level. `detailLevel: "listing"` sells the
  search response itself — title, price, sales with its window and unit,
  seller, image, discount — billing `product-listed` instead of
  `product-normalised`. One provider call returns a whole page, where full
  detail is one call per listing. Those records carry `status: "listed"`, and
  the fields a search hit cannot carry come back empty rather than null, so
  absent-from-this-endpoint stays distinguishable from the-listing-was-silent.
- An empty search result page is read as empty rather than as a missing field,
  which used to yield one garbage hit per page — and in listing mode a garbage
  hit looks like progress, so pagination carried on spending calls.
- Optional `quantity` input produces a `quote` per listing: the unit price
  resolved against the 阶梯价 ladder, the subtotal, which tier it landed on,
  and whether the quantity clears the seller's minimum. A span-priced listing
  is quoted as the span rather than silently at the low end.
- A listing URL pasted into the ids field has its id read out of it, and an
  entry with no id in it is skipped with a warning. Both used to be sent to
  the provider verbatim, at one billed call each for a guaranteed miss.
- An unsupported `platform` fails the run instead of being relabelled
  `taobao`, which produced records carrying a Taobao URL for listings that
  were not on Taobao.
- An overlapping wholesale ladder quotes the dearest matching rung rather than
  the first one found, so a quote never understates what an order costs.
- The listing price counts as a candidate when deriving a price span, so a
  listing at 9.90 with one variant at 19.90 reports the span.
- A negative figure is refused rather than returned as its magnitude: `-5`
  used to parse as `5.0`, because the digit scan dropped the sign.
- Listings are fetched in parallel, 5 at a time by default, which takes
  throughput from about 60 a minute to about 280 against a 300 ms provider
  round-trip. Request spacing is enforced across all workers rather than per
  worker, so raising `concurrency` cannot multiply the request rate past the
  provider's limit. Records are still written, and billed, in the order asked.
- `discountPct` against the struck-through price, and `skuCount` recorded
  before the SKU array can be dropped, so turning `includeSkus` off still
  tells you the listing has variants.
- Shipping terms distinguish 包邮 (seller pays), a stated fee, and 到付 (the
  courier collects, so neither applies).
- The three Taobao seller scores kept apart, each with a signed peer delta:
  高于同行20.15% is +20.15, 低于同行3.2% is -3.2, 持平 is 0.
- SKU matrix decoded from Taobao's packed `pid:vid:axis:value` string into
  `{axis: value}` per variant, with its own price and stock.
- 万 / 亿 / 千 converted in Decimal, not float: `2.3亿` is 230000000, not
  229999999.99999997.
- Fullwidth input folded through NFKC before parsing, so `１２８．００`,
  `￥`, `～` and a fullwidth comma read the same as their halfwidth forms.
- `stock` is an object, not an integer: the platform publishes a rounded
  figure (the provider docs label it 库存(没有精确，是模糊值)), so the field
  carries `isApproximate` and its source text rather than implying a count.
- A price span whose `sourceTextCn` is null was derived from the listing's own
  SKU prices rather than printed as a range by the listing.
- A listing upstream refused, that does not exist, or whose payload does not
  fit the configured field map is returned with a `status` explaining which,
  and is not billed.
