# Validating the field map against a live provider

The normalisation layer is finished and tested against payloads I wrote. What
is *not* established is the shape of a real provider response, so until one has
been through `audit.py`, every field claim in the README is a claim about
documentation.

**Provider: OTAPI 1688, via RapidAPI.** Chosen over OneBound for three reasons:
a free tier that makes validation cost nothing and is renewable daily; $25/month
stoppable rather than a 1,000 元 prepayment that expires in a year; and a
cheaper effective call rate inside the included quota. The cost is coverage —
this listing is **1688 only**, with no Taobao. That is an acceptable trade
because the wholesale ladder, the minimum order quantity and the
quantity-aware quote are 1688 concepts: the Actor's strongest differentiator
only applies where this provider works. Taobao is a later addition through
OneBound, once there is revenue to justify the prepayment.

## The tiers

| Plan | $/mo | Included | Effective per call | Overage | Bandwidth |
|---|---|---|---|---|---|
| **Basic** | **$0** | **20/day** | — | $0.008 | 10,240 MB/mo, then $0.001/MB |
| Pro | $25 | 900/day | $0.000926 | $0.005 | same |
| Ultra | $150 | 5,500/day | $0.000909 | $0.003 | same |
| Mega | $350 | 15,000/day | $0.000778 | $0.001 | same |

Validation fits inside Basic. At $0.005 a product the Pro tier leaves about a
50% margin inside its daily allowance and pays for itself after 6,500-7,500
products a month; past the allowance its $0.005 overage loses money (DESIGN.md).

## Progress

- **2026-10-19 — search: done.** One call, 50 hits for `T恤 批发`. The hit list
  is at `Result.Items.Items.Content` as the preset guessed; ids come back as
  `abb-…`. The search map in `otapi_1688_config` is now written from it, and a
  trimmed copy is a real fixture (`tests/fixtures/otapi-1688/`). Findings that
  changed code: 1688's headline price is the *cheapest* rung of the ladder, so
  quoting a small order off it understated it by up to 30% — the quote no
  longer falls back to the headline while a ladder exists; about half the hits
  carry their ladder in the search response, so listing mode quotes too; and a
  search page the provider did not fill is now the last one, instead of a
  billed call for an empty page 2. Sales: `SalesInLast30Days` is mapped;
  `payOrder30Day` and `saleNum` disagree with both it and `TotalSales`, so they
  are left unmapped until their meaning is known.
- **2026-10-20 — details: done.** Four calls: three ids from that search and
  a bogus one. All fifteen mapped fields resolve on real data, and the bogus id
  comes back `ErrorCode: NotFound`, classified `not_found` and unbilled. The
  item map is written; trimmed copies are fixtures, and the whole Actor replays
  them end to end. Findings that changed code: the minimum order is
  `FirstLotQuantity`; a promotion lives at `Promotions[0]` here but
  `PromotionPrice` in search; variants carry the *regular* price, which mixed
  with a promotion used to read as a false price span; the item's
  `VendorName` is sometimes a login and once a different company, so the
  vendor's `ShopName` wins; seller scores are typed (`Vendor.Scores`) with no
  Chinese label, so `aspectCn` is null; a property listed several times (seven
  `主要销售地区`) keeps every value; picture URLs sit under `Url`.
- **Total spent on validation: 5 calls**, inside the free tier.
- **2026-10-20 — abuse protections: done** in code (free-plan cap, failure
  circuit breaker), each with an end-to-end test that fails against the
  unprotected code.
- **2026-10-20 — README and samples checked against real records.** The
  examples now use real values; `samples/` holds real normalised records.
- **2026-10-20 — deployed.** `apify push` built `clifford_x/1688-wholesale-products`
  (Actor `OqcMBgcUFQI0WCV7G`, build 0.1.1), with the RapidAPI key as the
  secret `otapi-1688-rapidapi-key`. Smoke run on the platform (keyword
  `T恤 批发`, 2 listings, quantity 100): succeeded, both listings `ok`, a batch
  of 2 detail calls in 1.8 s, three real OTAPI calls in all. One listing came
  back span-priced (variants at 20.8 / 21.8 / 23), so its quote is null as
  designed — a quote range would serve such listings better; noted for later.
- **2026-10-20 — monetization active.** Pay per event, platform usage
  included: `product-normalised` $0.012, `product-listed` $0.002,
  `apify-actor-start` $0.01 (one per GB of memory, minimum one). Apify's
  default `apify-default-dataset-item` event was removed, so records for ids
  that could not be served stay free. The README pricing table matches.
- **2026-10-20 — Store prefill run on the platform.** Keyword `T恤 批发`,
  quantity 100, 3 listings: all `ok`, charged 1 start + 3 `product-normalised`,
  usage $0.0003. Two of the three were variant-priced with no ladder and came
  back with a null quote; fixed in build 0.1.4 (quoted as a range), re-run
  confirmed. Open: listing `524415310486` reports its shop as `阒蒇炎坝邢匏`,
  not a company name. Raw response not captured yet, so whether OTAPI masks
  it or the field map picks the wrong one is unknown.
- **2026-10-20 — repriced before publishing.** The Store's 1688 Actors sell
  full detail at $0.004-0.005, so `product-normalised` $0.012 → $0.005 and
  `apify-actor-start` $0.01 → $0.005; `product-listed` stays $0.002. Title
  renamed to lead with "1688 Scraper", the term the category is searched by.
  RapidAPI moves to Pro, since Basic's overage loses money at this price.
- **2026-10-20 — published on the Store.** Build 0.1.6, public at
  `apify.com/clifford_x/1688-wholesale-products`. Apify's daily auto-test runs
  the prefill (`T恤 批发`, 3 listings): about four data-source calls a day, and
  three empty or failed days in a row mark the Actor under maintenance.
- **2026-10-20 — alerts checked.** Apify: Issues, reviews, usage-anomaly
  and monthly-summary notifications already on (email and web). Monitoring
  alerts cover only runs in this account, not buyers' runs or the Store's
  daily test, whose failures Apify emails about itself. RapidAPI: no consumer
  alert setting exists; it alerts on its own at 85% and 100% of quota, and
  overage cannot be capped from the consumer side. The Basic quota was 100%
  used on 2026-10-20, so calls until it resets are overage at $0.008. Still
  on Basic: the move to Pro is a payment, left to the account owner.
- **Left before publishing:** throughput measured against real latency, and
  the checks below.

## Phase 0 — sign up (0 calls)

- [ ] Subscribe to **Basic** on
      `rapidapi.com/open-trade-commerce-open-trade-commerce-default/api/otapi-1688`.
      RapidAPI asks for a card even on the free plan; Basic itself bills nothing.
- [ ] Copy the **X-RapidAPI-Key**. The host is `otapi-1688.p.rapidapi.com`.
- [ ] Note the listed latency, ~2.5 s. The throughput figures in the README were
      measured against a 300 ms round-trip and will need redoing.

## Phase 1 — already done

`source.py` carries an `otapi-1688` preset, with the request side confirmed
from the provider's own request samples on 2026-10-19:

```
GET https://otapi-1688.p.rapidapi.com/BatchGetItemFullInfo?language=en&itemId=abb-1243920753
GET https://otapi-1688.p.rapidapi.com/BatchSearchItemsFrame?language=en&framePosition=0&frameSize=50&ItemTitle=…
x-rapidapi-key / x-rapidapi-host headers
```

Each of these breaks a call quietly rather than loudly when wrong, and each is
handled once, in the preset:

* the **paths** are `BatchGetItemFullInfo` / `BatchSearchItemsFrame`, not the
  playground's display names "Get Item" / "Search for items";
* 1688 ids carry an **`abb-` prefix** on the wire. It is added to every request
  and stripped from everything that comes back, so a buyer only ever types and
  sees the platform's own offer id, and URLs never come out as
  `/offer/abb-….html`;
* credentials go in **headers**, set on the session so they cannot be logged
  with a URL;
* paging is a **zero-based item offset** (`framePosition`), computed as
  `(page - 1) * frameSize`;
* `language` is **required on every call**;
* the preset serves **1688 only**, so a run asking for Taobao is refused before
  any call is made.

What is deliberately **not** filled in: `field_map`, `search_field_map` and the
`hints`. No sample response is published, and guessing them would manufacture
fields that read as present and are not. The error-field and hit-list paths are
OTAPI's documented conventions offered as candidates; each is only used if it
resolves, and Phase 3 confirms them.

## Phase 2 — capture (about 6 of the 20 free daily calls)

```
read -rs SOURCE_API_KEY && export SOURCE_API_KEY
```

**Start with the search.** It is the highest-value single call: it returns the
entire listing-mode shape *and* up to 50 real item ids, so there is no hunting
for ids in a browser first.

```
python capture.py --provider otapi-1688 --platform 1688 --search 蓝牙耳机
```

`capture.py` saves the search **raw**, not just parsed ids — with an empty
map it may parse none, and the raw response is the point of the call. Run
`python audit.py tests/fixtures/live --provider otapi-1688` once at this stage:
its "every list of objects" section shows where the hits live and their keys.
Read the hit ids from there, pick three — ideally ones whose
titles suggest wholesale (批发 / 厂家 / 现货) so a 阶梯价 ladder is likely —
then fetch them plus a bogus id:

```
python capture.py --provider otapi-1688 --platform 1688 <id1> <id2> <id3> 1
```

Five calls so far, and the allowance renews daily, so being thorough costs
nothing. What keeps it honest anyway: `--retries 0` is the default, and
`capture.py` refuses to re-fetch an id already on disk unless `--force`.

**Do not point `actor_main` at the live provider yet.** Its defaults are ten
listings five at a time; `capture.py` is the only thing that should touch the
network here.

## Phase 3 — audit (0 calls)

```
python audit.py tests/fixtures/live --provider otapi-1688
```

**Always pass `--provider`.** Without it the payloads are read with the default
query-string map and every field reads as missing for reasons unrelated to the
provider. With an empty map, every field is unread by design; the section that
matters is **"in the payload but not read"**, which lists what the response
actually contains, with values.

Write the map from that list, looking for:

- [ ] **The item id, title and price.** The minimum for any record.
- [ ] **The 阶梯价 ladder.** Without it the `quote` feature has nothing to
      walk and the main differentiator is gone; look hard before concluding it
      is absent. If its rungs are typed objects, map `tiers` to the list and
      set `hints["tiers"] = {"min_qty": …, "price": …}` (and `"max_qty"` if a
      top is given). A minimum-only ladder is closed automatically: each rung
      ends one below the next.
- [ ] **The minimum order quantity.** Same reasoning.
- [ ] **Sales / order volume.** `MinVolume` is a search filter, so the figure
      exists somewhere. If it sits in a name/value list, address it with a
      selector — `FeaturedValues[Name=SalesInLast30Days].Value` — rather than
      a position, since the order is not stable. If the window lives in the
      field's *name* rather than in text, state it in
      `hints["sales"] = {"window": …, "unit": …}`; a hint never overrides what
      text says.
- [ ] **Vendor ratings**, for `shopScores`.
- [ ] **The SKU / configuration array**, and whether its axes are named.
- [ ] **Shipping.** If it needs the separate vendor or description call, that is
      a second billed call per product — price it before adding it.
- [ ] **Both titles.** Map `title_cn` to the original and `title_en` to the
      translation if both exist. Even if they are swapped, the normaliser
      routes a title with no Chinese in it to `titleEn` — but map them right.
- [ ] **Stock precision.** If the count is exact, set
      `hints["stock"] = {"approximate": False}`; until then it is flagged.
- [ ] **The bogus id's error shape**, so the status classification is right.

## Phase 4 — write it down (0 calls)

1. Fill `field_map`, `search_field_map`, `hints` and, if the audit showed
   different ones, `search_items_paths` / `error_code_paths` in
   `otapi_1688_config`.
2. Copy the useful payloads from `tests/fixtures/live/` into `tests/fixtures/`.
   `live/` is gitignored on purpose: promoting one should be a decision.
3. A test per surprise. Every gap between the documentation and the response is
   a bug the next provider change can reintroduce.
4. `python -m pytest tests/ -q`

## Phase 5 — replay end to end (0 calls)

```
ACTOR_SOURCE_FIXTURES=tests/fixtures/live SOURCE_PROVIDER=otapi-1688 python -m actor_main
```

The real Actor against real payloads, no network, read through the preset's
maps exactly as live data would be. Without `SOURCE_PROVIDER` the replay uses
the default map and proves nothing. Check the records, then check
that the bogus id came back with a status and **was not billed**.

## 2026-10-25 — the data source is refusing everything

Found while smoke-testing the build before releasing it. Right now, every
endpoint answers the same way, slowly:

    NotAvailable: The application 'Storage' is not available.

- image search (flat `ImageUrl`): 65 s, refused;
- keyword search (`ItemTitle=T恤 批发`), the path that has worked since
  2026-10-19 and that the daily automated test uses: 108 s, refused;
- detail fetch (`BatchGetItemFullInfo`, id 669724481680): 54 s, refused,
  reported as `upstream_error` and not billed.

Normal latency on this host is ~2.5 s, so it is degraded, not merely erroring.
By 14:58 local it had stopped answering at all: a keyword probe timed out,
four attempts, which is what capped retries on a timeout to one — a hung
provider was costing four billed attempts per run.
The credentials are fine: a rejected key is a 401/403 and raises
`AuthenticationFailed`, which none of these did. RapidAPI's discussion board
for the API has nothing recent (its newest thread is three years old and
unanswered, which says something about support there).

**What this does to the published Actor.** Build 0.1.4, what buyers run today,
reads an error in a search body as a page with no hits — so a listing-mode or
keyword run right now *succeeds*, returns nothing, and says nothing was wrong.
Nothing is billed, but nothing is explained either. The fix in this branch
raises it instead, with the provider's own message. Detail runs were never
affected: that path has always checked the body.

## Image search — form proven, verdict suspended

What the four runs of 2026-10-25 do establish:

- **The parameter form.** OTAPI's documented `xmlParameters` document reaches
  OTAPI and is refused — `ContractViolation: Search parameters must have at
  least one of these values: CategoryId, VendorName, VendorId, ItemTitle,
  BrandId, Configurators` — while the flat `ImageUrl` parameter this host's
  own playground documents is accepted, no contract violation. That difference
  is a real answer and it is what the preset now uses. The playground lists
  `OrderBy`, `MinPrice`, `MaxPrice` and `MinVolume` beside it: unused levers.
- **A search response carrying an error used to read as an empty page.** The
  first image search returned a successful run with no records in 0.3 s; that
  is now raised. The one finding here that improves the keyword path too.

What is **not** established, and what I wrongly concluded before the smoke
test: that image search needs an OTAPI "Storage" application this instance
lacks. The same `NotAvailable: Storage` answer comes back for keyword searches
and detail fetches, so it is the outage above, not an image-specific gap. The
30 s timeout being too short is contaminated the same way — a keyword search
took 108 s during the outage. The longer image budget stays anyway: it costs
nothing when the provider is healthy, and a visual match is genuinely heavier
than a lookup.

**To settle it, once the provider is healthy** (a keyword run returning records
is the signal), one call:

    apify call -b image-test -i '{"imageUrl":"https://cbu01.alicdn.com/img/ibank/O1CN01ha5KYW1knTcju4X9C_!!2813694728-0-cib.jpg","detailLevel":"listing","maxProducts":5,"platform":"1688"}'

That image is the main picture of offer 672715568844, so finding that offer in
the results proves the match is real rather than merely accepted. Until then
the input stays `hidden` and the Store text promises nothing.

## Before publishing

- [x] **Re-measure throughput against the real latency.** Done 2026-10-25
      from the platform runs: a call costs 1-2 s healthy, so five workers is
      about 120 listings a minute, not 284. DESIGN.md now says so, and the
      README never carried a figure — nothing to correct there.
- [ ] **Check the daily test's cost.** The prefill is now a keyword
      (`T恤 批发`), 3 products and a quantity of 100: one search plus three
      listings, four calls. If Apify's daily automated run uses the prefilled
      input, that is 4 of Basic's 20 calls a day.
- [x] **Check the README's examples against real records.** Done
      2026-10-20: real values throughout; shipping, which the data source
      leaves empty, is no longer promised anywhere.
- [ ] Set `SOURCE_API_KEY` as an Actor secret, and run on the platform with
      `maxProducts: 2` before anything larger.

## Later: adding Taobao

OneBound (`api-gw.onebound.cn`) covers Taobao, Tmall and 1688 on one interface
at 0.022 元 a call, but the minimum top-up is 1,000 元 and a balance expires
after a year. Its account also needs 实名认证 — individual verification is
accepted, so no company entity is required. Revisit once 1688 revenue covers
the prepayment, and keep both providers behind the same `ProductSource`
protocol so it stays a config change.
