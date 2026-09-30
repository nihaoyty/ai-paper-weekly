"""把论文整理成大模型可用的输入，并同时留一份「事实基准」。

这里产出两样东西，两者内容一致：
* snapshot —— 事实校验的基准文本，文章里出现的链接和数字都必须能在这里找到；
* prompt_block —— 真正发给大模型的论文内容，用标签包裹，明确标记为不可信输入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from models import Paper

CONTENT_OPEN = "<untrusted_paper_content>"
CONTENT_CLOSE = "</untrusted_paper_content>"

# 给大模型看的来源说明，比日志里的短标签更具体。
# 说错通道会直接导致大模型写出错误的发表状态描述，所以必须区分清楚。
PROMPT_SOURCE_LABELS = {
    "arxiv": "arXiv 预印本",
    "journal": "期刊论文（OpenAlex 检索，经 Crossref 复核）",
    "conference": "会议官方论文集论文（属正式发表）",
}


@dataclass
class PaperContext:
    paper: Paper
    snapshot: str
    prompt_block: str


def _year_text(paper: Paper) -> str:
    """会议论文集列表页只给得出年份，给不出月日。如实标注「仅年份」。"""
    return f"{paper.year} 年（仅年份）" if paper.year else "未知"


def build_context(paper: Paper) -> PaperContext:
    source_label = PROMPT_SOURCE_LABELS.get(paper.source, "未知来源")
    lines = [
        f"[来源通道] {source_label}",
        f"[英文标题] {paper.title}",
    ]
    if paper.arxiv_id:
        version = f"v{paper.arxiv_version}" if paper.arxiv_version else ""
        lines.append(f"[arXiv ID] {paper.arxiv_id}{version}")
    lines.extend(
        [
            f"[首次公开日期] {paper.first_public_date or _year_text(paper)}",
            f"[最近更新日期] {paper.updated_at or '未知'}",
            f"[作者] {', '.join(paper.authors) if paper.authors else '未知'}",
        ]
    )
    if paper.is_arxiv:
        lines.extend(
            [
                f"[arXiv 主分类] {paper.primary_category or '未知'}",
                f"[arXiv 全部分类] {', '.join(paper.categories) if paper.categories else '未知'}",
            ]
        )
    lines.extend(
        [
            f"[发表状态] {paper.status}",
            f"[发表状态判断依据] {paper.status_evidence or '无'}",
            f"[状态核验来源] {paper.status_source_url or '无'}",
            f"[正式发表来源] {paper.venue or '无'}",
            f"[发表来源类型] {paper.venue_type or '无'}",
            f"[正式发表日期] {paper.publication_date or _year_text(paper)}",
            f"[文献体裁] {paper.work_type or '未知'}",
            f"[DOI] {paper.doi or paper.arxiv_doi or '无'}",
            f"[被引次数] {paper.cited_by_count if paper.cited_by_count is not None else '未知'}",
            f"[原文地址] {paper.source_url or '无'}",
            # PDF 地址所有来源都可能用到（会议论文集也会给官方 PDF），
            # 只对 arXiv 记录会让会议论文的 PDF 链接被判成「来源之外的链接」。
            f"[PDF 地址] {paper.pdf_url or '无'}",
        ]
    )
    if paper.is_arxiv:
        lines.extend(
            [
                f"[arXiv 页面备注] {paper.comment or '无'}",
                f"[arXiv journal-ref] {paper.journal_ref or '无'}",
            ]
        )
    lines.append(f"[摘要] {paper.abstract or '（本来源未提供摘要）'}")
    snapshot = "\n".join(lines)
    prompt_block = f"{CONTENT_OPEN}\n{snapshot}\n{CONTENT_CLOSE}"
    return PaperContext(paper=paper, snapshot=snapshot, prompt_block=prompt_block)


def build_contexts(papers: Iterable[Paper]) -> list[PaperContext]:
    return [build_context(paper) for paper in papers]