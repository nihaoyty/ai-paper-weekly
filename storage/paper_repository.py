"""论文与文章的本地持久化。

职责：
* 用 arXiv ID / DOI / OpenAlex ID 做去重，避免同一研究成果重复推荐；
* 记录每期文章的生成时间、选用论文、发表状态与产物路径；
* 写入采用「临时文件 + 原子替换」，避免中途失败把历史库写坏。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from models import ArticleRecord, Paper

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class RepositoryError(RuntimeError):
    """历史库读取或写入失败。"""


class PaperRepository:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = _empty_data()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            LOGGER.info("历史库不存在，将新建：%s", self.path)
            self._data = _empty_data()
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else _empty_data()
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryError(
                f"历史库 {self.path} 无法解析，已中止以免覆盖数据：{exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise RepositoryError(f"历史库 {self.path} 结构异常，顶层应为对象")

        parsed.setdefault("schema_version", SCHEMA_VERSION)
        parsed.setdefault("articles", [])
        parsed.setdefault("papers", {})
        self._data = parsed
        LOGGER.info(
            "加载历史库：%d 期文章，%d 条论文索引", len(self._data["articles"]), len(self._data["papers"])
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise RepositoryError(f"历史库写入失败：{exc}") from exc

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def known_paper_keys(self) -> set[str]:
        return set(self._data.get("papers", {}).keys())

    def is_known(self, paper: Paper) -> bool:
        known = self.known_paper_keys()
        return any(key in known for key in paper.dedupe_keys)

    def find_paper(self, key: str) -> Optional[dict[str, Any]]:
        return self._data.get("papers", {}).get(key)

    def find_article(self, article_id: str) -> Optional[dict[str, Any]]:
        for article in self._data.get("articles", []):
            if article.get("id") == article_id:
                return article
        return None

    def all_articles(self) -> list[dict[str, Any]]:
        return list(self._data.get("articles", []))

    def next_volume(self) -> int:
        volumes = [
            int(a.get("volume") or 0) for a in self._data.get("articles", [])
        ]
        return (max(volumes) + 1) if volumes else 1

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def record_article(self, record: ArticleRecord) -> None:
        articles = self._data.setdefault("articles", [])
        payload = record.to_dict()
        # 统一用 id 作为期号主键：ArticleRecord 的字段名是 article_id，
        # 直接 asdict 会写出 article_id，与 find_article 的读法不一致。
        payload["id"] = payload.pop("article_id")
        for index, existing in enumerate(articles):
            if existing.get("id") == payload["id"]:
                articles[index] = payload
                break
        else:
            articles.append(payload)

        index_papers = self._data.setdefault("papers", {})
        now = _now_iso()
        for item in record.papers:
            key_list = _entry_keys(item)
            existing_entry = next(
                (index_papers[k] for k in key_list if k in index_papers), None
            )
            if existing_entry is not None:
                entry = existing_entry
                if record.article_id not in entry.setdefault("articles", []):
                    entry["articles"].append(record.article_id)
                entry["last_seen"] = now
            else:
                entry = {
                    "keys": key_list,
                    "title": item.get("title", ""),
                    "arxiv_id": item.get("arxiv_id"),
                    "doi": item.get("doi"),
                    "status": item.get("status"),
                    "first_seen": now,
                    "last_seen": now,
                    "articles": [record.article_id],
                }
            for key in key_list:
                index_papers[key] = entry

        LOGGER.info("已记录第 %d 期：%s", record.volume, record.article_id)

    def mark_status(
        self,
        article_id: str,
        status: str,
        published_at: Optional[str] = None,
    ) -> bool:
        """更新某期文章状态。不存在或写入失败时返回 False。"""
        article = self.find_article(article_id)
        if article is None:
            LOGGER.warning("历史库中没有 %s，无法更新状态", article_id)
            return False
        article["status"] = status
        if published_at:
            article["published_at"] = published_at
        else:
            article.pop("published_at", None)
        self.save()
        return True


def _empty_data() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "articles": [], "papers": {}}


def _entry_keys(item: dict[str, Any]) -> list[str]:
    """从文章记录里的论文条目重建去重键。

    期刊论文没有 arXiv ID，靠 DOI；arXiv 论文靠 arXiv ID。
    两条都缺时退回显式记录的 uid，避免整条记录没有去重键。
    """
    keys: list[str] = []
    if item.get("arxiv_id"):
        keys.append(f"arxiv:{item['arxiv_id']}")
    if item.get("doi"):
        keys.append(f"doi:{str(item['doi']).lower()}")
    if not keys and item.get("uid"):
        keys.append(str(item["uid"]))
    return keys


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")