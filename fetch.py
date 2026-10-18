"""Fetch A-share announcement metadata and PDFs from CNINFO (巨潮资讯网).

Only public, login-free endpoints are used:

* ``POST http://www.cninfo.com.cn/new/hisAnnouncement/query`` - the same endpoint
  the public "信息披露" search page calls.
* ``GET  http://www.cninfo.com.cn/new/data/szse_stock.json`` - stock code -> orgId map.
* ``GET  http://static.cninfo.com.cn/<adjunctUrl>`` - the announcement PDF.

Every request is rate limited and retried with exponential backoff.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

LOG = logging.getLogger(__name__)

QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STOCK_LIST_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
STATIC_BASE = "http://static.cninfo.com.cn/"
DETAIL_URL = "http://www.cninfo.com.cn/new/disclosure/detail"

CN_TZ = timezone(timedelta(hours=8))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Friendly name -> CNINFO ``category`` code (verified against the live endpoint).
CATEGORIES: dict[str, str] = {
    "annual_report": "category_ndbg_szsh",
    "semiannual_report": "category_bndbg_szsh",
    "q1_report": "category_yjdbg_szsh",
    "q3_report": "category_sjdbg_szsh",
    "earnings_forecast": "category_yjygjxz_szsh",
    "dividend": "category_qyfpxzcs_szsh",
    "board_resolution": "category_dshgg_szsh",
    "supervisor_resolution": "category_jshgg_szsh",
    "shareholder_meeting": "category_gddh_szsh",
    "daily_operations": "category_rcjy_szsh",
    "equity_change": "category_gqbd_szsh",
    "share_incentive": "category_gqjl_szsh",
    "corrections": "category_bcgz_szsh",
    "governance": "category_gszl_szsh",
    "intermediary_report": "category_zj_szsh",
    "ipo": "category_sf_szsh",
    "placement": "category_zf_szsh",
    "other_financing": "category_qtrz_szsh",
    "delisting_risk": "category_tbclts_szsh",
}

_EM_TAG = re.compile(r"</?em>")


class CninfoError(RuntimeError):
    """Raised when CNINFO cannot be queried successfully."""


@dataclasses.dataclass(slots=True)
class Announcement:
    """One row of the CNINFO disclosure list."""

    stock_code: str
    stock_name: str
    org_id: str
    announcement_id: str
    title_cn: str
    publish_date: str  # ISO date, Asia/Shanghai
    pdf_url: str
    source_url: str
    adjunct_size_kb: int | None
    adjunct_type: str
    announcement_time_ms: int
    raw_type_codes: str | None = None

    @property
    def is_pdf(self) -> bool:
        """Pre-2005 filings are often ``.html`` attachments, not PDFs."""
        return self.adjunct_type.upper() == "PDF"

    @property
    def slug(self) -> str:
        return f"{self.stock_code}_{self.publish_date}_{self.announcement_id}"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _detail_url(code: str, ann_id: str, org_id: str, ann_time_ms: int) -> str:
    return (
        f"{DETAIL_URL}?stockCode={code}&announcementId={ann_id}"
        f"&orgId={org_id}&announcementTime={_ms_to_date(ann_time_ms)}"
    )


def _ms_to_date(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, CN_TZ).strftime("%Y-%m-%d")


class CninfoClient:
    """Rate-limited, retrying client for the public CNINFO endpoints."""

    def __init__(
        self,
        *,
        min_interval: float = 1.5,
        max_retries: int = 4,
        timeout: float = 30.0,
        cache_dir: str | Path | None = None,
    ) -> None:
        if min_interval < 1.0:
            raise ValueError("min_interval must be >= 1.0s to stay polite to CNINFO")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._last_request = 0.0
        self._stock_index: dict[str, dict[str, str]] | None = None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
            }
        )

    # ------------------------------------------------------------------ plumbing

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))
        self._last_request = time.monotonic()

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise CninfoError(f"HTTP {resp.status_code} from {url}")
                resp.raise_for_status()
                return resp
            except (requests.RequestException, CninfoError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                backoff = (2**attempt) + random.uniform(0, 1.0)
                LOG.warning(
                    "%s %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                    method,
                    url,
                    exc,
                    backoff,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(backoff)
        raise CninfoError(f"{method} {url} failed after retries: {last_error}")

    # ------------------------------------------------------------- stock lookup

    def _load_stock_index(self) -> dict[str, dict[str, str]]:
        if self._stock_index is not None:
            return self._stock_index

        cache_file = self.cache_dir / "szse_stock.json" if self.cache_dir else None
        payload: dict[str, Any] | None = None
        if cache_file and cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < 7 * 24 * 3600:
                payload = json.loads(cache_file.read_text("utf-8"))

        if payload is None:
            resp = self._request("GET", STOCK_LIST_URL)
            payload = resp.json()
            if cache_file:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps(payload, ensure_ascii=False), "utf-8"
                )

        index = {row["code"]: row for row in payload.get("stockList", [])}
        if not index:
            raise CninfoError("CNINFO stock list came back empty")
        self._stock_index = index
        return index

    def resolve_stock(self, code: str) -> dict[str, str]:
        """Return the CNINFO record (``code``/``orgId``/``zwjc``) for a stock code."""
        code = code.strip()
        row = self._load_stock_index().get(code)
        if row is None:
            raise CninfoError(f"unknown A-share code: {code!r}")
        return row

    # ------------------------------------------------------------------- queries

    def query(
        self,
        *,
        codes: Sequence[str] | None = None,
        start: str,
        end: str,
        keyword: str | None = None,
        category: str | None = None,
        plate: str | None = None,
        limit: int = 20,
        page_size: int = 30,
    ) -> list[Announcement]:
        """List announcements matching the filters.

        ``category`` accepts a friendly key from :data:`CATEGORIES` or a raw
        ``category_*`` code. ``start``/``end`` are ``YYYY-MM-DD``.
        """
        _validate_date(start)
        _validate_date(end)

        if category:
            category = CATEGORIES.get(category, category)
            if not category.startswith("category_"):
                raise ValueError(
                    f"unknown category {category!r}; known keys: {sorted(CATEGORIES)}"
                )

        stock_param = ""
        if codes:
            parts = []
            for code in codes:
                row = self.resolve_stock(code)
                parts.append(f"{row['code']},{row['orgId']}")
            stock_param = ";".join(parts)

        results: list[Announcement] = []
        seen: set[str] = set()
        page = 1
        while len(results) < limit:
            form = {
                "pageNum": page,
                "pageSize": min(page_size, 30),
                "column": "szse",  # covers both exchanges on this endpoint
                "tabName": "fulltext",
                "plate": plate or "",
                "stock": stock_param,
                "searchkey": keyword or "",
                "secid": "",
                "category": category or "",
                "trade": "",
                "seDate": f"{start}~{end}",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "false",  # no <em> highlight markup in titles
            }
            resp = self._request("POST", QUERY_URL, data=form)
            try:
                payload = resp.json()
            except ValueError as exc:  # pragma: no cover - defensive
                raise CninfoError(f"non-JSON response from query endpoint: {exc}") from exc

            rows = payload.get("announcements") or []
            for row in rows:
                ann = self._to_announcement(row)
                if ann is None or ann.announcement_id in seen:
                    continue
                seen.add(ann.announcement_id)
                results.append(ann)
                if len(results) >= limit:
                    break

            if not rows or not payload.get("hasMore"):
                break
            page += 1

        LOG.info("CNINFO returned %d announcement(s)", len(results))
        return results

    def _to_announcement(self, row: dict[str, Any]) -> Announcement | None:
        adjunct = row.get("adjunctUrl")
        if not adjunct:
            return None
        code = str(row.get("secCode") or "").strip()
        org_id = str(row.get("orgId") or "").strip()
        ann_id = str(row.get("announcementId") or "").strip()
        ann_time = int(row.get("announcementTime") or 0)
        title = _EM_TAG.sub("", str(row.get("announcementTitle") or "")).strip()
        return Announcement(
            stock_code=code,
            stock_name=str(row.get("secName") or "").strip(),
            org_id=org_id,
            announcement_id=ann_id,
            title_cn=title,
            publish_date=_ms_to_date(ann_time),
            pdf_url=STATIC_BASE + adjunct.lstrip("/"),
            source_url=_detail_url(code, ann_id, org_id, ann_time),
            adjunct_size_kb=row.get("adjunctSize"),
            adjunct_type=str(row.get("adjunctType") or "").strip()
            or Path(adjunct).suffix.lstrip(".").upper(),
            announcement_time_ms=ann_time,
            raw_type_codes=row.get("announcementType"),
        )

    # ----------------------------------------------------------------- downloads

    def download_pdf(self, ann: Announcement, dest_dir: str | Path) -> Path:
        """Download ``ann``'s PDF into ``dest_dir`` and return the local path."""
        if not ann.is_pdf:
            raise CninfoError(
                f"{ann.announcement_id} attachment is {ann.adjunct_type!r}, not PDF "
                f"({ann.pdf_url})"
            )
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{ann.slug}.pdf"
        if target.exists() and target.stat().st_size > 0:
            LOG.debug("reusing cached PDF %s", target)
            return target

        resp = self._request(
            "GET",
            ann.pdf_url,
            headers={"Referer": "http://www.cninfo.com.cn/", "Accept": "application/pdf,*/*"},
        )
        body = resp.content
        if not body.startswith(b"%PDF"):
            raise CninfoError(
                f"{ann.pdf_url} did not return a PDF "
                f"(content-type={resp.headers.get('Content-Type')!r}, {len(body)} bytes)"
            )
        tmp = target.with_suffix(".pdf.part")
        tmp.write_bytes(body)
        tmp.replace(target)
        LOG.info("downloaded %s (%d KB)", target.name, len(body) // 1024)
        return target


def _validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}") from exc


def iter_announcements(client: CninfoClient, **kwargs: Any) -> Iterable[Announcement]:
    """Convenience generator wrapper around :meth:`CninfoClient.query`."""
    yield from client.query(**kwargs)
