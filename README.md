# China A-Share Filings to English — CNINFO Announcements API

**Chinese stock announcements, returned as English summaries and structured, source-cited financial data.**

Every listed company in China publishes its regulatory filings to **CNINFO (巨潮资讯网)**, the official disclosure platform of the Shanghai and Shenzhen stock exchanges — in Chinese, as PDFs. Existing CNINFO scrapers hand you a title, a category and a PDF link, and you still cannot read any of it.

This Actor goes the rest of the way: it downloads the filing, reads it, and returns an **English summary plus typed key figures** — and **every number carries the Chinese sentence it came from**, so your analysts can verify it without reading the whole document.

---

## What you get per filing

```json
{
  "stockCode": "000001",
  "companyNameCn": "平安银行",
  "announcementType": "dividend",
  "titleCn": "2025年半年度权益分派实施公告",
  "titleEn": "Ping An Bank 2025 Interim Equity Distribution Implementation Announcement",
  "publishDate": "2025-09-30",
  "summaryEn": "Ping An Bank will distribute a 2025 interim cash dividend of RMB 2.36 per 10 shares (tax inclusive), totalling RMB 4.58 billion, based on 19,405,918,198 shares outstanding as of 30 June 2025. The equity record date is 14 October 2025 and the ex-dividend date is 15 October 2025...",
  "keyFigures": [
    {
      "label": "cash dividend per 10 shares (pre-tax)",
      "value": 2.36,
      "unit": "元",
      "currency": "CNY",
      "valueBase": 2.36,
      "unitBase": "元",
      "sourceTextCn": "每10 股派发现金股利人民币2.36 元（含税）",
      "sourceTextVerified": true
    },
    {
      "label": "total cash dividend",
      "value": 45.8,
      "unit": "亿元",
      "currency": "CNY",
      "valueBase": 4580000000.0,
      "unitBase": "元",
      "sourceTextCn": "合计派发现金股利人民币45.80亿元",
      "sourceTextVerified": true
    }
  ],
  "riskFlags": [],
  "pdfUrl": "http://static.cninfo.com.cn/finalpage/2025-09-30/1224693497.PDF",
  "sourceUrl": "http://www.cninfo.com.cn/new/disclosure/detail?stockCode=000001&..."
}
```

---

## Why this is different from a CNINFO scraper

| | Typical CNINFO scraper | This Actor |
|---|---|---|
| Title | Chinese | Chinese and English |
| Content | A PDF link | English summary written for professional investors |
| Numbers | None | Typed figures with unit, currency and normalised value |
| 万元 / 亿元 | Your problem | Converted in code, both forms kept |
| Verification | None | Each figure's source sentence, checked against the PDF |
| Filing type | Chinese category codes | Seven normalised English types |

## About the citations

Models misread documents. So every figure comes back with `sourceTextCn`, the
Chinese sentence it was taken from, and the code runs two checks on that
sentence: does it appear verbatim in the PDF, and does the number appear inside
it? Both have to pass before `sourceTextVerified` is true.

Two things I found while building this, which tell you what the checks are
actually for:

The first version only checked that the sentence existed somewhere in the
document. On a Vanke shareholder-loan filing it happily reported RMB 1.666
billion and cited a sentence that did not contain that number anywhere — the
model had cut the sentence short before reaching the figure. The check passed.
That is why the second check exists, and why a truncated citation now gets
repaired in code: the extractor finds where the number actually sits in the PDF
and pulls the real clause around it.

The second was dumber and took longer to find. Four citations kept failing as
"not in document" when they plainly were. The model was writing `3．` with a
fullwidth period where the PDF had `3.`. Same sentence, different byte. The
comparison is NFKC-normalised now.

Current state on the benchmark set: 35 figures across 6 filings, 35 pass both
checks.

## Typical uses

- **Equity research on China** — read today's A-share disclosures without a Mandarin-speaking analyst.
- **Event-driven and quant signals** — dividends, share pledges, related-party transactions and earnings pre-announcements as structured fields.
- **Corporate-actions pipelines** — dividend per share, record date, total payout, with the source sentence for audit.
- **Risk and compliance monitoring** — `riskFlags` surfaces litigation, arbitration, regulatory inquiries and going-concern language.
- **Due diligence on Chinese counterparties** — acquisitions, guarantees and shareholder-loan disclosures in English.

---

## Input

| Field | Type | Description |
|---|---|---|
| `stockCodes` | array | 6-digit A-share codes (`000001`, `600519`). Both Shanghai and Shenzhen. Empty = whole market. |
| `startDate` / `endDate` | string | Publication date range, `YYYY-MM-DD`. **Required.** |
| `category` | enum | Dividend, earnings pre-announcement, annual report, equity change, and 15 more. |
| `keyword` | string | Chinese title keyword, e.g. `股份质押` (share pledge), `关联交易` (related-party transaction). |
| `maxAnnouncements` | integer | Hard cap on filings processed. Each one is a billable event. |
| `extractEnglish` | boolean | Off = metadata and PDF links only, at the much cheaper listing rate. |
| `model` | enum | Haiku (default, fastest and cheapest) or Sonnet (higher accuracy on dense filings). |
| `anthropicApiKey` | secret | Optional. Supply your own key if your organisation requires processing under its own account. |

### Example input

```json
{
  "stockCodes": ["000001", "600519"],
  "startDate": "2025-01-01",
  "endDate": "2025-12-31",
  "category": "dividend",
  "maxAnnouncements": 50,
  "extractEnglish": true
}
```

---

## Pricing

Pay per event — you pay for filings actually returned.

| Event | When it fires |
|---|---|
| `actor-start` | Once per run. |
| `announcement-processed` | A filing extracted into English. |
| `announcement-processed-long` | Same, for filings of 50+ pages (annual reports). |
| `announcement-listed` | A filing returned as metadata only (`extractEnglish: false`). |

**You are not charged** for filings that are skipped — scanned image-only PDFs and pre-2005 HTML attachments are returned with a `status` explaining why, at no cost. Set a max total charge on the run and the Actor stops cleanly at the limit rather than running past it.

---

## Limits — read before you buy

Stated plainly, because they affect whether this fits your workflow:

- **No OCR.** Image-only scanned filings are detected and returned as `unsupported_scanned`, not processed. These are mostly pre-2010 documents.
- **Pre-2005 filings** are published as `.html` attachments rather than PDFs and are skipped.
- **Very long filings are truncated.** A 300-page annual report is reduced to a character budget by keeping the document head plus the highest-scoring sections. Figures in discarded sections are not seen. For annual results, the 摘要 (summary) filing gives better coverage than the full report.
- **`companyNameEn` is usually null.** A-share filings do not print an English company name, and this Actor does not guess one. Nothing is fabricated: anything the filing does not state comes back `null`.
- **`keyword` matches filing titles**, not full text — that is a CNINFO search limitation.
- Summaries are generated from the filing text. **This is not investment advice**, and the original PDF remains the authoritative source — which is why every record links to it.

---

## Data source and compliance

Data comes from the public, login-free disclosure endpoints of **CNINFO (www.cninfo.com.cn)**, the disclosure platform designated by the China Securities Regulatory Commission. Only publicly disclosed documents are accessed; no authentication, no paywall, no personal data. Requests are rate limited by default.

---

## FAQ

**Does it cover both Shanghai and Shenzhen?**
Yes — including STAR Market (688) and ChiNext (300). Verified end to end on both exchanges.

**How current is the data?**
As current as CNINFO. Filings are typically available within minutes of publication.

**Can I get the original Chinese?**
Yes. `titleCn` and every `sourceTextCn` are verbatim Chinese, and `pdfUrl` links to the official PDF.

**Can I use my own Anthropic API key?**
Yes — `anthropicApiKey` in Advanced. Leave it empty to use the included service.

**What filing types are classified?**
`dividend`, `related_party_transaction`, `acquisition`, `earnings_report`, `share_pledge`, `shareholder_change`, `other`.

---

*Keywords: China A-share filings, CNINFO English, Chinese stock announcements API, A-share disclosure data, Shanghai Stock Exchange filings, Shenzhen Stock Exchange announcements, Chinese company filings translation, China equity research data, A-share corporate actions, Chinese regulatory filings scraper, 巨潮资讯网.*

---

Running it locally or contributing? See [DEVELOPING.md](DEVELOPING.md).
