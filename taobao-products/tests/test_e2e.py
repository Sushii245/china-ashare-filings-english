"""The real Actor, end to end, in a subprocess per case.

Each case gets its own temporary storage directory, a scrubbed environment and
the recorded fixtures as its data source, so nothing leaks between cases or in
from the developer's shell. Pay-per-event is simulated with the SDK's local
test mode: ACTOR_TEST_PAY_PER_EVENT, a pricing table, and a max total charge.

These are the only tests that exercise main() itself — the charge-limit
handling, the batch loop, the status message — rather than its helpers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SPY = Path(__file__).parent / "e2e_spy"

#: Three listings that normalise cleanly, and one recorded refusal.
OK_IDS = ["600530677383", "612345678901", "555000111222"]
REFUSED_ID = "999000111222"

PRICING = {
    "pricingModel": "PAY_PER_EVENT",
    "pricingPerEvent": {
        "actorChargeEvents": {
            "product-normalised": {"eventTitle": "Listing", "eventPriceUsd": 0.005},
            "product-listed": {"eventTitle": "Search record", "eventPriceUsd": 0.002},
            # Locally the SDK prices any event it has no price for at $1 —
            # on the platform the same event costs $0. Pricing the synthetic
            # default-dataset event explicitly makes the simulation match the
            # platform; without it, $1 of budget covers no push at all.
            "apify-default-dataset-item": {"eventTitle": "Dataset item", "eventPriceUsd": 0},
        }
    },
}


def ppe(max_charge_usd: float) -> dict[str, str]:
    return {
        "ACTOR_TEST_PAY_PER_EVENT": "1",
        "APIFY_ACTOR_PRICING_INFO": json.dumps(PRICING),
        # An empty "{}" parses as unset, and the SDK then ignores the pricing
        # table altogether; the counts have to name at least one event.
        "APIFY_CHARGED_ACTOR_EVENT_COUNTS": json.dumps({"product-normalised": 0}),
        "ACTOR_MAX_TOTAL_CHARGE_USD": str(max_charge_usd),
    }


class Run:
    """What one Actor run did: its exit, its records, its log, its calls."""

    def __init__(self, proc, records, log, calls):
        self.proc, self.records, self.log = proc, records, log
        self.fetched = [value for kind, value in calls if kind == "fetch"]
        self.searched_pages = [int(value) for kind, value in calls if kind == "search"]


def run_actor(tmp_path: Path, actor_input: dict, **extra_env: str) -> Run:
    store = tmp_path / "storage" / "key_value_stores" / "default"
    store.mkdir(parents=True)
    (store / "INPUT.json").write_text(json.dumps(actor_input, ensure_ascii=False), encoding="utf-8")

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("APIFY_", "ACTOR_", "SOURCE_", "CRAWLEE_"))
    }
    spy_log = tmp_path / "calls.tsv"
    # The spy goes first so its sitecustomize wins; extra_env may prepend
    # another directory (e.g. an older actor_main) ahead of ROOT.
    pythonpath = extra_env.pop("PYTHONPATH", str(ROOT))
    env.update({
        "ACTOR_SOURCE_FIXTURES": str(FIXTURES),
        "PYTHONPATH": f"{SPY}{os.pathsep}{pythonpath}",
        "FETCH_SPY_LOG": str(spy_log),
    })
    env.update(extra_env)

    proc = subprocess.run(
        [sys.executable, "-m", "actor_main"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    dataset = tmp_path / "storage" / "datasets" / "default"
    records = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(dataset.glob("0*.json"))
    ] if dataset.exists() else []
    log = proc.stdout + proc.stderr
    calls = [
        tuple(line.split("\t", 1))
        for line in spy_log.read_text(encoding="utf-8").splitlines()
    ] if spy_log.exists() else []
    return Run(proc, records, log, calls)




def test_the_spy_sees_every_data_source_call(tmp_path):
    # The rest of this file trusts the call count, so check the counter first.
    run = run_actor(tmp_path, {"itemIds": OK_IDS, "platform": "1688"})
    assert run.proc.returncode == 0, run.log
    assert sorted(run.fetched) == sorted(OK_IDS)


def test_a_run_without_pay_per_event_returns_everything(tmp_path):
    run = run_actor(tmp_path, {"itemIds": OK_IDS, "platform": "1688"})
    assert run.proc.returncode == 0, run.log
    assert [r["itemId"] for r in run.records] == OK_IDS
    assert {r["status"] for r in run.records} == {"ok"}


def test_the_charge_limit_stops_before_fetching_what_it_cannot_bill(tmp_path):
    # $0.0125 covers two listings at $0.005. All three fit in one batch of five,
    # so a run that checks the limit only when pushing has already paid the
    # data source for the third by the time it finds out.
    run = run_actor(
        tmp_path, {"itemIds": OK_IDS, "platform": "1688", "concurrency": 5}, **ppe(0.0125)
    )
    assert run.proc.returncode == 0, run.log
    assert [r["itemId"] for r in run.records] == OK_IDS[:2]
    assert sorted(run.fetched) == sorted(OK_IDS[:2]), "fetched a listing it could not bill"
    assert "1 not fetched" in run.log


def test_unbilled_records_do_not_use_up_the_budget(tmp_path):
    # The refusal costs nothing, so the budget reserved for it carries on to
    # the next batch, and no id is dropped by the batch being trimmed.
    ids = [REFUSED_ID, *OK_IDS]
    run = run_actor(
        tmp_path, {"itemIds": ids, "platform": "1688", "concurrency": 5}, **ppe(0.0125)
    )
    assert run.proc.returncode == 0, run.log
    assert [(r["itemId"], r["status"]) for r in run.records] == [
        (REFUSED_ID, "risk_control_blocked"),
        (OK_IDS[0], "ok"),
        (OK_IDS[1], "ok"),
    ]
    assert sorted(run.fetched) == sorted([REFUSED_ID, OK_IDS[0], OK_IDS[1]])


def test_no_budget_at_all_fetches_nothing(tmp_path):
    run = run_actor(tmp_path, {"itemIds": OK_IDS, "platform": "1688"}, **ppe(0.004))
    assert run.proc.returncode == 0, run.log
    assert run.records == []
    assert run.fetched == []
    assert "3 not fetched" in run.log


def test_listing_mode_does_not_search_without_budget(tmp_path):
    # A search page is one billed data-source call; with no room for even one
    # record, making it would buy nothing deliverable.
    run = run_actor(
        tmp_path, {"keyword": "x", "detailLevel": "listing", "platform": "1688"}, **ppe(0.001)
    )
    assert run.proc.returncode == 0, run.log
    assert run.records == []
    assert run.searched_pages == []


def test_listing_mode_returns_listed_records(tmp_path):
    run = run_actor(
        tmp_path, {"keyword": "x", "detailLevel": "listing", "platform": "1688"}, **ppe(1.0)
    )
    assert run.proc.returncode == 0, run.log
    assert run.records and {r["status"] for r in run.records} == {"listed"}
    assert run.fetched == []          # listing mode never fetches a listing


def test_an_unserved_platform_fails_before_any_call(tmp_path):
    run = run_actor(
        tmp_path, {"itemIds": OK_IDS, "platform": "taobao"}, SOURCE_PROVIDER="otapi-1688"
    )
    assert run.proc.returncode != 0
    assert run.records == []
    assert run.fetched == [] and run.searched_pages == []
    assert "serves 1688 only" in run.log


def test_listing_mode_on_the_real_otapi_capture(tmp_path):
    # The whole Actor, reading a real provider response through the preset's
    # maps — the replay VALIDATION.md's Phase 5 describes.
    run = run_actor(
        tmp_path,
        {"keyword": "T恤 批发", "detailLevel": "listing", "platform": "1688", "quantity": 1},
        **ppe(1.0),
        ACTOR_SOURCE_FIXTURES=str(FIXTURES / "otapi-1688"),
        SOURCE_PROVIDER="otapi-1688",
    )
    assert run.proc.returncode == 0, run.log
    assert run.searched_pages == [1]
    assert run.fetched == []
    assert len(run.records) == 5
    assert {r["status"] for r in run.records} == {"listed"}
    by_id = {r["itemId"]: r for r in run.records}
    # One piece of a ladder that starts at two: the first rung's 4.3, flagged.
    quote = by_id["669724481680"]["quote"]
    assert quote["unitPrice"]["value"] == 4.3
    assert quote["meetsMinimumOrder"] is False


def test_full_detail_on_the_real_otapi_captures(tmp_path):
    # The whole Actor on three real detail responses and a real refusal:
    # quotes, billing, and the not-found id left unbilled.
    ids = ["623219903269", "669724481680", "672715568844", "999999999"]
    run = run_actor(
        tmp_path,
        {"itemIds": ids, "platform": "1688", "quantity": 50},
        **ppe(1.0),
        ACTOR_SOURCE_FIXTURES=str(FIXTURES / "otapi-1688"),
        SOURCE_PROVIDER="otapi-1688",
    )
    assert run.proc.returncode == 0, run.log
    by_id = {r["itemId"]: r for r in run.records}
    assert [by_id[i]["status"] for i in ids[:3]] == ["ok", "ok", "ok"]
    assert by_id["999999999"]["status"] == "not_found"
    assert [by_id[i]["quote"]["unitPrice"]["value"] for i in ids[:3]] == [26.9, 4.3, 40.0]
    assert "Returned 3 of 4 listing(s); 1 unavailable and not billed." in run.log


# --------------------------------------------------- abuse protections, e2e

BOGUS = [str(800000000001 + n) for n in range(25)]   # pass the id check, exist nowhere


def test_a_free_plan_run_fetches_at_most_three(tmp_path):
    ids = [*OK_IDS, REFUSED_ID]
    run = run_actor(
        tmp_path, {"itemIds": ids, "platform": "1688", "maxProducts": 10},
        APIFY_USER_IS_PAYING="0",
    )
    assert run.proc.returncode == 0, run.log
    assert sorted(run.fetched) == sorted(ids[:3])
    assert len(run.records) == 3
    assert "free Apify plan, limited to 3" in run.log


def test_a_paying_run_is_not_capped(tmp_path):
    ids = [*OK_IDS, REFUSED_ID]
    run = run_actor(
        tmp_path, {"itemIds": ids, "platform": "1688", "maxProducts": 10},
        APIFY_USER_IS_PAYING="1",
    )
    assert run.proc.returncode == 0, run.log
    assert sorted(run.fetched) == sorted(ids)
    assert "free Apify plan" not in run.log


def full_search_page(directory: Path) -> Path:
    """A 50-hit search page built from the real capture's five hits.

    The real fixture is trimmed to five hits, too few for a 20-record cap to
    bite; the hits are repeated under fresh ids to fill one provider page, so
    nothing about their structure is invented.
    """
    real = json.loads((FIXTURES / "otapi-1688" / "_search.json").read_text(encoding="utf-8"))
    hits = real["payload"]["Result"]["Items"]["Items"]["Content"]
    page = []
    for n in range(50):
        hit = json.loads(json.dumps(hits[n % len(hits)]))
        hit["Id"] = f"abb-{700000000000 + n}"
        page.append(hit)
    real["payload"]["Result"]["Items"]["Items"]["Content"] = page
    directory.mkdir()
    (directory / "_search.json").write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
    return directory


def test_a_free_plan_listing_run_costs_one_call(tmp_path):
    # A full page of 50 and a request for 100: uncapped, that is 50 records and
    # a second page; a free plan gets 20 records from the first page, one call.
    run = run_actor(
        tmp_path,
        {"keyword": "T恤 批发", "detailLevel": "listing", "platform": "1688", "maxProducts": 100},
        APIFY_USER_IS_PAYING="0",
        ACTOR_SOURCE_FIXTURES=str(full_search_page(tmp_path / "page")),
        SOURCE_PROVIDER="otapi-1688",
    )
    assert run.proc.returncode == 0, run.log
    assert run.searched_pages == [1]
    assert len(run.records) == 20
    assert "free Apify plan, limited to 20" in run.log


def test_a_run_of_made_up_ids_is_stopped_by_the_breaker(tmp_path):
    # 25 ids that exist nowhere, then three good ones. Unbilled, each still
    # costs a data-source call; the breaker stops after the second batch of
    # five, so the rest — good ones included — are never fetched.
    ids = [*BOGUS, *OK_IDS]
    run = run_actor(
        tmp_path,
        {"itemIds": ids, "platform": "1688", "maxProducts": 50, "concurrency": 5},
    )
    assert run.proc.returncode == 0, run.log
    assert len(run.fetched) == 10, run.fetched
    assert set(run.fetched) <= set(BOGUS)
    assert {r["status"] for r in run.records} == {"not_found"}
    assert "stopped early because 10 of 10 listings came back unavailable, 18 not fetched" in run.log


def test_a_few_failures_do_not_trip_the_breaker(tmp_path):
    ids = [*OK_IDS, *BOGUS[:9]]
    run = run_actor(
        tmp_path,
        {"itemIds": ids, "platform": "1688", "maxProducts": 50, "concurrency": 5},
    )
    assert run.proc.returncode == 0, run.log
    assert sorted(run.fetched) == sorted(ids)
    assert "stopped early" not in run.log


# -------------------------------------------------------- a rejected key

def test_a_rejected_key_fails_the_run_at_once(tmp_path):
    # Without this, each listing became "unavailable", the breaker tripped at
    # ten, and the run reported success — pointing at the ids rather than at
    # the key that had expired.
    run = run_actor(
        tmp_path,
        {"itemIds": [*OK_IDS, *BOGUS[:12]], "platform": "1688", "maxProducts": 50, "concurrency": 5},
        FETCH_SPY_REJECT_KEY="1",
    )
    assert run.proc.returncode != 0
    assert len(run.fetched) <= 5, run.fetched           # one batch at most
    assert run.records == []
    assert "rejected" in run.log and "You are not subscribed to this API." in run.log


def test_a_rejected_key_fails_a_listing_run_too(tmp_path):
    run = run_actor(
        tmp_path,
        {"keyword": "x", "detailLevel": "listing", "platform": "1688"},
        FETCH_SPY_REJECT_KEY="1",
    )
    assert run.proc.returncode != 0
    assert run.searched_pages == [1]
    assert "rejected" in run.log


# -------------------------------------------------- the search endpoint fails

def status_of(run: Run) -> str:
    return next(
        (line.split("Terminal status message]: ", 1)[1]
         for line in run.log.splitlines() if "Terminal status message" in line),
        "",
    )


def test_listing_search_down_from_the_start_fails_cleanly(tmp_path):
    # Used to crash with a traceback and exit 91, with no status at all.
    run = run_actor(
        tmp_path, {"keyword": "x", "detailLevel": "listing", "platform": "1688"},
        FETCH_SPY_SEARCH_DOWN="1",
    )
    assert run.proc.returncode != 0
    assert "Traceback" not in run.log
    assert "search failed" in status_of(run) and "503" in status_of(run)


def test_listing_search_down_later_keeps_what_it_delivered(tmp_path):
    # Page 1 is a full 50 hits, so page 2 is asked for — and fails. The 50
    # already delivered (and billed) stand; the run says why it stopped.
    run = run_actor(
        tmp_path,
        {"keyword": "x", "detailLevel": "listing", "platform": "1688", "maxProducts": 100},
        FETCH_SPY_SEARCH_DOWN="2",
        ACTOR_SOURCE_FIXTURES=str(full_search_page(tmp_path / "page")),
        SOURCE_PROVIDER="otapi-1688",
    )
    assert run.proc.returncode == 0, run.log
    assert "Traceback" not in run.log
    assert len(run.records) == 50
    assert run.searched_pages == [1, 2]
    assert "search page 2 failed" in status_of(run)


def test_keyword_search_down_with_nothing_else_fails_cleanly(tmp_path):
    run = run_actor(tmp_path, {"keyword": "x", "platform": "1688"}, FETCH_SPY_SEARCH_DOWN="1")
    assert run.proc.returncode != 0
    assert "Traceback" not in run.log
    assert "search failed" in status_of(run)
    assert run.fetched == []


def test_keyword_search_down_still_fetches_the_ids_given(tmp_path):
    run = run_actor(
        tmp_path, {"itemIds": OK_IDS, "keyword": "x", "platform": "1688"},
        FETCH_SPY_SEARCH_DOWN="1",
    )
    assert run.proc.returncode == 0, run.log
    assert "Traceback" not in run.log
    assert sorted(run.fetched) == sorted(OK_IDS)
    assert "search failed" in status_of(run)


# ----------------------------------------------- minimum-order filter, e2e
#
# On the real capture the hits' ladders start at 1, 2 and 1 for
# 623219903269, 669724481680 and 672715568844; 44153134578 and 590362159388
# carry no ladder. A filter of 1 therefore screens out 669724481680 alone.


def test_listing_mode_leaves_out_hits_over_the_moq_filter(tmp_path):
    run = run_actor(
        tmp_path,
        {"keyword": "T恤 批发", "detailLevel": "listing", "platform": "1688",
         "maxMinOrderQuantity": 1},
        **ppe(1.0),
        ACTOR_SOURCE_FIXTURES=str(FIXTURES / "otapi-1688"),
        SOURCE_PROVIDER="otapi-1688",
    )
    assert run.proc.returncode == 0, run.log
    assert run.searched_pages == [1]
    ids = [r["itemId"] for r in run.records]
    assert "669724481680" not in ids
    assert len(ids) == 4, "hits stating no minimum are kept"
    assert "1 over the minimum-order filter (1) left out" in run.log


def test_full_detail_never_fetches_a_hit_the_search_already_ruled_out(tmp_path):
    # The point of screening at search time: a detail call is a billed call,
    # and a listing the buyer filtered out can never be sold.
    run = run_actor(
        tmp_path,
        {"keyword": "T恤 批发", "platform": "1688", "maxMinOrderQuantity": 1},
        **ppe(1.0),
        ACTOR_SOURCE_FIXTURES=str(FIXTURES / "otapi-1688"),
        SOURCE_PROVIDER="otapi-1688",
    )
    assert run.proc.returncode == 0, run.log
    assert "669724481680" not in run.fetched
    assert sorted(run.fetched) == sorted(
        ["44153134578", "623219903269", "672715568844", "590362159388"]
    )


def test_ids_given_directly_are_filtered_after_fetching_and_not_billed(tmp_path):
    ids = ["623219903269", "669724481680", "672715568844"]
    run = run_actor(
        tmp_path,
        {"itemIds": ids, "platform": "1688", "maxMinOrderQuantity": 1},
        **ppe(1.0),
        ACTOR_SOURCE_FIXTURES=str(FIXTURES / "otapi-1688"),
        SOURCE_PROVIDER="otapi-1688",
    )
    assert run.proc.returncode == 0, run.log
    assert sorted(run.fetched) == sorted(ids)
    assert [r["itemId"] for r in run.records] == ["623219903269", "672715568844"]
    assert "Returned 2 of 3 listing(s); 1 over the minimum-order filter (1) left out." in run.log


def test_without_the_filter_nothing_is_left_out(tmp_path):
    run = run_actor(
        tmp_path,
        {"keyword": "T恤 批发", "detailLevel": "listing", "platform": "1688"},
        **ppe(1.0),
        ACTOR_SOURCE_FIXTURES=str(FIXTURES / "otapi-1688"),
        SOURCE_PROVIDER="otapi-1688",
    )
    assert len(run.records) == 5
    assert "minimum-order filter" not in run.log


def test_hits_past_the_limit_are_not_reported_as_left_out(tmp_path):
    # The first hit passes and fills maxProducts=1; 669724481680, further down
    # the page, was never going to be fetched, so it was not "left out".
    run = run_actor(
        tmp_path,
        {"keyword": "T恤 批发", "platform": "1688", "maxProducts": 1, "maxMinOrderQuantity": 1},
        **ppe(1.0),
        ACTOR_SOURCE_FIXTURES=str(FIXTURES / "otapi-1688"),
        SOURCE_PROVIDER="otapi-1688",
    )
    assert run.proc.returncode == 0, run.log
    assert run.fetched == ["44153134578"]
    assert "minimum-order filter" not in run.log
