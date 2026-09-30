"""会议论文集采集器集合。

每个来源对应 config/topics.yaml 里 retrieval.official_venues.sources 的一项。
这里只负责「按配置组装好采集器」，具体抓取逻辑在各子模块里。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Optional

from .acl_anthology import AclAnthologyClient
from .base import OfficialVenueClient, SeedCache, VenueRecord, html_to_text
from .cvf import CvfClient
from .iclr_virtual import IclrVirtualClient
from .neurips import NeuripsClient
from .pmlr import PmlrClient

LOGGER = logging.getLogger(__name__)

__all__ = [
    "AclAnthologyClient",
    "CvfClient",
    "IclrVirtualClient",
    "NeuripsClient",
    "OfficialVenueClient",
    "PmlrClient",
    "SeedCache",
    "VenueRecord",
    "build_clients",
    "html_to_text",
]

REGISTRY: dict[str, type[OfficialVenueClient]] = {
    IclrVirtualClient.id: IclrVirtualClient,
    CvfClient.id: CvfClient,
    AclAnthologyClient.id: AclAnthologyClient,
    PmlrClient.id: PmlrClient,
    NeuripsClient.id: NeuripsClient,
}

UNKNOWN_SOURCE_WARNING = (
    "config/topics.yaml 的 retrieval.official_venues.sources 里有未实现的来源 id「%s」，"
    "已跳过。可选：%s"
)


def official_venues_config(config: dict[str, Any]) -> dict[str, Any]:
    return ((config.get("retrieval") or {}).get("official_venues") or {}) or {}


def build_clients(
    config: dict[str, Any], root: Path
) -> list[OfficialVenueClient]:
    """按配置创建全部会议采集器。未启用时返回空列表。"""
    cfg = official_venues_config(config)
    if not cfg.get("enabled"):
        LOGGER.info("会议论文集通道未启用")
        return []

    cache = SeedCache(
        Path(root) / str(cfg.get("cache_dir") or "data/cache"),
        ttl_days=int(cfg.get("cache_ttl_days", 7)),
    )

    clients: list[OfficialVenueClient] = []
    for entry in cfg.get("sources") or []:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        source_id = str(entry.get("id") or "").strip()
        builder = REGISTRY.get(source_id)
        if builder is None:
            LOGGER.warning(UNKNOWN_SOURCE_WARNING, source_id, ", ".join(REGISTRY))
            continue
        years = _int_list(entry.get("years"))
        if not years:
            LOGGER.warning("会议来源 %s 没有配置年份，已跳过", source_id)
            continue
        clients.append(
            builder(cache, years=years, venues=_str_list(entry.get("venues")))
        )
    LOGGER.info("会议论文集通道已启用，共 %d 个来源：%s", len(clients), ", ".join(c.label for c in clients))
    return clients


def abstract_limit(config: dict[str, Any]) -> int:
    return int(official_venues_config(config).get("abstract_limit_per_issue", 120))


def _int_list(value: Any) -> list[int]:
    result: list[int] = []
    for item in value or []:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            LOGGER.warning("会议来源年份配置无法解析：%r", item)
    return result


def _str_list(value: Any) -> list[str]:
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def collect_all(
    clients: Iterable[OfficialVenueClient],
) -> tuple[list, dict[str, OfficialVenueClient]]:
    """抓取全部会议来源。

    返回 (论文列表, {论文 uid: 采集器})。后者用于后续按需补摘要：
    会议列表页不带摘要，只有拿到对应的采集器才能去抓论文页。
    """
    papers: list = []
    owners: dict[str, OfficialVenueClient] = {}
    for client in clients:
        found = client.collect()
        for paper in found:
            owners[paper.uid] = client
        papers.extend(found)
    return papers, owners