"""中文文章生成。

分为三步：
1. 逐篇调用大模型生成结构化解读（并发，单篇失败不影响其他篇）；
2. 调用大模型做跨论文的导读与总结；
3. 用确定性代码把结果拼装成 Markdown，链接与基本信息全部来自真实元数据。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from models import SOURCE_LABELS, PaperAnalysis
from services import prompts
from services.article_validator import ArticleValidator
from services.llm_client import LLMClient, LLMError
from services.paper_reader import PaperContext

LOGGER = logging.getLogger(__name__)

CN_NUMERALS = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")
NO_REPO_TEXT = "未找到可确认的官方开源代码"


@dataclass
class ArticleBundle:
    title: str
    markdown: str
    theme: str
    synthesis: dict[str, Any] = field(default_factory=dict)
    analyses: list[PaperAnalysis] = field(default_factory=list)
    article_issues: list[str] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        items = list(self.article_issues)
        for analysis in self.analyses:
            items.extend(analysis.warnings)
        return items


class ArticleGenerator:
    def __init__(
        self,
        llm: LLMClient,
        validator: ArticleValidator,
        max_workers: int = 3,
        stagger_seconds: float = 2.0,
        field_budgets: dict | None = None,
        synthesis_budgets: dict | None = None,
    ) -> None:
        self._llm = llm
        self._validator = validator
        self._max_workers = max(1, max_workers)
        # 并发提交之间留出间隔，避免同时打到服务端触发突发限流（429）
        self._stagger_seconds = max(0.0, stagger_seconds)
        self._field_budgets = field_budgets or {}
        self._synthesis_budgets = synthesis_budgets or {}

    # ------------------------------------------------------------------
    def analyze_all(self, contexts: list[PaperContext]) -> list[PaperAnalysis]:
        """并发解读所有论文，保持传入顺序。"""
        results: dict[int, PaperAnalysis] = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {}
            for index, ctx in enumerate(contexts):
                if index and self._stagger_seconds:
                    time.sleep(self._stagger_seconds)
                futures[pool.submit(self._analyze_one, ctx)] = index
            for future in as_completed(futures):
                index = futures[future]
                context = contexts[index]
                try:
                    results[index] = future.result()
                except Exception as exc:  # noqa: BLE001 单篇失败不应中断整期
                    LOGGER.error("论文 %s 解读失败：%s", context.paper.arxiv_id, exc)
                    results[index] = PaperAnalysis(
                        paper=context.paper,
                        content={},
                        warnings=[f"[阻断] 大模型解读失败：{exc}"],
                        ok=False,
                    )
        return [results[i] for i in sorted(results)]

    def _analyze_one(self, context: PaperContext) -> PaperAnalysis:
        LOGGER.info("开始解读：%s", context.paper.display_title[:60])
        content = self._llm.complete_json(
            prompts.ANALYSIS_SYSTEM,
            prompts.build_analysis_user_prompt(
                context.prompt_block,
                context.paper.topic_name or "AI 前沿",
                self._field_budgets,
            ),
        )
        analysis = PaperAnalysis(paper=context.paper, content=content)
        self._validator.validate_analysis(analysis, context)
        LOGGER.info(
            "解读完成：%s（%s）",
            context.paper.display_title[:50],
            "通过校验" if analysis.ok else "校验未通过",
        )
        return analysis

    # ------------------------------------------------------------------
    def synthesize(
        self,
        analyses: list[PaperAnalysis],
        group_name: str,
        window_note: str,
    ) -> dict[str, Any]:
        digests = [
            {
                "title_zh": str(a.content.get("title_zh") or a.paper.display_title),
                "one_line_summary": str(a.content.get("one_line_summary") or ""),
                "innovation": str(a.content.get("innovation") or ""),
                "practical_value": str(a.content.get("practical_value") or ""),
                "status": a.paper.status,
            }
            for a in analyses
        ]
        if not digests:
            raise LLMError("没有可用的单篇解读结果，无法生成导读")

        LOGGER.info("生成本期导读与总结")
        return self._llm.complete_json(
            prompts.SYNTHESIS_SYSTEM,
            prompts.build_synthesis_user_prompt(
                group_name, window_note, digests, self._synthesis_budgets
            ),
        )

    # ------------------------------------------------------------------
    def build_bundle(
        self,
        *,
        volume: int,
        group_name: str,
        window_note: str,
        generated_at: str,
        analyses: list[PaperAnalysis],
        synthesis: dict[str, Any],
        model_name: str,
        contexts: list[PaperContext],
    ) -> ArticleBundle:
        theme = str(synthesis.get("theme") or group_name).strip()
        title = f"AI 论文周报 Vol.{volume:02d}｜{theme}"
        markdown = self._render_markdown(
            title=title,
            volume=volume,
            group_name=group_name,
            window_note=window_note,
            generated_at=generated_at,
            model_name=model_name,
            analyses=analyses,
            synthesis=synthesis,
        )
        issues = self._validator.validate_article(markdown, contexts)
        issues.extend(self._length_issues(analyses, synthesis))
        return ArticleBundle(
            title=title,
            markdown=markdown,
            theme=theme,
            synthesis=synthesis,
            analyses=analyses,
            article_issues=issues,
        )

    # ------------------------------------------------------------------
    def _length_issues(
        self, analyses: list[PaperAnalysis], synthesis: dict[str, Any]
    ) -> list[str]:
        """篇幅检查。超长不阻断发布，但必须在报告里说清楚。"""
        target = self._validator.target_total(len(analyses))
        if target <= 0:
            return []
        body = self._validator.prose_length([a.content for a in analyses])
        tail = self._validator.synthesis_length(synthesis)
        if body + tail <= target:
            return []
        return [
            f"篇幅超出目标：正文约 {body + tail} 字"
            f"（各篇 {body} 字 + 导读总结 {tail} 字），目标 {target} 字。"
            f"如需缩短，请调小 config/topics.yaml 中 article 段的字数上限。"
        ]

    def _render_markdown(
        self,
        *,
        title: str,
        volume: int,
        group_name: str,
        window_note: str,
        generated_at: str,
        model_name: str,
        analyses: list[PaperAnalysis],
        synthesis: dict[str, Any],
    ) -> str:
        parts: list[str] = [
            f"# {title}",
            "",
            f"> AI 前沿论文笔记 · {group_name}",
            "",
            f"- 本期检索范围：{window_note}",
            f"- 文章生成时间：{generated_at}",
            f"- 解读模型：{model_name}",
            "- 状态说明：「正式发表」指已在顶会/顶刊白名单来源核验通过；"
            "「待核实」指来源或发表状态暂时无法确认；「预印本」指仅确认在 arXiv 公开。",
            "",
            "## 一、本期导读",
            "",
            str(synthesis.get("intro") or "（导读生成失败，请人工补充）").strip(),
            "",
        ]

        for index, analysis in enumerate(analyses):
            parts.extend(self._render_paper_section(index, analysis))

        summary_index = len(analyses) + 2
        parts.extend(
            [
                f"## {_cn(summary_index)}、本期学习总结",
                "",
                "### 论文之间的联系",
                "",
                str(synthesis.get("connections") or "（未能生成，请人工补充）").strip(),
                "",
                "### 技术发展方向",
                "",
                str(synthesis.get("trend") or "（未能生成，请人工补充）").strip(),
                "",
                "### 值得进一步学习",
                "",
                str(synthesis.get("next_steps") or "（未能生成，请人工补充）").strip(),
                "",
                f"## {_cn(summary_index + 1)}、参考资料",
                "",
            ]
        )
        for index, analysis in enumerate(analyses, start=1):
            paper = analysis.paper
            parts.append(
                f"{index}. {paper.display_title}（{paper.status}）— {paper.source_url}"
            )
        parts.append("")
        return "\n".join(parts)

    def _render_paper_section(self, index: int, analysis: PaperAnalysis) -> list[str]:
        paper = analysis.paper
        content = analysis.content or {}
        title_zh = str(content.get("title_zh") or paper.display_title).strip()

        lines: list[str] = [
            f"## {_cn(index + 2)}、论文{_cn(index + 1)}：{title_zh}",
            "",
        ]
        if content.get("one_line_summary"):
            lines.extend([f"> {str(content['one_line_summary']).strip()}", ""])

        origin = SOURCE_LABELS.get(paper.source, paper.source or "arXiv")
        if paper.is_arxiv:
            identifier = f"（arXiv {paper.arxiv_id}）"
        elif paper.doi:
            identifier = f"（DOI {paper.doi}）"
        else:
            identifier = ""

        # 会议论文集只给得出年份，给不出月日。这种情况如实写「仅年份」，
        # 既不编造日期，也不显示成「未知」。
        public_date = paper.first_public_date
        if not public_date and paper.year:
            public_date = f"{paper.year} 年（会议论文集仅提供年份）"

        lines.extend(
            [
                "### 基本信息",
                "",
                f"- **英文标题**：{paper.title}",
                f"- **作者**：{paper.author_line}",
                f"- **来源**：{origin}",
                f"- **首次公开**：{public_date or '未知'}{identifier}",
                f"- **发表状态**：**{paper.status}**",
                f"- **状态依据**：{paper.status_evidence or '无'}",
                f"- **原文链接**：{paper.source_url or '无'}",
            ]
        )
        if paper.pdf_url:
            lines.append(f"- **PDF**：{paper.pdf_url}")
        lines.append("")

        for key, heading in (
            ("background", "研究背景"),
            ("core_problem", "核心问题"),
            ("prior_work", "现有方案与局限"),
            ("innovation", "核心创新"),
            ("method", "技术原理"),
            ("experiments", "实验结果"),
            ("limitations", "研究局限"),
            ("practical_value", "工程启发"),
        ):
            value = str(content.get(key) or "").strip()
            if not value:
                continue
            lines.extend([f"### {heading}", "", value, ""])

        lines.extend(["### 相关资源", ""])
        if paper.arxiv_url:
            lines.append(f"- arXiv 原文：{paper.arxiv_url}")
        if paper.pdf_url:
            lines.append(f"- PDF：{paper.pdf_url}")
        if paper.venue:
            source = paper.venue_url or paper.status_source_url or ""
            lines.append(f"- 正式发表来源：{paper.venue}" + (f"（{source}）" if source else ""))
        else:
            lines.append("- 正式发表来源：暂未核验到")
        doi = paper.doi or paper.arxiv_doi
        if doi:
            lines.append(f"- DOI：{doi}")
        repo_url = content.get("repo_url")
        if isinstance(repo_url, str) and repo_url.strip():
            lines.append(f"- 开源代码：{repo_url.strip()}")
        else:
            lines.append(f"- 开源代码：{NO_REPO_TEXT}")
        lines.append("")
        return lines


def _cn(number: int) -> str:
    if 1 <= number <= len(CN_NUMERALS):
        return CN_NUMERALS[number - 1]
    return str(number)


def pick_usable(analyses: list[PaperAnalysis], min_count: int) -> list[PaperAnalysis]:
    """挑出校验通过的解读结果；不足最低数量时返回空列表，让本期不出文章。"""
    usable = [a for a in analyses if a.ok]
    dropped = [a for a in analyses if not a.ok]
    for analysis in dropped:
        LOGGER.warning(
            "本期丢弃论文 %s：%s",
            analysis.paper.display_title[:50],
            "；".join(analysis.warnings),
        )
    if len(usable) < min_count:
        LOGGER.error("可用解读仅 %d 篇，少于最低要求 %d 篇", len(usable), min_count)
        return []
    return usable


def describe_window(
    since: str, until: str, days: int, fallback: bool
) -> str:
    suffix = "（14 天内未找到足够论文，已扩大到最近 30 天）" if fallback else f"（最近 {days} 天）"
    return f"{since} ~ {until}{suffix}"