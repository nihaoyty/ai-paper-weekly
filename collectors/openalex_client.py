"""OpenAlex 元数据补全。

用途只有一个：为 arXiv 论文找到「正式发表来源」，交给
publication_verifier 判定发表状态。这里不做任何推测。
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from datetime import date
from typing import Any, Optional

import requests

LOGGER = logging.getLogger(__name__)

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_SOURCES_URL = "https://api.openalex.org/sources"
DEFAULT_TIMEOUT = 30
# OpenAlex 官方建议请求间隔 0.1 秒以上。实测 0.2 秒仍会触发 429，
# 而期刊通道一次要打十几个请求，所以放宽到 0.5 秒。
MIN_INTERVAL_SECONDS = 0.5
# 标题相似度低于该阈值时认为「没有匹配到同一篇论文」
TITLE_MATCH_THRESHOLD = 0.90


class OpenAlexError(RuntimeError):
    """OpenAlex 在重试后仍不可用（与「查无此记录」是两件事）。"""


class OpenAlexClient:
    def __init__(
        self,
        mailto: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
    ) -> None:
        self._mailto = (mailto or "").strip() or None
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": "ai-paper-weekly/0.1 (personal research digest)"}
        )
        self._last_request_at = 0.0

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def lookup_by_doi(self, doi: str) -> Optional[dict[str, Any]]:
        """按 DOI 精确查询。DOI 是最可靠的匹配方式。"""
        clean = (doi or "").strip()
        if not clean:
            return None
        clean = clean.replace("https://doi.org/", "").replace("http://doi.org/", "")
        payload = self._get(f"{OPENALEX_WORKS_URL}/https://doi.org/{clean}")
        if payload is None:
            return None
        return self._normalize(payload, match_score=1.0)

    def lookup_by_title(self, title: str) -> Optional[dict[str, Any]]:
        """按标题检索，取相似度最高且超过阈值的一条记录。"""
        if not (title or "").strip():
            return None
        payload = self._get(
            OPENALEX_WORKS_URL, params={"search": title, "per-page": 5}
        )
        if not payload:
            return None

        target = _normalize_title(title)
        best_work: Optional[dict[str, Any]] = None
        best_score = 0.0
        for work in payload.get("results") or []:
            score = difflib.SequenceMatcher(
                None, target, _normalize_title(work.get("display_name"))
            ).ratio()
            if score > best_score:
                best_score, best_work = score, work

        if best_work is None or best_score < TITLE_MATCH_THRESHOLD:
            LOGGER.debug("OpenAlex 未匹配到标题：%s（最高相似度 %.3f）", title, best_score)
            return None
        return self._normalize(best_work, match_score=round(best_score, 3))

    # ------------------------------------------------------------------
    # 期刊检索通道使用的方法
    # ------------------------------------------------------------------
    def find_source_id(self, name: str) -> Optional[str]:
        """按名称找 OpenAlex 的发表来源 ID，找不到返回 None。"""
        payload = self._get(
            OPENALEX_SOURCES_URL, params={"search": name, "per-page": 1}
        )
        results = (payload or {}).get("results") or []
        if not results:
            LOGGER.warning("OpenAlex 中没有找到来源：%s", name)
            return None
        source = results[0]
        found = source.get("display_name")
        if _normalize_title(found) != _normalize_title(name):
            # 名称对不上时仍然返回，但记下来，避免静默拉进错误期刊
            LOGGER.warning("OpenAlex 来源名与配置不一致：配置 %s，实际 %s", name, found)
        return source.get("id")

    def recent_works(
        self,
        source_id: str,
        since: date,
        until: date,
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """拉取某个来源在 [since, until] 期间正式出版的论文。"""
        short_id = source_id.rsplit("/", 1)[-1]
        filters = (
            f"primary_location.source.id:{short_id},"
            f"from_publication_date:{since.isoformat()},"
            f"to_publication_date:{until.isoformat()}"
        )
        payload = self._get(
            OPENALEX_WORKS_URL,
            params={
                "filter": filters,
                "per-page": max(1, min(per_page, 200)),
                "sort": "publication_date:desc",
            },
        )
        return (payload or {}).get("results") or []

    @staticmethod
    def normalize_work(work: dict[str, Any], match_score: float = 1.0) -> dict[str, Any]:
        return OpenAlexClient._normalize(work, match_score)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _get(
        self, url: str, params: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        query = dict(params or {})
        if self._mailto:
            query["mailto"] = self._mailto

        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            self._throttle()
            try:
                response = self._session.get(url, params=query, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning("OpenAlex 请求异常（第 %d/%d 次）：%s", attempt, self._max_retries, exc)
            else:
                if response.status_code == 404:
                    return None
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = OpenAlexError(f"OpenAlex 状态码 {response.status_code}")
                    LOGGER.warning("OpenAlex 返回 %d（第 %d 次）", response.status_code, attempt)
                elif response.status_code >= 400:
                    last_error = OpenAlexError(f"OpenAlex 状态码 {response.status_code}")
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        last_error = exc
                        LOGGER.warning("OpenAlex 返回内容不是 JSON")

            if attempt < self._max_retries:
                # 429 是限流，退避要给足，否则只是把失败拖长
                backoff = min(30.0, 5.0 * attempt)
                time.sleep(backoff)

        raise OpenAlexError(f"OpenAlex 不可用，已重试 {self._max_retries} 次：{last_error}")

    @staticmethod
    def _normalize(work: dict[str, Any], match_score: float) -> dict[str, Any]:
        venue = _best_venue(work)
        doi = (work.get("doi") or "").replace("https://doi.org/", "") or None
        return {
            "openalex_id": work.get("id"),
            "doi": doi,
            "title": work.get("display_name") or "",
            "abstract": _reconstruct_abstract(work),
            "authors": _extract_authors(work),
            "publication_date": work.get("publication_date"),
            "publication_year": work.get("publication_year"),
            "type": work.get("type"),
            "cited_by_count": work.get("cited_by_count"),
            "venue": venue["name"] if venue else None,
            "venue_type": venue["type"] if venue else None,
            "venue_url": venue["url"] if venue else None,
            "match_score": match_score,
        }


def _best_venue(work: dict[str, Any]) -> Optional[dict[str, Optional[str]]]:
    """从所有 location 里挑一个「正式出版来源」。

    只认 source.type 为 journal / conference 的来源。如果一篇论文在
    OpenAlex 里只有 arXiv 这类仓储来源，就说明还没找到正式发表记录。
    """
    locations: list[dict[str, Any]] = []
    primary = work.get("primary_location")
    if isinstance(primary, dict):
        locations.append(primary)
    locations.extend([loc for loc in (work.get("locations") or []) if isinstance(loc, dict)])

    fallback: Optional[dict[str, Optional[str]]] = None
    for loc in locations:
        source = loc.get("source")
        if not isinstance(source, dict):
            continue
        name = source.get("display_name")
        if not name:
            continue
        source_type = str(source.get("type") or "").lower()
        entry = {
            "name": str(name),
            "type": source_type or "unknown",
            "url": loc.get("landing_page_url") or source.get("homepage_url"),
        }
        if source_type in ("journal", "conference"):
            return entry
        if fallback is None and source_type != "repository":
            fallback = entry

    return fallback


def _normalize_title(title: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def _reconstruct_abstract(work: dict[str, Any]) -> str:
    """OpenAlex 的摘要是「倒排索引」，需要还原成正常顺序的文本。"""
    inverted = work.get("abstract_inverted_index")
    if not isinstance(inverted, dict) or not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        for index in indexes or []:
            if isinstance(index, int):
                positions.append((index, word))
    if not positions:
        return ""
    positions.sort(key=lambda item: item[0])
    return " ".join(word for _, word in positions)


def _extract_authors(work: dict[str, Any]) -> list[str]:
    names = []
    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        name = (authorship.get("author") or {}).get("display_name")
        if name:
            names.append(str(name).strip())
    return names