# 1688 wholesale product data — design

## What this sells

Not "another 1688 scraper". What a sourcing buyer actually needs from a 1688
listing is not its headline price, which is only the bottom rung of a wholesale
ladder, but **what the order they intend to place will cost**. Every scraper in
the category hands back the headline, or the ladder as text, and leaves the
arithmetic to the buyer. This Actor walks the ladder for a stated quantity and
returns the unit price, the subtotal, the rung it landed on, and whether the
quantity clears the seller's 起订量.

Around that: sales figures labelled with the window and unit they measure
(`月销1000+` and `已售2万+` are both "sales" on a page and not the same
measurement); Chinese and English titles kept in separate fields and never
mixed; typed prices, ladders and minimum orders; and two detail levels, so a
buyer can shortlist wide and cheap before paying for full records.

### What became of the citation contract

The A-share Actor's promise was that every figure carries the Chinese sentence
it came from. That holds where a value is *read from text* — a sales line like
`已售2万+`, a ladder printed as `2-99件`. It does not hold for a data source
that hands back typed numbers, and 1688 via OTAPI is one: there is no listing
text behind `{"MinQuantity": 100}` to quote. So `sourceTextCn` is filled where
there was text and `null` where the value arrived typed, and the README says so
plainly. A number dressed up as a citation — `"sourceTextCn": "860"` beside a
30-day window the text never stated — would be worse than the null, and there
is a test against exactly that.

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

(Those three are OneBound's conventions, kept for when it is added for
Taobao.)

### When the data source says no

Failures are sorted by whether waiting helps. Rate limits (429) and server
errors (5xx) are retried with backoff. A rejected key (401/403) is raised as
`AuthenticationFailed` and ends the run with the provider's own message: it is
a fact about the whole run, and treating it per listing made every listing
read as unavailable while the run reported success. Any other 4xx fails the
call at once. A non-JSON body is never retried either.

A search that fails is handled where it is made, not left to crash the run:
with nothing in hand the run fails with the reason, and with records already
delivered or ids already given it carries on with those and records the
failure in its status. An unhandled search error used to end the run with a
traceback and exit code 91, and no status at all.

### Everything provider-specific travels with the source

A provider differs in far more than its URL: its paths, its auth style, its
paging, how it writes ids, how it reports errors in a 200 body, where a search
response keeps its hits, and what each field is called. All of it lives on
`SourceConfig`, every source carries one as `source.config`, and the
normaliser, the Actor, `capture.py` and `audit.py` all read from it. An early
version normalised every record with one hard-coded map whatever the source,
which meant a second provider's records would all have come back unparsable
without any single line looking wrong.

The `otapi-1688` preset is the current data source. Its request side is
confirmed from the provider's own samples: `BatchGetItemFullInfo` and
`BatchSearchItemsFrame` (not the playground's display names), credentials in
`x-rapidapi-*` headers, a zero-based `framePosition` offset, a required
`language`, and 1688 ids written `abb-612345678901` on the wire — added to
every request, stripped from everything returned. Its field maps are left empty
until written from a real response; see `VALIDATION.md`.

### Searching by picture

OTAPI takes its search conditions as one XML document in a single
`xmlParameters` parameter, and an image search is that document with an
`ImageUrl` element — the form the provider's own `BatchSearchItemsFrame`
example uses. There is no documented flat `ImageUrl` parameter beside the flat
`ItemTitle` this Actor's keyword search uses, and guessing one would cost a
billed call to find out, so `SourceConfig` carries both shapes
(`image_param` for a provider with a flat one, `xml_params_param` for OTAPI's
document) and the preset names the documented one.

The image is sent as the **only** condition. The temptation is to pass the
keyword along with it, but a provider front-end that dropped the image
parameter would then run an ordinary keyword search, and in listing mode every
hit of it is billed — a page of plausible, unrelated records that look like a
successful image search. With the image alone, a dropped parameter leaves no
condition at all, which the provider refuses outright. For the same reason an
image URL the provider could not fetch, and a provider with no image search in
its config, are refused before the call rather than after it.

Everything downstream is unchanged: the hits come back in the same shape as a
keyword page, so both detail levels, the quote, the minimum-order screening
and the paging rules are shared, and an image search costs exactly what a
keyword search costs.

Two things the provider work changed in the shared code:

* **Risk-control markers are never matched against a product record.** The
  check used to scan the whole response body, and 滑块 is also a large 1688
  category (直线导轨滑块, linear-guide sliders): any such listing came back as a
  platform refusal, unbilled and empty. Markers are now matched only against a
  non-JSON body, or against a JSON body known not to be a listing because the
  map expects a title and none came back.
* **Structured data is first-class.** A ladder given as typed rungs with only a
  starting quantity each is closed by the next rung's start; a value in a
  name/value list is addressed by name (`FeaturedValues[Name=…].Value`) since
  the order is not stable; and the facts a typed value cannot state for itself
  — that a sales figure is a 30-day one, whether stock is exact — come from the
  provider's `hints`.

The normalisation layer — the part that is actually the product — is tested
against both shapes before any credential exists.

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

Data source: OTAPI 1688 on RapidAPI. The tiers include a daily allowance, and
the per-call price shown on the listing is the *overage* rate, not the base:

| Plan | $/mo | Included | Effective per call |
|---|---|---|---|
| Basic | $0 | 20/day | — |
| Pro | $25 | 900/day | $0.000926 |
| Ultra | $150 | 5,500/day | $0.000909 |
| Mega | $350 | 15,000/day | $0.000778 |

Full detail at $0.005 a product, on Pro. The price was $0.012 at first; the
1688 Actors on the Store sell full detail at $0.004-0.005, the leader
included, so it came down to the market before publishing.

    sell price                 $0.0050
    less Apify's 20%          -$0.0010
    less platform compute     -$0.00007
    less data-source call     -$0.00093   (Pro's allowance, fully used)
    less bandwidth            -$0.0005    (up to; ~0.5 MB a detail past 10 GB)
    ------------------------------------
    margin                     $0.0025    (about 50%)

Pro pays for itself after 6,500-7,500 products a month, and can be stopped any
month. **Past Pro's 900 calls a day each detail call is overage at $0.005,
more than the $0.004 left after Apify's cut**, so every full-detail product
beyond the allowance loses about $0.001. At that volume move to Ultra
($0.003 overage, 5,500 a day included) or raise the price. Basic is for
validation only: its $0.008 overage loses money on every call past 20 a
day.

Listing level at $0.002 a record: a search call returns up to 50 hits, so the
data-source cost per record is about $0.00002 and the margin about 76%. It is
also what a buyer shortlisting a hundred candidates wants, and it funnels into
full detail for the handful they settle on.

Two constraints that do not change with the provider:

* **A second call per product needs pricing first.** Shipping, descriptions and
  vendor details are separate endpoints. Each one added per product is another
  billed call; nothing gets added on the assumption that it is cheap.
* **Default `maxProducts` stays low.** Free-plan Apify users generate no
  revenue — Apify covers their platform cost but not the data-source call,
  which is ours.

Bandwidth is metered too: 10,240 MB a month included, then $0.001 per MB. A
real search page of 50 hits weighed 1.36 MB — most of it per-SKU size data —
so about 7,500 pages a month fit in the allowance, and past it a page costs
about $0.0014, under $0.00003 a record. Real detail responses weighed 143 KB to
1.24 MB — the heavy one has 108 variants and a long HTML description — so at
Pro's 27,000 calls a month, averaging ~600 KB, bandwidth would run about 6 GB
past the allowance: roughly $6 a month, $0.0002 a product. Small beside the
margin, but not zero, and it grows with variant count.

### Later: Taobao through OneBound

OTAPI's RapidAPI listing is 1688 only. OneBound covers Taobao, Tmall and 1688
on one interface at 0.022 元 a call (52% margin at $0.012), but its minimum
top-up is 1,000 元 (~$149) and a balance expires after a year, and the account
needs 实名认证 (individual verification is accepted). The cheap end of the
Taobao store ($3.00/1,000) nets $0.00233 a product — below that call cost — so
those listings only work on a committed monthly plan. Revisit once 1688 revenue
covers the prepayment; it is a preset, not a rewrite.

## Who can run up the bill

A billed record is always profitable, even at the data source's overage rates
($0.0096 net against $0.005–0.008 a call), so success cannot be weaponised.
Money is lost only on a call that earns nothing, and there are three ways to
make one:

* **Unbilled failures.** A not-found or refused listing is free to the buyer
  but still costs a call. A run of a thousand made-up ids would be a thousand
  calls. A circuit breaker stops a run once ten failures make up more than
  half of what was tried, or twenty come in a row — checked between batches,
  the earliest point at which stopping saves calls. Measured: 25 made-up ids
  followed by 3 good ones cost 28 calls before, 10 after.
* **Free-plan users.** Apify pays the developer nothing for their runs but
  covers only the platform cost, not the data-source calls. Those runs are
  capped at 3 listings, or 20 listing-level records — one search page, one
  call. The cap is enforced on the platform always, treating a missing plan
  flag as free; off the platform only when the flag is set explicitly, since
  the SDK reads "unset" and "free" alike and local development must not be
  capped.
* **No hard cap at the data source.** RapidAPI bills overage rather than
  suspending, so nothing upstream stops a runaway. The two guards above bound
  the loss per run; the remaining lever is per-run revenue: price the
  platform's built-in `apify-actor-start` event in the Monetization settings,
  so a run that fetches nothing billable still pays something.

## Throughput

*What the platform runs actually show (2026-10-20, 2026-10-25): three listings
at full detail with five workers finish in 5-6 s of wall clock including the
container starting, and one search page returning 20 records in 7 s. So a call
costs on the order of 1-2 s, not the 300 ms the simulated figures below assume,
and RapidAPI's own ~2.5 s listing is the right order of magnitude. At 2.5 s and
five workers the ceiling is about 120 listings a minute; at ten workers and
0.1 s spacing, about 240. Those two are arithmetic from the latency, not
measurements. None of these numbers is on the Store page, and the simulated
ones below must not go there.*

*Degraded, the same calls took 54-108 s (2026-10-25), which is why an image
search carries its own timeout and why a timed-out request is retried once.*


The A-share Actor spaces its requests a second apart because it reads CNINFO's
public endpoints, where that is simple courtesy. This one calls a provider API
billed per call: the constraint is the plan's rate limit, not a site owner's
patience, and a second between requests caps the Actor at ~60 listings a
minute when a competitor advertises 250+.

Listings are fetched five at a time by default, with the request spacing held
globally rather than per worker — N workers each honouring the interval
independently would be N times the intended rate, which is how a rate limit
gets tripped. Against a simulated 300 ms round-trip: ~61/min serial at the old 1 s spacing,
~284/min at the defaults, ~525/min at ten workers and 0.1 s. Those measure the
batching, not this data source; see the note above for what it really costs.

Batching keeps the billing path intact: a batch is fetched in parallel, then
its records are pushed and charged in sequence. Before each batch the Actor asks
the SDK how many more records the run's charge limit covers — counting the
event's price and the default-dataset item price together — and fetches only
that many. An earlier version checked only after pushing, so a limit reached
part-way through a batch had already paid the data source for the rest of it.
The same check runs before each search page in listing mode, since a page is
itself a billed call.

## Validating the map

The field map was written against the provider's documentation, not against a
response, so until a real payload has been through `audit.py` every field claim
is a claim about the docs. `VALIDATION.md` is the runbook: seven billed calls,
chosen so each one settles a different question, captured to disk once and
replayed offline from then on.

## Compliance boundary

* Product and shop listing data only. No buyer identities, no reviewer names,
  no contact details, no personal data.
* No captcha solving, no bot-detection evasion, no signature forgery. When
  upstream refuses, the record says so.
* Rate limited by default, and the limit is an input the buyer can raise but
  not remove.
