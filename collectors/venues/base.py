"""会议论文集采集的公共部分：种子数据缓存 + 采集器基类。

设计要点：
1. **列表页只给标题**。ICLR / CVF / PMLR / NeurIPS 的整卷索引都不含摘要，
   摘要必须再抓一次论文页（ICLR 单页约 84KB）。所以流程是
   「整卷索引 → 标题关键词粗筛 → 只对候选抓摘要」，绝不全量抓摘要。
2. **大文件必须缓存**。主持有 ACL Anthology 的 40MB BibTeX、CVF 的 8~12MB 索引，
   同一天多次运行不该重复下载。
3. **官方论文集本身就是发表证据**。所以这里产出的论文直接是「正式发表」，
   不需要再去 OpenAlex 反查。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

from models import Paper, PaperStatus

LOGGER = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class VenueRecord:
    """论文集索引里的一条记录。

    摘要要再抓一次论文页，所以「已拿到的摘要」和「摘要地址」分开记。
    """

    title: str
    authors: list[str] = field(default_factory=list)
    page_url: str = ""
    pdf_url: str = ""
    venue: str = ""
    venue_type: str = "conference"
    year: int = 0
    abstract: str = ""
    abstract_url: str = ""


class SeedCache:
    """会议论文集的大文件缓存。"""

    def __init__(self, cache_dir: Path, ttl_days: int = 7) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl = timedelta(days=max(0, ttl_days))
        self.hits = 0
        self.misses = 0

    def path_for(self, key: str, suffix: str = ".cache") -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:56]
        return self.cache_dir / f"{safe}-{digest}{suffix}"

    def is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        return age <= self.ttl

    def get_or_fetch(
        self, key: str, fetcher: Callable[[], bytes], suffix: str = ".cache"
    ) -> bytes:
        path = self.path_for(key, suffix)
        if self.is_fresh(path):
            self.hits += 1
            LOGGER.info("命中缓存：%s", path.name)
            return path.read_bytes()
        self.misses += 1
        LOGGER.info("下载并缓存：%s", key)
        data = fetcher()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return data


class OfficialVenueClient:
    """一个会议论文集来源。子类只需实现 fetch_index()，必要时覆盖摘要解析。"""

    id: str = ""
    label: str = ""

    def __init__(
        self,
        cache: SeedCache,
        years: Iterable[int],
        venues: Iterable[str] = (),
        timeout: int = 60,
        min_interval: float = 1.0,
    ) -> None:
        self.cache = cache
        self.years = sorted({int(y) for y in years})
        self.venues = [str(v).strip() for v in venues if str(v).strip()]
        self.timeout = timeout
        self.min_interval = min_interval
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self._last_request_at = 0.0
        self.abstract_requests = 0

    # ------------------------------------------------------------------
    # 子类实现
    # ------------------------------------------------------------------
    def fetch_index(self) -> list[VenueRecord]:
        raise NotImplementedError

    def extract_abstract(self, html: str) -> str:
        """从论文页 HTML 中取出摘要。默认取不到。"""
        return ""

    def extract_authors(self, html: str) -> list[str]:
        """从论文页 HTML 中取出作者。默认取不到（多数来源在索引页里就给了作者）。"""
        return []

    # ------------------------------------------------------------------
    def collect(self) -> list[Paper]:
        """拉取整卷索引，转成 Paper。摘要留空，等粗筛后再补。"""
        try:
            records = self.fetch_index()
        except Exception as exc:  # noqa: BLE001 单个来源失败不应拖垮整期
            LOGGER.warning("[%s] 索引获取失败：%s", self.label or self.id, exc)
            return []

        papers: list[Paper] = []
        seen: set[str] = set()
        for record in records:
            if not record.title:
                continue
            dedupe = record.page_url or record.title.lower()
            if dedupe in seen:
                continue
            seen.add(dedupe)
            papers.append(self.to_paper(record))
        LOGGER.info(
            "[%s] 取得 %d 篇正式发表论文（年份 %s）",
            self.label or self.id,
            len(papers),
            ",".join(str(y) for y in self.years),
        )
        return papers

    def fill_details(self, paper: Paper) -> bool:
        """按需抓取单篇论文页，补齐摘要（顺带补作者）。

        失败时返回 False，不抛异常：单篇抓不到不该拖垮整期。
        """
        url = paper.abstract_url or paper.venue_url or ""
        if not url:
            return False
        try:
            self.abstract_requests += 1
            html = self.fetch_text(url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[%s] 论文页抓取失败 %s：%s", self.label or self.id, url, exc)
            return False
        abstract = " ".join(self.extract_abstract(html).split())
        if abstract:
            paper.abstract = abstract
        if not paper.authors:
            # ICLR virtual 的索引页只有标题，作者要到单篇页才拿得到
            authors = self.extract_authors(html)
            if authors:
                paper.authors = authors
        if not abstract:
            LOGGER.debug("[%s] 未能从页面解析出摘要：%s", self.label or self.id, url)
        return bool(abstract)

    # ------------------------------------------------------------------
    def to_paper(self, record: VenueRecord) -> Paper:
        paper = Paper(
            title=" ".join(record.title.split()),
            abstract=" ".join(record.abstract.split()),
            authors=list(record.authors),
            venue=record.venue,
            venue_type=record.venue_type,
            venue_url=record.page_url or record.pdf_url,
            pdf_url=record.pdf_url,
            publication_year=record.year or None,
            # 会议论文集只给得出年份，给不出准确日期，所以不填 publication_date，
            # 避免编造一个假的月日。
            publication_date=None,
            source="conference",
            work_type="proceedings-article",
            abstract_url=record.abstract_url or record.page_url,
        )
        paper.status = PaperStatus.PUBLISHED
        paper.status_evidence = (
            f"来自 {record.venue} 官方论文集（{record.year}），属正式发表"
        )
        paper.status_source_url = record.page_url or record.pdf_url
        return paper

    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def fetch_text(self, url: str, retries: int = 3) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, retries) + 1):
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning("[%s] 请求异常（第 %d 次）：%s", self.label or self.id, attempt, exc)
            else:
                if response.status_code == 404:
                    return ""
                if response.status_code >= 400:
                    last_error = RuntimeError(f"状态码 {response.status_code}")
                    LOGGER.warning("[%s] 返回 %d", self.label or self.id, response.status_code)
                else:
                    return response.text
            if attempt < retries:
                time.sleep(min(20.0, 3.0 * attempt))
        raise RuntimeError(f"{url} 请求失败：{last_error}")

    def fetch_bytes(self, url: str, retries: int = 3) -> bytes:
        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, retries) + 1):
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout)
                if response.status_code >= 400:
                    last_error = RuntimeError(f"状态码 {response.status_code}")
                else:
                    return response.content
            except requests.RequestException as exc:
                last_error = exc
            if attempt < retries:
                time.sleep(min(20.0, 3.0 * attempt))
        raise RuntimeError(f"{url} 下载失败：{last_error}")


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def html_to_text(fragment: str) -> str:
    """HTML 片段转纯文本。会议论文集页面结构差异大，只做保守的清洗。"""
    text = _SCRIPT_RE.sub(" ", fragment or "")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = text.replace("&nbsp;", " ")
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    return " ".join(text.split())


def clean_bibtex_value(value: str) -> str:
    """去掉 BibTeX 里用于保护大小写的花括号，并合并空白。"""
    text = re.sub(r"[{}]", "", value or "")
    text = text.replace("\\&", "&").replace("\\%", "%").replace("\\_", "_")
    return " ".join(unescape(text).split())


def split_bibtex_authors(value: str) -> list[str]:
    """BibTeX 作者字段是 "A and B and C"。"""
    text = re.sub(r"[{}]", "", value or "")
    parts = re.split(r"\s+and\s+", text)
    return [" ".join(p.split()) for p in parts if p.strip()]