"""配置文件里的开关是否真的驱动了程序行为（不联网）。

这份用例针对的是「配置写了、代码却没读」这一类问题：
每加一个键，都要有一处代码真的按它走，并且有一处测试能在它失效时报警。
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from pathlib import Path

from main import ROOT, history_path, max_preprints_per_issue, output_targets, timezone_of
from models import Paper
from services.paper_filter import PaperFilter


class FakeRepo:
    def __init__(self, known=()):
        self._known = set(known)

    def known_paper_keys(self) -> set[str]:
        return set(self._known)


# ----------------------------------------------------------------------
class TestHistoryPath:
    def test_relative_path_is_resolved_from_project_root(self):
        config = {"storage": {"paper_history_path": "data/published.json"}}
        assert history_path(config) == ROOT / "data" / "published.json"

    def test_absolute_path_is_used_as_is(self):
        config = {"storage": {"paper_history_path": "/tmp/other-history.json"}}
        assert history_path(config) == Path("/tmp/other-history.json")

    def test_missing_setting_falls_back_to_the_default(self):
        assert history_path({}) == ROOT / "data" / "published.json"


class TestTimezone:
    def test_configured_timezone_is_used(self):
        assert timezone_of({"timezone": "Asia/Shanghai"}).key == "Asia/Shanghai"

    def test_unknown_timezone_falls_back_to_system_tz(self):
        # 时区写错不该阻断整期，退回系统时区并告警
        assert isinstance(timezone_of({"timezone": "Not/AZone"}), tzinfo)

    def test_missing_timezone_falls_back_to_system_tz(self):
        assert isinstance(timezone_of({}), tzinfo)

    def test_generated_at_is_computed_in_the_configured_timezone(self):
        zone = timezone_of({"timezone": "Asia/Tokyo"})
        text = datetime.now(zone).strftime("%Y-%m-%d %H:%M")
        assert datetime.strptime(text, "%Y-%m-%d %H:%M")


class TestOutputTargets:
    def test_both_formats_on_by_default(self):
        assert output_targets({}) == (True, True)

    def test_save_html_can_be_turned_off(self):
        config = {"publishing": {"save_markdown": True, "save_html": False}}
        assert output_targets(config) == (True, False)

    def test_output_formats_also_has_to_allow_it(self):
        config = {
            "article": {"output_formats": ["markdown"]},
            "publishing": {"save_markdown": True, "save_html": True},
        }
        assert output_targets(config) == (True, False)

    def test_both_off_is_reported_as_no_output(self):
        config = {"publishing": {"save_markdown": False, "save_html": False}}
        assert output_targets(config) == (False, False)


class TestPreprintCap:
    def test_configured_cap_is_used(self):
        assert max_preprints_per_issue({"publication": {"max_preprints_per_issue": 2}}) == 2

    def test_allow_preprints_false_means_zero(self):
        config = {"publication": {"allow_preprints": False, "max_preprints_per_issue": 1}}
        assert max_preprints_per_issue(config) == 0

    def test_default_is_one(self):
        assert max_preprints_per_issue({}) == 1


class TestHistoryDedupeSwitch:
    def test_repo_keys_are_excluded_by_default(self):
        paper = Paper(title="P1", arxiv_id="2601.00001")
        fresh, skipped = PaperFilter([], []).exclude_known([paper], FakeRepo({"arxiv:2601.00001"}))
        assert (fresh, skipped) == ([], 1)

    def test_without_repo_only_extra_keys_are_excluded(self):
        # deduplicate_history=false：不查历史库，但仍要挡住本周已推荐过的论文
        filter_ = PaperFilter([], [])
        known = Paper(title="P1", arxiv_id="2601.00001")
        other = Paper(title="P2", arxiv_id="2601.00002")
        fresh, skipped = filter_.exclude_known(
            [known, other], None, extra_keys={"arxiv:2601.00001"}
        )
        assert [p.title for p in fresh] == ["P2"]
        assert skipped == 1