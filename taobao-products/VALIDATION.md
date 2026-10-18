# Validating the field map against a live provider

The field map was written against OneBound's documentation, not against a
response. Until a real payload has been through `audit.py`, every claim in the
README is a claim about the docs.

Calls are billed at 0.022 元 each, so the discipline is: **fetch each distinct
case once, save it, and do everything else offline.** The whole plan below
costs **7 calls — about 0.15 元, under $0.03.**

---

## Phase 0 — before spending anything

- [ ] **Check the free test quota.** Providers in this space usually grant a
      trial allowance on signup. If one exists, Phase 2 costs nothing.
- [ ] **Confirm the price list is still current.** The one this project is
      costed against is dated 2026-06-26. Ask for A-class `item_get` per-call
      and monthly rates in writing.
- [ ] **Ask specifically whether 1688 `item_get` bills as A-class.** The
      platform matrix shows 淘宝 / 天猫 / 1688 all supported on one interface,
      but the per-interface price table only itemises `taobao.item_get`. If
      1688 is billed higher, the margin model needs redoing for it.
- [ ] **Confirm whether `secret` is required** alongside `key`, and the exact
      base URL and paths. The docs give `https://api-gw.onebound.cn` and
      `/taobao/item_get`.
- [ ] Do **not** enable `item_fee`. It is B-class at 0.05 元, 2.3x the item
      call, and the margin model goes negative if shipping needs it. Call 2
      below is what decides whether that is ever necessary.

## Phase 1 — pick the seven targets (0 calls)

Open each listing in a browser first and confirm with your own eyes that it has
the feature, then copy the id out of the URL (`item.htm?id=…`, or
`/offer/….html` on 1688). Picking deliberately is what keeps this to 7 calls
instead of 30.

| # | Pick a listing that… | What the call settles |
|---|---|---|
| 1 | is on **Taobao** and whose **variants differ in price** (e.g. 颜色/版本 at different prices) | Whether `price_range` exists at all; whether the SKU-derived span works on real data; the real shape of `skus.sku` and `props_list` |
| 2 | is **包邮** and shows **月销 N+** | **The margin question**: does `post_fee` actually come back in the item record? And is the sales field populated, in what wording? |
| 3 | is **预售** with a stated dispatch window | Whether `presale_text` exists and what it looks like |
| 4 | is on **1688**, with a visible **阶梯价 ladder** and a **起订量** | `price_ranges` and `min_num` shapes; whether 1688 answers on the same interface |
| 5 | is a **bogus id** — just use `1` | The error shape: `error`, `error_code`, and whether the `"0000"`-is-success logic and the risk-control classification hold |
| 6 | is on **Tmall** (a 天猫 listing, not a 淘宝 one) | Whether Tmall differs from Taobao in this response, and whether shop scores appear |
| 7 | — a **search**, not an item | The search response shape and whether id extraction works |

Write the ids down before you start.

## Phase 2 — capture (7 calls)

Export the credentials once so they stay out of your shell history:

```
read -rs SOURCE_API_KEY && export SOURCE_API_KEY
read -rs SOURCE_API_SECRET && export SOURCE_API_SECRET
export SOURCE_BASE_URL=https://api-gw.onebound.cn
```

Then, five Taobao/Tmall items plus one search — **six calls**:

```
python capture.py --platform taobao --search 蓝牙耳机 \
    <id1> <id2> <id3> <id6> 1
```

And the 1688 offer — **one call**:

```
python capture.py --platform 1688 <id4>
```

What keeps this from leaking calls:

- `--retries 0` is the default, because a retry is another billed call.
- `--cache yes` is the default. A repeat of the same id may be served from the
  provider's cache rather than billed again, so leave it on while validating.
- `capture.py` **refuses to re-fetch an id already on disk** unless you pass
  `--force`.
- **Never run `actor_main` against the live provider during validation.** Its
  defaults are 10 listings, 5 at a time — one careless run is 10 calls.
  `capture.py` is the only thing that should touch the network here.

The script prints how many calls it billed. Check that number matches.

## Phase 3 — audit (0 calls)

```
python audit.py tests/fixtures/live
```

For every payload it prints three things: which mapped paths resolved and which
came back `MISSING`, what is in the payload that the map does not read yet
(with values, so you can judge), and the record a buyer would receive.

Read it looking for:

- [ ] **`shipping` MISSING on call 2.** If `post_fee` is not in the item
      record, shipping cannot be returned without the B-class call, and the
      honest move is to drop the field and say so in the README — not to pay
      0.05 元 per product for it.
- [ ] **`sales` MISSING or useless.** The provider sample showed `total_sold`
      blank and `sales` as `"1"`. Sales semantics is the headline
      differentiator, so if the real field is thin, check `total_sold` and
      whatever the unread-keys list offers, and if nothing carries it, the
      README's lead has to change.
- [ ] **`shop_scores` MISSING.** Expected — the docs do not define it. Check
      the unread keys for `seller_info` / `shopinfo` subfields that do.
- [ ] **`price_range` MISSING.** Expected. Confirm the SKU-derived span in the
      "what the buyer would receive" block looks right.
- [ ] **`tiers` on call 4.** If `price_ranges` is missing, the whole `quote`
      feature has nothing to walk. Look hard at the unread keys for the ladder
      under another name.
- [ ] **Unread keys worth adding.** `promo_type`, `favcount`, review counts,
      delivery-time fields — anything a sourcing buyer would act on.
- [ ] **Call 5's error shape.** Confirm `error_code` is not `"0000"` and that
      the record came out `upstream_error`, not `ok`.

## Phase 4 — fix and lock in (0 calls)

1. Correct `field_map` in `source.py` for each `MISSING` entry, using the real
   path from the unread-keys list.
2. Promote the useful payloads: copy them from `tests/fixtures/live/` into
   `tests/fixtures/`. `live/` is gitignored on purpose — moving a payload into
   the suite should be a decision, not a side effect.
3. Write a test per surprise. Every difference between the docs and the
   response is a bug the next provider change can reintroduce.
4. `python -m pytest tests/ -q`

## Phase 5 — one end-to-end replay (0 calls)

```
ACTOR_SOURCE_FIXTURES=tests/fixtures/live python -m actor_main
```

This runs the real Actor against the real payloads with no network. Check the
dataset records, then check that the refused and bogus ids came back with a
`status` and **were not billed**.

## Before publishing — the default input

`input_schema.json` prefills `itemIds` with `600530677383`, which I made up
while writing the fixtures. Apify runs a published Actor every day against its
default input and flags it as under maintenance after repeated empty or failed
runs, so **replace that prefill with an id you verified in Phase 1** before the
Actor goes public. A cheaper default is `detailLevel: "listing"` with a keyword
prefill: one call, and it cannot go stale the way a single id can.

## Only then

Set `SOURCE_BASE_URL` and `SOURCE_API_KEY` as Actor secrets, push the build,
and run it on the platform with `maxProducts: 2` before anything larger.
