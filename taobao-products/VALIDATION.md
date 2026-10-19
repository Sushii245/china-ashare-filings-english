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

Validation fits inside Basic. At $0.012 a product the Pro tier leaves a 72%
margin and pays for itself after about 2,900 products a month.

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
- **Next — detail calls.** `BatchGetItemFullInfo` on three of those ids plus a
  bogus one, to write the item map.

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

## Before publishing

- [ ] Re-measure throughput against the real ~2.5 s latency and correct the
      README. Five workers at that latency is nowhere near 284 a minute.
- [ ] **Check the daily test's cost.** The prefill is now a keyword
      (`T恤 批发`), 3 products and a quantity of 100: one search plus three
      listings, four calls. If Apify's daily automated run uses the prefilled
      input, that is 4 of Basic's 20 calls a day.
- [ ] **Check the README's examples against real records.** Its JSON is the
      Actor's output shape with illustrative values; once real records exist,
      make sure nothing there promises a field the data source does not fill.
- [ ] Set `SOURCE_API_KEY` as an Actor secret, and run on the platform with
      `maxProducts: 2` before anything larger.

## Later: adding Taobao

OneBound (`api-gw.onebound.cn`) covers Taobao, Tmall and 1688 on one interface
at 0.022 元 a call, but the minimum top-up is 1,000 元 and a balance expires
after a year. Its account also needs 实名认证 — individual verification is
accepted, so no company entity is required. Revisit once 1688 revenue covers
the prepayment, and keep both providers behind the same `ProductSource`
protocol so it stays a config change.
