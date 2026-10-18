"""Turn Chinese announcement text into structured English data with the Claude API.

Output is constrained by a JSON schema (``output_config.format``, generated from
the Pydantic models below), so the response is always schema-valid JSON - no
prompt-level "please answer in JSON" and no regex repair.

The API key is read from the environment (``ANTHROPIC_API_KEY``) by the SDK; it is
never read from a file in this repo.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import time
import unicodedata
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field

LOG = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"

#: USD per million tokens, first-party Claude API rates (input, output).
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-fable-5-1": (10.00, 50.00),
}

#: Claude Haiku 5.5 charges a higher rate above a 100K-token prompt.
_HAIKU_LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.50, 2.50))}

AnnouncementType = Literal[
    "dividend",
    "related_party_transaction",
    "acquisition",
    "earnings_report",
    "share_pledge",
    "shareholder_change",
    "other",
]

#: Chinese amount/share units -> multiplier into the base unit.
UNIT_MULTIPLIERS: dict[str, tuple[float, str]] = {
    "元": (1.0, "元"),
    "人民币元": (1.0, "元"),
    "千元": (1e3, "元"),
    "万元": (1e4, "元"),
    "百万元": (1e6, "元"),
    "亿元": (1e8, "元"),
    "万": (1e4, "元"),
    "亿": (1e8, "元"),
    "股": (1.0, "股"),
    "万股": (1e4, "股"),
    "亿股": (1e8, "股"),
    "CNY": (1.0, "元"),
    "RMB": (1.0, "元"),
    "yuan": (1.0, "元"),
    "ten thousand yuan": (1e4, "元"),
    "hundred million yuan": (1e8, "元"),
    "shares": (1.0, "股"),
    "元/股": (1.0, "元/股"),
    "人民币元/股": (1.0, "元/股"),
    "万元/股": (1e4, "元/股"),
}

#: Units that are never monetary - ``currency`` must be null for these.
NON_MONETARY_UNITS = {"股", "万股", "亿股", "%", "％", "年", "月", "日", "倍", "个基点", "家", "shares"}


class KeyFigure(BaseModel):
    """One quantitative fact, with the Chinese sentence it came from."""

    label: str = Field(description="Short English label, e.g. 'cash dividend per 10 shares'.")
    value: float | None = Field(description="The number exactly as stated. null if not stated.")
    unit: str | None = Field(
        description="Unit as written in the filing, e.g. '元', '万元', '亿元', '股', '%'. null if none."
    )
    currency: str | None = Field(description="ISO currency code such as 'CNY', or null for non-monetary figures.")
    source_text_cn: str | None = Field(
        description="The single Chinese sentence from the filing that states this figure, copied verbatim."
    )


class AnnouncementExtraction(BaseModel):
    """Model-produced fields. URLs and provenance are filled in by the caller."""

    stock_code: str | None = Field(description="6-digit A-share code as printed in the filing, or null.")
    company_name_cn: str | None = Field(description="Chinese company name as printed, or null.")
    company_name_en: str | None = Field(
        description="Official English company name ONLY if it appears in the filing; otherwise null."
    )
    announcement_type: AnnouncementType = Field(description="Standardised English category.")
    title_en: str | None = Field(description="Faithful English translation of the filing title.")
    publish_date: str | None = Field(description="ISO date (YYYY-MM-DD) of the filing, or null.")
    summary_en: str = Field(description="3-5 sentences for a professional investor. Facts only.")
    key_figures: list[KeyFigure] = Field(description="Quantitative facts stated in the filing. Empty list if none.")
    risk_flags: list[str] = Field(
        description="Short English flags for risk warnings, litigation, arbitration, regulatory "
        "inquiries, penalties or going-concern language. Empty list if none."
    )


SYSTEM_PROMPT = """You are a filings analyst converting Chinese A-share regulatory \
announcements (from CNINFO / 巨潮资讯网) into structured English data for institutional \
investors who do not read Chinese.

Rules you must follow:

1. EXTRACT, NEVER INFER. Every value must be stated in the provided text. If the text \
does not state something, return null (or an empty list). Do not guess a company's \
English name, do not estimate figures, do not infer a date from context.
2. Numbers are copied exactly as written, with the unit as written. Put the number in \
`value` and the Chinese unit (元 / 万元 / 亿元 / 股 / 万股 / %) in `unit`. Never convert \
units yourself - the caller does that deterministically.
3. `source_text_cn` must be an exact, verbatim substring of the provided Chinese text, \
and it MUST contain the number you put in `value`. Copy the sentence, clause, or table \
row where the number actually appears - never a nearby heading, never a paraphrase, never \
a translation. Filings often state figures inside tables rendered as `a | b | c` rows; \
copying that row verbatim is correct and expected. Never drop a figure just because it \
sits in a table rather than a sentence.
4. `summary_en` is 3-5 sentences, factual and specific (who, what, how much, when, what \
it means for shareholders). No hedging, no advice, no speculation about share price.
5. `announcement_type` picks the single best category, by subject matter:
   - `dividend` - 权益分派 / 利润分配 / 派息 / 送股 / 转增.
   - `related_party_transaction` - anything labelled 关联交易 (including shareholder loans
     from a related party).
   - `acquisition` - 收购 / 重大资产重组 / 资产出售 / 合并. This applies even when the
     filing only announces an intent agreement (意向协议), a plan (筹划), or a
     preliminary/提示性公告 - a planned acquisition is still `acquisition`.
   - `earnings_report` - 年度报告 / 半年度报告 / 季度报告 / 业绩快报 / 业绩预告, including
     annual and interim reports and pre-announcements of results.
   - `share_pledge` - 股份质押 / 股权质押 / 解除质押.
   - `shareholder_change` - 减持 / 增持 / 权益变动 / 控制权变更 not driven by an acquisition.
   - `other` - only when none of the above fits. Prefer a specific category over `other`.
6. `risk_flags` covers only risks the filing itself raises (风险提示, 诉讼, 仲裁, 监管 \
问询, 处罚, 退市风险, 担保风险). Do not invent generic market risks.
7. If the text is truncated, work only from what you were given.

8. Every entry in `key_figures` must have a non-null `value`. Dates, names, and other \
non-numeric facts belong in `summary_en`, not in `key_figures`.
9. `currency` is only for monetary figures. Share counts (股/万股), percentages, periods \
(年/月/日) and multiples (倍) take `currency: null`.

Pick the 3-10 figures that an investor would actually care about rather than every \
number on the page. If the filing's numbers sit in a table, extract them from the table."""


@dataclasses.dataclass(slots=True)
class ExtractionResult:
    data: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    stop_reason: str | None

    def usage_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency_s": round(self.latency_s, 2),
            "stop_reason": self.stop_reason,
        }


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost for one request at first-party Claude API rates."""
    rate_in, rate_out = PRICING.get(model, PRICING[DEFAULT_MODEL])
    threshold = _HAIKU_LONG_PROMPT.get(model)
    if threshold and input_tokens > threshold[0]:
        rate_in, rate_out = threshold[1]
    return input_tokens / 1e6 * rate_in + output_tokens / 1e6 * rate_out


def normalise_key_figures(figures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``value_base``/``unit_base`` (万元/亿元 converted to 元) to each figure.

    The original ``value``/``unit`` are left untouched, as required for audit.
    """
    out: list[dict[str, Any]] = []
    for figure in figures:
        figure = dict(figure)
        value, unit = figure.get("value"), (figure.get("unit") or "").strip()
        base_value: float | None = None
        base_unit: str | None = None
        if value is not None and unit:
            key = unit.replace("人民币", "").strip() or unit
            match = UNIT_MULTIPLIERS.get(key) or UNIT_MULTIPLIERS.get(unit)
            if match:
                multiplier, base_unit = match
                base_value = float(value) * multiplier
            elif unit in ("%", "％"):
                base_unit = "%"
                base_value = float(value)
        if unit in NON_MONETARY_UNITS and figure.get("currency"):
            # The model occasionally tags share counts or percentages as CNY.
            figure["currency"] = None
        figure["value_base"] = base_value
        figure["unit_base"] = base_unit
        out.append(figure)
    return out


def _compact(text: str) -> str:
    """Normalise text for substring comparison.

    Drops whitespace and applies NFKC, because the model reliably reproduces the
    characters of a quote but often emits fullwidth punctuation (``3．``) where
    the PDF has ASCII (``3.``). That is a rendering difference, not a different
    quote.
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _number_variants(value: float) -> list[str]:
    """Textual forms a number may take in a filing.

    ``1666000.0`` -> ``{'1666000', '1,666,000'}``;
    ``49478429211.96`` -> ``{'49478429211.96', '49,478,429,211.96'}``.
    """
    value = abs(value)
    text = str(value)
    if "e" in text or "E" in text:  # very large/small floats
        text = f"{value:.6f}".rstrip("0").rstrip(".")
    if text.endswith(".0"):
        text = text[:-2]

    variants = {text}
    whole, _, frac = text.partition(".")
    try:
        grouped = f"{int(whole):,}"
    except ValueError:  # pragma: no cover - defensive
        return sorted(variants)
    variants.add(f"{grouped}.{frac}" if frac else grouped)
    return sorted(v for v in variants if v)


#: Characters that end a clause in a Chinese filing.
_SENTENCE_END = "。；;！？\n"

#: Widest span recovered around a figure, per side.
_RECOVER_WINDOW = 180

_PAGE_MARKER = re.compile(r"\[page \d+(?: table)?\]")


def _locate_value(
    value: float, source_text: str, *, near: int | None = None
) -> tuple[int, int] | None:
    """Find a number in the filing text, tolerating whitespace inside it.

    PDF extraction frequently splits numbers (``16.66 亿元``, ``19,405,918,198 股``),
    so each character is matched with optional whitespace between.

    A figure usually appears several times (cover title, body, tables). When
    ``near`` is given, the occurrence closest to it wins - the model's own
    (truncated) citation points at roughly the right place, so the body sentence
    beats the title.
    """
    spans: list[tuple[int, int]] = []
    for variant in _number_variants(value):
        pattern = r"\s*".join(re.escape(ch) for ch in variant)
        spans.extend(m.span() for m in re.finditer(pattern, source_text))
    if not spans:
        return None
    if near is None:
        return min(spans)
    return min(spans, key=lambda span: abs(span[0] - near))


def recover_quote(
    value: float, source_text: str, *, near_quote: str | None = None
) -> str | None:
    """Return the clause around ``value`` in ``source_text``, or None.

    Used when the model cites a sentence that does not actually contain the
    number it reported - usually because it truncated a long sentence before
    reaching the figure. Recovering the span in code is exact and costs no
    tokens.
    """
    near: int | None = None
    if near_quote:
        # Anchor on the longest leading fragment of the model's quote that we
        # can still find in the text.
        probe = re.sub(r"\s+", "", near_quote)[:24]
        for size in range(len(probe), 5, -1):
            pattern = r"\s*".join(re.escape(ch) for ch in probe[:size])
            match = re.search(pattern, source_text)
            if match:
                near = match.start()
                break

    span = _locate_value(value, source_text, near=near)
    if span is None:
        return None
    start, end = span

    left = start
    while left > 0 and start - left < _RECOVER_WINDOW and source_text[left - 1] not in _SENTENCE_END:
        left -= 1
    right = end
    limit = len(source_text)
    while right < limit and right - end < _RECOVER_WINDOW and source_text[right] not in _SENTENCE_END:
        right += 1
    if right < limit and source_text[right] in "。；;！？":
        right += 1  # keep the terminator

    clause = _PAGE_MARKER.sub("", source_text[left:right])
    return re.sub(r"\s+", " ", clause).strip() or None


def verify_source_quotes(
    figures: list[dict[str, Any]], source_text: str
) -> list[dict[str, Any]]:
    """Check each figure's citation against the filing text.

    Two independent checks, because a quote that exists in the document but does
    not contain the number is still a bad citation:

    * ``quote_in_document`` - the quote occurs verbatim in the extracted text.
    * ``value_in_quote``    - the number itself occurs in the quote.

    ``source_text_verified`` is the conjunction, and is what a human reviewer
    should trust.
    """
    haystack = _compact(source_text)
    for figure in figures:
        quote = figure.get("source_text_cn") or ""
        compact_quote = _compact(quote)
        in_doc = bool(compact_quote) and compact_quote in haystack

        value = figure.get("value")
        if value is None:
            value_found: bool | None = None
        else:
            stripped = compact_quote.replace(",", "")
            value_found = any(
                variant in compact_quote or variant.replace(",", "") in stripped
                for variant in _number_variants(float(value))
            )

        if value is not None and not (in_doc and value_found):
            # Bad citation - try to recover the real clause from the filing.
            recovered = recover_quote(float(value), source_text, near_quote=quote)
            if recovered:
                figure["source_text_cn_model"] = quote or None
                figure["source_text_cn"] = recovered
                figure["source_text_recovered"] = True
                compact_quote = _compact(recovered)
                in_doc = compact_quote in haystack
                stripped = compact_quote.replace(",", "")
                value_found = any(
                    variant in compact_quote or variant.replace(",", "") in stripped
                    for variant in _number_variants(float(value))
                )

        figure.setdefault("source_text_recovered", False)
        figure["quote_in_document"] = in_doc
        figure["value_in_quote"] = value_found
        figure["source_text_verified"] = in_doc and value_found is not False
    return figures


class ClaudeExtractor:
    """Thin wrapper: one Claude request per announcement."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = "low",
        max_tokens: int = 4096,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        if client is None and not (
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ):
            raise RuntimeError(
                "No Claude credentials found. Export ANTHROPIC_API_KEY before running "
                "extraction, or pass --no-llm to only fetch and parse."
            )
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = client or anthropic.Anthropic()

    def extract(
        self,
        *,
        announcement_text: str,
        title_cn: str,
        stock_code: str,
        stock_name: str,
        publish_date: str,
        source_url: str,
        pdf_url: str,
        truncated: bool = False,
    ) -> ExtractionResult:
        user_content = (
            f"Filing metadata from the CNINFO disclosure list (authoritative for code, "
            f"name and date):\n"
            f"- stock_code: {stock_code}\n"
            f"- company_name_cn: {stock_name}\n"
            f"- publish_date: {publish_date}\n"
            f"- title_cn: {title_cn}\n"
            f"- text_truncated: {str(truncated).lower()}\n\n"
            f"Filing text (Chinese, extracted from the official PDF):\n"
            f"<filing>\n{announcement_text}\n</filing>\n\n"
            f"Extract the structured record."
        )

        started = time.monotonic()
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            output_format=AnnouncementExtraction,
            output_config={"effort": self.effort},
        )
        latency = time.monotonic() - started

        if response.stop_reason == "refusal":
            raise RuntimeError(f"model declined the request: {response.stop_details}")

        parsed = response.parsed_output
        if parsed is None:
            raise RuntimeError(
                f"model returned no parsable output (stop_reason={response.stop_reason})"
            )

        data = parsed.model_dump()
        figures = [f for f in (data.get("key_figures") or []) if f.get("value") is not None]
        data["key_figures"] = verify_source_quotes(
            normalise_key_figures(figures), announcement_text
        )
        # Provenance is never model-generated.
        data["source_url"] = source_url
        data["pdf_url"] = pdf_url

        usage = response.usage
        cost = estimate_cost(self.model, usage.input_tokens, usage.output_tokens)
        LOG.info(
            "extracted %s (%s): %d in / %d out tokens, $%.5f, %.1fs",
            stock_code, data.get("announcement_type"),
            usage.input_tokens, usage.output_tokens, cost, latency,
        )
        return ExtractionResult(
            data=data,
            model=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            latency_s=latency,
            stop_reason=response.stop_reason,
        )
