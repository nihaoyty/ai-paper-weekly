#!/usr/bin/env python3
"""AI Paper Weekly —— Phase 1 本地 MVP 入口。

一条命令完成：检索 arXiv -> 筛选去重 -> 核验发表状态 -> 大模型中文解读
-> 事实校验 -> 输出 Markdown 与微信公众号兼容的 HTML。

用法见 README.md，或运行 `python main.py --help`。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv

from collectors.arxiv_client import ArxivClient, ArxivError
from collectors.crossref_client import CrossrefClient, CrossrefError
from collectors.journal_client import JournalClient
from collectors.openalex_client import OpenAlexClient, OpenAlexError
from collectors.publication_verifier import PublicationVerifier
from collectors.venues import abstract_limit, build_clients, collect_all
from models import ArticleRecord, Paper, PaperStatus, SOURCE_LABELS
from publishers import notifier
from publishers.html_publisher import HtmlPublisher
from publishers.markdown_publisher import MarkdownPublisher
from publishers.wechat_mp import WeChatError, cover_image_from_env, draft_client_from_env
from services.article_generator import ArticleGenerator, describe_window, pick_usable
from services.article_validator import ArticleValidator
from services.llm_client import LLMClient, LLMError
from services.paper_filter import PaperFilter, TopicGroup
from services.paper_reader import PaperContext, build_contexts
from services.weekly_plan import WeeklyPlan, WeeklyPlanner
from storage.paper_repository import PaperRepository, RepositoryError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "topics.yaml"
REPO_PATH = ROOT / "data" / "published.json"
OUTPUT_DIR = ROOT / "output"
TEMPLATE_PATH = ROOT / "templates" / "article.html"

DEFAULT_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INSUFFICIENT = 3

LOGGER = logging.getLogger("ai_paper_weekly")


@dataclass
class SelectionResult:
    papers: list[Paper]
    pool: list[Paper]
    since: date
    used_fallback: bool
    skipped_known: int


# ======================================================================
# 基础设施
# ======================================================================
def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"找不到配置文件：{CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    for key in ("schedule", "groups", "topics", "search"):
        if key not in config:
            raise SystemExit(f"配置文件缺少必需字段：{key}")
    return config


def history_path(config: dict[str, Any]) -> Path:
    """历史库位置（storage.paper_history_path）。

    写相对路径时按项目根目录解析，避免「在别的目录下运行就找不到历史库」。
    """
    raw = str((config.get("storage") or {}).get("paper_history_path") or "").strip()
    if not raw:
        return REPO_PATH
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


def timezone_of(config: dict[str, Any]):
    """配置里的 timezone。取不到就退回系统时区，不让时区写错阻断整期。"""
    name = str(config.get("timezone") or "").strip()
    if not name:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        LOGGER.warning("timezone %r 无法识别，改用系统时区", name)
        return datetime.now().astimezone().tzinfo


def output_targets(config: dict[str, Any]) -> tuple[bool, bool]:
    """本期要输出哪些格式，返回 (markdown, html)。

    publishing.save_markdown / save_html 与 article.output_formats 都要允许才输出，
    任一关掉就不写该格式。
    """
    formats = {
        str(item).strip().lower()
        for item in ((config.get("article") or {}).get("output_formats") or ["markdown", "html"])
    }
    publishing = config.get("publishing") or {}
    return (
        "markdown" in formats and bool(publishing.get("save_markdown", True)),
        "html" in formats and bool(publishing.get("save_html", True)),
    )


def notify_enabled(config: dict[str, Any]) -> bool:
    """publishing.notify.enabled：出稿后是否推微信通知。"""
    return bool(((config.get("publishing") or {}).get("notify") or {}).get("enabled", True))


def push_notice(config: dict[str, Any], message: tuple[str, str]) -> None:
    """按配置推一条微信通知。推送失败只记日志，不影响出稿。"""
    if not notify_enabled(config):
        LOGGER.info("publishing.notify.enabled=false，跳过微信通知")
        return
    notifier.notify(*message)


def push_failure(args: argparse.Namespace, scope: str, reason: str) -> None:
    """运行失败的通知。

    这里刻意不看 publishing.notify.enabled：配置可能根本没读成功，
    而云端跑挂了没人盯着终端，失败消息必须发出去（没配 token 时自然是空操作）。
    """
    if args.selftest or args.history:
        return
    notifier.notify(*notifier.build_failure_message(scope=scope, reason=reason))


def create_wechat_draft(
    config: dict[str, Any],
    *,
    title: str,
    markdown: str,
    digest: str,
    html_publisher: HtmlPublisher,
) -> Optional[str]:
    """按配置把本期文章放进公众号草稿箱，返回草稿的 media_id。

    只建草稿、不群发：个人订阅号没有 freepublish / mass 权限，
    群发必须你人工在后台点一次。
    """
    wechat_cfg = (config.get("publishing") or {}).get("wechat") or {}
    if not bool(wechat_cfg.get("enabled", False)):
        LOGGER.info("publishing.wechat.enabled=false，不创建公众号草稿")
        return None
    for key in ("auto_publish", "auto_mass_send"):
        if wechat_cfg.get(key):
            LOGGER.warning(
                "publishing.wechat.%s 为 true，但个人订阅号没有该权限，"
                "程序只会建草稿，仍需人工群发",
                key,
            )

    client = draft_client_from_env()
    if client is None:
        return None
    cover = cover_image_from_env()
    if cover is None:
        raise WeChatError(
            "未配置 WECHAT_COVER_IMAGE，无法创建草稿："
            "微信要求图文消息必须带封面，请指向一张本地图片（jpg/png）"
        )

    thumb_media_id = client.upload_cover(cover)
    media_id = client.create_draft(
        title=title,
        content=html_publisher.render_body(markdown),
        thumb_media_id=thumb_media_id,
        digest=digest,
    )
    LOGGER.info("已创建公众号草稿：media_id=%s", media_id)
    return media_id


def build_filter(config: dict[str, Any]) -> PaperFilter:
    search_cfg = config["search"]
    return PaperFilter(
        topics=config["topics"],
        groups=config["groups"],
        recent_days=int(search_cfg.get("primary_window_days", 14)),
        require_title_match=bool(search_cfg.get("require_title_match", False)),
        exclude_title_patterns=search_cfg.get("exclude_title_patterns") or [],
    )


def build_journal_client(config: dict[str, Any]) -> Optional[JournalClient]:
    """期刊通道。未启用或没配来源时返回 None，此时只跑 arXiv。"""
    journal_cfg = (config.get("search") or {}).get("journals") or {}
    sources = [str(s) for s in (journal_cfg.get("sources") or []) if str(s).strip()]
    if not journal_cfg.get("enabled") or not sources:
        LOGGER.info("期刊通道未启用，本期只检索 arXiv")
        return None
    return JournalClient(
        OpenAlexClient(mailto=os.getenv("OPENALEX_MAILTO")),
        sources,
        per_source=int(journal_cfg.get("per_source", 30)),
    )


def resolve_group_id(config: dict[str, Any], args: argparse.Namespace) -> str:
    if args.group:
        return args.group
    env_group = os.getenv("RUN_GROUP", "").strip()
    if env_group:
        return env_group

    target = args.date or date.today()
    weekday_key = WEEKDAY_KEYS[target.weekday()]
    entry = (config.get("schedule") or {}).get(weekday_key)
    if not entry:
        raise SystemExit(
            f"{target.isoformat()}（{weekday_key}）在 config/topics.yaml 的 schedule 中"
            f"没有安排主题，可以用 --group 手动指定。"
        )
    return str(entry["group"])


def _max_tokens_from_env() -> Optional[int]:
    """输出长度上限。留空用 32768；填 none / default / 0 表示交给服务端决定。"""
    raw = os.getenv("LLM_MAX_TOKENS", "").strip()
    if not raw:
        return 32768
    if raw.lower() in ("none", "default", "0"):
        return None
    return int(raw)


def build_validator(config: dict[str, Any]) -> ArticleValidator:
    article_cfg = config.get("article") or {}
    return ArticleValidator(
        field_budgets=article_cfg.get("field_budgets"),
        synthesis_budgets=article_cfg.get("synthesis_budgets"),
        tolerance=float(article_cfg.get("tolerance", 1.25)),
    )


def build_planner(config: dict[str, Any]) -> WeeklyPlanner:
    search_cfg = config.get("search") or {}
    return WeeklyPlanner(
        weekly_config=config.get("weekly_plan") or {},
        schedule_config=config.get("schedule") or {},
        fallback_select_count=int(search_cfg.get("select_count", 3)),
        fallback_min_count=int(search_cfg.get("min_select_count", 2)),
    )


def max_preprints_per_issue(config: dict[str, Any]) -> int:
    """硬约束：每期最多允许多少篇「非正式发表」的论文。

    publication.allow_preprints=false 时直接归零：此时文章里只允许出现
    正式发表/正式录用的论文，宁缺毋滥。
    """
    publication = config.get("publication") or {}
    if not bool(publication.get("allow_preprints", True)):
        return 0
    return int(publication.get("max_preprints_per_issue", 1))


def _source_label(paper: Paper) -> str:
    return SOURCE_LABELS.get(paper.source, paper.source or "未知")


def llm_from_env() -> LLMClient:
    return LLMClient(
        api_key=os.getenv("LLM_API_KEY", "").strip(),
        base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        model=os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        timeout=int(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "3")),
        max_tokens=_max_tokens_from_env(),
    )


# ======================================================================
# 主流程
# ======================================================================
def run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    config = load_config()
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    run_date = args.date or date.today()
    group_id = resolve_group_id(config, args)
    paper_filter = build_filter(config)
    group: TopicGroup = paper_filter.get_group(group_id)

    repo = PaperRepository(history_path(config))
    repo.load()

    article_id = f"{run_date.isoformat()}-{group_id}"
    existing = repo.find_article(article_id)
    if existing and not args.force:
        LOGGER.info(
            "本期已存在（%s，状态 %s），跳过。如需重跑请加 --force",
            article_id, existing.get("status"),
        )
        return EXIT_OK

    LOGGER.info("=== 本期主题组：%s（%s） ===", group.name, group.id)

    weekday_key = WEEKDAY_KEYS[run_date.weekday()]
    planner = build_planner(config)
    plan = planner.plan(run_date, weekday_key, repo)
    LOGGER.info("%s", plan.describe())
    if plan.desired <= 0:
        LOGGER.info("本周目标已达成，本期不生成文章。")
        return EXIT_OK

    # 周内去重：本周前面几期已经推荐过的论文不再重复推荐
    week_keys: set[str] = set()
    if bool((config.get("weekly_plan") or {}).get("deduplicate_within_week", True)):
        week_keys = planner.week_paper_keys(repo, plan.week_start, run_date)

    selection = prepare_papers(args, config, paper_filter, group, repo, plan, week_keys)
    if selection is None:
        push_notice(
            config,
            notifier.build_failure_message(
                scope="论文检索与筛选", reason="没有选出足够的论文，本期未生成文章（宁缺毋滥）"
            ),
        )
        return EXIT_INSUFFICIENT

    LOGGER.info("本期选用 %d 篇（历史重复已跳过 %d 篇）：", len(selection.papers), selection.skipped_known)
    for index, paper in enumerate(selection.papers, start=1):
        LOGGER.info(
            "  %d. [%s][%s][%s] %s（分数 %.1f）",
            index,
            _source_label(paper),
            paper.status,
            paper.venue or paper.year or "-",
            paper.display_title[:60],
            paper.score,
        )

    if args.dry_run:
        LOGGER.info("--dry-run 已开启：只做检索与筛选，不调用大模型。")
        LOGGER.info("候选池前 10 名（用于调整 config/topics.yaml 的关键词与门槛）：")
        for index, paper in enumerate(selection.pool[:10], start=1):
            origin = _source_label(paper)
            LOGGER.info(
                "  %2d. %5.1f [%s][%s][%s] %s",
                index, paper.score, origin, paper.status,
                paper.topic_name or "-", paper.display_title[:64],
            )
        LOGGER.info("完整运行请去掉 --dry-run。")
        return EXIT_OK

    search_cfg = config["search"]
    window_note = describe_window(
        selection.since.isoformat(),
        run_date.isoformat(),
        int(search_cfg["fallback_window_days"] if selection.used_fallback else search_cfg["primary_window_days"]),
        selection.used_fallback,
    )
    return generate_article(
        config=config,
        group=group,
        run_date=run_date,
        article_id=article_id,
        volume=int(existing["volume"]) if existing and existing.get("volume") else repo.next_volume(),
        selection=selection,
        window_note=window_note,
        repo=repo,
    )


def prepare_papers(
    args: argparse.Namespace,
    config: dict[str, Any],
    paper_filter: PaperFilter,
    group: TopicGroup,
    repo: PaperRepository,
    plan: WeeklyPlan,
    week_keys: set[str],
) -> Optional[SelectionResult]:
    search_cfg = config["search"]
    today = args.date or date.today()
    primary_days = int(search_cfg["primary_window_days"])
    fallback_days = int(search_cfg["fallback_window_days"])
    target_year = int(search_cfg.get("target_year") or 0)
    use_history = bool((config.get("weekly_plan") or {}).get("deduplicate_history", True))

    arxiv = ArxivClient([str(c) for c in search_cfg["arxiv_categories"]])
    journal = build_journal_client(config)
    venue_clients = build_clients(config, ROOT)

    # 会议论文集与期刊通道的结果只跟「运行日」有关，跟 arXiv 的时间窗无关，
    # 所以只取一次。回退时间窗时不必重新抓取——会议/期刊的抓取代价高得多。
    conference_papers, venue_owners = collect_all(venue_clients)
    journal_papers = _collect_journal(journal, search_cfg, today)

    def gather(since: date) -> tuple[list[Paper], int]:
        arxiv_papers = arxiv.search(since, today, int(search_cfg["max_candidates"]))
        LOGGER.info(
            "候选来源：arXiv %d 篇 + 期刊 %d 篇 + 会议论文集 %d 篇",
            len(arxiv_papers), len(journal_papers), len(conference_papers),
        )
        papers = arxiv_papers + journal_papers + conference_papers
        if target_year:
            papers = _filter_by_year(papers, target_year)
        deduped = paper_filter.dedupe(papers)
        # deduplicate_history=false 时只做周内去重，不再查历史库
        fresh, skipped = paper_filter.exclude_known(
            deduped, repo if use_history else None, extra_keys=week_keys
        )
        return paper_filter.score(fresh, group), skipped

    since = today - timedelta(days=primary_days)
    scored, skipped = gather(since)
    used_fallback = False

    if len(scored) < int(search_cfg["min_relevant"]):
        LOGGER.warning(
            "最近 %d 天只筛出 %d 篇相关论文，回退到最近 %d 天",
            primary_days, len(scored), fallback_days,
        )
        since = today - timedelta(days=fallback_days)
        scored, skipped = gather(since)
        used_fallback = True

    if not scored:
        LOGGER.error("最近 %d 天内没有找到任何符合研究方向的论文，本期不生成文章。", fallback_days)
        return None

    # 会议论文集列表页只有标题，摘要按需去论文页抓，绝不全量抓
    scored = _fill_conference_abstracts(scored, venue_owners, config, paper_filter, group)

    enrich_top_n = int(search_cfg.get("enrich_top_n", 15))
    verifier = PublicationVerifier(config.get("venue_whitelist") or [])
    if bool(search_cfg.get("verify_publication", True)):
        enriched_ids = _enrich(scored[:enrich_top_n], verifier, scored[enrich_top_n:])
    else:
        # 不核验时如实保留各来源给出的状态：会议论文仍带官方论文集认定的「正式发表」，
        # arXiv 论文停留在「待核实」。绝不因为「没查」就当成已发表。
        LOGGER.warning(
            "search.verify_publication=false：跳过发表状态核验，"
            "arXiv 论文将保持「待核实」"
        )
        enriched_ids = {paper.uid for paper in scored}

    scored = paper_filter.score(scored, group)
    min_count = plan.min_count
    pool = [p for p in scored if p.uid in enriched_ids]
    if len(pool) < min_count:
        LOGGER.warning("已核验的候选不足，放宽为全部候选参与选取")
        pool = scored

    selected = paper_filter.select(
        pool,
        group,
        plan.desired,
        min_count,
        min_score=float(search_cfg.get("min_score", 0.0)),
        diversity_ratio=float(search_cfg.get("diversity_ratio", 0.8)),
    )
    selected = _apply_preprint_cap(
        selected, pool, plan.desired, max_preprints_per_issue(config)
    )
    if len(selected) < min_count:
        LOGGER.error(
            "本期只选出 %d 篇论文，低于最低要求 %d 篇，本期不生成文章（宁缺毋滥）。",
            len(selected), min_count,
        )
        return None

    return SelectionResult(
        papers=selected,
        pool=pool,
        since=since,
        used_fallback=used_fallback,
        skipped_known=skipped,
    )


def _collect_journal(
    journal: Optional[JournalClient], search_cfg: dict[str, Any], today: date
) -> list[Paper]:
    """期刊通道：独立检索近期正式发表的论文，不是只用来补元数据。"""
    if journal is None:
        return []
    journal_days = int((search_cfg.get("journals") or {}).get("window_days", 180))
    # 期刊出版节奏比 arXiv 慢，用更长的窗口，否则常年拉不到东西
    try:
        return journal.search(today - timedelta(days=journal_days), today)
    except OpenAlexError as exc:
        LOGGER.warning("期刊通道检索失败，本期不带期刊候选：%s", exc)
        return []


def _filter_by_year(papers: list[Paper], year: int) -> list[Paper]:
    """严格按目标年份筛选。年份判不出来的一律不要，避免把旧论文当新论文。"""
    kept = [paper for paper in papers if paper.year == year]
    dropped = len(papers) - len(kept)
    if dropped:
        LOGGER.info(
            "按 search.target_year=%d 过滤掉 %d 篇（年份不符或无法确定）", year, dropped
        )
    return kept


def _fill_conference_abstracts(
    scored: list[Paper],
    venue_owners: dict[str, Any],
    config: dict[str, Any],
    paper_filter: PaperFilter,
    group: TopicGroup,
) -> list[Paper]:
    """给会议论文补摘要，然后重新打分。

    预算由 retrieval.official_venues.abstract_limit_per_issue 控制。
    到达上限即停止，剩下的论文按「只有标题」参与打分：
    宁可少抓，也不为了打分好看去无限抓页面。
    """
    limit = abstract_limit(config)
    filled = 0
    for paper in scored:
        if filled >= limit:
            LOGGER.warning("会议摘要抓取已达本期上限 %d 篇，其余论文按标题参与打分", limit)
            break
        if paper.source != "conference" or paper.abstract:
            continue
        client = venue_owners.get(paper.uid)
        if client is None:
            continue
        if client.fill_details(paper):
            filled += 1
    if not filled:
        return scored
    LOGGER.info("已为 %d 篇会议论文补齐摘要，重新评分", filled)
    return paper_filter.score(scored, group)


def _is_verified(paper: Paper) -> bool:
    """是否已有「正式发表 / 正式录用」的可靠证据。"""
    return paper.status in (PaperStatus.PUBLISHED, PaperStatus.ACCEPTED)


def _apply_preprint_cap(
    selected: list[Paper],
    pool: list[Paper],
    count: int,
    max_preprints: int,
) -> list[Paper]:
    """硬约束：每期最多 max_preprints 篇「非正式发表」的论文。

    超出时按入选顺序截断，再从候选池里用正式发表/正式录用的论文补位。
    补不齐就少出——配置明确了「正式发表不足时宁可少出，不用预印本凑数」。
    """
    verified = [paper for paper in selected if _is_verified(paper)]
    others = [paper for paper in selected if not _is_verified(paper)]
    if len(others) > max_preprints:
        LOGGER.info(
            "预印本 / 待核实论文 %d 篇，超过每期上限 %d 篇，截断 %d 篇",
            len(others), max_preprints, len(others) - max_preprints,
        )
        others = others[:max_preprints]

    result = verified + others
    if len(result) < count:
        chosen = {id(paper) for paper in result}
        for paper in pool:
            if len(result) >= count:
                break
            if id(paper) in chosen or not _is_verified(paper):
                continue
            result.append(paper)
            chosen.add(id(paper))
    result.sort(key=lambda paper: -paper.score)
    return result


def _apply_crossref(
    paper: Paper, verifier: PublicationVerifier, client: CrossrefClient
) -> Paper:
    """用 Crossref 复核期刊论文的发表状态。

    配置明确规定 publication.openalex_venue_match_is_sufficient=false：
    OpenAlex 的来源名只是聚合结果，不能作为「正式发表」的依据。
    只有 Crossref 里存在该 DOI 的注册记录，并命中白名单，才算正式发表。
    """
    doi = str(paper.doi or "").strip()
    if not doi:
        paper.status = PaperStatus.UNVERIFIED
        paper.status_evidence = (
            f"OpenAlex 期刊通道检索到《{paper.venue}》，但没有 DOI，"
            "无法在 Crossref 复核出版记录，按配置不认定为正式发表"
        )
        return paper

    try:
        record = client.lookup(doi)
    except CrossrefError as exc:
        # 核验服务本身不可用，不能因此把一篇已发表的论文改判。
        # 保留原结论，但必须把「本期没复核成」记进日志。
        LOGGER.warning("Crossref 不可用，本期未能复核 %s：%s", doi, exc)
        return paper

    if record is None:
        paper.status = PaperStatus.UNVERIFIED
        paper.status_evidence = (
            f"OpenAlex 显示该论文发表于《{paper.venue}》，"
            f"但 Crossref 中没有 DOI {doi} 的注册记录，按配置不认定为正式发表"
        )
        return paper

    container = record.get("container_title") or paper.venue or ""
    matched = verifier.match_venue(container)
    if not matched:
        paper.status = PaperStatus.UNVERIFIED
        paper.status_evidence = (
            f"Crossref 记录显示该论文发表于《{container}》，"
            "但该来源不在顶会/顶刊白名单内，级别未经核验"
        )
        return paper

    paper.status = PaperStatus.PUBLISHED
    paper.doi = record.get("doi") or doi
    if record.get("published"):
        # 用出版商注册的正式发表日期替换聚合结果里的日期
        paper.publication_date = str(record["published"])
    paper.status_source_url = f"https://doi.org/{paper.doi}"
    paper.status_evidence = (
        f"Crossref 注册记录：DOI {paper.doi} 发表于《{container}》"
        f"（出版商 {record.get('publisher') or '未知'}，"
        f"发表日期 {record.get('published') or '未知'}）；"
        f"命中顶会/顶刊白名单「{matched}」"
    )
    return paper


def _enrich(
    to_enrich: list[Paper],
    verifier: PublicationVerifier,
    rest: list[Paper],
) -> set[str]:
    """对分数最高的若干篇做发表状态核验，其余如实标记为未核验。"""
    client = OpenAlexClient(mailto=os.getenv("OPENALEX_MAILTO"))
    crossref = CrossrefClient(
        mailto=os.getenv("CROSSREF_MAILTO") or os.getenv("OPENALEX_MAILTO")
    )
    enrichment_available = True
    enriched_ids: set[str] = set()

    for paper in to_enrich:
        # 会议论文集拿到的本来就是官方出版记录，发表状态已经确定。
        # 这里必须跳过重复核验：一旦再查一次没匹配上，就会把「正式发表」
        # 错改成「预印本」，并写出一句与事实相反的判断依据。
        if paper.source == "conference":
            enriched_ids.add(paper.uid)
            continue
        # 期刊论文改由 Crossref 复核，理由见 _apply_crossref
        if paper.source == "journal":
            _apply_crossref(paper, verifier, crossref)
            enriched_ids.add(paper.uid)
            continue

        record = None
        if enrichment_available:
            try:
                if paper.arxiv_doi:
                    record = client.lookup_by_doi(paper.arxiv_doi)
                if record is None:
                    record = client.lookup_by_title(paper.title)
            except OpenAlexError as exc:
                # OpenAlex 挂了不影响主流程，但必须如实反映「没核验过」
                LOGGER.warning("OpenAlex 不可用，后续论文将标记为待核实：%s", exc)
                enrichment_available = False
        verifier.apply(paper, record, enrichment_available=enrichment_available)
        enriched_ids.add(paper.uid)

    # 期刊与会议论文的发表状态来自官方出版记录，不属于「未核验」，
    # 不能被下面的兜底逻辑覆盖成「待核实」。
    official = [paper for paper in rest if paper.source in ("journal", "conference")]
    remaining = [paper for paper in rest if paper.source not in ("journal", "conference")]
    for paper in official:
        enriched_ids.add(paper.uid)

    if enrichment_available:
        for paper in remaining:
            verifier.mark_unverified(
                paper,
                "超出本期元数据核验数量上限，未对本文执行发表状态核验；"
                "如需核验可调大 config/topics.yaml 中的 search.enrich_top_n。",
            )
    else:
        for paper in remaining:
            verifier.apply(paper, None, enrichment_available=False)
            enriched_ids.add(paper.uid)

    LOGGER.info(
        "发表状态核验完成（OpenAlex %s）", "正常" if enrichment_available else "不可用"
    )
    return enriched_ids


# ======================================================================
# 生成与落盘
# ======================================================================
def generate_article(
    *,
    config: dict[str, Any],
    group: TopicGroup,
    run_date: date,
    article_id: str,
    volume: int,
    selection: SelectionResult,
    window_note: str,
    repo: PaperRepository,
) -> int:
    llm = llm_from_env()
    article_cfg = config.get("article") or {}
    validator = build_validator(config)
    generator = ArticleGenerator(
        llm,
        validator,
        field_budgets=article_cfg.get("field_budgets"),
        synthesis_budgets=article_cfg.get("synthesis_budgets"),
    )

    contexts = build_contexts(selection.papers)
    analyses = generator.analyze_all(contexts)

    min_count = int(config["search"]["min_select_count"])
    usable = pick_usable(analyses, min_count)
    if not usable:
        LOGGER.error("所有论文解读均未通过校验，本期不生成文章。")
        push_notice(
            config,
            notifier.build_failure_message(
                scope="大模型解读", reason="所有论文解读均未通过事实校验，本期未生成文章"
            ),
        )
        return EXIT_INSUFFICIENT

    contexts_by_id: dict[str, PaperContext] = {ctx.paper.uid: ctx for ctx in contexts}
    usable_contexts = [contexts_by_id[a.paper.uid] for a in usable]
    synthesis = generator.synthesize(usable, group.name, window_note)

    generated_at = datetime.now(timezone_of(config)).strftime("%Y-%m-%d %H:%M")
    bundle = generator.build_bundle(
        volume=volume,
        group_name=group.name,
        window_note=window_note,
        generated_at=generated_at,
        analyses=usable,
        synthesis=synthesis,
        model_name=llm.model,
        contexts=usable_contexts,
    )

    slug = f"{run_date.isoformat()}-vol{volume:02d}-{group.id}"
    save_markdown, save_html = output_targets(config)
    if not save_markdown and not save_html:
        LOGGER.error(
            "article.output_formats 与 publishing.save_markdown / save_html "
            "同时关掉了所有输出格式，本期没有可发布的产物，已中止。"
        )
        return EXIT_ERROR
    markdown_publisher = MarkdownPublisher(OUTPUT_DIR)
    html_publisher = HtmlPublisher(OUTPUT_DIR, TEMPLATE_PATH)

    md_path = markdown_publisher.save_article(slug, bundle.markdown) if save_markdown else None
    html_path = (
        html_publisher.save_article(slug, bundle.markdown, bundle.title, generated_at)
        if save_html
        else None
    )
    report = ArticleValidator.render_report(
        bundle.title,
        generated_at,
        [(contexts_by_id[a.paper.uid], a.warnings) for a in usable],
        bundle.article_issues,
    )
    # 校验报告与输出格式无关，始终落盘：发布前必须人工读它
    report_path = markdown_publisher.save_report(slug, report)

    files = {"validation": str(report_path.relative_to(ROOT))}
    if md_path:
        files["markdown"] = str(md_path.relative_to(ROOT))
    if html_path:
        files["html"] = str(html_path.relative_to(ROOT))

    record = ArticleRecord(
        article_id=article_id,
        volume=volume,
        created_at=generated_at,
        group_id=group.id,
        group_name=group.name,
        theme=bundle.theme,
        status="generated",
        title=bundle.title,
        files=files,
        papers=[
            {
                "uid": analysis.paper.uid,
                "source": analysis.paper.source,
                "arxiv_id": analysis.paper.arxiv_id or None,
                "doi": analysis.paper.doi or analysis.paper.arxiv_doi or None,
                "title": analysis.paper.display_title,
                "status": analysis.paper.status,
                "venue": analysis.paper.venue,
                "year": analysis.paper.year,
                "url": analysis.paper.source_url,
                "topic_id": analysis.paper.topic_id,
                "score": analysis.paper.score,
            }
            for analysis in usable
        ],
        warnings=bundle.warnings,
    )
    try:
        repo.record_article(record)
        repo.save()
    except RepositoryError as exc:
        LOGGER.error(
            "文章已生成并保存到 %s，但历史库写入失败：%s。"
            "请先修复历史库；本期论文尚未登记，下次运行可能重复推荐。",
            OUTPUT_DIR, exc,
        )
        return EXIT_ERROR

    # 草稿建失败不该让这一期作废：文章与历史库都已落盘
    try:
        create_wechat_draft(
            config,
            title=bundle.title,
            markdown=bundle.markdown,
            digest=str(synthesis.get("intro") or "")[:100],
            html_publisher=html_publisher,
        )
    except WeChatError as exc:
        LOGGER.error("公众号草稿未创建：%s", exc)

    LOGGER.info("=== 本期完成 ===")
    LOGGER.info("卷号：Vol.%02d  主题：%s", volume, bundle.theme)
    LOGGER.info("Markdown：%s", md_path or "（按配置未输出）")
    LOGGER.info("HTML：%s", html_path or "（按配置未输出）")
    LOGGER.info("校验报告：%s", report_path)
    if bundle.warnings:
        LOGGER.warning("共有 %d 条待核实项，发布前请先读校验报告。", len(bundle.warnings))
    else:
        LOGGER.info("校验未发现可疑项。")

    push_notice(
        config,
        notifier.build_issue_message(
            volume=volume,
            theme=bundle.theme,
            group_name=group.name,
            papers=[(_source_label(a.paper), a.paper.status, a.paper.display_title) for a in usable],
            warning_count=len(bundle.warnings),
            link=notifier.issue_link(slug),
        ),
    )
    LOGGER.info("下一步：人工阅读校验报告 -> 把 HTML 正文复制到公众号后台。")
    return EXIT_OK


# ======================================================================
# 辅助命令
# ======================================================================
def selftest() -> int:
    load_dotenv(ROOT / ".env")
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    failures = 0

    def report(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))

    try:
        config = load_config()
        report(
            "配置文件",
            True,
            f"{len(config['topics'])} 个方向 / {len(config['groups'])} 个主题组",
        )
    except SystemExit as exc:
        report("配置文件", False, str(exc))
        return EXIT_ERROR

    api_key = os.getenv("LLM_API_KEY", "").strip()
    report(".env 中的 LLM_API_KEY", bool(api_key), "" if api_key else "未填写，无法调用大模型")

    try:
        client = ArxivClient([str(config["search"]["arxiv_categories"][0])])
        today = date.today()
        papers = client.search(today - timedelta(days=3), today, 2)
        report("arXiv API", True, f"最近 3 天取回 {len(papers)} 篇")
    except (ArxivError, ValueError) as exc:
        report("arXiv API", False, str(exc))

    try:
        OpenAlexClient(mailto=os.getenv("OPENALEX_MAILTO")).lookup_by_title(
            "Attention Is All You Need"
        )
        report("OpenAlex API", True)
    except OpenAlexError as exc:
        report("OpenAlex API", False, str(exc))

    try:
        clients = build_clients(config, ROOT)
        report(
            "会议论文集通道",
            True,
            f"{len(clients)} 个来源：" + "、".join(c.label for c in clients)
            if clients
            else "未启用",
        )
    except Exception as exc:  # noqa: BLE001 配置写错时要看到原因，而不是整条自检挂掉
        report("会议论文集通道", False, str(exc))

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = OUTPUT_DIR / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report("输出目录可写", True, str(OUTPUT_DIR))
    except OSError as exc:
        report("输出目录可写", False, str(exc))

    try:
        path = history_path(config)
        repo = PaperRepository(path)
        repo.load()
        report("历史库", True, f"{len(repo.all_articles())} 期记录（{path}）")
    except RepositoryError as exc:
        report("历史库", False, str(exc))

    report(
        "微信通知",
        True,
        "已配置 PUSHPLUS_TOKEN"
        if notifier.pushplus_token()
        else "未配置 PUSHPLUS_TOKEN，出稿后不会推微信（见 README 第九节）",
    )

    if api_key:
        try:
            llm = LLMClient(
                api_key=api_key,
                base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
                model=os.getenv("LLM_MODEL", DEFAULT_MODEL),
                timeout=int(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
                max_retries=1,
                max_tokens=_max_tokens_from_env(),
            )
            result = llm.complete_json(
                "你只输出 JSON，不要输出其他内容。",
                '请原样输出这个 JSON：{"status": "ok"}',
            )
            report("大模型调用", result.get("status") == "ok", f"返回 {result}")
        except LLMError as exc:
            report("大模型调用", False, str(exc))

    print()
    if failures:
        print(f"自检未通过：{failures} 项，请按上面的提示逐项修复。")
        return EXIT_ERROR
    print("自检全部通过，可以运行 `python main.py` 生成第一期文章。")
    return EXIT_OK


def show_history() -> int:
    setup_logging("WARNING")
    repo = PaperRepository(history_path(load_config()))
    repo.load()
    articles = repo.all_articles()
    if not articles:
        print("历史库为空，还没有生成过文章。")
        return EXIT_OK
    print(f"共 {len(articles)} 期：")
    for article in articles:
        print(
            f"  Vol.{int(article.get('volume') or 0):02d}  "
            f"{article.get('created_at')}  "
            f"[{article.get('status')}]  "
            f"{article.get('title')}"
        )
    print(f"\n已登记 {len(repo.known_paper_keys())} 条论文标识（用于去重）。")
    return EXIT_OK


# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AI Paper Weekly：检索 arXiv -> 生成中文解读 -> 输出 Markdown / HTML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py --selftest        检查配置、网络与密钥\n"
            "  python main.py --dry-run         只检索与筛选，不调用大模型\n"
            "  python main.py                   按今天的星期自动选主题\n"
            "  python main.py --group g_wed     指定主题组\n"
            "  python main.py --force           忽略「本期已生成」直接重跑\n"
            "  python main.py --history         查看历史记录\n"
        ),
    )
    parser.add_argument("--group", help="主题组 ID，如 g_mon / g_wed / g_fri")
    parser.add_argument("--date", type=_iso_date, help="把某一天当作运行日（YYYY-MM-DD）")
    parser.add_argument("--force", action="store_true", help="忽略幂等检查，强制重新生成")
    parser.add_argument("--dry-run", action="store_true", help="只检索与筛选，不调用大模型")
    parser.add_argument("--selftest", action="store_true", help="检查配置、网络与密钥是否可用")
    parser.add_argument("--history", action="store_true", help="打印历史文章与去重统计")
    return parser


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD，收到 {value}") from exc


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.selftest:
            return selftest()
        if args.history:
            return show_history()
        return run(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_ERROR
    except (ArxivError, OpenAlexError, LLMError, RepositoryError) as exc:
        LOGGER.error("运行失败：%s", exc)
        push_failure(args, "运行", str(exc))
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 兜底，保证日志里有完整堆栈
        LOGGER.exception("未预期的错误")
        push_failure(args, "运行", f"未预期的错误：{exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())