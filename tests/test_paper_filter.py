"""筛选、去重、打分与选文的单元测试（不联网）。"""

from __future__ import annotations

from datetime import date

import pytest

from models import Paper, PaperStatus
from services.paper_filter import PaperFilter

TOPICS = [
    {
        "id": "t_spec",
        "name": "推理加速",
        "keywords_strong": ["speculative decoding"],
        "keywords": [],
    },
    {
        "id": "t_agent",
        "name": "智能体",
        "keywords_strong": ["tool use"],
        "keywords": [],
    },
]
GROUPS = [{"id": "g_test", "name": "测试组", "topics": ["t_spec", "t_agent"]}]


def make_paper(
    arxiv_id: str,
    title: str,
    abstract: str = "no keywords here",
    published_at: str | None = None,
    status: str = PaperStatus.UNVERIFIED,
    doi: str | None = None,
) -> Paper:
    return Paper(
        arxiv_id=arxiv_id,
        arxiv_version="1",
        title=title,
        abstract=abstract,
        published_at=published_at or date.today().isoformat(),
        status=status,
        doi=doi,
    )


@pytest.fixture
def paper_filter() -> PaperFilter:
    return PaperFilter(TOPICS, GROUPS, recent_days=14)


class FakeRepo:
    def __init__(self, keys: set[str]) -> None:
        self._keys = keys

    def known_paper_keys(self) -> set[str]:
        return self._keys


class TestTitleMatchGate:
    def test_abstract_only_match_is_dropped_when_gate_on(self) -> None:
        """真实案例：IQA 论文只在摘要里提了多智能体，标题与主题无关。"""
        strict = PaperFilter(TOPICS, GROUPS, recent_days=14, require_title_match=True)
        paper = make_paper(
            "2509.00010",
            "Evolutionary Agent for Open-ended Image Quality Assessment",
            "we build a multi-agent pipeline with tool use",
        )
        group = strict.get_group("g_test")
        # 标题命中 tool use 之外，摘要命中 tool use；标题本身不含关键词
        assert strict.score([paper], group) == []

    def test_title_match_passes_when_gate_on(self) -> None:
        strict = PaperFilter(TOPICS, GROUPS, recent_days=14, require_title_match=True)
        paper = make_paper("2509.00011", "Tool Use in Long-Horizon Tasks")
        group = strict.get_group("g_test")
        assert len(strict.score([paper], group)) == 1

    def test_gate_off_keeps_abstract_only_match(self) -> None:
        loose = PaperFilter(TOPICS, GROUPS, recent_days=14, require_title_match=False)
        paper = make_paper(
            "2509.00012", "Unrelated Title", "we apply speculative decoding here"
        )
        group = loose.get_group("g_test")
        assert len(loose.score([paper], group)) == 1


class TestNonResearchExclusion:
    def test_survey_by_work_type_excluded(self) -> None:
        paper = make_paper("2509.00020", "Speculative Decoding at Scale")
        paper.work_type = "review"
        pf = PaperFilter(TOPICS, GROUPS, recent_days=14)
        group = pf.get_group("g_test")
        assert pf.score([paper], group) == []

    def test_survey_by_title_pattern_excluded(self) -> None:
        pf = PaperFilter(
            TOPICS,
            GROUPS,
            recent_days=14,
            exclude_title_patterns=["a survey", "an overview"],
        )
        paper = make_paper(
            "2509.00021", "Speculative Decoding in LLMs: A Survey of Methods"
        )
        group = pf.get_group("g_test")
        assert pf.score([paper], group) == []

    def test_normal_title_is_not_excluded(self) -> None:
        pf = PaperFilter(
            TOPICS, GROUPS, recent_days=14, exclude_title_patterns=["a survey"]
        )
        paper = make_paper("2509.00022", "Speculative Decoding with Multi-Parent Quantization")
        group = pf.get_group("g_test")
        assert len(pf.score([paper], group)) == 1


class TestDedupe:
    def test_same_arxiv_id_kept_once(self, paper_filter: PaperFilter) -> None:
        papers = [
            make_paper("2509.00001", "A"),
            make_paper("2509.00001", "A"),
            make_paper("2509.00002", "B"),
        ]
        assert len(paper_filter.dedupe(papers)) == 2

    def test_prefers_entry_with_more_metadata(self, paper_filter: PaperFilter) -> None:
        thin = make_paper("2509.00003", "A")
        rich = make_paper("2509.00003", "A", doi="10.1/x")
        rich.venue = "NeurIPS"
        result = paper_filter.dedupe([thin, rich])
        assert len(result) == 1
        assert result[0].doi == "10.1/x"


class TestExcludeKnown:
    def test_matches_by_arxiv_id(self, paper_filter: PaperFilter) -> None:
        papers = [make_paper("2509.00001", "A"), make_paper("2509.00002", "B")]
        fresh, skipped = paper_filter.exclude_known(papers, FakeRepo({"arxiv:2509.00001"}))
        assert skipped == 1
        assert [p.arxiv_id for p in fresh] == ["2509.00002"]

    def test_matches_by_doi(self, paper_filter: PaperFilter) -> None:
        papers = [make_paper("2509.00009", "A", doi="10.1000/ABC")]
        _, skipped = paper_filter.exclude_known(papers, FakeRepo({"doi:10.1000/abc"}))
        assert skipped == 1


class TestScoring:
    def test_title_match_outweighs_abstract_match(self, paper_filter: PaperFilter) -> None:
        by_title = make_paper("2509.00001", "Speculative Decoding at Scale")
        by_abstract = make_paper(
            "2509.00002", "Unrelated Title", "we apply speculative decoding in passing"
        )
        group = paper_filter.get_group("g_test")
        scored = paper_filter.score([by_title, by_abstract], group)
        assert scored[0].arxiv_id == "2509.00001"
        assert by_title.score > by_abstract.score

    def test_hyphen_and_plural_normalization(self, paper_filter: PaperFilter) -> None:
        paper = make_paper("2509.00004", "Tool-Use and Tool Use in Agents")
        group = paper_filter.get_group("g_test")
        scored = paper_filter.score([paper], group)
        assert len(scored) == 1
        assert scored[0].topic_id == "t_agent"

    def test_irrelevant_paper_is_dropped(self, paper_filter: PaperFilter) -> None:
        paper = make_paper("2509.00005", "A Study of Something Else Entirely")
        group = paper_filter.get_group("g_test")
        assert paper_filter.score([paper], group) == []


class TestSelect:
    def _group(self, paper_filter: PaperFilter):
        return paper_filter.get_group("g_test")

    def _papers(self) -> list[Paper]:
        strong = make_paper("2509.00001", "speculative decoding study")
        strong.topic_id, strong.score = "t_spec", 10.0
        second = make_paper("2509.00002", "more speculative decoding")
        second.topic_id, second.score = "t_spec", 9.5
        other = make_paper("2509.00003", "tool use study")
        other.topic_id, other.score = "t_agent", 9.0
        return [strong, second, other]

    def test_min_score_blocks_weak_papers(self, paper_filter: PaperFilter) -> None:
        # 分数分别为 10.0 / 9.5 / 9.0，门槛 9.6 只放过第一篇
        selected = paper_filter.select(
            self._papers(), self._group(paper_filter), 3, 1, min_score=9.6
        )
        assert [p.arxiv_id for p in selected] == ["2509.00001"]

    def test_diversity_prefers_unused_topic(self, paper_filter: PaperFilter) -> None:
        selected = paper_filter.select(
            self._papers(), self._group(paper_filter), 2, 1, diversity_ratio=0.8
        )
        # 第一篇取最高分；第二篇虽然 t_spec 的 9.5*0.8=7.6 低于 t_agent 的 9.0
        assert [p.topic_id for p in selected] == ["t_spec", "t_agent"]

    def test_pure_score_order_when_diversity_disabled(
        self, paper_filter: PaperFilter
    ) -> None:
        selected = paper_filter.select(
            self._papers(), self._group(paper_filter), 2, 1, diversity_ratio=1.0
        )
        assert [p.arxiv_id for p in selected] == ["2509.00001", "2509.00002"]

    def test_returns_everything_when_min_score_disables_all(
        self, paper_filter: PaperFilter
    ) -> None:
        assert paper_filter.select(self._papers(), self._group(paper_filter), 3, 1, min_score=99) == []