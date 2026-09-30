"""每周论文数量规划：周计数、缺额结转、周内去重。

配置见 config/topics.yaml 的 weekly_plan 与 schedule：
    每周一 3 篇、周三 3 篇、周五 4 篇，合计 10 篇。
    某期因「真实论文不足」少发了，缺额会结转给本周后面的期次。

一条硬规则：**只有成功生成并落盘的文章才计入本周完成数**
（storage.count_only_finalized_articles）。历史库里只会有已完成的期次，
所以直接按历史库统计即可，不需要额外的中间状态。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

LOGGER = logging.getLogger(__name__)

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_INDEX = {key: index for index, key in enumerate(WEEKDAY_KEYS)}


@dataclass
class WeeklyPlan:
    """本周本期次的数量规划结果。"""

    week_start: date
    weekday_key: str
    target_count: int          # 本周目标总篇数
    base_count: int            # 今天的基准篇数（不含结转）
    carried: int               # 从本周前面期次结转过来的缺额
    desired: int               # 今天的目标篇数 = base + carried（受上限约束）
    min_count: int             # 低于这个数就不生成文章
    counted_before: int        # 本周此前已完成的篇数
    expected_before: int       # 本周此前期次的基准篇数之和
    carry_enabled: bool

    @property
    def shortfall(self) -> int:
        """本周到目前累计的缺额（未受上限截断的原始值）。"""
        return max(0, self.expected_before - self.counted_before)

    def describe(self) -> str:
        text = (
            f"本周（自 {self.week_start.isoformat()} 起）目标 {self.target_count} 篇，"
            f"此前已完成 {self.counted_before} 篇"
        )
        if self.carry_enabled and self.carried:
            text += f"，结转缺额 {self.carried} 篇"
        text += f"；本期目标 {self.desired} 篇（基准 {self.base_count}，最低 {self.min_count}）"
        return text


class WeeklyPlanner:
    def __init__(
        self,
        weekly_config: Optional[dict] = None,
        schedule_config: Optional[dict] = None,
        fallback_select_count: int = 3,
        fallback_min_count: int = 2,
    ) -> None:
        cfg = weekly_config or {}
        self.target_count = int(cfg.get("target_count", 10))
        self.week_start_key = str(cfg.get("week_start", "mon")).lower()
        self.carry_enabled = bool(cfg.get("carry_shortfall", True))
        self.max_per_issue = int(cfg.get("max_per_issue", 10))
        self.dedupe_within_week = bool(cfg.get("deduplicate_within_week", True))
        self.schedule = schedule_config or {}
        self.fallback_select_count = int(fallback_select_count)
        self.fallback_min_count = int(fallback_min_count)

        if self.week_start_key not in _WEEKDAY_INDEX:
            LOGGER.warning(
                "weekly_plan.week_start 取值 %r 无法识别，回退为 mon", self.week_start_key
            )
            self.week_start_key = "mon"

    # ------------------------------------------------------------------
    def week_start_of(self, day: date) -> date:
        """返回 day 所在统计周的起始日。"""
        offset = (_WEEKDAY_INDEX[day.strftime("%a").lower()] - _WEEKDAY_INDEX[self.week_start_key]) % 7
        return day - timedelta(days=offset)

    def count_finalized(self, repo, since: date, until: Optional[date] = None) -> int:
        """统计 [since, until) 区间内已完成的论文篇数。

        只读历史库：里面只有「生成并落盘成功」的期次，天然满足
        count_only_finalized_articles 的语义。
        """
        total = 0
        for article in repo.all_articles():
            created = _article_date(article)
            if created is None or created < since:
                continue
            if until is not None and created >= until:
                continue
            total += len(article.get("papers") or [])
        return total

    def week_paper_keys(self, repo, week_start: date, until: Optional[date] = None) -> set[str]:
        """本周已推荐过的论文去重键，用于周内去重。"""
        keys: set[str] = set()
        for article in repo.all_articles():
            created = _article_date(article)
            if created is None or created < week_start:
                continue
            if until is not None and created >= until:
                continue
            for item in article.get("papers") or []:
                if item.get("arxiv_id"):
                    keys.add(f"arxiv:{item['arxiv_id']}")
                if item.get("doi"):
                    keys.add(f"doi:{str(item['doi']).lower()}")
                if item.get("uid"):
                    keys.add(str(item["uid"]))
        return keys

    # ------------------------------------------------------------------
    def plan(self, run_date: date, weekday_key: str, repo) -> WeeklyPlan:
        week_start = self.week_start_of(run_date)
        entry = self.schedule.get(weekday_key) or {}
        base_count = int(entry.get("select_count", self.fallback_select_count))
        min_count = int(entry.get("min_select_count", self.fallback_min_count))

        counted_before = self.count_finalized(repo, week_start, run_date)
        expected_before = self._expected_before(weekday_key)

        carried = 0
        if self.carry_enabled:
            carried = max(0, expected_before - counted_before)

        desired = base_count + carried
        desired = min(desired, self.max_per_issue)
        desired = min(desired, max(0, self.target_count - counted_before))

        return WeeklyPlan(
            week_start=week_start,
            weekday_key=weekday_key,
            target_count=self.target_count,
            base_count=base_count,
            carried=carried,
            desired=max(0, desired),
            min_count=min_count,
            counted_before=counted_before,
            expected_before=expected_before,
            carry_enabled=self.carry_enabled,
        )

    def _expected_before(self, weekday_key: str) -> int:
        """本周今天是之前的那几期，按基准篇数合计应完成多少篇。"""
        today_index = _WEEKDAY_INDEX.get(weekday_key)
        if today_index is None:
            return 0
        total = 0
        for key in WEEKDAY_KEYS:
            if _WEEKDAY_INDEX[key] >= today_index:
                break
            entry = self.schedule.get(key)
            if entry:
                total += int(entry.get("select_count", 0))
        return total


def _article_date(article: dict) -> Optional[date]:
    """这一期该算进哪一周。

    优先取期号里带的日期（article_id 形如 2026-09-21-g_mon），它表示的正是
    「这一期是哪一天的刊物」。created_at 是真实的落盘时刻，只在补跑历史期次
    （用 --date 指定过去或未来日期）时才会和它不一致——那时按 created_at 统计
    会把这期算进错误的周。所以 created_at 仅作兜底。
    """
    embedded = _date_prefix(str(article.get("id") or ""))
    if embedded is not None:
        return embedded
    return _date_prefix(str(article.get("created_at") or ""))


def _date_prefix(raw: str) -> Optional[date]:
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None