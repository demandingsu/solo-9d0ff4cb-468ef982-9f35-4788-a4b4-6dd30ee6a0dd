"""Regression tests for the region screening rules.

Run with:  python -m unittest discover -s tests -v
Uses httpx's in-process mock transport; no network or demo container needed.
"""

from __future__ import annotations

import unittest

import httpx

from app.crawler import parse_notice_html
from app.screening import (
    STATUS_MATCHED,
    STATUS_OUTSIDE,
    STATUS_UNCLEAR,
    parse_coverage,
    parse_region,
    resolve_place,
    screen_regions,
)

COVERAGE_TEXT = "云洲省云州市滨河区\n云洲省云州市西岭县"


def detail(
    project: str,
    section: str,
    body: str,
    *,
    place: str | None = None,
    extra: str = "",
    type_code: str = "original",
) -> str:
    place_dd = f'<dd data-place="{place}">{place}</dd>' if place else ""
    return f"""
    <html><body><article
      data-project-no="{project}" data-section-no="{section}"
      data-notice-type="{type_code}" data-publish-time="2026-10-11 09:00">
      <h1>公告 {project}</h1>
      <dl><dt>项目编号</dt><dd>{project}</dd>{place_dd}</dl>
      <p>项目编号：{project}；标段编号：{section}。</p>
      <p>{body}</p>
      {extra}
    </article></body></html>
    """


MULTI_LOT = """
<html><body><article
  data-project-no="P-100" data-section-no="01,02,03"
  data-notice-type="original" data-publish-time="2026-10-11 09:00">
  <h1>多标段工程招标公告</h1>
  <dl>
    <dt>项目编号</dt><dd>P-100</dd>
    <dt>项目地点</dt><dd data-place="云洲省云州市滨河区">云洲省云州市滨河区</dd>
    <dt>代理机构地址</dt><dd>云洲省澜江市高新区创业大厦18层</dd>
  </dl>
  <table>
    <tr data-section-no="01"><td>01</td><td data-place="云洲省云州市北岸区港湾大道">云洲省云州市北岸区港湾大道</td></tr>
    <tr data-section-no="02"><td>02</td><td data-place="云洲省云州市滨河区临江路">云洲省云州市滨河区临江路</td></tr>
    <tr data-section-no="03"><td>03</td><td>（未单列，沿用项目地点）</td></tr>
  </table>
  <p>项目编号：P-100；标段编号：01、02、03。</p>
  <p>项目地点：云洲省云州市滨河区。</p>
  <p>代理机构地址：云洲省澜江市高新区创业大厦18层。</p>
</article></body></html>
"""

TWO_PLACES = detail(
    "P-200",
    "01",
    "交付地点：云洲省云州市滨河区港前路3号库、云洲省云州市西岭县集镇仓储点。",
    place="云洲省云州市滨河区港前路3号库、云洲省云州市西岭县集镇仓储点",
)
MIXED_PLACES = detail(
    "P-201",
    "01",
    "交付地点：云洲省云州市滨河区港前路5号库、云洲省澜江市高新区物流园。",
    place="云洲省云州市滨河区港前路5号库、云洲省澜江市高新区物流园",
)
BARE_DISTRICT = detail(
    "P-202",
    "01",
    "施工地点：滨河区滨河大道两侧绿地。",
    place="滨河区滨河大道两侧绿地",
)
NO_PLACE = detail(
    "P-203",
    "01",
    "开标地点：云洲省云州市滨河区公共资源交易中心二楼。"
    "投标文件递交地点：云洲省云州市滨河区公共资源交易中心二楼。"
    "代理机构地址：云洲省云州市滨河区政务大道66号。",
)
EXTENSION_WITH_PLACE = detail(
    "P-200",
    "01",
    "交付地点：云洲省澜江市高新区物流园。",
    place="云洲省澜江市高新区物流园",
    type_code="extension",
)


def link_list(hrefs: list[str], next_href: str | None = None) -> str:
    rows = "".join(
        f'<li><a class="notice-link" href="{href}">公告 {href}</a></li>' for href in hrefs
    )
    pager = f'<a rel="next" href="{next_href}">下一页</a>' if next_href else ""
    return f"<html><body><main><ul>{rows}</ul>{pager}</main></body></html>"


def make_factory(routes: dict[str, str], failing: set[str] | None = None):
    failing = failing or set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in failing:
            return httpx.Response(503, text="service unavailable")
        if path not in routes:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=routes[path], headers={"content-type": "text/html"})

    def factory():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        )

    return factory


def base_routes() -> dict[str, str]:
    return {
        "/list": link_list(
            ["/d/multi", "/d/two", "/d/mixed", "/d/ext"], next_href="/list2"
        ),
        "/list2": link_list(["/d/bare", "/d/none"]),
        "/d/multi": MULTI_LOT,
        "/d/two": TWO_PLACES,
        "/d/mixed": MIXED_PLACES,
        "/d/bare": BARE_DISTRICT,
        "/d/none": NO_PLACE,
        "/d/ext": EXTENSION_WITH_PLACE,
    }


class CoverageParseTests(unittest.TestCase):
    def test_full_entries_are_parsed(self):
        coverage, warnings = parse_coverage(COVERAGE_TEXT)
        self.assertEqual(warnings, [])
        self.assertEqual(
            [region.label() for region in coverage],
            ["云洲省云州市滨河区", "云洲省云州市西岭县"],
        )

    def test_bare_district_is_rejected_with_warning(self):
        coverage, warnings = parse_coverage("滨河区")
        self.assertEqual(coverage, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("缺少上级", warnings[0])

    def test_unparseable_entry_is_rejected_with_warning(self):
        coverage, warnings = parse_coverage("云洲省云州市滨河区、随便什么地址")
        self.assertEqual(len(coverage), 1)
        self.assertEqual(len(warnings), 1)

    def test_municipality_counts_as_city_level(self):
        coverage, warnings = parse_coverage("北京市海淀区")
        self.assertEqual(warnings, [])
        self.assertEqual(coverage[0].city, "北京市")
        self.assertEqual(coverage[0].district, "海淀区")


class RegionResolveTests(unittest.TestCase):
    def setUp(self):
        self.coverage, _ = parse_coverage(COVERAGE_TEXT)

    def test_full_path_inside_coverage(self):
        result = resolve_place("云洲省云州市滨河区临江路12号", "notice", self.coverage)
        self.assertEqual(result.status, STATUS_MATCHED)
        self.assertEqual(result.region_label, "云洲省云州市滨河区")

    def test_full_path_outside_coverage(self):
        result = resolve_place("云洲省澜江市高新区物流园", "notice", self.coverage)
        self.assertEqual(result.status, STATUS_OUTSIDE)

    def test_district_without_superior_is_unclear(self):
        # “滨河区”虽在覆盖名单里，但原文缺少上级区划，不得猜测归属。
        result = resolve_place("滨河区滨河大道两侧绿地", "notice", self.coverage)
        self.assertEqual(result.status, STATUS_UNCLEAR)
        self.assertIn("缺少上级", result.reason)

    def test_unidentifiable_place_is_unclear(self):
        result = resolve_place("详见图纸", "notice", self.coverage)
        self.assertEqual(result.status, STATUS_UNCLEAR)

    def test_same_district_name_in_other_city_is_not_matched(self):
        result = resolve_place("云洲省澜江市滨河区某路", "notice", self.coverage)
        self.assertEqual(result.status, STATUS_OUTSIDE)

    def test_region_chain_parsing(self):
        region = parse_region("云洲省云州市西岭县集镇仓储点")
        self.assertEqual(
            (region.province, region.city, region.district),
            ("云洲省", "云州市", "西岭县"),
        )


class PlaceExtractionTests(unittest.TestCase):
    def test_structured_notice_and_section_places(self):
        parsed = parse_notice_html(MULTI_LOT, "http://test/d/multi")
        by_scope = {(p.scope, p.section) for p in parsed.places}
        self.assertIn(("notice", None), by_scope)
        self.assertIn(("section", "01"), by_scope)
        self.assertIn(("section", "02"), by_scope)
        # 代理机构地址不得成为任何级别的项目地点。
        self.assertFalse(any("澜江市高新区" in p.raw for p in parsed.places))

    def test_opening_and_submission_places_are_ignored(self):
        parsed = parse_notice_html(NO_PLACE, "http://test/d/none")
        self.assertEqual(parsed.places, [])

    def test_text_label_produces_notice_scope(self):
        parsed = parse_notice_html(TWO_PLACES, "http://test/d/two")
        raws = [p.raw for p in parsed.places]
        self.assertIn("云洲省云州市滨河区港前路3号库", raws)
        self.assertIn("云洲省云州市西岭县集镇仓储点", raws)
        self.assertTrue(all(p.scope == "notice" for p in parsed.places))


class ScreeningTests(unittest.IsolatedAsyncioTestCase):
    async def screen(self, routes, coverage=COVERAGE_TEXT, failing=None):
        return await screen_regions(
            "http://test/list",
            coverage,
            client_factory=make_factory(routes, failing),
        )

    async def test_full_round_splits_lots_correctly(self):
        outcome = await self.screen(base_routes())
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.notice_count, 5)  # 延期公告不参与地点初筛

        candidates = {(lot.project_no, lot.section_no) for lot in outcome.candidates}
        review = {(lot.project_no, lot.section_no) for lot in outcome.review}
        outside = {(lot.project_no, lot.section_no) for lot in outcome.outside}

        # 多标段公告：02 用专属地点（覆盖内），03 补公告共用地点（覆盖内）。
        self.assertIn(("P-100", "02"), candidates)
        self.assertIn(("P-100", "03"), candidates)
        # 01 有专属地点（北岸区），公告共用的滨河区地点不得补给它。
        self.assertIn(("P-100", "01"), outside)
        # 同一标段多个履约地点全部在覆盖内才是候选。
        self.assertIn(("P-200", "01"), candidates)
        # 一个地点在覆盖外即整个标段落在范围外。
        self.assertIn(("P-201", "01"), outside)
        # 地点缺上级区划 → 待核；未载明履约地点 → 待核。
        self.assertIn(("P-202", "01"), review)
        self.assertIn(("P-203", "01"), review)

        # 待核与候选标段都必须带地点原文、来源和截标核对入口。
        result = outcome.as_dict(check_url_fn=lambda p, s: f"/check?p={p}&s={s}")
        for group in ("candidates", "review"):
            for lot in result[group]:
                self.assertTrue(lot["sources"][0]["url"])
                self.assertTrue(lot["check_url"])
        p100_03 = next(
            lot for lot in result["candidates"] if lot["section_no"] == "03"
        )
        self.assertEqual(p100_03["places"][0]["scope"], "notice")
        self.assertEqual(p100_03["places"][0]["raw"], "云洲省云州市滨河区")

    async def test_agency_address_never_counts_as_project_place(self):
        outcome = await self.screen(base_routes())
        p100_03 = next(
            lot for lot in outcome.candidates if lot.section_no == "03"
        )
        self.assertEqual([p.raw for p in p100_03.places], ["云洲省云州市滨河区"])
        # 若代理机构地址（澜江市高新区）被算入，03 标段会落入范围外。
        no_place_lot = next(
            lot for lot in outcome.review if lot.project_no == "P-203"
        )
        self.assertEqual(no_place_lot.places, [])

    async def test_detail_failure_marks_round_incomplete(self):
        outcome = await self.screen(base_routes(), failing={"/d/two"})
        self.assertFalse(outcome.complete)
        self.assertTrue(outcome.issues)
        # 已读到的候选仍列出，但 complete=False 阻止“没有候选/已穷尽”的说法。
        self.assertTrue(any(lot.project_no == "P-100" for lot in outcome.candidates))
        self.assertFalse(any(lot.project_no == "P-200" for lot in outcome.candidates))

    async def test_list_failure_marks_round_incomplete(self):
        outcome = await self.screen(base_routes(), failing={"/list2"})
        self.assertFalse(outcome.complete)
        self.assertEqual(outcome.issues[0].stage, "list")

    async def test_invalid_coverage_aborts_before_crawling(self):
        outcome = await self.screen(base_routes(), coverage="滨河区")
        self.assertIsNotNone(outcome.coverage_error)
        self.assertFalse(outcome.complete)
        self.assertEqual(outcome.detail_page_count, 0)

    async def test_empty_directory_yields_no_candidates_but_complete(self):
        routes = {"/list": link_list([])}
        outcome = await self.screen(routes)
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.candidates, [])
        self.assertEqual(outcome.review, [])


if __name__ == "__main__":
    unittest.main()
