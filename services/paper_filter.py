"""候选论文的筛选、打分、去重与排序。

打分是纯规则的、可复现的：关键词命中 + 时间新鲜度 + 发表状态加权。
不调用大模型，避免「筛论文」这一步就被幻觉污染。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from models import Paper, PaperStatus

LOGGER = logging.getLogger(__name__)

# 发表状态加权：可靠的来源优先，预印本不加分也不排除
STATUS_BONUS = {
    PaperStatus.PUBLISHED: 1.5,
    PaperStatus.ACCEPTED: 1.5,
    PaperStatus.UNVERIFIED: 0.75,
    PaperStatus.PREPRINT: 0.0,
}
STRONG_WEIGHT = 3.0
NORMAL_WEIGHT = 1.0
RECENCY_BONUS = 2.0
# 标题命中比摘要提及重要得多：标题是论文的主题，摘要里可能只是顺带一提
TITLE_WEIGHT = 2.0


@dataclass
class Topic:
    id: str
    name: str
    keywords_strong: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class TopicGroup:
    id: str
    name: str
    topic_ids: list[str] = field(default_factory=list)


class PaperFilter:
    def __init__(
        self,
        topics: Iterable[dict],
        groups: Iterable[dict],
        recent_days: int = 14,
        require_title_match: bool = False,
        exclude_title_patterns: Iterable[str] = (),
    ) -> None:
        self.topics: dict[str, Topic] = {}
        for item in topics:
            topic = Topic(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                keywords_strong=[str(k) for k in (item.get("keywords_strong") or [])],
                keywords=[str(k) for k in (item.get("keywords") or [])],
            )
            self.topics[topic.id] = topic

        self.groups: dict[str, TopicGroup] = {}
        for item in groups:
            group = TopicGroup(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                topic_ids=[str(t) for t in (item.get("topics") or [])],
            )
            unknown = [t for t in group.topic_ids if t not in self.topics]
            if unknown:
                LOGGER.warning("主题组 %s 引用了未定义的方向：%s", group.id, unknown)
            self.groups[group.id] = group

        self._recent_days = recent_days
        self._require_title_match = bool(require_title_match)
        self._exclude_title_patterns = [
            re.compile(re.escape(_normalize_text(p)))
            for p in exclude_title_patterns
            if _normalize_text(p)
        ]
        self._patterns: dict[str, list[tuple[re.Pattern[str], float]]] = {}
        self._compile_patterns()

    # ------------------------------------------------------------------
    def get_group(self, group_id: str) -> TopicGroup:
        if group_id not in self.groups:
            raise KeyError(f"配置中不存在主题组 {group_id}，可选：{list(self.groups)}")
        return self.groups[group_id]

    def group_topics(self, group: TopicGroup) -> list[Topic]:
        return [self.topics[t] for t in group.topic_ids if t in self.topics]

    # ------------------------------------------------------------------
    def dedupe(self, papers: Iterable[Paper]) -> list[Paper]:
        """按稳定标识去重，同一研究成果只保留信息最全的一条。"""
        kept: dict[str, Paper] = {}
        keyless: list[Paper] = []
        for paper in papers:
            keys = paper.dedupe_keys
            if not keys:
                # 没有任何稳定标识（arXiv ID / DOI / 官方论文页地址）。
                # 这里宁可原样保留，也不能在去重这一步把它静默丢掉。
                keyless.append(paper)
                continue
            existing_key = next((k for k in keys if k in kept), None)
            if existing_key is None:
                for key in keys:
                    kept.setdefault(key, paper)
                continue
            current = kept[existing_key]
            if _completeness(paper) > _completeness(current):
                for key in [k for k, v in kept.items() if v is current]:
                    kept[key] = paper
                for key in keys:
                    kept.setdefault(key, paper)
        # 同一篇论文会挂在多个 key 下，按对象身份去重
        unique: list[Paper] = []
        seen_ids: set[int] = set()
        for paper in kept.values():
            if id(paper) not in seen_ids:
                seen_ids.add(id(paper))
                unique.append(paper)
        unique.extend(keyless)
        return self._merge_by_title(unique)

    def _merge_by_title(self, papers: list[Paper]) -> list[Paper]:
        """跨来源归并：同一篇论文在 arXiv 与会议论文集会各有一条记录。

        arXiv 版有 arxiv:<id> 键，会议论文集版只有 venue:<url> 键，
        按标识去重时它们是两条。这里再用「归一化后完全相等的标题」归并一次。

        只做完全相等，不做相似度匹配：把两篇不同论文并成一篇，
        比重复推荐一篇的代价更大。
        """
        by_title: dict[str, Paper] = {}
        result: list[Paper] = []
        for paper in papers:
            key = _normalize_text(paper.title)
            if not key:
                result.append(paper)
                continue
            existing = by_title.get(key)
            if existing is None:
                by_title[key] = paper
                result.append(paper)
                continue
            if _prefer(paper, existing):
                _merge_missing(paper, existing)
                result[result.index(existing)] = paper
                by_title[key] = paper
            else:
                _merge_missing(existing, paper)
        return result

    def exclude_known(
        self,
        papers: Iterable[Paper],
        repo=None,
        extra_keys: Iterable[str] = (),
    ) -> tuple[list[Paper], int]:
        """剔除已推荐过的论文，返回 (新论文, 被剔除数量)。

        repo 为 None 表示不查历史库（weekly_plan.deduplicate_history=false），
        此时只按 extra_keys 去重——那是本周已经推荐过的论文标识。
        """
        known = repo.known_paper_keys() if repo is not None else set()
        known = known | {str(key) for key in extra_keys}
        fresh: list[Paper] = []
        skipped = 0
        for paper in papers:
            if any(key in known for key in paper.dedupe_keys):
                skipped += 1
                continue
            fresh.append(paper)
        return fresh, skipped

    # ------------------------------------------------------------------
    def score(self, papers: Iterable[Paper], group: TopicGroup) -> list[Paper]:
        """就地写入 topic_id / topic_name / score，返回按分数降序的列表。

        重复调用是安全的（每次都重算），便于「先粗排、补元数据后再精排」。
        """
        topics = self.group_topics(group)
        scored: list[Paper] = []
        for paper in papers:
            if self._is_excluded(paper):
                continue

            title_text = _normalize_text(paper.title)
            abstract_text = _normalize_text(paper.abstract)
            best_topic: Optional[Topic] = None
            best_score = 0.0
            for topic in topics:
                title_score, title_hits = self._keyword_score(title_text, topic.id)
                # 标题是论文的主题，摘要里可能只是顺带一提。
                # 但这条门槛只对 arXiv 论文生效：期刊标题是自然语言表述
                # （如 "Causal evidence that language models use confidence..."），
                # 不会出现 kv cache 这类术语，套用该门槛会把期刊论文全部挡掉。
                if self._require_title_match and paper.is_arxiv and title_hits == 0:
                    continue
                abstract_score, _ = self._keyword_score(abstract_text, topic.id)
                value = title_score * TITLE_WEIGHT + abstract_score
                if value > best_score:
                    best_score, best_topic = value, topic

            if best_topic is None or best_score <= 0:
                continue

            paper.topic_id = best_topic.id
            paper.topic_name = best_topic.name
            paper.score = round(
                best_score
                + (RECENCY_BONUS if self._is_recent(paper) else 0.0)
                + STATUS_BONUS.get(paper.status, 0.0),
                3,
            )
            scored.append(paper)

        scored.sort(key=lambda p: (-p.score, p.uid))
        return scored

    def _is_excluded(self, paper: Paper) -> bool:
        """综述类论文与靠标题特征识别的非研究体裁。"""
        if paper.is_non_research:
            LOGGER.debug("排除非研究体裁 %s：%s", paper.work_type, paper.title[:60])
            return True
        title_text = _normalize_text(paper.title)
        for pattern in self._exclude_title_patterns:
            if pattern.search(title_text):
                LOGGER.debug("按标题特征排除：%s", paper.title[:60])
                return True
        return False

    def select(
        self,
        papers: list[Paper],
        group: TopicGroup,
        count: int,
        min_count: int,
        min_score: float = 0.0,
        diversity_ratio: float = 0.8,
    ) -> list[Paper]:
        """按综合分数从高到低选文，同时尽量覆盖组内多个方向。

        diversity_ratio 表示：某个方向已经被选过论文后，该方向后续论文的
        分数要乘以这个系数再参与比较。<1 时倾向于换方向，=1 时纯粹按分数排。
        """
        eligible = [p for p in papers if p.score >= min_score]
        if not eligible:
            LOGGER.warning("没有论文达到分数门槛 %.1f，本期无候选", min_score)
            return []

        buckets: dict[str, list[Paper]] = {t.id: [] for t in self.group_topics(group)}
        for paper in eligible:
            if paper.topic_id in buckets:
                buckets[paper.topic_id].append(paper)

        selected: list[Paper] = []
        used_topics: set[str] = set()
        while len(selected) < count:
            best_topic: Optional[str] = None
            best_value = -1.0
            for topic in self.group_topics(group):
                bucket = buckets.get(topic.id) or []
                if not bucket:
                    continue
                value = bucket[0].score
                if topic.id in used_topics:
                    value *= diversity_ratio
                if value > best_value:
                    best_value, best_topic = value, topic.id
            if best_topic is None:
                break
            selected.append(buckets[best_topic].pop(0))
            used_topics.add(best_topic)

        if len(selected) < min_count:
            LOGGER.warning(
                "本期只选出 %d 篇论文，低于最低要求 %d 篇", len(selected), min_count
            )
        return selected

    # ------------------------------------------------------------------
    def _keyword_score(self, normalized_text: str, topic_id: str) -> tuple[float, int]:
        """返回 (加权得分, 命中关键词个数)。"""
        score = 0.0
        hits = 0
        for pattern, weight in self._patterns.get(topic_id, []):
            if pattern.search(normalized_text):
                score += weight
                hits += 1
        return score, hits

    def _is_recent(self, paper: Paper) -> bool:
        published = _parse_date(paper.first_public_date)
        if published is None:
            return False
        return (date.today() - published).days <= self._recent_days

    def _compile_patterns(self) -> None:
        for topic in self.topics.values():
            compiled: list[tuple[re.Pattern[str], float]] = []
            for keyword, weight in [
                *((k, STRONG_WEIGHT) for k in topic.keywords_strong),
                *((k, NORMAL_WEIGHT) for k in topic.keywords),
            ]:
                normalized = _normalize_text(keyword)
                if not normalized:
                    continue
                # 允许简单复数形式，避免 robot / robots 这类漏匹配
                pattern = re.compile(
                    rf"(?<![a-z0-9]){re.escape(normalized)}(?:s|es)?(?![a-z0-9])"
                )
                compiled.append((pattern, weight))
            self._patterns[topic.id] = compiled


def _normalize_text(value: str) -> str:
    """统一大小写，并把 - _ / 归一成空格，让 'tool-use' 与 'tool use' 等价。"""
    text = str(value or "").lower()
    text = re.sub(r"[-_/]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _completeness(paper: Paper) -> int:
    return sum(
        1
        for value in (paper.doi, paper.venue, paper.openalex_id, paper.publication_date)
        if value
    )


# 归并同一篇论文的两条记录时，优先留下可信度更高的那条
STATUS_RANK = {
    PaperStatus.PUBLISHED: 3,
    PaperStatus.ACCEPTED: 2,
    PaperStatus.UNVERIFIED: 1,
    PaperStatus.PREPRINT: 0,
}

# 归并时可以从另一条记录补过来的字段
_MERGE_FIELDS = (
    "abstract",
    "arxiv_id",
    "arxiv_version",
    "arxiv_url",
    "pdf_url",
    "doi",
    "venue",
    "venue_type",
    "venue_url",
    "publication_date",
    "publication_year",
    "openalex_id",
    "abstract_url",
    "primary_category",
    "published_at",
    "updated_at",
    "comment",
    "journal_ref",
)


def _prefer(candidate: Paper, current: Paper) -> bool:
    """candidate 是否比 current 更值得保留。"""
    candidate_rank = STATUS_RANK.get(candidate.status, 0)
    current_rank = STATUS_RANK.get(current.status, 0)
    if candidate_rank != current_rank:
        return candidate_rank > current_rank
    return _completeness(candidate) > _completeness(current)


def _merge_missing(target: Paper, source: Paper) -> None:
    """把 source 里 target 缺的字段补过去，只补空值，不覆盖已有信息。"""
    for name in _MERGE_FIELDS:
        if not getattr(target, name, None) and getattr(source, name, None):
            setattr(target, name, getattr(source, name))
    if not target.authors and source.authors:
        target.authors = list(source.authors)
    if not target.categories and source.categories:
        target.categories = list(source.categories)


def _parse_date(value: str) -> Optional[date]:
    try:
        year, month, day = (int(x) for x in str(value).split("-")[:3])
        return date(year, month, day)
    except (TypeError, ValueError):
        return None