"""论文发表状态核验。

这是全项目可信度的关键一环。规则刻意保守：

* 只有 OpenAlex 给出 journal / conference 类型的来源、且该来源命中
  配置里的顶会顶刊白名单，才允许标记「正式发表」；
* arXiv 页面上的作者备注（comment）、journal-ref、DOI 一律只作为
  「待核实」的线索，不作为结论；
* 任何情况下都不允许用大模型的判断来决定发表状态。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from models import Paper, PaperStatus

LOGGER = logging.getLogger(__name__)

ACADEMIC_SOURCE_TYPES = ("journal", "conference")


@dataclass(frozen=True)
class _Rule:
    name: str
    patterns: tuple[str, ...]
    match: str  # contains | exact


class PublicationVerifier:
    def __init__(self, venue_whitelist: Iterable[dict[str, Any]]) -> None:
        self._rules = _compile_rules(venue_whitelist)
        LOGGER.debug("加载顶会/顶刊白名单 %d 条", len(self._rules))
        if not self._rules:
            LOGGER.warning("顶会/顶刊白名单为空，本期不会有论文被标记为「正式发表」")

    def apply(
        self,
        paper: Paper,
        record: Optional[dict[str, Any]],
        enrichment_available: bool = True,
    ) -> Paper:
        """就地写入 paper.status / status_evidence / status_source_url。"""
        hints: list[str] = []

        if record:
            self._fill_from_record(paper, record)
            venue = record.get("venue")
            venue_type = str(record.get("venue_type") or "")
            if venue and venue_type in ACADEMIC_SOURCE_TYPES:
                matched = self._match_venue(venue)
                paper.status_source_url = record.get("openalex_id") or paper.arxiv_url
                if matched:
                    paper.status = PaperStatus.PUBLISHED
                    paper.status_evidence = (
                        f"OpenAlex 记录显示该论文发表于《{venue}》"
                        f"（来源类型：{venue_type}），命中顶会/顶刊白名单「{matched}」"
                    )
                else:
                    paper.status = PaperStatus.UNVERIFIED
                    paper.status_evidence = (
                        f"OpenAlex 记录显示该论文发表于《{venue}》，"
                        f"但该来源不在顶会/顶刊白名单内，级别未经核验"
                    )
                return paper
            hints.append("OpenAlex 记录中未发现 journal / conference 类型的发表来源")
        elif not enrichment_available:
            hints.append("OpenAlex 本次不可用，未能核验发表状态")

        if paper.journal_ref:
            hints.append(f"arXiv 页面 journal-ref 标注为「{paper.journal_ref}」")
        if paper.arxiv_doi:
            hints.append(f"arXiv 页面标注 DOI 为 {paper.arxiv_doi}")
        if paper.comment:
            hints.append(f"arXiv 页面作者备注为「{paper.comment}」")

        paper.status_source_url = paper.arxiv_url
        if hints:
            paper.status = PaperStatus.UNVERIFIED
            paper.status_evidence = (
                "；".join(hints) + "。以上信息均来自 arXiv 页面，未经会议/期刊官方渠道核验。"
            )
        else:
            paper.status = PaperStatus.PREPRINT
            paper.status_evidence = "仅确认在 arXiv 平台公开，未检索到正式发表的对应记录。"

        LOGGER.debug("%s -> %s", paper.arxiv_id, paper.status)
        return paper

    def mark_unverified(self, paper: Paper, reason: str) -> Paper:
        """用于「本次没有对该论文执行核验」的情况，如实标记为待核实。"""
        paper.status = PaperStatus.UNVERIFIED
        paper.status_evidence = reason
        paper.status_source_url = paper.arxiv_url
        return paper

    def match_venue(self, venue: str) -> Optional[str]:
        """来源名是否命中白名单，命中则返回白名单里的名称。"""
        return self._match_venue(venue)

    # ------------------------------------------------------------------
    def _match_venue(self, venue: str) -> Optional[str]:
        normalized = _normalize_venue(venue)
        for rule in self._rules:
            for pattern in rule.patterns:
                if rule.match == "exact":
                    if normalized == pattern:
                        return rule.name
                elif re.search(
                    rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])", normalized
                ):
                    return rule.name
        return None

    @staticmethod
    def _fill_from_record(paper: Paper, record: dict[str, Any]) -> None:
        paper.openalex_id = record.get("openalex_id") or paper.openalex_id
        paper.doi = record.get("doi") or paper.doi or (paper.arxiv_doi or None)
        paper.venue = record.get("venue") or paper.venue
        paper.venue_type = record.get("venue_type") or paper.venue_type
        paper.venue_url = record.get("venue_url") or paper.venue_url
        paper.publication_date = record.get("publication_date") or paper.publication_date
        paper.cited_by_count = record.get("cited_by_count")


def _compile_rules(whitelist: Iterable[dict[str, Any]]) -> list[_Rule]:
    rules: list[_Rule] = []
    for item in whitelist or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        mode = str(item.get("match") or "contains").lower()
        if mode not in ("contains", "exact"):
            LOGGER.warning("白名单条目 %s 的 match 取值非法（%s），回退为 contains", name, mode)
            mode = "contains"
        raw_patterns = [name] + [str(a) for a in (item.get("aliases") or [])]
        patterns = tuple(
            p for p in (_normalize_venue(x) for x in raw_patterns) if p
        )
        if patterns:
            rules.append(_Rule(name=name, patterns=patterns, match=mode))
    return rules


def _normalize_venue(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()