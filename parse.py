"""PDF -> text for CNINFO announcements.

Uses PyMuPDF for page text and for table detection (tables are re-emitted as
pipe-delimited rows so the structure survives into the prompt).

Two cost-control mechanisms live here:

* :func:`parse_pdf` flags image-only (scanned) PDFs as ``unsupported`` instead of
  shipping an empty prompt to the model. MVP does no OCR.
* :func:`build_prompt_text` keeps long filings (annual reports etc.) inside a
  character budget by always keeping the head of the document and then adding the
  highest-scoring remaining blocks in document order.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

LOG = logging.getLogger(__name__)

#: Below this many extracted characters per page a PDF is treated as scanned.
SCANNED_CHARS_PER_PAGE = 40

#: Default character budget handed to the model (~1 CJK char ≈ 1 token).
DEFAULT_MAX_CHARS = 24_000

#: Characters of the document head that are always kept (cover page + summary).
HEAD_CHARS = 6_000

_WS = re.compile(r"[ \t　]+")
_BLANKS = re.compile(r"\n{3,}")

#: Section/keyword signals that matter for the fields we extract.
_KEYWORDS = (
    "关联交易", "交易金额", "成交金额", "收购", "转让", "股权质押", "质押",
    "业绩预告", "净利润", "归属于", "营业收入", "每10股", "现金股利", "分配",
    "权益分派", "股权登记日", "除权", "风险", "诉讼", "仲裁", "问询", "监管",
    "处罚", "担保", "增资", "减持", "增持", "控股股东", "实际控制人",
    "审议", "决议", "基数", "总股本", "交易对方", "评估值", "承诺",
    "重大事项提示", "特别风险提示", "释义",
)
_KEYWORD_RE = re.compile("|".join(map(re.escape, _KEYWORDS)))
_MONEY_RE = re.compile(r"\d[\d,]*\.?\d*\s*(?:万元|亿元|元|万股|亿股|股|%|％)")


@dataclasses.dataclass(slots=True)
class ParsedPdf:
    path: str
    page_count: int
    char_count: int
    chars_per_page: float
    is_scanned: bool
    status: str  # "ok" | "unsupported_scanned" | "empty"
    text: str
    table_count: int

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data.pop("text")
        return data


def parse_pdf(path: str | Path, *, extract_tables: bool = True,
              max_table_pages: int = 20) -> ParsedPdf:
    """Extract text (and tables) from a CNINFO announcement PDF."""
    path = Path(path)
    chunks: list[str] = []
    table_count = 0

    with fitz.open(path) as doc:
        page_count = doc.page_count
        for index, page in enumerate(doc):
            page_text = page.get_text("text") or ""
            page_tables: list[str] = []
            if extract_tables and index < max_table_pages:
                page_tables = _tables_as_text(page)
                table_count += len(page_tables)
            chunks.append(f"\n[page {index + 1}]\n{page_text}")
            for table_md in page_tables:
                chunks.append(f"\n[page {index + 1} table]\n{table_md}")

    text = _normalise("".join(chunks))
    # Page markers are not content - measure only the real characters.
    content_chars = len(re.sub(r"\[page \d+(?: table)?\]", "", text).strip())
    per_page = content_chars / page_count if page_count else 0.0

    if content_chars == 0:
        status = "empty"
    elif per_page < SCANNED_CHARS_PER_PAGE:
        status = "unsupported_scanned"
    else:
        status = "ok"

    if status != "ok":
        LOG.warning(
            "%s: %s (%d pages, %.1f chars/page) - no OCR in MVP",
            path.name, status, page_count, per_page,
        )

    return ParsedPdf(
        path=str(path),
        page_count=page_count,
        char_count=content_chars,
        chars_per_page=round(per_page, 1),
        is_scanned=status == "unsupported_scanned",
        status=status,
        text=text,
        table_count=table_count,
    )


def _tables_as_text(page: "fitz.Page") -> list[str]:
    try:
        finder = page.find_tables()
    except Exception as exc:  # pragma: no cover - PyMuPDF heuristics can throw
        LOG.debug("table detection failed on %s: %s", page, exc)
        return []

    out: list[str] = []
    for table in finder.tables:
        try:
            rows = table.extract()
        except Exception:  # pragma: no cover
            continue
        lines = []
        for row in rows:
            cells = [(c or "").replace("\n", " ").strip() for c in row]
            if any(cells):
                lines.append(" | ".join(cells))
        if len(lines) >= 2:
            out.append("\n".join(lines))
    return out


def _normalise(text: str) -> str:
    text = text.replace(" ", " ")
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()


def build_prompt_text(
    parsed: ParsedPdf,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    head_chars: int = HEAD_CHARS,
) -> tuple[str, bool]:
    """Return ``(text, truncated)`` fitting inside ``max_chars``.

    Short announcements go through verbatim. Long ones keep the head of the
    document plus the highest-scoring remaining paragraphs, in document order,
    with an explicit elision marker so the model knows text was removed.
    """
    text = parsed.text
    if len(text) <= max_chars:
        return text, False

    head = text[:head_chars]
    rest = text[head_chars:]
    blocks = [b for b in re.split(r"\n\s*\n", rest) if b.strip()]

    scored = [
        (
            len(_KEYWORD_RE.findall(block)) * 3 + len(_MONEY_RE.findall(block)),
            position,
            block,
        )
        for position, block in enumerate(blocks)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))

    budget = max_chars - len(head) - 200
    picked: list[tuple[int, str]] = []
    for score, position, block in scored:
        if score == 0 or budget <= 0:
            break
        block = block[:4000]
        if len(block) > budget:
            continue
        picked.append((position, block))
        budget -= len(block) + 20

    picked.sort(key=lambda item: item[0])
    body = "\n\n[... omitted ...]\n\n".join(block for _, block in picked)
    selected = (
        f"{head}\n\n[... document truncated for cost control; "
        f"relevant sections follow ...]\n\n{body}"
    )
    LOG.info(
        "%s: %d chars -> %d chars (%d of %d blocks kept)",
        Path(parsed.path).name, len(text), len(selected), len(picked), len(blocks),
    )
    return selected[:max_chars], True
