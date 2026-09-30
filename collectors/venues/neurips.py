"""NeurIPS 官方 proceedings 采集器。

实测（2026-09）：2024 年卷可正常抓取（4493 篇），
2025 / 2026 年页面还是空壳（会议未开完或论文集未挂出）。
与 PMLR 一样，缺卷时只告警，不编造。
"""

from __future__ import annotations

import re

from .base import LOGGER, OfficialVenueClient, VenueRecord, html_to_text

INDEX_URL = "https://proceedings.neurips.cc/paper_files/paper/{year}"

ENTRY_RE = re.compile(
    r'href="(?P<url>/paper_files/paper/(?P<year>\d{4})/hash/[0-9a-f]+-Abstract[^"]*)"'
    r'(?P<attrs>[^>]*)>(?P<text>.*?)</a>',
    re.S | re.I,
)
TITLE_ATTR_RE = re.compile(r'title="(?P<text>[^"]*)"', re.I)
ABSTRACT_RE = re.compile(r"<h4>\s*Abstract\s*</h4>\s*<p>(?P<text>.*?)</p>", re.S | re.I)

BASE = "https://proceedings.neurips.cc"


class NeuripsClient(OfficialVenueClient):
    id = "neurips"
    label = "NeurIPS proceedings"

    def fetch_index(self) -> list[VenueRecord]:
        records: list[VenueRecord] = []
        for year in self.years:
            url = INDEX_URL.format(year=year)
            html = self.fetch_text(url)
            if not html:
                LOGGER.warning("[%s] %d 年索引页不可用：%s", self.label, year, url)
                continue
            found = self._parse_index(html, year)
            if not found:
                LOGGER.warning(
                    "[%s] %d 年论文集尚未发布（页面为空），本期跳过该来源",
                    self.label, year,
                )
            records.extend(found)
        return records

    def _parse_index(self, html: str, year: int) -> list[VenueRecord]:
        records: list[VenueRecord] = []
        seen: set[str] = set()
        for match in ENTRY_RE.finditer(html):
            url = match.group("url")
            if url in seen:
                continue
            attr = TITLE_ATTR_RE.search(match.group("attrs") or "")
            title = html_to_text(attr.group("text") if attr else match.group("text"))
            if not title:
                continue
            seen.add(url)
            page_url = url if url.startswith("http") else f"{BASE}{url}"
            records.append(
                VenueRecord(
                    title=title,
                    page_url=page_url,
                    venue=f"NeurIPS {year}",
                    venue_type="conference",
                    year=year,
                    abstract_url=page_url,
                )
            )
        LOGGER.info("[%s] %d 年索引取得 %d 篇", self.label, year, len(records))
        return records

    def extract_abstract(self, html: str) -> str:
        match = ABSTRACT_RE.search(html)
        if not match:
            return ""
        return html_to_text(match.group("text"))