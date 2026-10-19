"""Count the data-source calls an end-to-end Actor run makes.

Python imports a ``sitecustomize`` module found on sys.path at start-up, so
putting this directory on PYTHONPATH for a subprocess wraps the fixture
source's two call points before the Actor imports it. Each call is appended to
the file named by FETCH_SPY_LOG. Only the tests set that; nothing in the Actor
knows this exists.
"""

import os

_LOG = os.environ.get("FETCH_SPY_LOG")

if _LOG:
    import source

    def _record(kind: str, value: str) -> None:
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{kind}\t{value}\n")

    _fetch = source.FixtureSource.fetch_product
    _search = source.FixtureSource.search_raw

    def fetch_product(self, item_id):
        _record("fetch", item_id)
        return _fetch(self, item_id)

    def search_raw(self, keyword, *, page=1):
        _record("search", str(page))
        return _search(self, keyword, page=page)

    source.FixtureSource.fetch_product = fetch_product
    source.FixtureSource.search_raw = search_raw
