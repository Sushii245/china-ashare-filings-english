# CNINFO Announcement Extractor

Turns Chinese A-share regulatory filings from **CNINFO (巨潮资讯网)** into
structured **English** records: an analyst-grade summary plus typed key figures
that each carry the Chinese sentence they came from.

Only public, login-free CNINFO endpoints are used.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # never stored in this repo
```

## Run

```bash
# What category filters exist
python main.py --list-categories

# Fetch + parse only (no model calls, no API key needed)
python main.py --codes 000001 --category dividend \
    --start 2025-01-01 --end 2025-12-31 --limit 2 --no-llm

# Full pipeline
python main.py --codes 000002 --keyword 关联交易 \
    --start 2025-01-01 --end 2025-12-31 --limit 1

# The six filings behind REPORT.md
python run_samples.py
```

Results land in `samples/<code>_<date>_<announcementId>.json`, plus a
`samples/summary.json` with per-filing token usage and cost. PDFs are cached in
`data/pdfs/` (git-ignored).

### Useful flags

| Flag | Meaning |
| --- | --- |
| `--codes` | Comma-separated 6-digit codes (`000001,600519`); omit to search the whole market |
| `--category` | Category key from `--list-categories` |
| `--keyword` | Chinese title keyword (e.g. `股份质押`) |
| `--plate sz\|sh` | Restrict to one exchange when no codes are given |
| `--model` | Claude model id (default `claude-haiku-5-5`) |
| `--effort` | `low`…`max`; the main cost/quality lever |
| `--max-chars` | Character budget of filing text sent to the model (default 24,000) |
| `--no-llm` | Fetch and parse only |
| `--min-interval` | Minimum seconds between CNINFO requests (default 1.5, floor 1.0) |

## Modules

| File | Responsibility |
| --- | --- |
| `fetch.py` | CNINFO list query, stock-code → `orgId` lookup, PDF download; rate limiting + retry with backoff |
| `parse.py` | PyMuPDF text and table extraction, scanned-PDF detection, relevance-based truncation for long filings |
| `extract.py` | Claude call with a JSON-schema-constrained response, unit normalisation, quote verification, cost accounting |
| `main.py` | CLI entry point |
| `run_samples.py` | The fixed sample set used by `REPORT.md` |

### Apify Actor (Stage 2)

| File | Responsibility |
| --- | --- |
| `.actor/actor.json` | Actor definition |
| `.actor/input_schema.json` | Input fields shown in the Apify Console |
| `.actor/dataset_schema.json` | Dataset views (overview / key figures) |
| `Dockerfile` | Build on `apify/actor-python:3.13` |
| `actor_main.py` | Actor entry point with pay-per-event charging |
| `STORE_README.md` | Apify Store listing page; copy to the Actor root as `README.md` when publishing |

Run the Actor locally:

```bash
mkdir -p storage/key_value_stores/default
cat > storage/key_value_stores/default/INPUT.json <<'JSON'
{"stockCodes":["000001"],"startDate":"2025-01-01","endDate":"2025-12-31",
 "category":"dividend","maxAnnouncements":2,"extractEnglish":true}
JSON
ACTOR_TEST_PAY_PER_EVENT=1 python -m actor_main
```

Add `ACTOR_MAX_TOTAL_CHARGE_USD=<n>` to exercise the charge-limit path. In local
test mode every event is priced at $1, so unknown event names show up in the
charging log at that price — that is a test artifact, not real billing.

Deploy with `apify login && apify push`. Per-event prices are set in the Actor's
Monetization settings, not in this repo. **Do not define
`apify-default-dataset-item` there** — it is the SDK's synthetic per-item event
and pricing it would double-charge on top of the named events.

Measured results, known problems and pricing analysis live in [`REPORT.md`](REPORT.md).
Outreach templates and channels are in [`OUTREACH.md`](OUTREACH.md).

## Output shape

```jsonc
{
  "announcement": { "stock_code": "000001", "title_cn": "...", "pdf_url": "...", "source_url": "..." },
  "pdf": { "page_count": 3, "char_count": 1628, "status": "ok", "truncated": false },
  "status": "ok",
  "extraction": {
    "stock_code": "000001",
    "company_name_cn": "平安银行股份有限公司",
    "company_name_en": null,                  // null unless printed in the filing
    "announcement_type": "dividend",
    "title_en": "...",
    "publish_date": "2025-09-30",
    "summary_en": "...",                      // 3-5 sentences
    "key_figures": [
      {
        "label": "cash dividend per 10 shares",
        "value": 2.36, "unit": "元", "currency": "CNY",
        "value_base": 2.36, "unit_base": "元",   // 万元/亿元 converted in code, not by the model
        "source_text_cn": "每10 股派发现金股利人民币2.36 元（含税）",
        "quote_in_document": true,               // quote occurs verbatim in the PDF text
        "value_in_quote": true,                  // the number occurs inside that quote
        "source_text_verified": true             // both checks passed
      }
    ],
    "risk_flags": [],
    "source_url": "...", "pdf_url": "..."
  },
  "usage": { "model": "claude-haiku-5-5", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0 }
}
```

## Design notes

- **No fabrication.** Anything the filing does not state comes back `null` or an
  empty list. `company_name_en` stays `null` unless the English name is printed.
- **Units.** The model reports the number and unit exactly as written; `万元`/`亿元`
  → `元` conversion happens in Python (`extract.normalise_key_figures`), so it is
  deterministic and auditable. Both forms are kept.
- **Auditability.** Every figure carries `source_text_cn`, and the code runs two
  independent checks on it: the quote must occur verbatim in the extracted PDF
  text (`quote_in_document`, compared after NFKC normalisation so fullwidth
  punctuation does not cause false negatives), and the number must occur inside
  that quote (`value_in_quote`). A quote that exists but does not contain the
  figure is a bad citation, and `source_text_verified` is the conjunction of both.
- **Provenance.** `source_url` / `pdf_url` are filled from the CNINFO list, never
  by the model.
- **Scanned PDFs** are detected (characters per page below `parse.SCANNED_CHARS_PER_PAGE`)
  and marked `unsupported_scanned`. No OCR in this MVP.
- **Non-PDF attachments** (pre-2005 filings are often `.html`) are marked
  `unsupported_format` and skipped.
- **Long filings** (annual reports) keep the document head plus the
  highest-scoring remaining sections inside `--max-chars`, with an explicit
  elision marker so the model knows text was removed.
- **Politeness.** At least 1.0s (default 1.5s + jitter) between CNINFO requests;
  retries use exponential backoff on 429/5xx and connection errors.

## Limits

- No OCR, so image-only filings yield no data.
- Truncation of very long filings is heuristic; figures outside the kept sections
  are not seen. For annual reports, prefer the 摘要 (summary) filing.
- CNINFO's `searchkey` matches titles, not full text.
