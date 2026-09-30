"""arXiv 论文检索。

只负责「按分类 + 时间窗拿到真实论文元数据」，不做筛选、不做解读。
所有字段都直接来自 arXiv 官方 API 返回，不经过任何模型加工。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

import feedparser
import requests

from models import Paper

LOGGER = logging.getLogger(__name__)

ARXIV_API_URL = "https://export.arxiv.org/api/query"
DEFAULT_TIMEOUT = 30
# arXiv 官方要求请求间隔不少于 3 秒，否则可能被限流
MIN_INTERVAL_SECONDS = 3.0
USER_AGENT = "ai-paper-weekly/0.1 (personal research digest)"


class ArxivError(RuntimeError):
    """arXiv 检索在重试后仍然失败。"""


class ArxivClient:
    def __init__(
        self,
        categories: list[str],
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
    ) -> None:
        if not categories:
            raise ValueError("arXiv 分类不能为空")
        self._categories = list(categories)
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_at = 0.0

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def search(self, since: date, until: date, max_results: int) -> list[Paper]:
        """检索 [since, until] 区间内提交的论文，按提交时间倒序返回。"""
        if since > until:
            raise ValueError(f"时间窗非法：since={since} 晚于 until={until}")

        query = self._build_query(since, until)
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max(1, max_results),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        LOGGER.info(
            "arXiv 检索：%s ~ %s，分类 %d 个，上限 %d 篇",
            since, until, len(self._categories), max_results,
        )
        entries = self._fetch(params)

        papers: list[Paper] = []
        seen: set[str] = set()
        for entry in entries:
            paper = self._parse_entry(entry)
            if paper is None or paper.arxiv_id in seen:
                continue
            seen.add(paper.arxiv_id)
            papers.append(paper)

        LOGGER.info("arXiv 返回 %d 条记录，解析出 %d 篇有效论文", len(entries), len(papers))
        return papers

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _build_query(self, since: date, until: date) -> str:
        cat_expr = " OR ".join(f"cat:{c}" for c in self._categories)
        start = datetime(since.year, since.month, since.day, 0, 0)
        end = datetime(until.year, until.month, until.day, 23, 59)
        date_expr = (
            f"submittedDate:[{start.strftime('%Y%m%d%H%M')} "
            f"TO {end.strftime('%Y%m%d%H%M')}]"
        )
        return f"({cat_expr}) AND {date_expr}"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _fetch(self, params: dict[str, Any]) -> list[Any]:
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            self._throttle()
            try:
                response = self._session.get(
                    ARXIV_API_URL, params=params, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning("arXiv 请求异常（第 %d/%d 次）：%s", attempt, self._max_retries, exc)
            else:
                if response.status_code in (429, 503):
                    last_error = ArxivError(f"arXiv 限流，状态码 {response.status_code}")
                    LOGGER.warning(
                        "arXiv 限流 %d（第 %d/%d 次）", response.status_code, attempt, self._max_retries
                    )
                elif response.status_code >= 400:
                    last_error = ArxivError(f"arXiv 返回状态码 {response.status_code}")
                    LOGGER.warning("arXiv 返回 %d", response.status_code)
                else:
                    feed = feedparser.parse(response.content)
                    if feed.get("bozo") and not feed.entries:
                        last_error = ArxivError(f"arXiv 返回内容无法解析：{feed.get('bozo_exception')}")
                        LOGGER.warning("arXiv 返回内容无法解析")
                    else:
                        return list(feed.entries)

            if attempt < self._max_retries:
                backoff = min(30.0, 5.0 * attempt)
                LOGGER.info("等待 %.0f 秒后重试 arXiv", backoff)
                time.sleep(backoff)

        raise ArxivError(f"arXiv 检索失败，已重试 {self._max_retries} 次：{last_error}")

    def _parse_entry(self, entry: Any) -> Optional[Paper]:
        raw_id = str(entry.get("id") or "").strip()
        if not raw_id:
            return None

        short_id = raw_id.rsplit("/", 1)[-1]
        version_match = re.search(r"v(\d+)$", short_id)
        version = version_match.group(1) if version_match else ""
        arxiv_id = short_id[: version_match.start()] if version_match else short_id

        title = _clean_text(entry.get("title"))
        abstract = _clean_text(entry.get("summary"))
        if not title or not abstract:
            LOGGER.debug("跳过缺少标题或摘要的记录：%s", raw_id)
            return None

        categories = [str(t.get("term")) for t in entry.get("tags", []) if t.get("term")]
        primary = ""
        raw_primary = entry.get("arxiv_primary_category")
        if isinstance(raw_primary, dict):
            primary = str(raw_primary.get("term") or "")
        elif isinstance(raw_primary, str):
            primary = raw_primary
        if not primary and categories:
            primary = categories[0]

        return Paper(
            arxiv_id=arxiv_id,
            arxiv_version=version,
            title=title,
            abstract=abstract,
            authors=[
                str(a.get("name")).strip()
                for a in entry.get("authors", [])
                if a.get("name")
            ],
            published_at=_to_date_str(entry.get("published_parsed")),
            updated_at=_to_date_str(entry.get("updated_parsed")),
            primary_category=primary,
            categories=categories,
            arxiv_url=f"https://arxiv.org/abs/{arxiv_id}",
            pdf_url=self._pdf_url(entry, arxiv_id),
            comment=_clean_text(entry.get("arxiv_comment")),
            journal_ref=_clean_text(entry.get("arxiv_journal_ref")),
            arxiv_doi=_clean_text(entry.get("arxiv_doi")),
        )

    @staticmethod
    def _pdf_url(entry: Any, arxiv_id: str) -> str:
        for link in entry.get("links", []) or []:
            if link.get("type") == "application/pdf":
                return str(link.get("href") or "")
        return f"https://arxiv.org/pdf/{arxiv_id}"


def _clean_text(value: Any) -> str:
    """arXiv 的标题/摘要带换行和多余空格，统一压成单行。"""
    if not value:
        return ""
    return " ".join(str(value).split())


def _to_date_str(struct_time: Any) -> str:
    if not struct_time:
        return ""
    try:
        return datetime(*struct_time[:6]).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def default_window(days: int, today: Optional[date] = None) -> tuple[date, date]:
    """返回 (since, until) 时间窗，until 为今天。"""
    end = today or date.today()
    return end - timedelta(days=days), end