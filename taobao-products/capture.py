"""Spend one provider call per listing and keep the payload forever.

Calls are billed, so the rule for validation is: fetch each distinct case
once, write the raw response to disk, and do every subsequent experiment
against the saved copy. Nothing in this script retries a billed call by
default, and it refuses to overwrite a payload you already paid for unless you
say so.

    python capture.py --base-url https://api-gw.onebound.cn \
        --api-key KEY --api-secret SECRET \
        --platform taobao 600530677383 612345678901

Saved files land in ``tests/fixtures/live/`` and are readable by
``FixtureSource``, so the test suite and ``audit.py`` can replay them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import source as source_mod

LOG = logging.getLogger("capture")

DEFAULT_DIR = Path(__file__).parent / "tests" / "fixtures" / "live"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item_ids", nargs="*", help="listing ids to capture")
    parser.add_argument(
        "--provider", choices=sorted(source_mod.PROVIDER_PRESETS),
        help="use a built-in provider preset instead of --base-url (handles that "
             "provider's auth style, paging and required parameters)",
    )
    parser.add_argument("--base-url", default=os.environ.get("SOURCE_BASE_URL"))
    parser.add_argument("--api-key", default=os.environ.get("SOURCE_API_KEY"))
    parser.add_argument("--api-secret", default=os.environ.get("SOURCE_API_SECRET"))
    parser.add_argument(
        "--platform", choices=("taobao", "tmall", "1688"),
        help="defaults to the provider's own platform; taobao without --provider",
    )
    parser.add_argument("--detail-path", help="override the detail endpoint path")
    parser.add_argument(
        "--search", metavar="KEYWORD",
        help="also capture one page of search results (one extra billed call)",
    )
    parser.add_argument("--lang", help="pass a lang parameter, e.g. en")
    parser.add_argument(
        "--cache", default="yes", choices=("yes", "no"),
        help="provider-side cache. Keep 'yes' while validating: a repeat of the "
             "same id may be served from their cache instead of billing again.",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_DIR))
    parser.add_argument(
        "--retries", type=int, default=0,
        help="retries on a 5xx. Zero by default: a retry is another billed call.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="re-fetch ids whose payload is already on disk",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.api_key:
        print("need --api-key (or SOURCE_API_KEY)", file=sys.stderr)
        return 2
    if not args.provider and not args.base_url:
        print(
            "need --provider, or --base-url (or SOURCE_BASE_URL)",
            file=sys.stderr,
        )
        return 2

    if args.provider:
        config = source_mod.PROVIDER_PRESETS[args.provider](args.api_key)
        config.max_retries = args.retries
    else:
        config = source_mod.SourceConfig(
            base_url=args.base_url,
            api_key=args.api_key,
            api_secret=args.api_secret or None,
            cache=args.cache,
            lang=args.lang,
            max_retries=args.retries,
        )
        if (args.platform or "taobao") == "1688":
            config.detail_path = "/1688/item_get"
            config.search_path = "/1688/item_search"
    config.min_interval = 1.0      # unhurried: this is a one-off, not a run
    if args.detail_path:
        config.detail_path = args.detail_path

    # A capture labelled with the wrong platform replays with the wrong URLs,
    # so the provider's own platform is the default, and a mismatch is refused.
    platform = args.platform or (config.platforms[0] if args.provider else "taobao")
    if platform not in config.platforms:
        print(
            f"{args.provider or 'this provider'} serves {', '.join(config.platforms)}, "
            f"not {platform}",
            file=sys.stderr,
        )
        return 2
    client = source_mod.UpstreamSource(config, platform=platform)

    # Created only once the run is known to go ahead, so a refused run does
    # not leave an empty capture directory that reads like a failed capture.
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    spent = 0
    for item_id in args.item_ids:
        target = out_dir / f"{item_id}.json"
        if target.exists() and not args.force:
            LOG.info("%s already captured, skipping (use --force to re-fetch)", item_id)
            continue

        try:
            raw = client.fetch_product(item_id)
        except source_mod.AuthenticationFailed as exc:
            # Every remaining id would fail the same way.
            print(f"the data source rejected the key: {exc}", file=sys.stderr)
            return 2
        spent += 1

        # Saved in the wrapper form so a refusal is replayable too.
        target.write_text(
            json.dumps(
                {
                    "platform": raw.platform,
                    "status": raw.status,
                    "statusDetail": raw.status_detail,
                    "payload": raw.payload,
                },
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        LOG.info("%s -> %s (%s)", item_id, target.name, raw.status)

    if args.search:
        target = out_dir / "_search.json"
        if target.exists() and not args.force:
            LOG.info("search already captured, skipping (use --force to re-fetch)")
        else:
            try:
                # The raw response, not just the ids: the first search against
                # a new provider is the call that shows where its hits live and
                # what each one carries. Keeping only parsed ids — which, with
                # an unaudited map, may well be none — would throw that away.
                payload = client.search_response(args.search)
                spent += 1
                hits = source_mod._hits_from_search_payload(payload, config)
                ids = source_mod._ids_from_hits(hits, config)
                target.write_text(
                    json.dumps(
                        {
                            "keyword": args.search,
                            "provider": args.provider,
                            "payload": payload,
                            "ids": ids,
                        },
                        ensure_ascii=False,
                        indent=1,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                LOG.info(
                    "search %r -> %d hit(s) found by the current paths -> %s",
                    args.search,
                    len(hits),
                    target.name,
                )
                if not hits:
                    LOG.info(
                        "no hits at the configured paths; audit.py will show "
                        "where they actually are"
                    )
            except source_mod.SourceError as exc:
                LOG.error("search failed: %s", exc)

    LOG.info("billed calls this run: %d", spent)
    LOG.info("now run: python audit.py %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
