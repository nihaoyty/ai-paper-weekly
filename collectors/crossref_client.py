"""Crossref 客户端：期刊论文的权威发表证据。

为什么不用 OpenAlex 判断期刊论文是否正式发表：
OpenAlex 的来源名是聚合结果，出现「Nature Machine Intelligence」字样
并不等于该论文真的登在这本刊上。Crossref 是 DOI 的注册机构，
它返回的记录直接来自出版商提交的元数据，是判断正式发表的可信依据。

用法上，本模块只回答一个问题：这个 DOI 是否已在 Crossref 注册，注册信息是什么。
它不做任何猜测；查不到就返回 None，由调用方如实标记为「未核实」。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import requests

LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.crossref.org/works/"
DEFAULT_TIMEOUT = 30
MIN_INTERVAL_SECONDS = 0.5


class CrossrefError(RuntimeError):
    """Crossref 不可用（网络异常、限流、5xx）。"""


class CrossrefClient:
    def __init__(
        self,
        mailto: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
    ) -> None:
        self.mailto = (mailto or os.getenv("CROSSREF_MAILTO") or "").strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        # Crossref 的 polite pool：带上联系方式可以获得更稳定的配额
        agent = "AI-Paper-Weekly/1.0"
        if self.mailto:
            agent = f"{agent} (mailto:{self.mailto})"
        self.session.headers.update({"User-Agent": agent, "Accept": "application/json"})
        self._last_request_at = 0.0
        self.requests_made = 0

    def lookup(self, doi: str) -> Optional[dict[str, Any]]:
        """按 DOI 查注册记录。没注册过返回 None；服务异常抛 CrossrefError。"""
        cleaned = _normalize_doi(doi)
        if not cleaned:
            return None

        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, self.max_retries) + 1):
            self._throttle()
            self.requests_made += 1
            try:
                response = self.session.get(API_BASE + cleaned, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning("Crossref 请求异常（第 %d 次）：%s", attempt, exc)
            else:
                if response.status_code == 404:
                    # DOI 未注册。这是有效结论，不是故障。
                    return None
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = RuntimeError(f"状态码 {response.status_code}")
                    LOGGER.warning("Crossref 返回 %d（第 %d 次）", response.status_code, attempt)
                elif response.status_code >= 400:
                    raise CrossrefError(f"Crossref 返回 {response.status_code}：{cleaned}")
                else:
                    try:
                        return normalize_message(response.json().get("message") or {})
                    except ValueError as exc:
                        raise CrossrefError(f"Crossref 返回内容无法解析：{exc}") from exc
            if attempt < self.max_retries:
                time.sleep(min(15.0, 5.0 * attempt))
        raise CrossrefError(f"Crossref 持续不可用：{last_error}")

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()


def _normalize_doi(doi: str) -> str:
    text = str(doi or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "DOI:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix) :]
    return text.strip()


def normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    """把 Crossref 的 message 压成项目里要用的少数字段。"""
    container = message.get("container-title") or []
    titles = message.get("title") or []
    issued = message.get("published") or message.get("issued") or message.get("published-print") or {}
    parts = (issued.get("date-parts") or [[None]])[0]
    published = ""
    if parts and parts[0]:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        day = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        published = f"{year:04d}-{month:02d}-{day:02d}"

    return {
        "doi": message.get("DOI") or "",
        "title": str(titles[0]).strip() if titles else "",
        "container_title": str(container[0]).strip() if container else "",
        "type": str(message.get("type") or ""),
        "publisher": str(message.get("publisher") or ""),
        "published": published,
        "url": str(message.get("URL") or ""),
    }