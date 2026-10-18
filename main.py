#!/usr/bin/env python3
"""CLI: CNINFO announcement -> structured English JSON.

Examples
--------
List the supported category filters::

    python main.py --list-categories

Fetch and parse only (no model calls, no API key needed)::

    python main.py --codes 000001 --start 2025-01-01 --end 2025-12-31 \
        --category dividend --limit 2 --no-llm

Full pipeline (needs ANTHROPIC_API_KEY)::

    python main.py --codes 000002 --start 2025-01-01 --end 2025-12-31 \
        --keyword 关联交易 --limit 1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import extract as extract_mod
import fetch as fetch_mod
import parse as parse_mod

LOG = logging.getLogger("cninfo")


def load_dotenv(path: str | Path = ".env") -> None:
    """Load ``KEY=value`` lines from a project-local ``.env`` into the environment.

    Project config wins over inherited shell variables: the host shell may
    already export ``ANTHROPIC_BASE_URL``, and silently keeping it would send
    requests to the wrong endpoint. Overridden key *names* are logged; values
    never are.
    """
    env_file = Path(path)
    if not env_file.exists():
        return
    loaded, overridden = [], []
    for raw in env_file.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if os.environ.get(key) not in (None, value):
            overridden.append(key)
        os.environ[key] = value
        loaded.append(key)
    if loaded:
        LOG.info(
            "loaded %d key(s) from %s%s",
            len(loaded),
            env_file,
            f" (overriding shell value for: {', '.join(overridden)})" if overridden else "",
        )


def build_parser() -> argparse.ArgumentParser:
    today = date.today()
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Extract structured English data from Chinese A-share announcements (CNINFO).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--codes", help="Comma-separated 6-digit A-share codes, e.g. 000001,600519")
    parser.add_argument("--start", default=str(today - timedelta(days=365)), help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=str(today), help="End date YYYY-MM-DD")
    parser.add_argument("--keyword", help="Title keyword filter (Chinese), e.g. 股权质押")
    parser.add_argument(
        "--category",
        help=f"Category filter. Keys: {', '.join(sorted(fetch_mod.CATEGORIES))}",
    )
    parser.add_argument("--plate", choices=["sz", "sh"], help="Restrict to one exchange when no codes are given")
    parser.add_argument("--limit", type=int, default=5, help="Max announcements to process")

    parser.add_argument("--model", default=extract_mod.DEFAULT_MODEL, help="Claude model id")
    parser.add_argument(
        "--effort",
        default="low",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort (cost/quality lever)",
    )
    parser.add_argument("--max-tokens", type=int, default=4096, help="Max output tokens per extraction")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=parse_mod.DEFAULT_MAX_CHARS,
        help="Character budget of filing text sent to the model",
    )
    parser.add_argument("--no-llm", action="store_true", help="Fetch and parse only; skip Claude")

    parser.add_argument("--out", default="samples", help="Directory for result JSON")
    parser.add_argument("--pdf-dir", default="data/pdfs", help="Directory for downloaded PDFs")
    parser.add_argument("--cache-dir", default=".cache", help="Directory for the stock-code index cache")
    parser.add_argument("--min-interval", type=float, default=1.5, help="Minimum seconds between CNINFO requests")
    parser.add_argument("--list-categories", action="store_true", help="Print category keys and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser


def process_one(
    ann: fetch_mod.Announcement,
    *,
    client: fetch_mod.CninfoClient,
    extractor: extract_mod.ClaudeExtractor | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    record: dict[str, Any] = {"announcement": ann.to_dict()}

    if not ann.is_pdf:
        record["status"] = "unsupported_format"
        record["extraction"] = None
        LOG.warning("%s: attachment is %s, not PDF - skipped", ann.slug, ann.adjunct_type)
        return record

    pdf_path = client.download_pdf(ann, args.pdf_dir)
    parsed = parse_mod.parse_pdf(pdf_path)
    record["pdf"] = parsed.to_dict()

    if parsed.status != "ok":
        record["status"] = parsed.status
        record["extraction"] = None
        LOG.warning("%s: skipped (%s)", ann.slug, parsed.status)
        return record

    prompt_text, truncated = parse_mod.build_prompt_text(parsed, max_chars=args.max_chars)
    record["pdf"]["prompt_chars"] = len(prompt_text)
    record["pdf"]["truncated"] = truncated

    if extractor is None:
        record["status"] = "parsed_only"
        record["extraction"] = None
        return record

    try:
        result = extractor.extract(
            announcement_text=prompt_text,
            title_cn=ann.title_cn,
            stock_code=ann.stock_code,
            stock_name=ann.stock_name,
            publish_date=ann.publish_date,
            source_url=ann.source_url,
            pdf_url=ann.pdf_url,
            truncated=truncated,
        )
    except Exception as exc:  # one bad filing must not kill the batch
        LOG.error("%s: extraction failed: %s", ann.slug, exc)
        record["status"] = "extraction_failed"
        record["error"] = str(exc)
        record["extraction"] = None
        return record

    record["status"] = "ok"
    record["extraction"] = result.data
    record["usage"] = result.usage_dict()
    return record


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-filing records into the summary written to ``summary.json``."""
    usages = [r.get("usage") or {} for r in records]
    return {
        "count": len(records),
        "ok": sum(1 for r in records if r.get("status") == "ok"),
        "skipped": [
            {
                "announcement_id": r["announcement"]["announcement_id"],
                "status": r.get("status"),
                "error": r.get("error"),
            }
            for r in records
            if r.get("status") != "ok"
        ],
        "total_cost_usd": round(sum(u.get("cost_usd", 0.0) for u in usages), 6),
        "total_input_tokens": sum(u.get("input_tokens", 0) for u in usages),
        "total_output_tokens": sum(u.get("output_tokens", 0) for u in usages),
        "items": [
            {
                "stock_code": r["announcement"]["stock_code"],
                "stock_name": r["announcement"]["stock_name"],
                "title_cn": r["announcement"]["title_cn"],
                "publish_date": r["announcement"]["publish_date"],
                "pdf_url": r["announcement"]["pdf_url"],
                "source_url": r["announcement"]["source_url"],
                "status": r.get("status"),
                "announcement_type": (r.get("extraction") or {}).get("announcement_type"),
                "pages": (r.get("pdf") or {}).get("page_count"),
                "pdf_chars": (r.get("pdf") or {}).get("char_count"),
                "prompt_chars": (r.get("pdf") or {}).get("prompt_chars"),
                "truncated": (r.get("pdf") or {}).get("truncated"),
                "key_figure_count": len((r.get("extraction") or {}).get("key_figures") or []),
                "usage": r.get("usage"),
            }
            for r in records
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    load_dotenv()

    if args.list_categories:
        for key, code in sorted(fetch_mod.CATEGORIES.items()):
            print(f"{key:24s} {code}")
        return 0

    client = fetch_mod.CninfoClient(min_interval=args.min_interval, cache_dir=args.cache_dir)

    extractor: extract_mod.ClaudeExtractor | None = None
    if not args.no_llm:
        try:
            extractor = extract_mod.ClaudeExtractor(
                model=args.model, effort=args.effort, max_tokens=args.max_tokens
            )
        except RuntimeError as exc:
            LOG.error("%s", exc)
            return 2

    codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
    try:
        announcements = client.query(
            codes=codes,
            start=args.start,
            end=args.end,
            keyword=args.keyword,
            category=args.category,
            plate=args.plate,
            limit=args.limit,
        )
    except (fetch_mod.CninfoError, ValueError) as exc:
        LOG.error("CNINFO query failed: %s", exc)
        return 1

    if not announcements:
        LOG.warning("no announcements matched the filters")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for index, ann in enumerate(announcements, start=1):
        LOG.info("[%d/%d] %s %s %s", index, len(announcements), ann.publish_date, ann.stock_code, ann.title_cn)
        try:
            record = process_one(ann, client=client, extractor=extractor, args=args)
        except fetch_mod.CninfoError as exc:
            LOG.error("%s: download failed: %s", ann.slug, exc)
            record = {"announcement": ann.to_dict(), "status": "download_failed", "error": str(exc)}
        records.append(record)
        (out_dir / f"{ann.slug}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )

    summary = build_summary(records)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")

    LOG.info(
        "done: %d/%d extracted, %d in / %d out tokens, $%.5f total",
        summary["ok"], summary["count"],
        summary["total_input_tokens"], summary["total_output_tokens"], summary["total_cost_usd"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
