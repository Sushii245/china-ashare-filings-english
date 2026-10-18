# Taobao / Tmall product data — design

## What this sells

Not "another Taobao scraper". The store already has 26, four of them above
1,700 monthly users, and the cheapest is at $0.0015 per product. Competing on
"same fields, more reliable" is a losing position once upstream access has to
be paid for: at $0.003 per product the margin after Apify's 20% and the
upstream call is somewhere around zero.

What nobody in that list returns is **Chinese commerce semantics resolved into
typed English fields**. Concretely, every competitor hands back one of these:

    "sales": "月销1000+"        # a string the buyer has to parse
    "sales": 1000               # wrong: drops the window and the "+"

Those are two different metrics. `月销 1000+` is a 30-day floor; `已售 2万+` is
a cumulative floor of 20,000. A sourcing analyst sorting by "best selling"
across both gets garbage. This Actor returns:

    "sales": {
      "count": 1000,            # the floor, as an integer
      "isApproximate": true,    # the "+" was present
      "window": "monthly",      # monthly | cumulative | last_30_days | null
      "unit": "orders",         # orders | buyers | items
      "sourceTextCn": "月销1000+"
    }

The same treatment for prices and ranges, 阶梯价 tiers, 起订量, shipping terms,
the three separate 店铺评分 scores, SKU matrices, and 预售 vs 现货. Each
normalised field keeps the Chinese string it came from, so the buyer can audit
it without reading Chinese — the same contract as the A-share Actor.

Target price: **$0.012 per product.** Above zen-studio's $0.010, far below
sian.agency's $0.036 (which has 1,354 lifetime users, so the top of the range
is real).

## Data path, and why it is pluggable

Probed on 2026-10-18 from a residential IP, with no authentication:

| Entry point | Result |
|---|---|
| `item.taobao.com/item.htm?id=…` | 5 KB JS redirect shell, no product data |
| `h5api.m.taobao.com/h5/mtop.taobao.pcdetail.data.get/1.0/` | `RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!` plus a login redirect |
| `s.taobao.com/search?q=…` | 34 KB React shell, `window.initBaxia`, "请不要禁用JS", zero product fields |
| `detail.1688.com/offer/…` | `punish:…&action=deny&…cloud_ip_bl` — explicit block page |

There is no usable login-free endpoint. Everything requires a signed,
authenticated mtop session, which in practice means handling `h5_tk`, the sign
parameter, and the slider captcha behind them. This project does not do that.

So acquisition is a protocol with adapters:

* `FixtureSource`   — replays recorded payloads. Drives the whole test suite.
* `UpstreamSource`  — one commercial product-data provider, configured by base
                      URL, credentials and a small response map. Swapping
                      provider is a config change, not a rewrite.

Three things the provider documentation changed about the default field map,
each found by reading it rather than by a test failing:

* **The success code is a string of zeros.** `error_code` is `"0000"` on
  success and `"2000"` on failure, so testing that field for truth classifies
  every good response as an error.
* **There is no `price_range` field.** A span across variants has to be derived
  from the SKU prices, and when it is derived it carries no source text —
  a null `sourceTextCn` on a range means "computed, not quoted".
* **`item.num` is documented as 库存(没有精确，是模糊值)** — a deliberately
  fuzzy stock figure. Returning it as a bare integer would claim a precision
  the platform never gave, which is the exact failure this Actor sells against.

The normalisation layer — the part that is actually the product — is fed from a
provider-neutral intermediate shape, so it is tested and correct before any
credential exists.

## Modules

    models.py      Typed records. The output contract lives here.
    units.py       Price, quantity, sales, tier and shipping parsing.
                   Fullwidth/halfwidth, 万/亿, ranges, floors. Highest bug
                   density in the project, so it owns its own test file.
    source.py      Acquisition protocol + FixtureSource + UpstreamSource.
    normalize.py   Provider payload -> typed record. No I/O.
    main.py        Local CLI, for running against fixtures or a live key.
    actor_main.py  Apify lifecycle, pay-per-event billing, clean stop on the
                   charge limit.

## Billing

Pay per event, one primary event:

    product-normalised   a product returned with normalised fields

A product that cannot be fetched or parsed is written to the dataset with a
`status` explaining why, **and is not charged** — same rule as the A-share
Actor's scanned-PDF case. `riskControlBlocked` is a status, not an exception:
the buyer sees which ids were refused upstream rather than a failed run.

## Unit economics

Priced at $0.012 per product, against OneBound's A-class pay-as-you-go rate
(0.022 元 per call, price list dated 2026-06-26, at 6.7029 CNY/USD):

    sell price                 $0.0120
    less Apify's 20%          -$0.0024
    less platform compute     -$0.00007   (512 MB, ~1.5 s per item)
    less upstream call        -$0.00328
    ------------------------------------
    margin                     $0.00625   (52%)

Pay-as-you-go is the launch choice despite a worse unit rate than the monthly
plan (0.01 元, a 52% to 67% margin improvement) because the monthly plan is a
one-year commitment at 1,500 元/month and breaks even only above ~27,800
products a month. Revisit once monthly volume is reliably past that.

Two things this model makes non-negotiable:

* **Shipping is never a second call.** `item_fee` is a B-class interface at
  0.05 元 — 2.3x the item call. Fetching it per product takes the margin to
  **-$0.00121**, i.e. every sale loses money. Shipping comes out of the item
  record or comes back null.

* **Default `maxProducts` stays low.** Free-plan users generate no revenue —
  Apify covers their platform cost but not the upstream call, which is ours.
  At 20% of volume served to free-plan users the real margin is $0.00434, not
  $0.00625.

For reference, the cheap end of the store ($3.00/1,000) nets $0.00233 per
product, which is below the pay-as-you-go call cost. Those listings only work
on the committed monthly plan, which is why they compete on price: they are
carrying a fixed cost that has to be amortised.

## Compliance boundary

* Product and shop listing data only. No buyer identities, no reviewer names,
  no contact details, no personal data.
* No captcha solving, no bot-detection evasion, no signature forgery. When
  upstream refuses, the record says so.
* Rate limited by default, and the limit is an input the buyer can raise but
  not remove.
