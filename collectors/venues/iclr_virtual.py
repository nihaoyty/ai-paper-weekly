"""ICLR 官方 virtual 站点采集器。

为什么不用 OpenReview：ICLR 的论文数据源本是 OpenReview API，
但该接口已被人机验证封死（实测返回 403），无法在无人值守的脚本中调用。
ICLR 官方的 virtual 站点（iclr.cc/virtual/<year>）公开列出全部录用论文，
因此改从这里抓取，这同样是「会议官方页面」，可以作为正式发表的证据。

实测（2026-09）：ICLR 2026 索引页共 5468 篇。
"""

from __future__ import annotations

import re

from .base import LOGGER, OfficialVenueClient, VenueRecord, html_to_text

INDEX_URL = "https://iclr.cc/virtual/{year}/papers.html"

# 论文链接：/virtual/2026/poster/12345
POSTER_LINK_RE = re.compile(
    r'href="(?:https?://iclr\.cc)?/virtual/(?P<year>\d{4})/poster/(?P<pid>\d+)"'
    r'[^>]*>(?P<text>.*?)</a>',
    re.S | re.I,
)

# 单篇论文页的摘要块。ICLR virtual 用 <h3>Abstract</h3><div>...</div> 的结构。
ABSTRACT_RE = re.compile(r"Abstract\s*</h\d>\s*<div[^>]*>(.*?)</div>", re.S | re.I)

# 索引页只有标题，作者藏在单篇页的 JSON-LD 里：
#   "author": [ {"@type": "Person", "name": "Yuchen Yan"}, ... ]
JSONLD_AUTHOR_RE = re.compile(r'"author"\s*:\s*\[(?P<block>.*?)\]', re.S)
JSONLD_NAME_RE = re.compile(r'"name"\s*:\s*"(?P<name>[^"]+)"')


class IclrVirtualClient(OfficialVenueClient):
    id = "iclr_virtual"
    label = "ICLR virtual"

    def fetch_index(self) -> list[VenueRecord]:
        records: list[VenueRecord] = []
        for year in self.years:
            url = INDEX_URL.format(year=year)
            html = self.fetch_text(url)
            if not html:
                LOGGER.warning("[%s] %d 年索引页不可用：%s", self.label, year, url)
                continue
            records.extend(self._parse_index(html, year))
        return records

    def _parse_index(self, html: str, year: int) -> list[VenueRecord]:
        records: list[VenueRecord] = []
        seen: set[str] = set()
        for match in POSTER_LINK_RE.finditer(html):
            pid = match.group("pid")
            if pid in seen:
                continue
            title = html_to_text(match.group("text"))
            if not title:
                continue
            seen.add(pid)
            page_url = f"https://iclr.cc/virtual/{year}/poster/{pid}"
            records.append(
                VenueRecord(
                    title=title,
                    page_url=page_url,
                    venue="ICLR",
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
        return html_to_text(match.group(1))

    def extract_authors(self, html: str) -> list[str]:
        block = JSONLD_AUTHOR_RE.search(html)
        if not block:
            return []
        names = [html_to_text(m.group("name")) for m in JSONLD_NAME_RE.finditer(block.group("block"))]
        # 同名只留一次，顺序保持页面顺序（即作者顺序）
        seen: set[str] = set()
        authors: list[str] = []
        for name in names:
            if name and name not in seen:
                seen.add(name)
                authors.append(name)
        return authors