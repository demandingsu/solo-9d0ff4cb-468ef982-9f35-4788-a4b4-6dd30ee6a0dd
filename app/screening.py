"""Screen tender notices by performance location (履约地点初筛).

The bid specialist only takes projects in a handful of districts.  Given a
notice-index URL and the districts the company covers, this module crawls the
same-site pagination, reads every tender (original) notice, and extracts the
construction/delivery places per project and per lot.

The rules are deliberately conservative:

* An agency/purchaser/contact address is never a performance place; only
  explicit 施工/交付/服务/项目地点-style labels count (see crawler).
* A notice-level shared place is only given to lots that have no place of
  their own.
* A lot is a candidate only when *every* one of its performance places falls
  inside the covered districts.
* A place that lacks its superior division (no city above the district), or
  whose district cannot be identified at all, sends the lot to the review
  list — the tool never guesses an attribution.
* Any list-page or detail fetch failure marks the whole round incomplete, and
  an incomplete round must never be presented as "no candidates".
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .crawler import (
    FETCH_TIMEOUT,
    MAX_DETAIL_PAGES,
    MAX_LIST_PAGES,
    USER_AGENT,
    FetchIssue,
    ParsedNotice,
    _fetch,
    canonical_project,
    canonical_section,
    extract_links,
    normalize_space,
    normalize_url,
    normalize_width,
    parse_notice_html,
    section_matches,
)

# ---------------------------------------------------------------------------
# Region parsing (省 / 市 / 区县)
# ---------------------------------------------------------------------------

_PROVINCE = r"[一-龥]{2,6}?(?:省|自治区)"
_MUNICIPALITY = r"(?:北京市|天津市|上海市|重庆市)"
_CITY = r"[一-龥]{2,7}?(?:市|自治州|地区|盟)"
_DISTRICT = r"[一-龥]{2,7}?(?:区|县|旗|市)"

REGION_CHAIN_RE = re.compile(
    r"(?:(?P<province>" + _PROVINCE + r")|(?P<municipality>" + _MUNICIPALITY + r"))?"
    r"(?:(?P<city>" + _CITY + r"))?"
    r"(?P<district>" + _DISTRICT + r")"
)

COVERAGE_SPLIT_RE = re.compile(r"[、，,；;/|\n]+")
SECTION_SPLIT_RE = re.compile(r"[、,，;；|]+")


@dataclass(frozen=True)
class RegionPath:
    province: str | None
    city: str | None
    district: str | None

    def label(self) -> str:
        parts: list[str] = []
        if self.province:
            parts.append(self.province)
        if self.city and self.city != self.province:
            parts.append(self.city)
        if self.district:
            parts.append(self.district)
        return "".join(parts)


def parse_region(text: str) -> RegionPath | None:
    """Extract the first province/city/district chain from free text."""

    match = REGION_CHAIN_RE.search(normalize_width(text or ""))
    if not match:
        return None
    province = match.group("province")
    municipality = match.group("municipality")
    city = match.group("city")
    district = match.group("district")
    if municipality:
        # A direct-administered municipality is its own city level.
        province = province or municipality
        city = city or municipality
    return RegionPath(province=province, city=city, district=district)


def parse_coverage(text: str) -> tuple[list[RegionPath], list[str]]:
    """Parse the user-supplied covered districts.

    Entries that cannot be resolved to a district plus its city level are
    excluded with a warning — a bare district name would force the matcher to
    guess, which is exactly what this tool must not do.
    """

    entries: list[RegionPath] = []
    warnings: list[str] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for piece in COVERAGE_SPLIT_RE.split(normalize_width(text or "")):
        item = piece.strip(" \t。.")
        if not item:
            continue
        region = parse_region(item)
        if region is None or not region.district:
            warnings.append(
                f"覆盖条目「{item}」未能识别出区县级区划，未纳入覆盖范围。"
            )
            continue
        if not region.city:
            warnings.append(
                f"覆盖条目「{item}」缺少上级（市级）区划，未纳入覆盖范围；"
                "请按“省+市+区县”完整填写。"
            )
            continue
        key = (region.province, region.city, region.district)
        if key in seen:
            continue
        seen.add(key)
        entries.append(region)
    return entries, warnings


# ---------------------------------------------------------------------------
# Place resolution
# ---------------------------------------------------------------------------

STATUS_MATCHED = "matched"
STATUS_OUTSIDE = "outside"
STATUS_UNCLEAR = "unclear"


@dataclass
class PlaceResolution:
    raw: str
    scope: str  # "section" | "notice"
    status: str  # matched | outside | unclear
    region_label: str | None
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "scope": self.scope,
            "scope_label": "标段专属地点" if self.scope == "section" else "公告共用地点",
            "status": self.status,
            "region_label": self.region_label,
            "reason": self.reason,
        }


def resolve_place(raw: str, scope: str, coverage: list[RegionPath]) -> PlaceResolution:
    """Decide whether one place text falls inside the covered districts."""

    region = parse_region(raw)
    if region is None or not region.district:
        return PlaceResolution(
            raw=raw,
            scope=scope,
            status=STATUS_UNCLEAR,
            region_label=None,
            reason="无法从地点原文识别出区县级区划",
        )
    if not region.city:
        return PlaceResolution(
            raw=raw,
            scope=scope,
            status=STATUS_UNCLEAR,
            region_label=region.label(),
            reason="缺少上级（市级）区划，无法确认归属",
        )
    for covered in coverage:
        if region.district != covered.district:
            continue
        if covered.city and region.city != covered.city:
            continue
        if covered.province and region.province and covered.province != region.province:
            continue
        return PlaceResolution(
            raw=raw, scope=scope, status=STATUS_MATCHED, region_label=region.label(), reason=None
        )
    return PlaceResolution(
        raw=raw, scope=scope, status=STATUS_OUTSIDE, region_label=region.label(), reason=None
    )


# ---------------------------------------------------------------------------
# Lot-level screening result
# ---------------------------------------------------------------------------

LOT_CANDIDATE = "candidate"
LOT_REVIEW = "review"
LOT_OUTSIDE = "outside"


@dataclass
class LotScreening:
    project_no: str
    section_no: str
    status: str  # candidate | review | outside
    reason: str
    places: list[PlaceResolution]
    source_urls: list[str]
    source_titles: list[str]

    def as_dict(self, public_url_fn=None, check_url_fn=None) -> dict[str, Any]:
        def pub(url: str) -> str:
            return public_url_fn(url) if public_url_fn else url

        check_url = None
        if check_url_fn and self.project_no and self.section_no:
            check_url = check_url_fn(self.project_no, self.section_no)
        return {
            "project_no": self.project_no,
            "section_no": self.section_no,
            "status": self.status,
            "reason": self.reason,
            "places": [place.as_dict() for place in self.places],
            "sources": [
                {"url": pub(url), "title": title}
                for url, title in zip(self.source_urls, self.source_titles)
            ],
            "check_url": check_url,
        }


@dataclass
class ScreenOutcome:
    list_url: str
    coverage_text: str
    coverage: list[RegionPath]
    coverage_warnings: list[str]
    coverage_error: str | None
    complete: bool
    candidates: list[LotScreening]
    review: list[LotScreening]
    outside: list[LotScreening]
    issues: list[FetchIssue]
    warnings: list[str]
    searched_pages: list[str]
    detail_page_count: int
    notice_count: int

    def as_dict(self, public_url_fn=None, check_url_fn=None) -> dict[str, Any]:
        def pub(url: str | None) -> str | None:
            if not url:
                return None
            return public_url_fn(url) if public_url_fn else url

        return {
            "input_url": self.list_url,
            "coverage": [region.label() for region in self.coverage],
            "coverage_warnings": self.coverage_warnings,
            "coverage_error": self.coverage_error,
            "complete": self.complete,
            "candidates": [
                lot.as_dict(public_url_fn, check_url_fn) for lot in self.candidates
            ],
            "review": [lot.as_dict(public_url_fn, check_url_fn) for lot in self.review],
            "outside": [lot.as_dict(public_url_fn, check_url_fn) for lot in self.outside],
            "counts": {
                "candidate": len(self.candidates),
                "review": len(self.review),
                "outside": len(self.outside),
            },
            "failures": [
                issue.as_dict() | {"url": pub(issue.url) or issue.url}
                for issue in self.issues
            ],
            "warnings": self.warnings,
            "searched_pages": [pub(url) or url for url in self.searched_pages],
            "detail_page_count": self.detail_page_count,
            "notice_count": self.notice_count,
        }


# ---------------------------------------------------------------------------
# Lot assembly
# ---------------------------------------------------------------------------


@dataclass
class _LotBucket:
    project_display: str
    section_display: str
    own_places: list[str] = field(default_factory=list)
    shared_places: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_titles: list[str] = field(default_factory=list)

    def add_places(self, raws: list[str], own: bool) -> None:
        target = self.own_places if own else self.shared_places
        known = {normalize_width(item) for item in target}
        for raw in raws:
            key = normalize_width(raw)
            if key and key not in known:
                known.add(key)
                target.append(raw)


def _notice_sections(parsed: ParsedNotice) -> list[str]:
    """All lot numbers a notice covers, in display form, deduplicated."""

    sections: list[str] = []
    seen: set[str] = set()
    for candidate in parsed.section_candidates:
        for piece in SECTION_SPLIT_RE.split(normalize_width(candidate)):
            display = piece.strip(" \t。，,；;、：:()（）")
            if not display:
                continue
            key = canonical_section(display)
            if not key or key in seen:
                continue
            seen.add(key)
            sections.append(display)
    return sections


def _build_lots(
    notices: list[tuple[ParsedNotice, str]],
    coverage: list[RegionPath],
    warnings: list[str],
) -> tuple[list[LotScreening], list[LotScreening], list[LotScreening]]:
    buckets: dict[tuple[str, str], _LotBucket] = {}
    skipped: list[str] = []

    for parsed, source_url in notices:
        project = parsed.project_candidates[0] if parsed.project_candidates else ""
        sections = _notice_sections(parsed)
        if not project or not sections:
            skipped.append(source_url)
            continue
        for section_display in sections:
            key = (canonical_project(project), canonical_section(section_display))
            bucket = buckets.get(key)
            if bucket is None:
                bucket = _LotBucket(
                    project_display=project, section_display=section_display
                )
                buckets[key] = bucket
            own = [
                place.raw
                for place in parsed.places
                if place.scope == "section"
                and place.section
                and section_matches(section_display, [place.section])
            ]
            shared = [place.raw for place in parsed.places if place.scope == "notice"]
            bucket.add_places(own, own=True)
            bucket.add_places(shared, own=False)
            if source_url not in bucket.source_urls:
                bucket.source_urls.append(source_url)
                bucket.source_titles.append(parsed.title)

    if skipped:
        warnings.append(
            f"{len(skipped)} 条招标公告未能识别项目编号或标段号，未纳入地点筛选，"
            "请人工留意。"
        )

    candidates: list[LotScreening] = []
    review: list[LotScreening] = []
    outside: list[LotScreening] = []

    for key in sorted(buckets):
        bucket = buckets[key]
        # A notice-level shared place only fills lots without their own.
        adopted = bucket.own_places if bucket.own_places else bucket.shared_places
        scope = "section" if bucket.own_places else "notice"

        if not adopted:
            review.append(
                LotScreening(
                    project_no=bucket.project_display,
                    section_no=bucket.section_display,
                    status=LOT_REVIEW,
                    reason=(
                        "公告未载明施工/交付地点（代理机构地址、开标地点、"
                        "文件递交地点均不作为履约地点），需人工查阅公告确认。"
                    ),
                    places=[],
                    source_urls=bucket.source_urls,
                    source_titles=bucket.source_titles,
                )
            )
            continue

        places = [resolve_place(raw, scope, coverage) for raw in adopted]
        unclear = [place for place in places if place.status == STATUS_UNCLEAR]
        if unclear:
            details = "；".join(
                f"地点「{place.raw}」{place.reason}" for place in unclear
            )
            review.append(
                LotScreening(
                    project_no=bucket.project_display,
                    section_no=bucket.section_display,
                    status=LOT_REVIEW,
                    reason=details + "；归属不明，不猜测，需人工确认。",
                    places=places,
                    source_urls=bucket.source_urls,
                    source_titles=bucket.source_titles,
                )
            )
            continue

        if all(place.status == STATUS_MATCHED for place in places):
            candidates.append(
                LotScreening(
                    project_no=bucket.project_display,
                    section_no=bucket.section_display,
                    status=LOT_CANDIDATE,
                    reason=f"全部 {len(places)} 个履约地点均落在覆盖区县。",
                    places=places,
                    source_urls=bucket.source_urls,
                    source_titles=bucket.source_titles,
                )
            )
            continue

        outside_places = [place for place in places if place.status == STATUS_OUTSIDE]
        details = "；".join(
            f"地点「{place.raw}」归属 {place.region_label or '覆盖范围外'}"
            for place in outside_places
        )
        outside.append(
            LotScreening(
                project_no=bucket.project_display,
                section_no=bucket.section_display,
                status=LOT_OUTSIDE,
                reason=details + "，不在覆盖范围。",
                places=places,
                source_urls=bucket.source_urls,
                source_titles=bucket.source_titles,
            )
        )

    return candidates, review, outside


# ---------------------------------------------------------------------------
# Crawl driver
# ---------------------------------------------------------------------------


async def screen_regions(
    list_url: str,
    coverage_text: str,
    *,
    client_factory=None,
) -> ScreenOutcome:
    """Crawl a notice index and split its lots into candidate/review/outside."""

    coverage, coverage_warnings = parse_coverage(coverage_text)
    issues: list[FetchIssue] = []
    warnings: list[str] = []
    searched_pages: list[str] = []
    notice_count = 0
    detail_count = 0

    def _outcome(
        *,
        complete: bool,
        candidates=None,
        review=None,
        outside=None,
        coverage_error: str | None = None,
    ) -> ScreenOutcome:
        return ScreenOutcome(
            list_url=list_url,
            coverage_text=coverage_text,
            coverage=coverage,
            coverage_warnings=coverage_warnings,
            coverage_error=coverage_error,
            complete=complete,
            candidates=candidates or [],
            review=review or [],
            outside=outside or [],
            issues=issues,
            warnings=warnings,
            searched_pages=searched_pages,
            detail_page_count=detail_count,
            notice_count=notice_count,
        )

    if not coverage:
        return _outcome(
            complete=False,
            coverage_error=(
                "未能识别有效的覆盖区县：请按“省+市+区县”完整填写，"
                "每行一个（也可用顿号、逗号或分号分隔）。"
            ),
        )

    try:
        start_url = normalize_url(list_url)
    except ValueError as exc:
        return _outcome(complete=False, coverage_error=str(exc))

    original_notices: list[tuple[ParsedNotice, str]] = []
    seen_pages: set[str] = set()
    seen_details: set[str] = set()
    queue: list[str] = [start_url]
    incomplete = False

    timeout = httpx.Timeout(FETCH_TIMEOUT, connect=min(8.0, FETCH_TIMEOUT))
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)

    if client_factory is None:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
            limits=limits,
        )
        owns_client = True
    else:
        client = client_factory()
        owns_client = False

    try:
        while queue:
            if len(searched_pages) >= MAX_LIST_PAGES:
                incomplete = True
                warnings.append(f"公告列表超过 {MAX_LIST_PAGES} 页，已停止继续翻页。")
                break
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            searched_pages.append(page_url)

            html, error, final_page_url = await _fetch(client, page_url, "list")
            if html is None:
                issues.append(
                    FetchIssue(url=page_url, reason=f"公告列表页抓取失败：{error}", stage="list")
                )
                continue
            if final_page_url != page_url:
                if final_page_url not in searched_pages:
                    searched_pages.append(final_page_url)
                seen_pages.add(final_page_url)

            try:
                links = extract_links(html, final_page_url)
            except Exception as exc:
                issues.append(
                    FetchIssue(
                        url=page_url,
                        reason=f"公告列表页解析失败：{exc.__class__.__name__}",
                        stage="list",
                    )
                )
                continue

            for next_url in links.pagination:
                if next_url not in seen_pages and next_url not in queue:
                    queue.append(next_url)

            detail_urls = [url for url in links.detail_urls if url not in seen_details]
            if detail_count + len(detail_urls) > MAX_DETAIL_PAGES:
                incomplete = True
                warnings.append(f"公告详情超过 {MAX_DETAIL_PAGES} 页，已停止继续读取。")
                detail_urls = detail_urls[: max(0, MAX_DETAIL_PAGES - detail_count)]

            async def fetch_detail(detail_url: str):
                html, fetch_error, final_url = await _fetch(client, detail_url, "detail")
                return detail_url, html, fetch_error, final_url

            results = await asyncio.gather(
                *(fetch_detail(url) for url in detail_urls),
                return_exceptions=False,
            )

            for detail_url, detail_html, fetch_error, final_detail_url in results:
                if detail_url in seen_details:
                    continue
                seen_details.add(detail_url)
                detail_count += 1
                if detail_html is None:
                    issues.append(
                        FetchIssue(
                            url=detail_url,
                            reason=f"公告详情页抓取失败：{fetch_error}",
                            stage="detail",
                        )
                    )
                    continue
                source_url = final_detail_url or detail_url
                try:
                    parsed = parse_notice_html(detail_html, source_url)
                except Exception as exc:
                    issues.append(
                        FetchIssue(
                            url=detail_url,
                            reason=f"公告详情页解析失败：{exc.__class__.__name__}",
                            stage="detail",
                        )
                    )
                    continue
                # 初筛只依据招标（原）公告；延期/终止/结果公告不含地点口径，
                # 其投标状态由截标核验负责。
                if parsed.notice_type != "original":
                    continue
                notice_count += 1
                original_notices.append((parsed, source_url))
    finally:
        if owns_client:
            await client.aclose()

    if issues:
        # 任何分页或详情读取失败都使整轮不完整，不得声称“没有候选”。
        incomplete = True

    candidates, review, outside = _build_lots(original_notices, coverage, warnings)

    return _outcome(
        complete=not incomplete,
        candidates=candidates,
        review=review,
        outside=outside,
    )
