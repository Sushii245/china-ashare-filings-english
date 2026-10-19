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
            "product-normalised": {"eventTitle": "Listing", "eventPriceUsd": 0.012},
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
    # $0.03 covers two listings at $0.012. All three fit in one batch of five,
    # so a run that checks the limit only when pushing has already paid the
    # data source for the third by the time it finds out.
    run = run_actor(
        tmp_path, {"itemIds": OK_IDS, "platform": "1688", "concurrency": 5}, **ppe(0.03)
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
        tmp_path, {"itemIds": ids, "platform": "1688", "concurrency": 5}, **ppe(0.03)
    )
    assert run.proc.returncode == 0, run.log
    assert [(r["itemId"], r["status"]) for r in run.records] == [
        (REFUSED_ID, "risk_control_blocked"),
        (OK_IDS[0], "ok"),
        (OK_IDS[1], "ok"),
    ]
    assert sorted(run.fetched) == sorted([REFUSED_ID, OK_IDS[0], OK_IDS[1]])


def test_no_budget_at_all_fetches_nothing(tmp_path):
    run = run_actor(tmp_path, {"itemIds": OK_IDS, "platform": "1688"}, **ppe(0.005))
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
