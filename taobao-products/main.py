"""Local CLI, for running the pipeline outside Apify.

Against the recorded payloads, which needs no credential::

    python main.py --fixtures tests/fixtures

Against a provider::

    python main.py --base-url https://… --api-key … 600530677383 612345678901
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import normalize as normalize_mod
import source as source_mod


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item_ids", nargs="*", help="listing ids to fetch")
    parser.add_argument("--fixtures", help="directory of recorded payloads")
    parser.add_argument(
        "--provider", choices=sorted(source_mod.PROVIDER_PRESETS),
        default=os.environ.get("SOURCE_PROVIDER") or None,
        help="a built-in provider preset; with --fixtures, replays through its maps",
    )
    parser.add_argument("--base-url", default=os.environ.get("SOURCE_BASE_URL"))
    parser.add_argument("--api-key", default=os.environ.get("SOURCE_API_KEY"))
    parser.add_argument(
        "--platform", choices=("taobao", "tmall", "1688"),
        help="defaults to the provider's own platform; taobao without --provider",
    )
    parser.add_argument("--keyword", help="search instead of naming ids")
    parser.add_argument(
        "--image-url",
        help="search by picture instead: a public http(s) product photo URL",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--quantity", type=int,
        help="price each listing at this many pieces (walks the 阶梯价 ladder)",
    )
    parser.add_argument("--out", help="write JSON here instead of stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    preset = (
        source_mod.PROVIDER_PRESETS[args.provider](args.api_key or "")
        if args.provider
        else None
    )
    platform = args.platform or (preset.platforms[0] if preset else "taobao")

    if args.fixtures:
        data_source: source_mod.ProductSource = source_mod.FixtureSource(
            args.fixtures, platform=platform, config=preset
        )
    elif preset is not None:
        if not args.api_key:
            print(f"--provider {args.provider} needs --api-key", file=sys.stderr)
            return 2
        data_source = source_mod.UpstreamSource(preset, platform=platform)
    elif args.base_url:
        data_source = source_mod.UpstreamSource(
            source_mod.SourceConfig(base_url=args.base_url, api_key=args.api_key),
            platform=platform,
        )
    else:
        print(
            "no data source: pass --fixtures, --provider with --api-key, or "
            "--base-url with --api-key",
            file=sys.stderr,
        )
        return 2

    if platform not in data_source.config.platforms:
        print(
            f"this data source serves {', '.join(data_source.config.platforms)}, "
            f"not {platform}",
            file=sys.stderr,
        )
        return 2

    item_ids = list(args.item_ids)
    if args.image_url:
        item_ids.extend(data_source.search(image_url=args.image_url))
    elif args.keyword:
        item_ids.extend(data_source.search(args.keyword))
    if not item_ids and args.fixtures:
        item_ids = data_source.search("")
    item_ids = item_ids[: args.limit]

    if not item_ids:
        print("nothing to fetch", file=sys.stderr)
        return 2

    records = []
    for item_id in item_ids:
        product = normalize_mod.normalize(
            data_source.fetch_product(item_id),
            config=data_source.config,
            quantity=args.quantity,
        )
        logging.info("%s -> %s", item_id, product.status)
        records.append(product.to_item())

    text = json.dumps(records, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        logging.info("wrote %d record(s) to %s", len(records), args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
