"""Turn one captured payload into a verdict on the field map. No network.

For each payload saved by ``capture.py`` it answers the three questions a
billed call was spent to answer:

1. Which mapped paths actually resolved, and which came back empty — those are
   the fields the buyer would silently lose.
2. What is in the payload that the map does not read yet — candidates worth
   adding, printed with their value so you can judge.
3. What record a buyer would receive, so the normalisation is judged on real
   data rather than on fixtures I wrote.

    python audit.py tests/fixtures/live --provider otapi-1688
    python audit.py tests/fixtures/live/612345678901.json --provider otapi-1688 --full

Always pass ``--provider`` for a provider's capture: without it the payload is
read with the default query-string map, and every field reads as missing for
reasons that have nothing to do with the provider.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

import normalize as normalize_mod
import source as source_mod

#: Paths deeper than this are summarised rather than listed one by one.
MAX_DEPTH = 4
#: Values longer than this are elided in the listing.
MAX_VALUE = 72


def leaf_paths(node: Any, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, Any]]:
    """Walk a payload, yielding (dotted path, value) for each leaf.

    Lists are reported at index 0 only: a 40-variant SKU array says the same
    thing forty times, and the point here is the shape, not the contents.
    """
    if depth >= MAX_DEPTH:
        yield prefix, node
        return
    if isinstance(node, dict):
        if not node:
            yield prefix, {}
            return
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from leaf_paths(value, child, depth + 1)
    elif isinstance(node, (list, tuple)):
        if not node:
            yield prefix, []
            return
        yield from leaf_paths(node[0], f"{prefix}[0]", depth + 1)
    else:
        yield prefix, node


def short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text if len(text) <= MAX_VALUE else text[: MAX_VALUE - 1] + "…"


def audit_search(path: Path, config: source_mod.SourceConfig) -> None:
    """Show where a captured search response keeps its hits."""
    data = json.loads(path.read_text(encoding="utf-8"))
    payload = data.get("payload") if isinstance(data, dict) else None
    print(f"\n{'='*74}\n{path.name}   keyword={data.get('keyword')!r}\n{'='*74}")
    if payload is None:
        print("  no raw payload saved — re-capture with the current capture.py")
        return

    print("\n-- configured hit-list paths " + "-"*45)
    for candidate in config.search_items_paths or ("(none)",):
        value = source_mod.dig(payload, candidate) if candidate != "(none)" else None
        kind = (
            f"list of {len(value)}" if isinstance(value, list)
            else type(value).__name__ if value is not None else "absent"
        )
        print(f"  {candidate:<40} {kind}")

    print("\n-- every list of objects in the response (where the hits may be) " + "-"*8)
    found = False
    for dotted, value in _lists_of_dicts(payload):
        found = True
        print(f"  {dotted:<40} {len(value)} item(s); first keys: "
              f"{', '.join(list(value[0])[:8])}")
    if not found:
        print("  (none)")

    hits = source_mod._hits_from_search_payload(payload, config)
    if hits:
        print("\n-- first hit, mapped " + "-"*53)
        for name, dotted in sorted(config.search_field_map.items()):
            value = source_mod.dig(hits[0], dotted) if dotted else None
            print(f"  {name:<20} {dotted:<28} {short(value) if value not in (None, '') else 'MISSING'}")
        print(f"\n-- ids read: {source_mod._ids_from_hits(hits, config)[:10]}")


def _lists_of_dicts(node: Any, prefix: str = "", depth: int = 0):
    if depth > 6:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _lists_of_dicts(value, f"{prefix}.{key}" if prefix else str(key), depth + 1)
    elif isinstance(node, list) and node and all(isinstance(v, dict) for v in node):
        yield prefix, node


def audit_one(
    path: Path, *, config: source_mod.SourceConfig, full: bool = False
) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "payload" in data:
        payload = data.get("payload")
        status = data.get("status", "ok")
        platform = data.get("platform", "taobao")
    else:
        payload, status, platform = data, "ok", "taobao"

    print(f"\n{'='*74}\n{path.name}   status={status}   platform={platform}\n{'='*74}")

    if payload is None:
        print("  no payload — nothing to audit (this is a refusal or an error case)")
        print(f"  statusDetail: {data.get('statusDetail')}")
        return 0, 0

    field_map = config.field_map

    print("\n-- mapped fields " + "-"*57)
    resolved = missing = 0
    for name, dotted in sorted(field_map.items()):
        if not dotted:
            print(f"  {name:<20} {'(unmapped)':<28} -")
            continue
        value = source_mod.dig(payload, dotted)
        if value in (None, "", [], {}):
            print(f"  {name:<20} {dotted:<28} MISSING")
            missing += 1
        else:
            print(f"  {name:<20} {dotted:<28} {short(value)}")
            resolved += 1

    mapped_prefixes = {v for v in field_map.values() if v}
    print("\n-- in the payload but not read " + "-"*43)
    unread = []
    for dotted, value in leaf_paths(payload):
        bare = dotted.replace("[0]", "")
        if any(bare == m or bare.startswith(m + ".") for m in mapped_prefixes):
            continue
        if value in (None, "", [], {}):
            continue
        unread.append((dotted, value))
    if not unread:
        print("  (nothing)")
    else:
        for dotted, value in unread if full else unread[:25]:
            print(f"  {dotted:<42} {short(value)}")
        if not full and len(unread) > 25:
            print(f"  … and {len(unread) - 25} more (pass --full)")

    print("\n-- what the buyer would receive " + "-"*42)
    raw = source_mod.RawProduct(
        item_id=path.stem, platform=platform, payload=payload, status=status
    )
    record = normalize_mod.normalize(raw, config=config, quantity=100).to_item()
    interesting = (
        "status", "titleCn", "price", "originalPrice", "discountPct", "sales",
        "stock", "shipping", "minOrderQuantity", "priceTiers", "quote",
        "shopScores", "skuCount", "isPresale", "attributesCn",
    )
    for key in interesting:
        print(f"  {key:<18} {short(record.get(key))}")

    if not field_map:
        print("\n-- the field map is empty: every field above is unread by design. "
              "Write the map\n   from the 'not read' list, then re-run.")
    print(f"\n-- verdict: {resolved} mapped path(s) resolved, {missing} missing, "
          f"{len(unread)} unread key(s) in the payload")
    return resolved, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="a captured .json file, or a directory of them")
    parser.add_argument("--full", action="store_true", help="list every unread key")
    parser.add_argument(
        "--provider", choices=sorted(source_mod.PROVIDER_PRESETS),
        help="read the payloads with this provider preset's maps",
    )
    args = parser.parse_args(argv)
    config = (
        source_mod.PROVIDER_PRESETS[args.provider]("")
        if args.provider
        else source_mod.SourceConfig(base_url="")
    )

    target = Path(args.target)
    if target.is_dir():
        files = sorted(p for p in target.glob("*.json") if not p.name.startswith("_"))
        search = target / "_search.json"
    else:
        files = [target] if not target.name.startswith("_") else []
        search = target if target.name == "_search.json" else None
    if search is not None and search.exists():
        audit_search(search, config)
    if not files:
        if search is None or not search.exists():
            print(f"no captured payloads under {target}", file=sys.stderr)
            return 2
        return 0

    totals = [audit_one(p, config=config, full=args.full) for p in files]
    ok = sum(r for r, _ in totals)
    bad = sum(m for _, m in totals)
    print(f"\n{'='*74}\n{len(files)} payload(s): {ok} resolved, {bad} missing in total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
