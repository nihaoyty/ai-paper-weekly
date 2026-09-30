"""每周数量规划与选文约束的单元测试（不联网）。"""

from __future__ import annotations

from datetime import date

from main import _apply_preprint_cap
from models import Paper, PaperStatus
from services.paper_filter import PaperFilter
from services.weekly_plan import WeeklyPlanner

SCHEDULE = {
    "mon": {"group": "g_mon", "select_count": 3, "min_select_count": 2},
    "wed": {"group": "g_wed", "select_count": 3, "min_select_count": 2},
    "fri": {"group": "g_fri", "select_count": 4, "min_select_count": 2},
}

WEEKLY = {
    "target_count": 10,
    "week_start": "mon",
    "carry_shortfall": True,
    "max_per_issue": 10,
    "deduplicate_within_week": True,
}

# 2026-09-21 是周一
MON = date(2026, 9, 21)
WED = date(2026, 9, 23)
FRI = date(2026, 9, 25)


class FakeRepo:
    """只提供 WeeklyPlanner 需要的 all_articles()。"""

    def __init__(self, articles=None):
        self._articles = articles or []

    def all_articles(self):
        return self._articles


def article(created_at: str, count: int, volume: int = 1) -> dict:
    return {
        "id": f"{created_at[:10]}-vol{volume:02d}",
        "volume": volume,
        "created_at": created_at,
        "papers": [
            {"uid": f"arxiv:2401.{i:04d}", "arxiv_id": f"2401.{i:04d}"} for i in range(count)
        ],
    }


def planner() -> WeeklyPlanner:
    return WeeklyPlanner(weekly_config=WEEKLY, schedule_config=SCHEDULE)


class TestWeekStart:
    def test_monday_is_the_week_start(self):
        assert planner().week_start_of(MON) == MON

    def test_sunday_belongs_to_the_previous_monday(self):
        assert planner().week_start_of(date(2026, 9, 27)) == MON

    def test_next_monday_starts_a_new_week(self):
        assert planner().week_start_of(date(2026, 9, 28)) == date(2026, 9, 28)


class TestIssueDateBeatsFileTimestamp:
    """补跑历史期次时，期号里的日期才是「这一期算进哪一周」的依据。

    用 --date 生成过去的期次时，created_at 是真实的落盘时刻，会落在错误的周里。
    """

    def test_issue_date_wins_over_created_at(self):
        repo = FakeRepo(
            [
                {
                    "id": "2026-09-28-g_mon",
                    "created_at": "2026-09-23T15:22:00+08:00",
                    "papers": [{"uid": "arxiv:2401.0000"}],
                }
            ]
        )
        # 期号说这是 09-28 那一期的刊物，就该算进 09-28 起的那一周
        assert planner().count_finalized(repo, date(2026, 9, 28), date(2026, 9, 30)) == 1
        # 而真实的落盘日期（09-23）所在的那一周不应把它算进去
        assert planner().count_finalized(repo, MON, date(2026, 9, 28)) == 0

    def test_week_paper_keys_follows_the_issue_date(self):
        repo = FakeRepo(
            [
                {
                    "id": "2026-09-28-g_mon",
                    "created_at": "2026-09-23T15:22:00+08:00",
                    "papers": [{"uid": "arxiv:2401.0000"}],
                }
            ]
        )
        assert planner().week_paper_keys(repo, date(2026, 9, 28), date(2026, 9, 30)) == {
            "arxiv:2401.0000"
        }
        assert planner().week_paper_keys(repo, MON, date(2026, 9, 28)) == set()

    def test_falls_back_to_created_at_without_a_dated_id(self):
        repo = FakeRepo(
            [
                {
                    "id": "vol-10",
                    "created_at": "2026-09-23T15:22:00+08:00",
                    "papers": [{"uid": "arxiv:2401.0000"}],
                }
            ]
        )
        assert planner().count_finalized(repo, WED, FRI) == 1

    def test_record_without_any_date_is_ignored(self):
        repo = FakeRepo([{"id": "vol-10", "papers": [{"uid": "arxiv:2401.0000"}]}])
        assert planner().count_finalized(repo, MON, FRI) == 0


class TestPlanWithoutShortfall:
    def test_monday_uses_base_count(self):
        plan = planner().plan(MON, "mon", FakeRepo())
        assert plan.base_count == 3
        assert plan.carried == 0
        assert plan.desired == 3
        assert plan.min_count == 2

    def test_friday_uses_base_count_when_earlier_issues_are_complete(self):
        repo = FakeRepo([article("2026-09-21 10:00", 3), article("2026-09-23 10:00", 3, volume=2)])
        plan = planner().plan(FRI, "fri", repo)
        assert plan.counted_before == 6
        assert plan.carried == 0
        assert plan.desired == 4

    def test_previous_week_does_not_count(self):
        repo = FakeRepo([article("2026-09-16 10:00", 3)])
        plan = planner().plan(MON, "mon", repo)
        assert plan.counted_before == 0


class TestShortfallCarry:
    def test_monday_shortfall_is_carried_to_wednesday(self):
        repo = FakeRepo([article("2026-09-21 10:00", 2)])
        plan = planner().plan(WED, "wed", repo)
        assert plan.expected_before == 3
        assert plan.counted_before == 2
        assert plan.carried == 1
        assert plan.desired == 4

    def test_two_issues_of_shortfall_are_carried_to_friday(self):
        repo = FakeRepo([article("2026-09-21 10:00", 2), article("2026-09-23 10:00", 2, volume=2)])
        plan = planner().plan(FRI, "fri", repo)
        assert plan.counted_before == 4
        assert plan.carried == 2
        assert plan.desired == 6

    def test_carry_can_be_turned_off(self):
        weekly = dict(WEEKLY, carry_shortfall=False)
        repo = FakeRepo([article("2026-09-21 10:00", 2)])
        plan = WeeklyPlanner(weekly_config=weekly, schedule_config=SCHEDULE).plan(WED, "wed", repo)
        assert plan.carried == 0
        assert plan.desired == 3

    def test_week_total_never_exceeds_target(self):
        # 周一、周三都超额完成时，周五不会把总量顶到 10 篇以上
        repo = FakeRepo([article("2026-09-21 10:00", 5), article("2026-09-23 10:00", 5, volume=2)])
        plan = planner().plan(FRI, "fri", repo)
        assert plan.desired == 0

    def test_max_per_issue_caps_the_issue_size(self):
        weekly = dict(WEEKLY, max_per_issue=4)
        repo = FakeRepo([article("2026-09-21 10:00", 0), article("2026-09-23 10:00", 0, volume=2)])
        plan = WeeklyPlanner(weekly_config=weekly, schedule_config=SCHEDULE).plan(FRI, "fri", repo)
        assert plan.desired == 4

    def test_describe_is_human_readable(self):
        text = planner().plan(WED, "wed", FakeRepo([article("2026-09-21 10:00", 2)])).describe()
        assert "结转缺额 1 篇" in text


class TestWeekPaperKeys:
    def test_collects_keys_from_this_week_only(self):
        repo = FakeRepo([article("2026-09-21 10:00", 2), article("2026-09-14 10:00", 1, volume=2)])
        keys = planner().week_paper_keys(repo, MON, WED)
        assert keys == {"arxiv:2401.0000", "arxiv:2401.0001"}


# ----------------------------------------------------------------------
def paper(title: str, status: str, score: float, source: str = "conference") -> Paper:
    item = Paper(title=title, source=source, score=score)
    item.status = status
    return item


class TestPreprintCap:
    def test_truncates_preprints_beyond_the_cap(self):
        selected = [
            paper("P1", PaperStatus.PUBLISHED, 9.0),
            paper("P2", PaperStatus.PREPRINT, 8.0),
            paper("P3", PaperStatus.PREPRINT, 7.0),
        ]
        result = _apply_preprint_cap(selected, selected, count=3, max_preprints=1)
        assert len(result) == 2
        assert [item.title for item in result] == ["P1", "P2"]

    def test_backfills_with_verified_papers_from_the_pool(self):
        selected = [
            paper("P1", PaperStatus.PUBLISHED, 9.0),
            paper("P2", PaperStatus.PREPRINT, 8.0),
            paper("P3", PaperStatus.PREPRINT, 7.0),
        ]
        pool = selected + [paper("V1", PaperStatus.ACCEPTED, 6.0)]
        result = _apply_preprint_cap(selected, pool, count=3, max_preprints=1)
        assert [item.title for item in result] == ["P1", "P2", "V1"]

    def test_unverified_papers_count_against_the_cap(self):
        selected = [
            paper("P1", PaperStatus.UNVERIFIED, 9.0),
            paper("P2", PaperStatus.PREPRINT, 8.0),
        ]
        result = _apply_preprint_cap(selected, selected, count=2, max_preprints=1)
        # 待核实同样不是正式发表，不能用来凑数
        assert len(result) == 1
        assert result[0].title == "P1"

    def test_short_of_verified_papers_produces_fewer_articles(self):
        selected = [paper("P1", PaperStatus.PUBLISHED, 9.0), paper("P2", PaperStatus.PREPRINT, 8.0)]
        result = _apply_preprint_cap(selected, selected, count=4, max_preprints=1)
        assert len(result) == 2


# ----------------------------------------------------------------------
class TestCrossSourceDedupe:
    def test_same_title_from_arxiv_and_conference_merges_into_published(self):
        paper_filter = PaperFilter(topics=[], groups=[])
        preprint = Paper(
            title="OctoTools: A Multi-Agent Framework",
            arxiv_id="2601.00001",
            arxiv_url="https://arxiv.org/abs/2601.00001",
            abstract="arxiv abstract",
            source="arxiv",
        )
        preprint.status = PaperStatus.PREPRINT
        conference = Paper(
            title="OctoTools: A Multi-Agent Framework",
            venue="ACL 2026",
            venue_url="https://aclanthology.org/2026.acl-long.1/",
            source="conference",
        )
        conference.status = PaperStatus.PUBLISHED

        merged = paper_filter.dedupe([preprint, conference])
        assert len(merged) == 1
        survivor = merged[0]
        # 保留正式发表的那条，但把 arXiv 侧的摘要补进来
        assert survivor.status == PaperStatus.PUBLISHED
        assert survivor.venue == "ACL 2026"
        assert survivor.abstract == "arxiv abstract"
        # arXiv ID 也补上了，历史去重才能同时命中两种键
        assert survivor.arxiv_id == "2601.00001"

    def test_different_titles_are_not_merged(self):
        paper_filter = PaperFilter(topics=[], groups=[])
        first = Paper(title="Paper A")
        second = Paper(title="Paper B")
        assert len(paper_filter.dedupe([first, second])) == 2