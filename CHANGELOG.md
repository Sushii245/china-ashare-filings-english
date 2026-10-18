# Changelog

## 0.1 — 2026-10-18

First release.

- Search CNINFO by stock code, date range, filing category (19 official
  categories) and Chinese title keyword. Shanghai and Shenzhen, including
  STAR Market and ChiNext.
- English summary plus typed key figures per filing: value, unit, currency,
  and the value normalised to base units (万元 / 亿元 converted in code, with
  the original form kept).
- Every figure carries `sourceTextCn`, the Chinese sentence it came from.
  Two independent checks run against the PDF: the sentence must occur
  verbatim, and the number must occur inside it. Failures are reported, not
  hidden.
- Seven normalised filing types: dividend, related-party transaction,
  acquisition, earnings report, share pledge, shareholder change, other.
- Scanned image-only PDFs are detected and skipped rather than guessed at.
  Pre-2005 filings, which are published as HTML attachments, are skipped too.
  Neither is billed.
- Long filings are truncated to a character budget, keeping the document head
  plus the highest-scoring sections.
- Metadata-only mode returns filing details and PDF links without calling a
  model, at the cheaper listing rate.
