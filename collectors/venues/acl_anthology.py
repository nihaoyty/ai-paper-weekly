"""ACL Anthology 采集器：ACL / EMNLP / NAACL。

ACL Anthology 的卷页（如 2026.acl-long）整页 6.8MB，逐卷抓很容易超时；
官方提供了一个全量 BibTeX 数据包，**自带 abstract 字段**，一次下载即可。

实测（2026-09）：`anthology+abstracts.bib.gz` 约 40.5MB，2026 年会议已收录。

内存注意：解压后是数百 MB，所以用 gzip 流式逐条读取，读完一条就丢弃，
峰值内存只跟单条记录成正比。
"""

from __future__ import annotations

import gzip
import io
import re

from .base import (
    LOGGER,
    OfficialVenueClient,
    VenueRecord,
    clean_bibtex_value,
    split_bibtex_authors,
)

BIB_URL = "https://aclanthology.org/anthology+abstracts.bib.gz"

ENTRY_START_RE = re.compile(r"@\w+\s*\{\s*(?P<key>[^,\s]+)\s*,")

# 卷标识不在引用键里（引用键形如 "yung-etal-2026-semi" 或 "wildre-2026-1"），
# 而在 url 字段里：https://aclanthology.org/2026.acl-long.1/
ANTHOLOGY_URL_RE = re.compile(
    r"aclanthology\.org/(?P<year>\d{4})\.(?P<collection>[a-z0-9][a-z0-9-]*)\.(?P<num>\d+)",
    re.I,
)

# 卷标识前缀到会议名的映射
VENUE_TOKENS = (("emnlp", "EMNLP"), ("naacl", "NAACL"), ("acl", "ACL"))

# Findings 不是主会论文集，教程也不是研究论文，标成会议名会失真，直接跳过
SKIP_COLLECTION_MARKERS = ("findings", "tutorial")


def _field(block: str, name: str) -> str:
    """从一条 BibTeX 记录里取出字段值，兼容 "..." 与 {...} 两种写法。"""
    match = re.search(rf"(?:^|,|\n)\s*{re.escape(name)}\s*=\s*", block, re.I)
    if not match:
        return ""
    start = match.end()
    if start >= len(block):
        return ""
    opener = block[start]
    if opener == '"':
        index = start + 1
        while index < len(block):
            if block[index] == '"' and block[index - 1] != "\\":
                break
            index += 1
        return block[start + 1 : index]
    if opener == "{":
        depth = 0
        index = start
        while index < len(block):
            if block[index] == "{":
                depth += 1
            elif block[index] == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        return block[start + 1 : index]
    return re.match(r"[^,\n]*", block[start:]).group(0).strip()


def parse_anthology_url(url: str) -> tuple[int, str, int] | None:
    """从 ACL Anthology 的 url 解析出 (年份, 卷标识, 序号)。"""
    match = ANTHOLOGY_URL_RE.search(str(url or ""))
    if not match:
        return None
    return int(match.group("year")), match.group("collection").lower(), int(match.group("num"))


def venue_from_collection(collection: str) -> str:
    """卷标识 → 会议名。判不出来返回空串。"""
    lowered = collection.lower()
    if any(marker in lowered for marker in SKIP_COLLECTION_MARKERS):
        return ""
    for token, venue in VENUE_TOKENS:
        if lowered.startswith(token):
            return venue
    return ""


def iter_bibtex_entries(stream):
    """流式解析 BibTeX，逐条产出 (引用键, 记录正文)。

    这里必须是生成器：解压后的 BibTeX 有数百 MB，
    一次性读进内存会把峰值内存推高到 GB 级别。
    """
    buffer: list[str] = []
    key: str | None = None
    for raw in stream:
        line = raw.decode("utf-8", "replace")
        if line.startswith("@"):
            if key is not None:
                yield key, "".join(buffer)
            match = ENTRY_START_RE.match(line)
            key = match.group("key") if match else None
            buffer = [line]
        elif key is not None:
            buffer.append(line)
    if key is not None:
        yield key, "".join(buffer)


class AclAnthologyClient(OfficialVenueClient):
    id = "acl_anthology"
    label = "ACL Anthology"

    def fetch_index(self) -> list[VenueRecord]:
        wanted_venues = set(self.venues or ["ACL", "EMNLP", "NAACL"])
        wanted_years = set(self.years)

        raw = self.cache.get_or_fetch(
            "acl-anthology-abstracts", self._download_bib, suffix=".bib.gz"
        )
        records: list[VenueRecord] = []
        seen: set[str] = set()
        scanned = 0
        for _, block in iter_bibtex_entries(gzip.open(io.BytesIO(raw))):
            scanned += 1
            url = clean_bibtex_value(_field(block, "url"))
            parsed = parse_anthology_url(url)
            if parsed is None:
                continue
            year, collection, number = parsed
            if year not in wanted_years:
                continue
            # 序号 0 是卷首/前言这类整卷条目，不是论文
            if number == 0:
                continue
            venue = venue_from_collection(collection)
            if not venue or venue not in wanted_venues:
                continue
            title = clean_bibtex_value(_field(block, "title"))
            if not title:
                continue
            page_url = url or f"https://aclanthology.org/{year}.{collection}.{number}/"
            if page_url in seen:
                continue
            seen.add(page_url)
            records.append(
                VenueRecord(
                    title=title,
                    authors=split_bibtex_authors(_field(block, "author")),
                    page_url=page_url,
                    venue=f"{venue} {year}",
                    venue_type="conference",
                    year=year,
                    # 数据包自带摘要，不需要再访问论文页
                    abstract=clean_bibtex_value(_field(block, "abstract")),
                )
            )
        LOGGER.info(
            "[%s] 扫描 %d 条记录，命中 %s%d 年论文 %d 篇",
            self.label, scanned, "/".join(sorted(wanted_venues)),
            min(wanted_years) if wanted_years else 0, len(records),
        )
        return records

    def _download_bib(self) -> bytes:
        return self.fetch_bytes(BIB_URL)