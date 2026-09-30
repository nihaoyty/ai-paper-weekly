"""PMLR 采集器：ICML / COLM。

PMLR 的卷号是全局递增的（ICML 2025 = v267），无法从年份反推出卷号，
所以先抓一次首页的卷列表，再按「会议名 + 年份」定位到具体卷。

实测（2026-09）：ICML 2026 的卷尚未发布（最新是 v267 = ICML 2025）。
这种情况下只告警、不产出一条假数据，也不去猜一个不存在的卷号。
"""

from __future__ import annotations

import re

from .base import LOGGER, OfficialVenueClient, VenueRecord, html_to_text

LIST_URL = "https://proceedings.mlr.press/"
VOLUME_URL = "https://proceedings.mlr.press/v{num}/"

VENUE_ALIASES = {
    # PMLR 首页把这些会议写成缩写：Proceedings of ICML 2025 / Proceedings of COLM 2025
    "ICML": ("icml", "international conference on machine learning"),
    "COLM": ("colm", "conference on language modeling"),
}

# 首页的卷列表是 <li><a href="v267"><b>Volume 267</b></a> Proceedings of ICML 2025</li>
# 注意链接没有结尾斜杠，卷名也是缩写，所以必须整条 <li> 一起解析，
# 用「附近文字」匹配会串到相邻卷上去。
VOLUME_ITEM_RE = re.compile(
    r'<li[^>]*>\s*<a\s+href="v(?P<num>\d+)/?"[^>]*>(?P<label>.*?)</a>(?P<rest>[^<]*)</li>',
    re.S | re.I,
)
PAPER_BLOCK_RE = re.compile(r'<div\s+class="paper">', re.I)
TITLE_RE = re.compile(r'<p\s+class="title">(?P<text>.*?)</p>', re.S | re.I)
DETAILS_RE = re.compile(r'<p\s+class="details">(?P<text>.*?)</p>', re.S | re.I)
# details 里除了作者还写了卷期信息，必须只取 <span class="authors"> 这一段
AUTHORS_SPAN_RE = re.compile(r'<span\s+class="authors">(?P<text>.*?)</span>', re.S | re.I)
HTML_LINK_RE = re.compile(r'href="(?P<url>[^"]+?\.html)"', re.I)
PDF_LINK_RE = re.compile(r'href="(?P<url>[^"]+?\.pdf)"', re.I)
ABSTRACT_RE = re.compile(r'id="abstract"[^>]*>(?P<text>.*?)</div>', re.S | re.I)


class PmlrClient(OfficialVenueClient):
    id = "pmlr"
    label = "PMLR"

    def fetch_index(self) -> list[VenueRecord]:
        html = self.fetch_text(LIST_URL)
        if not html:
            LOGGER.warning("[%s] 卷列表页不可用：%s", self.label, LIST_URL)
            return []

        records: list[VenueRecord] = []
        for year in self.years:
            for venue in self.venues or ["ICML"]:
                volume = self._find_volume(html, venue, year)
                if volume is None:
                    LOGGER.warning(
                        "[%s] 未找到 %s %d 的论文集卷，官方尚未发布，本期跳过该来源",
                        self.label, venue, year,
                    )
                    continue
                volume_html = self.fetch_text(VOLUME_URL.format(num=volume))
                if not volume_html:
                    LOGGER.warning("[%s] v%d 卷页不可用", self.label, volume)
                    continue
                records.extend(self._parse_volume(volume_html, volume, venue, year))
        return records

    def _iter_volumes(self, html: str):
        """产出首页里的 (卷号, 卷名)。"""
        for match in VOLUME_ITEM_RE.finditer(html):
            text = html_to_text(f"{match.group('label')} {match.group('rest')}")
            yield int(match.group("num")), text

    def _find_volume(self, html: str, venue: str, year: int) -> int | None:
        aliases = VENUE_ALIASES.get(venue, (venue.lower(),))
        for number, text in self._iter_volumes(html):
            lowered = text.lower()
            if str(year) not in lowered:
                continue
            if any(alias in lowered for alias in aliases):
                return number
        return None

    def _parse_volume(
        self, html: str, volume: int, venue: str, year: int
    ) -> list[VenueRecord]:
        records: list[VenueRecord] = []
        seen: set[str] = set()
        for chunk in PAPER_BLOCK_RE.split(html)[1:]:
            title_match = TITLE_RE.search(chunk)
            if not title_match:
                continue
            title = html_to_text(title_match.group("text"))
            if not title:
                continue

            page_url = ""
            for link in HTML_LINK_RE.finditer(chunk):
                url = link.group("url")
                if f"v{volume}/" in url and not url.endswith("/"):
                    page_url = url if url.startswith("http") else f"https://proceedings.mlr.press/{url.lstrip('/')}"
                    break
            if not page_url:
                continue
            if page_url in seen:
                continue
            seen.add(page_url)

            authors: list[str] = []
            authors_span = AUTHORS_SPAN_RE.search(chunk)
            if authors_span:
                authors = [
                    name.strip()
                    for name in html_to_text(authors_span.group("text")).split(",")
                    if name.strip()
                ]
            else:
                details = DETAILS_RE.search(chunk)
                if details:
                    authors = [
                        name.strip()
                        for name in html_to_text(details.group("text")).replace(";", ",").split(",")
                        if name.strip()
                    ]
            pdf_match = PDF_LINK_RE.search(chunk)
            records.append(
                VenueRecord(
                    title=title,
                    authors=authors,
                    page_url=page_url,
                    pdf_url=(
                        pdf_match.group("url")
                        if pdf_match and pdf_match.group("url").startswith("http")
                        else ""
                    ),
                    venue=f"{venue} {year}",
                    venue_type="conference",
                    year=year,
                    abstract_url=page_url,
                )
            )
        LOGGER.info(
            "[%s] v%d（%s %d）索引取得 %d 篇",
            self.label, volume, venue, year, len(records),
        )
        return records

    def extract_abstract(self, html: str) -> str:
        match = ABSTRACT_RE.search(html)
        if not match:
            return ""
        return html_to_text(match.group("text"))