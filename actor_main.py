"""Apify Actor entry point.

Wraps the Stage 1 pipeline (``fetch`` -> ``parse`` -> ``extract``) in the Apify
Actor lifecycle and charges pay-per-event.

Chargeable events (prices are configured in the Actor's Monetization settings,
not here):

``actor-start``                   once per run
``announcement-listed``           a filing returned as metadata only
``announcement-processed``        a filing extracted into English
``announcement-processed-long``   same, for filings above ``LONG_FILING_PAGES``

Run it locally with::

    ACTOR_TEST_PAY_PER_EVENT=1 python -m actor_main
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from apify import Actor

import extract as extract_mod
import fetch as fetch_mod
import parse as parse_mod

#: Filings at or above this page count bill at the long-filing rate.
LONG_FILING_PAGES = 50

EVENT_START = "actor-start"
EVENT_LISTED = "announcement-listed"
EVENT_PROCESSED = "announcement-processed"
EVENT_PROCESSED_LONG = "announcement-processed-long"


def _to_camel_record(
    ann: fetch_mod.Announcement,
    parsed: parse_mod.ParsedPdf | None,
    extraction: dict[str, Any] | None,
    status: str,
    model: str | None,
) -> dict[str, Any]:
    """Build one flat dataset item.

    Per-run cost accounting stays out of the dataset - it is the operator's
    business, not output the customer paid for.
    """
    extraction = extraction or {}
    return {
        "stockCode": ann.stock_code,
        "companyNameCn": extraction.get("company_name_cn") or ann.stock_name,
        "companyNameEn": extraction.get("company_name_en"),
        "announcementType": extraction.get("announcement_type"),
        "titleCn": ann.title_cn,
        "titleEn": extraction.get("title_en"),
        "publishDate": ann.publish_date,
        "summaryEn": extraction.get("summary_en"),
        "keyFigures": [
            {
                "label": figure.get("label"),
                "value": figure.get("value"),
                "unit": figure.get("unit"),
                "currency": figure.get("currency"),
                "valueBase": figure.get("value_base"),
                "unitBase": figure.get("unit_base"),
                "sourceTextCn": figure.get("source_text_cn"),
                "sourceTextVerified": figure.get("source_text_verified"),
                "sourceTextRecovered": figure.get("source_text_recovered"),
            }
            for figure in extraction.get("key_figures") or []
        ],
        "riskFlags": extraction.get("risk_flags") or [],
        "pdfUrl": ann.pdf_url,
        "sourceUrl": ann.source_url,
        "announcementId": ann.announcement_id,
        "status": status,
        "processing": {
            "pages": parsed.page_count if parsed else None,
            "scanned": parsed.is_scanned if parsed else None,
            "truncated": extraction.get("_truncated"),
            "model": model,
        },
    }


def _process_blocking(
    ann: fetch_mod.Announcement,
    *,
    client: fetch_mod.CninfoClient,
    extractor: extract_mod.ClaudeExtractor | None,
    pdf_dir: str,
    max_chars: int,
) -> tuple[parse_mod.ParsedPdf | None, dict[str, Any] | None, str]:
    """Download, parse and extract one filing. Runs off the event loop."""
    if extractor is None:
        # Metadata-only tier: never download the PDF. The listing already has
        # everything this tier returns, and the customer is not paying for it.
        return None, None, "listed_only"

    if not ann.is_pdf:
        return None, None, "unsupported_format"

    pdf_path = client.download_pdf(ann, pdf_dir)
    parsed = parse_mod.parse_pdf(pdf_path)
    if parsed.status != "ok":
        return parsed, None, parsed.status

    text, truncated = parse_mod.build_prompt_text(parsed, max_chars=max_chars)
    result = extractor.extract(
        announcement_text=text,
        title_cn=ann.title_cn,
        stock_code=ann.stock_code,
        stock_name=ann.stock_name,
        publish_date=ann.publish_date,
        source_url=ann.source_url,
        pdf_url=ann.pdf_url,
        truncated=truncated,
    )
    data = dict(result.data)
    data["_truncated"] = truncated
    data["_usage"] = result.usage_dict()
    return parsed, data, "ok"


def _event_for(parsed: parse_mod.ParsedPdf | None, extracted: bool) -> str:
    if not extracted:
        return EVENT_LISTED
    if parsed and parsed.page_count >= LONG_FILING_PAGES:
        return EVENT_PROCESSED_LONG
    return EVENT_PROCESSED


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        start = actor_input.get("startDate")
        end = actor_input.get("endDate")
        if not start or not end:
            await Actor.fail(status_message="startDate and endDate are required.")
            return

        codes = [c.strip() for c in (actor_input.get("stockCodes") or []) if c and c.strip()]
        category = (actor_input.get("category") or "").strip() or None
        keyword = (actor_input.get("keyword") or "").strip() or None
        limit = int(actor_input.get("maxAnnouncements") or 10)
        extract_english = bool(actor_input.get("extractEnglish", True))
        model = actor_input.get("model") or extract_mod.DEFAULT_MODEL
        max_chars = int(actor_input.get("maxChars") or parse_mod.DEFAULT_MAX_CHARS)
        min_interval = max(1.0, float(actor_input.get("minRequestInterval") or 2))

        user_key = (actor_input.get("anthropicApiKey") or "").strip()
        if user_key:
            os.environ["ANTHROPIC_API_KEY"] = user_key
            Actor.log.info("Using the API key supplied in the input.")

        # A non-monetised Actor (and any non-PPE pricing model) charges nothing,
        # so `charged_count` is always 0 there and must not be read as "limit
        # reached" - that would stop the run after the first item.
        pricing = Actor.get_charging_manager().get_pricing_info()
        is_ppe = bool(pricing.is_pay_per_event)
        Actor.log.info(
            "Pricing model: %s (pay-per-event: %s)",
            pricing.pricing_model or "none", is_ppe,
        )

        await Actor.charge(EVENT_START)

        extractor: extract_mod.ClaudeExtractor | None = None
        if extract_english:
            try:
                extractor = extract_mod.ClaudeExtractor(model=model, effort="low")
            except RuntimeError as exc:
                await Actor.fail(status_message=f"Extraction is unavailable: {exc}")
                return

        client = fetch_mod.CninfoClient(min_interval=min_interval, cache_dir="/tmp/cninfo-cache")

        try:
            announcements = await asyncio.to_thread(
                client.query,
                codes=codes or None,
                start=start,
                end=end,
                keyword=keyword,
                category=category,
                limit=limit,
            )
        except (fetch_mod.CninfoError, ValueError) as exc:
            await Actor.fail(status_message=f"CNINFO query failed: {exc}")
            return

        if not announcements:
            await Actor.set_status_message(
                "No announcements matched the filters.", is_terminal=True
            )
            return

        Actor.log.info("CNINFO returned %d announcement(s).", len(announcements))

        processed = failed = skipped = 0
        hit_charge_limit = False
        for index, ann in enumerate(announcements, start=1):
            Actor.log.info(
                "[%d/%d] %s %s %s", index, len(announcements),
                ann.publish_date, ann.stock_code, ann.title_cn,
            )
            try:
                parsed, extraction, status = await asyncio.to_thread(
                    _process_blocking,
                    ann,
                    client=client,
                    extractor=extractor,
                    pdf_dir="/tmp/cninfo-pdfs",
                    max_chars=max_chars,
                )
            except Exception as exc:  # one bad filing must not kill the run
                Actor.log.warning("%s failed: %s", ann.announcement_id, exc)
                parsed, extraction, status = None, None, "failed"
                failed += 1

            record = _to_camel_record(ann, parsed, extraction, status, model if extraction else None)
            usage = (extraction or {}).pop("_usage", None)
            if usage:
                Actor.log.info(
                    "  %s tokens in/out %s/%s",
                    usage["model"], usage["input_tokens"], usage["output_tokens"],
                )

            # Only successful work is billed.
            if status in ("ok", "listed_only"):
                event = _event_for(parsed, extracted=status == "ok")
                charge = await Actor.push_data(record, charged_event_name=event)
                if is_ppe and charge.charged_count == 0:
                    # The run's charge limit no longer covers another item, so
                    # nothing was stored either. `event_charge_limit_reached`
                    # alone is not enough: it is computed from this event's own
                    # price and ignores the synthetic default-dataset event
                    # added on top, so it can still read False while pushes
                    # already store nothing. Continuing here would keep paying
                    # for model calls whose output can never be delivered.
                    Actor.log.warning(
                        "Run charge limit reached after %d filing(s); stopping early. "
                        "Raise the run's max total charge to process the rest.",
                        processed,
                    )
                    hit_charge_limit = True
                    break
                if charge.charged_count > 0 or not is_ppe:
                    processed += 1
                if is_ppe and charge.event_charge_limit_reached:
                    Actor.log.warning(
                        "Run charge limit reached after %d filing(s); stopping early.", processed
                    )
                    hit_charge_limit = True
                    break
            else:
                await Actor.push_data(record)
                skipped += 1
                Actor.log.warning("  not billed (%s)", status)

        parts = [f"Returned {processed} of {len(announcements)} matching filing(s)"]
        if skipped:
            parts.append(f"{skipped} skipped as unsupported (scanned or non-PDF), not billed")
        if failed:
            parts.append(f"{failed} failed")
        if hit_charge_limit:
            parts.append("stopped at the run's charge limit")
        await Actor.set_status_message("; ".join(parts) + ".", is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
