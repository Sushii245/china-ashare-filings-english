"""Parsing Chinese commerce text into typed values.

This is the part of the Actor a buyer is actually paying for, and the part most
likely to be wrong, so it is pure functions over strings with no I/O and its own
test file.

Three classes of trap it exists to handle:

* **Width and symbol.** Listings mix fullwidth and halfwidth freely: ``１２８``
  for 128, ``￥`` (U+FFE5) for ``¥`` (U+00A5), ``～`` for ``~``, and a fullwidth
  comma inside a thousands group. Everything is NFKC-normalised before a regex
  sees it, for the same reason the A-share Actor does it: the same sentence with
  a different byte is still the same sentence.

* **Chinese myriads.** ``2万`` is 20,000 and ``1.5亿`` is 150,000,000. A parser
  that reads the leading digits and stops is off by four orders of magnitude.

* **Sales semantics.** ``月销1000+``, ``已售2万+`` and ``100+人付款`` are three
  different measurements. The ``+`` makes each one a floor, not a value.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from models import Money, Quantity, Sales, SalesUnit, SalesWindow, Shipping

#: Chinese myriad multipliers, largest first so 亿 is matched before 万.
_MULTIPLIERS: tuple[tuple[str, int], ...] = (
    ("亿", 100_000_000),
    ("万", 10_000),
    ("千", 1_000),
    ("百", 100),
)

#: Currency marks that may sit in front of or behind a number.
_CURRENCY = "¥￥$＄元"

#: Separators that mean "a span from a to b" once NFKC has folded the fullwidth
#: forms. The hyphen class covers the three dashes listings actually use.
_RANGE_SEP = r"(?:-|~|—|–|至|到)"

_NUM = r"\d+(?:\.\d+)?"


def normalize_text(value: str | None) -> str:
    """NFKC-fold, collapse whitespace and strip.

    NFKC turns ``１２８`` into ``128``, ``￥`` into ``¥`` and ``～`` into ``~``,
    which is what lets a single regex handle both widths.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    # NFKC leaves U+00A0 and the ideographic space alone in some runtimes.
    text = text.replace(" ", " ").replace("　", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_cn_number(value: str | None) -> float | None:
    """Read a Chinese-formatted number, honouring 万 / 亿 / 千 / 百.

    ``"2万"`` -> 20000.0, ``"1.5亿"`` -> 150000000.0, ``"1,280"`` -> 1280.0.
    Returns ``None`` when there is no number to read.
    """
    text = normalize_text(value)
    if not text:
        return None
    # Thousands separators only count between digits, so "1,280" folds but a
    # trailing comma in prose does not swallow the next number.
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    match = re.search(
        rf"(-?)({_NUM})\s*([{''.join(m[0] for m in _MULTIPLIERS)}]?)", text
    )
    if not match:
        return None
    if match.group(1):
        # Every figure read here — price, sales, stock, quantity — is
        # non-negative. A minus means the field is not what we think it is, and
        # dropping the sign to return the magnitude is worse than refusing.
        return None
    suffix = match.group(3)
    # Decimal, not float: 2.3 * 100_000_000 is 229999999.99999997 in binary
    # floating point, and a price or a sales count that is off by a hundredth
    # of a unit reads as a parser bug to the buyer.
    try:
        number = Decimal(match.group(2))
    except InvalidOperation:
        return None
    for mark, factor in _MULTIPLIERS:
        if suffix == mark:
            return float(number * factor)
    return float(number)


def _money_from(text: str, raw: str) -> Money | None:
    """Build a :class:`Money` from an already-normalised fragment."""
    stripped = re.sub(rf"[{_CURRENCY}]", "", text)
    low_high = re.search(
        rf"({_NUM}(?:\s*[万亿千百])?)\s*{_RANGE_SEP}\s*({_NUM}(?:\s*[万亿千百])?)",
        stripped,
    )
    if low_high:
        low = parse_cn_number(low_high.group(1))
        high = parse_cn_number(low_high.group(2))
        if low is None or high is None:
            return None
        if high < low:
            low, high = high, low
        return Money(value=low, value_max=high, source_text_cn=raw)
    single = parse_cn_number(stripped)
    if single is None:
        return None
    return Money(value=single, source_text_cn=raw)


def parse_price(value: str | float | int | None) -> Money | None:
    """Parse a price, single or a span across SKUs.

    Accepts the numeric forms a provider may already have typed, and the string
    forms a listing shows: ``"¥128.00"``, ``"128.00-298.00"``, ``"￥1,280"``,
    ``"128元"``, ``"1.5万"``.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Money(value=float(value))
    raw = str(value).strip()
    text = normalize_text(raw)
    if not text:
        return None
    return _money_from(text, raw)


#: Window markers, checked in order. ``月销`` is a rolling month; ``30天`` is
#: stated separately by some providers and is not the same promise.
_WINDOW_MARKERS: tuple[tuple[str, SalesWindow], ...] = (
    ("30天", "last_30_days"),
    ("近30日", "last_30_days"),
    ("月销", "monthly"),
    ("月成交", "monthly"),
    ("本月", "monthly"),
    ("累计", "cumulative"),
    ("总销量", "cumulative"),
    ("已售", "cumulative"),
    ("已拼", "cumulative"),
)

#: Unit markers. ``人付款`` counts payers, not pieces: two units bought by one
#: buyer is 1. ``件`` counts pieces.
_UNIT_MARKERS: tuple[tuple[str, SalesUnit], ...] = (
    ("人付款", "buyers"),
    ("人收货", "buyers"),
    ("人购买", "buyers"),
    ("人已买", "buyers"),
    ("件", "items"),
    ("笔", "orders"),
    ("单", "orders"),
)


def parse_sales(value: str | float | int | None) -> Sales | None:
    """Parse a sales figure together with what it measures.

    ``"月销1000+"``   -> 1000, monthly, approximate
    ``"已售2万+"``    -> 20000, cumulative, approximate
    ``"100+人付款"``  -> 100, buyers, approximate
    ``"成交1203笔"``  -> 1203, orders, exact
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # A typed number states no window, no unit and no floor, and quotes no
        # listing text — so none of those are claimed. The provider's hints
        # supply the window where its field name implies one.
        if value < 0:
            return None
        return Sales(count=int(value), is_approximate=False)
    raw = (value or "").strip()
    text = normalize_text(raw)
    if not text:
        return None
    # Find and strip the window marker before reading the number. Markers
    # like 30天 and 近30日 contain digits of their own, and a parser that just
    # takes the first number in "30天销量1200" returns 30.
    window: SalesWindow | None = None
    remainder = text
    for marker, mapped in _WINDOW_MARKERS:
        if marker in text:
            window = mapped
            remainder = text.replace(marker, " ", 1)
            break

    count = parse_cn_number(remainder)
    if count is None:
        return None

    unit: SalesUnit | None = None
    for marker, mapped_unit in _UNIT_MARKERS:
        if marker in text:
            unit = mapped_unit
            break

    # "+" and 以上 both mean the real figure is at least this. So does 多,
    # but only directly after the number — 多 is a common character, and
    # "月销200 多规格可选" states an exact figure beside unrelated prose.
    is_approx = (
        "+" in text
        or "以上" in text
        # No whitespace allowed: "1000多" is a floor, "200 多规格可选" is an
        # exact figure next to an unrelated word.
        or re.search(r"\d多", text) is not None
    )

    return Sales(
        count=int(count),
        is_approximate=is_approx,
        window=window,
        unit=unit,
        source_text_cn=raw or None,
    )


def parse_quantity(value: str | float | int | None) -> Quantity | None:
    """Parse a piece count such as a minimum order quantity.

    ``"起订量：2件"`` -> 2, ``"100件起批"`` -> 100, ``"≥1000"`` -> 1000.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Quantity(value=int(value))
    raw = str(value).strip()
    text = normalize_text(raw)
    count = parse_cn_number(text)
    if count is None:
        return None
    unit_match = re.search(r"\d\s*(件|个|只|套|箱|包|双|台|米|kg|g)", text, re.I)
    return Quantity(
        value=int(count),
        unit_cn=unit_match.group(1) if unit_match else None,
        source_text_cn=raw or None,
    )


def parse_shipping(value: str | None) -> Shipping | None:
    """Parse delivery terms.

    ``"包邮"`` -> seller pays. ``"运费10元"`` -> buyer pays 10. ``"到付"`` ->
    neither, the courier collects. An unrecognised string still comes back with
    ``source_text_cn`` set so nothing is silently dropped.
    """
    raw = (value or "").strip()
    text = normalize_text(raw)
    if not text:
        return None

    if "到付" in text or "货到付款" in text:
        return Shipping(cash_on_delivery=True, source_text_cn=raw)

    if re.search(r"包邮|免运费|免邮|卖家承担运费", text):
        return Shipping(included=True, fee=Money(value=0.0), source_text_cn=raw)

    fee_match = re.search(rf"(?:运费|快递|邮费)\D{{0,4}}({_NUM})", text)
    if fee_match:
        fee = parse_price(fee_match.group(1))
        return Shipping(included=False, fee=fee, source_text_cn=raw)

    # A bare number in a shipping field is a fee.
    bare = parse_cn_number(text)
    if bare is not None and not re.search(r"[一-鿿]", text.replace("元", "")):
        return Shipping(
            included=bare == 0.0,
            fee=Money(value=bare, source_text_cn=raw),
            source_text_cn=raw,
        )

    return Shipping(source_text_cn=raw)


#: A 阶梯价 row, in the three shapes 1688 writes them.
_TIER_PATTERNS = (
    # 1-99件 / 1~99
    re.compile(rf"({_NUM})\s*{_RANGE_SEP}\s*({_NUM})"),
    # ≥1000件 / 1000件以上 / 1000起
    re.compile(rf"(?:≥|>=|>)\s*({_NUM})"),
    re.compile(rf"({_NUM})\s*(?:件|个|只|套)?\s*(?:以上|起|\+)"),
)


def parse_tier_range(value: str | None) -> tuple[int, int | None] | None:
    """Parse the quantity span of one 阶梯价 row.

    ``"1-99件"`` -> (1, 99), ``"≥1000件"`` -> (1000, None),
    ``"1000件以上"`` -> (1000, None).
    """
    text = normalize_text(value)
    if not text:
        return None
    span = _TIER_PATTERNS[0].search(text)
    if span:
        low = parse_cn_number(span.group(1))
        high = parse_cn_number(span.group(2))
        if low is None or high is None:
            return None
        return int(low), int(high)
    for pattern in _TIER_PATTERNS[1:]:
        open_ended = pattern.search(text)
        if open_ended:
            low = parse_cn_number(open_ended.group(1))
            if low is None:
                return None
            return int(low), None
    return None


#: Taobao's three seller scores. The English aspect names are the contract; the
#: Chinese ones are what the page says.
SHOP_SCORE_ASPECTS: tuple[tuple[str, str], ...] = (
    ("description_match", "宝贝描述"),
    ("service", "卖家服务"),
    ("logistics", "物流服务"),
)


def parse_peer_delta(value: str | None) -> float | None:
    """Parse a 高于/低于同行 percentage into a signed number.

    ``"高于同行20.15%"`` -> 20.15, ``"低于同行3.2%"`` -> -3.2,
    ``"持平"`` -> 0.0.
    """
    text = normalize_text(value)
    if not text:
        return None
    if "持平" in text:
        return 0.0
    match = re.search(rf"({_NUM})\s*%", text)
    if not match:
        return None
    magnitude = float(match.group(1))
    if "低于" in text:
        return -magnitude
    return magnitude


def parse_presale_days(value: str | None) -> int | None:
    """Parse the dispatch promise of a 预售 listing.

    ``"付款后15天内发货"`` -> 15, ``"7天内发货"`` -> 7.
    """
    text = normalize_text(value)
    if not text:
        return None
    match = re.search(rf"({_NUM})\s*(?:天|日)\s*(?:内)?\s*(?:发货|内发)", text)
    if match:
        return int(float(match.group(1)))
    match = re.search(rf"(?:发货|内发)\D{{0,4}}({_NUM})\s*(?:天|日)", text)
    if match:
        return int(float(match.group(1)))
    return None
