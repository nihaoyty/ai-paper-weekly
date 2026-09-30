"""CVF Open Access 采集器：CVPR / ICCV / ECCV。

这两个会议（CVPR/ICCV/ECCV）的官方论文集统一由 Computer Vision Foundation
的 Open Access 站点托管，页面结构简单且稳定。

实测（2026-09）：CVPR 2026 用 `?day=all` 一次拿到全量 4042 篇（约 12MB）。

注意：ICCV 是奇数年、ECCV 是偶数年，2026 年只有 CVPR 与 ECCV 存在，
缺失的会议会返回 404，此时只记日志不报错。
"""

from __future__ import annotations

import re

from .base import LOGGER, OfficialVenueClient, VenueRecord, html_to_text

ORIGIN = "https://openaccess.thecvf.com"
INDEX_URL = ORIGIN + "/{venue}{year}?day=all"

ENTRY_SPLIT_RE = re.compile(r'<dt\s+class="ptitle">', re.I)
# 论文链接形如 href="/content/CVPR2026/html/Author_Title_CVPR_2026_paper.html"
# 必须连 href 一起取下来：文件名是 <slug>_paper.html，按 slug 重新拼会丢掉 "_paper"，
# 拼出来的地址会 404，摘要也就抓不到了。
PAPER_LINK_RE = re.compile(
    r'href="(?P<href>[^"]*?html/(?P<slug>[^/"]+)_paper\.html)"[^>]*>(?P<text>.*?)</a>',
    re.S | re.I,
)
# 只取主论文 PDF：supplemental 在它后面
PDF_LINK_RE = re.compile(r'href="(?P<url>[^"]*/papers/[^"]+\.pdf)"', re.I)
# 条目里内嵌了官方 BibTeX，作者字段比页面上的作者链接干净得多
BIBTEX_AUTHOR_RE = re.compile(r"author\s*=\s*\{(?P<text>[^}]*)\}", re.I)
AUTHOR_INPUT_RE = re.compile(r'name="query_author"\s+value="(?P<name>[^"]*)"', re.I)
ABSTRACT_RE = re.compile(r'id="abstract"[^>]*>(?P<text>.*?)</div>', re.S | re.I)


def _absolute(url: str) -> str:
    """CVF 页面里的链接是站内绝对路径（/content/...），补上站点前缀。"""
    if not url:
        return ""
    return url if url.startswith("http") else ORIGIN + "/" + url.lstrip("/")


class CvfClient(OfficialVenueClient):
    id = "cvf"
    label = "CVF Open Access"

    def fetch_index(self) -> list[VenueRecord]:
        records: list[VenueRecord] = []
        for year in self.years:
            for venue in self.venues or ["CVPR"]:
                url = INDEX_URL.format(venue=venue, year=year)
                html = self.fetch_text(url)
                if not html:
                    LOGGER.warning(
                        "[%s] %s%d 索引不可用（可能该会议本年不召开）：%s",
                        self.label, venue, year, url,
                    )
                    continue
                records.extend(self._parse_index(html, venue, year))
        return records

    def _parse_index(self, html: str, venue: str, year: int) -> list[VenueRecord]:
        records: list[VenueRecord] = []
        seen: set[str] = set()
        for chunk in ENTRY_SPLIT_RE.split(html)[1:]:
            link = PAPER_LINK_RE.search(chunk)
            if not link:
                continue
            slug = link.group("slug")
            if slug in seen:
                continue
            title = html_to_text(link.group("text"))
            if not title:
                continue
            seen.add(slug)

            authors_match = BIBTEX_AUTHOR_RE.search(chunk)
            if authors_match:
                authors = [
                    " ".join(name.split())
                    for name in authors_match.group("text").split(" and ")
                    if name.strip()
                ]
            else:
                authors = [
                    " ".join(name.split())
                    for name in AUTHOR_INPUT_RE.findall(chunk)
                    if name.strip()
                ]
            pdf_match = PDF_LINK_RE.search(chunk)
            page_url = _absolute(link.group("href"))
            records.append(
                VenueRecord(
                    title=title,
                    authors=authors,
                    page_url=page_url,
                    pdf_url=_absolute(pdf_match.group("url")) if pdf_match else "",
                    venue=f"{venue} {year}",
                    venue_type="conference",
                    year=year,
                    abstract_url=page_url,
                )
            )
        LOGGER.info("[%s] %s%d 索引取得 %d 篇", self.label, venue, year, len(records))
        return records

    def extract_abstract(self, html: str) -> str:
        match = ABSTRACT_RE.search(html)
        if not match:
            return ""
        return html_to_text(match.group("text"))