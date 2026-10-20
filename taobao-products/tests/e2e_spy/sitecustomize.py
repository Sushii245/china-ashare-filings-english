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

    # Simulates a rejected key: every call raises the way the live client does
    # on a 401/403. Used by the end-to-end tests of the Actor's failure path.
    if os.environ.get("FETCH_SPY_REJECT_KEY"):

        def _rejected():
            return source.AuthenticationFailed(
                "HTTP 403: You are not subscribed to this API."
            )

        def reject_fetch(self, item_id):
            _record("fetch", item_id)
            raise _rejected()

        def reject_search(self, keyword, *, page=1):
            _record("search", str(page))
            raise _rejected()

        source.FixtureSource.fetch_product = reject_fetch
        source.FixtureSource.search_raw = reject_search

    # Simulates a search endpoint that is down: retries exhausted. Pages listed
    # in FETCH_SPY_SEARCH_DOWN fail ("1", or "2" to fail only later pages).
    _down = os.environ.get("FETCH_SPY_SEARCH_DOWN")
    if _down:
        _down_pages = {int(p) for p in _down.split(",")}
        _search_ok = source.FixtureSource.search_raw

        def search_down(self, keyword, *, page=1):
            if page in _down_pages:
                _record("search", str(page))
                raise source.SourceError(
                    "GET /BatchSearchItemsFrame failed after retries: HTTP 503 from "
                    "/BatchSearchItemsFrame: Service Unavailable"
                )
            return _search_ok(self, keyword, page=page)

        source.FixtureSource.search_raw = search_down
