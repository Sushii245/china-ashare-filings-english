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

## Phase 0 — sign up (0 calls)

- [ ] Subscribe to **Basic** on
      `rapidapi.com/open-trade-commerce-open-trade-commerce-default/api/otapi-1688`.
      RapidAPI asks for a card even on the free plan; Basic itself bills nothing.
- [ ] Copy the **X-RapidAPI-Key**. The host is `otapi-1688.p.rapidapi.com`.
- [ ] Note the listed latency, ~2.5 s. The throughput figures in the README were
      measured against a 300 ms round-trip and will need redoing.

## Phase 1 — already done

`source.py` carries an `otapi-1688` preset handling the three things that each
break a call quietly rather than loudly:

* credentials go in **headers**, not the query string, and are set on the
  session so they cannot be logged with a URL;
* paging is a **zero-based item offset** (`framePosition`), computed as
  `(page - 1) * frameSize` — sending the page number returns the second *item*
  onwards, not the second page, and nothing in the response says so;
* `language` is **required on every call**.

The preset's `field_map` is deliberately **empty**. Phase 3 writes it from a
real response. Guessing it would produce fields that read as present and are
not, which is the failure this whole exercise exists to avoid.

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

Read `tests/fixtures/live/_search.json`, pick three ids — ideally ones whose
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
python audit.py tests/fixtures/live
```

With an empty field map every mapped path will read `MISSING` — that is
expected, and the section that matters is **"in the payload but not read"**,
which lists what the response actually contains, with values.

Write the map from that list, looking for:

- [ ] **The item id, title and price.** The minimum for any record.
- [ ] **The 阶梯价 ladder.** OTAPI calls configurations and price breaks
      something of its own; without it the `quote` feature has nothing to walk
      and the main differentiator is gone. Look hard before concluding it is
      absent.
- [ ] **The minimum order quantity.** Same reasoning.
- [ ] **Sales / order volume.** `MinVolume` is a search filter, so the figure
      exists somewhere. Its wording decides whether the sales-semantics
      promise survives — and whether an English-language response still carries
      a Chinese source string to cite, which the whole citation contract
      depends on.
- [ ] **Vendor ratings**, for `shopScores`.
- [ ] **The SKU / configuration array**, and whether its axes are named.
- [ ] **Shipping.** If it needs the separate vendor or description call, that is
      a second billed call per product — price it before adding it.
- [ ] **`language=en` effects.** If the provider returns English titles, they
      must not land in `titleCn`. A field named for Chinese holding English is
      worse than a null.
- [ ] **The bogus id's error shape**, so the status classification is right.

## Phase 4 — write it down (0 calls)

1. Fill `field_map` in `otapi_1688_config`.
2. Copy the useful payloads from `tests/fixtures/live/` into `tests/fixtures/`.
   `live/` is gitignored on purpose: promoting one should be a decision.
3. A test per surprise. Every gap between the documentation and the response is
   a bug the next provider change can reintroduce.
4. `python -m pytest tests/ -q`

## Phase 5 — replay end to end (0 calls)

```
ACTOR_SOURCE_FIXTURES=tests/fixtures/live python -m actor_main
```

The real Actor against real payloads, no network. Check the records, then check
that the bogus id came back with a status and **was not billed**.

## Before publishing

- [ ] Re-measure throughput against the real ~2.5 s latency and correct the
      README. Five workers at that latency is nowhere near 284 a minute.
- [ ] **Replace the prefilled item id.** `input_schema.json` prefills
      `600530677383`, which I invented while writing fixtures. Apify runs a
      published Actor against its default input daily and flags it as under
      maintenance after repeated empty or failed runs. Use a verified id, or
      default to `detailLevel: "listing"` with a keyword — one call, and it
      cannot go stale the way a single id can.
- [ ] **Re-point the positioning to 1688.** The README leads with Taobao, and
      on this provider the Actor does not do Taobao. Leading with a platform it
      cannot serve is the fastest route to a one-star review.
- [ ] Set `SOURCE_API_KEY` as an Actor secret, and run on the platform with
      `maxProducts: 2` before anything larger.

## Later: adding Taobao

OneBound (`api-gw.onebound.cn`) covers Taobao, Tmall and 1688 on one interface
at 0.022 元 a call, but the minimum top-up is 1,000 元 and a balance expires
after a year. Its account also needs 实名认证 — individual verification is
accepted, so no company entity is required. Revisit once 1688 revenue covers
the prepayment, and keep both providers behind the same `ProductSource`
protocol so it stays a config change.
