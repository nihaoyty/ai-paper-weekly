"""项目统一的数据模型。

只放数据结构和枚举，不放业务逻辑，避免各模块之间循环依赖。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


class PaperStatus:
    """论文发表状态。

    取值固定为四种，不允许出现第五种，也不允许用大模型推测。
    """

    PUBLISHED = "正式发表"   # 已在会议正式论文集或期刊官网发表
    ACCEPTED = "正式录用"    # 有可靠录用信息，尚未正式出版
    PREPRINT = "预印本"      # 仅确认在 arXiv 等平台公开
    UNVERIFIED = "待核实"    # 来源或发表状态暂时无法确认

    ALL = (PUBLISHED, ACCEPTED, PREPRINT, UNVERIFIED)


# OpenAlex 的 work type 中属于「不是原始研究」的体裁，不纳入选文
NON_RESEARCH_TYPES = frozenset(
    {"review", "editorial", "letter", "erratum", "paratext", "book-review"}
)

# 来源通道的中文名。日志和文章里都取它，避免同一个通道出现两种说法。
SOURCE_LABELS = {
    "arxiv": "arXiv",
    "journal": "期刊",
    "conference": "会议",
}


@dataclass
class Paper:
    """一篇候选论文。

    有两个来源，字段必须都能装下：
    * arXiv 预印本 —— arxiv_id / arxiv_url / pdf_url 有值；
    * 期刊论文（经 OpenAlex 检索）—— arxiv_id 为空，doi / venue / publication_date 有值。
    """

    # ---- 来自 arXiv；期刊论文为空串 ----
    arxiv_id: str = ""
    arxiv_version: str = ""
    title: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    published_at: str = ""            # arXiv 首次提交日期 YYYY-MM-DD
    updated_at: str = ""              # arXiv 最近更新日期 YYYY-MM-DD
    primary_category: str = ""
    categories: list[str] = field(default_factory=list)
    arxiv_url: str = ""
    pdf_url: str = ""
    comment: str = ""                 # arXiv 页面备注，未经官方渠道核验
    journal_ref: str = ""             # arXiv 记录的期刊信息，未经核验
    arxiv_doi: str = ""               # arXiv 记录的 DOI，未经核验

    # ---- 来自 OpenAlex ----
    doi: Optional[str] = None
    openalex_id: Optional[str] = None
    venue: Optional[str] = None       # 正式发表来源名称
    venue_type: Optional[str] = None  # journal / conference / repository ...
    venue_url: Optional[str] = None
    publication_date: Optional[str] = None
    publication_year: Optional[int] = None
    cited_by_count: Optional[int] = None
    work_type: Optional[str] = None   # article / review / proceedings-article ...
    source: str = "arxiv"             # 来源通道：arxiv | journal | conference
    abstract_url: Optional[str] = None  # 摘要需额外抓取时的地址（会议论文集列表页不带摘要）

    # ---- 核验结论 ----
    status: str = PaperStatus.UNVERIFIED
    status_evidence: str = ""         # 判断依据，必须可追溯到具体来源
    status_source_url: Optional[str] = None

    # ---- 内部使用 ----
    topic_id: Optional[str] = None
    topic_name: Optional[str] = None
    score: float = 0.0

    @property
    def is_arxiv(self) -> bool:
        return bool(self.arxiv_id)

    @property
    def is_non_research(self) -> bool:
        """综述、社论等体裁。只有在 OpenAlex 给出了 type 时才判定得出。"""
        return str(self.work_type or "").lower() in NON_RESEARCH_TYPES

    @property
    def uid(self) -> str:
        """候选池内的稳定标识。arXiv 论文用 arXiv ID，期刊论文退到 DOI。"""
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id}"
        if self.doi:
            return f"doi:{self.doi.lower()}"
        if self.openalex_id:
            return f"openalex:{self.openalex_id.rsplit('/', 1)[-1]}"
        return f"title:{self.title[:60]}"

    @property
    def source_url(self) -> str:
        """对外可点的原文地址。"""
        if self.arxiv_url:
            return self.arxiv_url
        if self.doi:
            return f"https://doi.org/{self.doi}"
        return self.venue_url or ""

    @property
    def first_public_date(self) -> str:
        return self.published_at or self.publication_date or ""

    @property
    def year(self) -> Optional[int]:
        """发表年份。会议论文集列表页往往只给得出年份，给不出具体日期。"""
        if self.publication_year:
            return int(self.publication_year)
        raw = self.publication_date or self.published_at or ""
        head = str(raw)[:4]
        return int(head) if head.isdigit() else None

    @property
    def is_published(self) -> bool:
        return self.status == PaperStatus.PUBLISHED

    @property
    def dedupe_keys(self) -> list[str]:
        """去重键。以稳定标识为准，绝不用标题。

        会议论文没有 DOI 也没有 arXiv ID，此时用官方论文页地址当稳定标识。
        跨来源（同一篇论文既有会议版又有 arXiv 版）的合并另由
        PaperFilter.dedupe 的标题归并处理。
        """
        keys = []
        if self.arxiv_id:
            keys.append(f"arxiv:{self.arxiv_id}")
        if self.doi:
            keys.append(f"doi:{self.doi.lower()}")
        if self.openalex_id:
            keys.append(f"openalex:{self.openalex_id.rsplit('/', 1)[-1]}")
        if not keys and self.venue_url:
            keys.append(f"venue:{self.venue_url}")
        return keys

    @property
    def display_title(self) -> str:
        return self.title or "(无标题)"

    @property
    def author_line(self) -> str:
        if not self.authors:
            return "作者信息缺失"
        if len(self.authors) <= 4:
            return ", ".join(self.authors)
        return ", ".join(self.authors[:3]) + f" 等 {len(self.authors)} 人"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Paper":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PaperAnalysis:
    """大模型对单篇论文的解读结果（结构固定，便于校验）。"""

    paper: Paper
    content: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True


@dataclass
class ArticleRecord:
    """一期文章在历史库中的记录。"""

    article_id: str
    volume: int
    created_at: str
    group_id: str
    group_name: str
    theme: str
    status: str = "generated"         # generated / published / failed
    title: str = ""
    files: dict[str, str] = field(default_factory=dict)
    papers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    published_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)