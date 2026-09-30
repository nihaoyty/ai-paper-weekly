"""期刊论文检索通道（经 OpenAlex）。

补上 arXiv 之外的另一半：**已正式发表**的论文。这些论文自带发表来源，
发表状态可以直接判定为「正式发表」，不需要像 arXiv 预印本那样等着被索引。

关于会议为什么没做：实测三条路都不通，不是没试。
* OpenAlex 对 ML 顶会的论文集索引停在 2021 年（ICML 最新 2021-07，
  NeurIPS 2021-12，ICLR 2021-05），拿不到近期会议论文；
* OpenReview API 返回 403 ChallengeRequiredError，需要过人机验证；
* DBLP 返回 "Making sure you're not a bot!" 拦截页。
要覆盖会议论文集，得单独爬 PMLR / proceedings.neurips.cc / CVF 的静态页面。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Iterable, Optional

from collectors.openalex_client import OpenAlexClient, OpenAlexError
from models import Paper, PaperStatus

LOGGER = logging.getLogger(__name__)


class JournalClient:
    def __init__(
        self,
        client: OpenAlexClient,
        source_names: Iterable[str],
        per_source: int = 30,
    ) -> None:
        self._client = client
        self._source_names = [str(n).strip() for n in source_names if str(n).strip()]
        self._per_source = per_source
        self._resolved: Optional[dict[str, str]] = None

    # ------------------------------------------------------------------
    def resolve_sources(self) -> dict[str, str]:
        """把配置里的期刊名解析成 OpenAlex source ID（只解析一次）。"""
        if self._resolved is not None:
            return self._resolved

        resolved: dict[str, str] = {}
        for name in self._source_names:
            try:
                source_id = self._client.find_source_id(name)
            except OpenAlexError as exc:
                LOGGER.warning("解析期刊来源失败（%s）：%s", name, exc)
                continue
            if source_id:
                resolved[name] = source_id
        self._resolved = resolved
        LOGGER.info(
            "期刊通道就绪：%d/%d 个来源解析成功", len(resolved), len(self._source_names)
        )
        return resolved

    def search(self, since: date, until: date) -> list[Paper]:
        """拉取所有配置期刊在时间窗内正式出版的论文。"""
        sources = self.resolve_sources()
        if not sources:
            LOGGER.warning("没有解析到任何期刊来源，期刊通道本次不产出候选")
            return []

        papers: list[Paper] = []
        for name, source_id in sources.items():
            try:
                works = self._client.recent_works(
                    source_id, since, until, per_page=self._per_source
                )
            except OpenAlexError as exc:
                LOGGER.warning("拉取期刊 %s 失败：%s", name, exc)
                continue
            for work in works:
                paper = self._to_paper(work, fallback_venue=name)
                if paper is not None:
                    papers.append(paper)
            LOGGER.info("期刊 %s：%s ~ %s 共 %d 篇", name, since, until, len(works))

        LOGGER.info("期刊通道共取得 %d 篇候选论文", len(papers))
        return papers

    # ------------------------------------------------------------------
    def _to_paper(
        self, work: dict[str, Any], fallback_venue: str
    ) -> Optional[Paper]:
        record = OpenAlexClient.normalize_work(work)
        title = record.get("title") or ""
        abstract = record.get("abstract") or ""
        if not title:
            return None

        work_type = str(record.get("type") or "")
        venue = record.get("venue") or fallback_venue

        paper = Paper(
            title=title,
            abstract=abstract,
            authors=list(record.get("authors") or []),
            publication_date=record.get("publication_date"),
            doi=record.get("doi"),
            openalex_id=record.get("openalex_id"),
            venue=venue,
            venue_type=record.get("venue_type") or "journal",
            venue_url=record.get("venue_url"),
            cited_by_count=record.get("cited_by_count"),
            work_type=work_type,
            source="journal",
        )

        # 期刊通道拿到的就是正式出版的记录，状态可以直接定，无需再猜
        paper.status = PaperStatus.PUBLISHED
        paper.status_evidence = (
            f"经 OpenAlex 期刊通道检索到：发表于《{venue}》"
            f"（来源类型：{paper.venue_type}，出版日期 {paper.publication_date or '未知'}，"
            f"体裁 {work_type or '未知'}）"
        )
        paper.status_source_url = record.get("openalex_id") or paper.source_url
        return paper