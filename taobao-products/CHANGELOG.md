# Changelog

## 0.1 — unreleased

First build. Not published yet: the acquisition layer needs a product-data
provider configured (see DESIGN.md for why there is no login-free path).

- Taobao, Tmall and 1688 listings as typed records. Input by item id, by
  listing URL, or by search keyword.
- Prices parsed as spans, not strings: a listing showing `128.00-298.00`
  returns `value` 128 and `valueMax` 298, with the original text kept.
- Sales figures carry their meaning. `月销1000+` and `已售2万+` are both
  "sales" on the page and are not the same measurement, so each record states
  the count, whether it is a floor, the window (monthly, last 30 days,
  cumulative) and what is counted (orders, buyers, items).
- 阶梯价 wholesale ladders as a sorted list of `{minQty, maxQty, unitPrice}`,
  with the top step left open-ended. Minimum order quantity typed separately.
- Optional `quantity` input produces a `quote` per listing: the unit price
  resolved against the 阶梯价 ladder, the subtotal, which tier it landed on,
  and whether the quantity clears the seller's minimum. A span-priced listing
  reports a null unit price rather than silently using the low end.
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
