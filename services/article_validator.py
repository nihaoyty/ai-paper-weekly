"""文章事实校验。

这里做的是「确定性校验」：不调用大模型，只用规则检查生成内容是否
真的能在原始来源里找到。发现的问题一律记录成报告，由人决定是否采用。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from models import SOURCE_LABELS, PaperAnalysis, PaperStatus
from services.paper_reader import PaperContext

LOGGER = logging.getLogger(__name__)

REQUIRED_FIELDS = (
    ("title_zh", "中文标题"),
    ("one_line_summary", "一句话概括"),
    ("background", "研究背景"),
    ("core_problem", "核心问题"),
    ("prior_work", "现有方案"),
    ("innovation", "核心创新"),
    ("method", "技术原理"),
    ("experiments", "实验结果"),
    ("limitations", "研究局限"),
    ("practical_value", "工程启发"),
)

URL_PATTERN = re.compile(r"https?://[^\s)\]<>\"'，。；]+")
# 只校验带单位的量化数字，避免把年份、编号误判成数据。
# 末尾的 (?![a-z0-9]) 用来防止「2025 MAS-2025」被当成「2025 M（百万）」。
NUMBER_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|×|倍|亿|万|[BKMGT]|x)(?![a-z0-9])",
    re.IGNORECASE,
)
ACCEPTANCE_CLAIM_PATTERN = re.compile(
    r"(已被.{0,12}(录用|接收|发表)|被.{0,12}录用|accepted\s+(?:at|to)|camera-?ready|to\s+appear\s+in)",
    re.IGNORECASE,
)
TOP_VENUE_CLAIM_PATTERN = re.compile(r"(顶会|顶刊|顶级会议|顶级期刊)")
# 出现在断言之前的否定词，用于排除「不能视为已被录用」这类澄清性表述
NEGATION_PREFIX_PATTERN = re.compile(r"(不能|无法|难以|并不|不算|尚未|未能|不|未|非|无)[^。；！？\n]{0,10}$")


@dataclass
class ValidationResult:
    fatal: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dropped_repo_url: bool = False

    @property
    def ok(self) -> bool:
        return not self.fatal

    def all_issues(self) -> list[str]:
        return [f"[阻断] {x}" for x in self.fatal] + [f"[提醒] {x}" for x in self.warnings]


class ArticleValidator:
    def __init__(
        self,
        field_budgets: Mapping[str, Sequence[int]] | None = None,
        synthesis_budgets: Mapping[str, Sequence[int]] | None = None,
        tolerance: float = 1.25,
    ) -> None:
        # 各字段的字数上限来自 config/topics.yaml 的 article 段
        self._limits: dict[str, int] = {
            key: int(value[1])
            for key, value in (field_budgets or {}).items()
            if len(value) == 2
        }
        self._synthesis_limits: dict[str, int] = {
            key: int(value[1])
            for key, value in (synthesis_budgets or {}).items()
            if len(value) == 2
        }
        self._tolerance = max(1.0, tolerance)

    # ------------------------------------------------------------------
    def over_budget_fields(
        self, content: Mapping[str, Any]
    ) -> dict[str, tuple[int, int]]:
        """返回超出「上限 × tolerance」的字段 -> (当前字数, 上限)。"""
        over: dict[str, tuple[int, int]] = {}
        for key, limit in self._limits.items():
            value = content.get(key)
            if not isinstance(value, str):
                continue
            length = len(value.strip())
            if length > limit * self._tolerance:
                over[key] = (length, limit)
        return over

    def prose_length(self, contents: Sequence[Mapping[str, Any]]) -> int:
        """统计解读正文总字数（不含元数据、链接与状态说明）。"""
        total = 0
        for content in contents:
            for key in self._limits:
                value = content.get(key)
                if isinstance(value, str):
                    total += len(value.strip())
        return total

    def synthesis_length(self, synthesis: Mapping[str, Any]) -> int:
        total = 0
        for key in self._synthesis_limits:
            value = synthesis.get(key)
            if isinstance(value, str):
                total += len(value.strip())
        return total

    def target_total(self, paper_count: int) -> int:
        """整篇正文字数目标：各篇上限之和 + 导读总结上限之和。"""
        per_paper = sum(self._limits.values())
        return per_paper * max(1, paper_count) + sum(self._synthesis_limits.values())

    # ------------------------------------------------------------------
    def validate_analysis(
        self, analysis: PaperAnalysis, context: PaperContext
    ) -> ValidationResult:
        result = ValidationResult()
        content = analysis.content or {}
        snapshot = _normalize_for_match(context.snapshot)
        paper = context.paper

        # 1) 必填字段
        for key, label in REQUIRED_FIELDS:
            value = content.get(key)
            if not isinstance(value, str) or not value.strip():
                result.fatal.append(f"缺少或为空：{label}（{key}）")

        # 2) 链接必须来自原文
        for key in ("repo_url",):
            url = content.get(key)
            if isinstance(url, str) and url.strip():
                if _normalize_url(url) not in _normalize_url(context.snapshot):
                    result.fatal.append(
                        f"链接无法追溯：{key}={url} 未出现在论文原文信息中，已丢弃"
                    )
                    content[key] = None
                    result.dropped_repo_url = True
                else:
                    result.warnings.append(f"代码仓库链接来自原文摘要，建议人工点开确认：{url}")

        text_all = " ".join(
            str(v) for k, v in content.items() if k != "repo_url" and isinstance(v, str)
        )
        for url in URL_PATTERN.findall(text_all):
            if _normalize_url(url) not in _normalize_url(context.snapshot):
                result.warnings.append(f"正文出现来源之外的链接，请人工确认：{url}")

        # 3) 数字必须与原文写法一致
        for token in sorted(set(NUMBER_PATTERN.findall(text_all))):
            if _normalize_number(token) not in snapshot:
                result.warnings.append(
                    f"数字「{token.strip()}」未能在论文原文中找到，请人工核对"
                )

        # 4) 发表状态措辞
        if paper.status in (PaperStatus.PREPRINT, PaperStatus.UNVERIFIED):
            if _has_unnegated_match(ACCEPTANCE_CLAIM_PATTERN, text_all):
                result.warnings.append(
                    f"论文状态为「{paper.status}」，但正文出现录用/发表类表述，"
                    f"请确认是否已注明「未经官方渠道核验」"
                )
            if _has_unnegated_match(TOP_VENUE_CLAIM_PATTERN, text_all):
                result.warnings.append(
                    f"论文状态为「{paper.status}」，但正文出现「顶会/顶刊」字样，请改为如实描述"
                )

        # 5) 篇幅
        over = self.over_budget_fields(content)
        if over:
            detail = "、".join(
                f"{key}（{current} 字 / 上限 {limit} 字）"
                for key, (current, limit) in over.items()
            )
            result.warnings.append(f"字段超出字数上限：{detail}")

        analysis.warnings = result.all_issues()
        analysis.ok = result.ok
        if not result.ok:
            LOGGER.error("%s 校验未通过：%s", paper.arxiv_id, "；".join(result.fatal))
        elif result.warnings:
            LOGGER.warning("%s 有 %d 条待核实项", paper.arxiv_id, len(result.warnings))
        return result

    # ------------------------------------------------------------------
    def validate_article(
        self, markdown: str, contexts: list[PaperContext]
    ) -> list[str]:
        """整篇级别的检查。返回待核实项列表。"""
        issues: list[str] = []
        combined = _normalize_url("\n".join(ctx.snapshot for ctx in contexts))

        for url in sorted(set(URL_PATTERN.findall(markdown))):
            if _normalize_url(url) not in combined:
                issues.append(f"文章中出现来源之外的链接，必须人工确认后才能发布：{url}")

        for context in contexts:
            paper = context.paper
            if paper.source_url and paper.source_url not in markdown:
                issues.append(
                    f"论文 {paper.uid} 的原文链接未出现在参考资料中"
                )
            if paper.status not in markdown:
                issues.append(
                    f"论文 {paper.uid} 的发表状态「{paper.status}」未在文章中标注"
                )
        return issues

    # ------------------------------------------------------------------
    @staticmethod
    def render_report(
        article_title: str,
        generated_at: str,
        per_paper: list[tuple[PaperContext, list[str]]],
        article_issues: list[str],
    ) -> str:
        lines = [
            f"# 自动校验报告｜{article_title}",
            "",
            f"- 生成时间：{generated_at}",
            "- 校验方式：规则校验（链接可追溯性、数字一致性、发表状态措辞、篇幅）",
            "- 说明：本报告只做提示，不代表论文本身有问题；请人工决定是否修改后再发布。",
            "",
        ]
        total = len(article_issues) + sum(len(items) for _, items in per_paper)
        if total == 0:
            lines.append("## 结论\n\n未发现可疑项。仍建议人工通读一遍再发布。")
            return "\n".join(lines)

        if article_issues:
            lines.append("## 整篇问题")
            lines.append("")
            lines.extend(f"- {item}" for item in article_issues)
            lines.append("")

        for context, items in per_paper:
            if not items:
                continue
            paper = context.paper
            lines.append(f"## {paper.display_title}")
            lines.append("")
            # 会议与期刊论文没有 arXiv 地址，别留一行空的「arXiv：」
            lines.append(f"- 来源：{SOURCE_LABELS.get(paper.source, paper.source or '未知')}")
            lines.append(f"- 原文：{paper.source_url or '（无）'}")
            lines.append(f"- 发表状态：{paper.status}")
            lines.append("")
            lines.extend(f"- {item}" for item in items)
            lines.append("")

        return "\n".join(lines)


def _normalize_url(value: str) -> str:
    return re.sub(r"[\s,，。；;）)]+$", "", str(value or "").strip())


def _normalize_for_match(value: str) -> str:
    return _normalize_number(str(value or "").lower())


def _normalize_number(value: str) -> str:
    """归一化后再比对，避免把排版差异当成数据不一致。

    arXiv 摘要里数字的写法五花八门，下面几类等价写法必须先抹平，
    否则会把正确的数据误报成编造：
    * LaTeX 转义：86.3\\% 与 86.3%；
    * 倍率符号：5.5$\\times$、5.5×、5.5 倍、5.5x 是同一个数据；
    * 百分号全半角：% 与 ％。
    """
    text = str(value or "").lower()
    text = text.replace("\\%", "%")
    for variant in ("$\\times$", "\\times", "×", "倍", "times"):
        text = text.replace(variant, "x")
    text = text.replace("％", "%")
    # 去掉残余的 LaTeX 记号，只留下数字与单位
    text = text.replace("\\", "").replace("$", "")
    return re.sub(r"[\s,，_]+", "", text)


def _has_unnegated_match(pattern: re.Pattern[str], text: str) -> bool:
    """找出未被否定的断言。

    「不能视为已被录用」里的「已被录用」是模型在澄清，不是在断言。
    如果只看关键词，这类正确的表述会被误报。所以命中后要回看前文，
    若紧邻否定词就跳过。
    """
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 12) : match.start()]
        if NEGATION_PREFIX_PATTERN.search(prefix):
            continue
        return True
    return False