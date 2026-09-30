"""历史库持久化的单元测试（不联网）。"""

from __future__ import annotations

import json

import pytest

from models import ArticleRecord
from storage.paper_repository import PaperRepository, RepositoryError


def make_record(article_id: str = "2026-09-21-g_mon", volume: int = 1) -> ArticleRecord:
    return ArticleRecord(
        article_id=article_id,
        volume=volume,
        created_at="2026-09-21 23:28",
        group_id="g_mon",
        group_name="大模型推理与 AI Agent",
        theme="推理编排",
        title="AI 论文周报 Vol.01｜推理编排",
        papers=[
            {
                "arxiv_id": "2609.21554",
                "doi": "10.1000/ABC",
                "title": "MIRAGE",
                "status": "待核实",
                "url": "https://arxiv.org/abs/2609.21554",
            }
        ],
    )


@pytest.fixture
def repo(tmp_path) -> PaperRepository:
    repository = PaperRepository(tmp_path / "published.json")
    repository.load()
    return repository


class TestRecordAndFind:
    def test_find_article_after_record(self, repo: PaperRepository) -> None:
        """回归测试：写入用的是 article_id、读取用的是 id，曾导致幂等检查失效。"""
        repo.record_article(make_record())
        found = repo.find_article("2026-09-21-g_mon")
        assert found is not None
        assert found["id"] == "2026-09-21-g_mon"
        assert found["volume"] == 1
        assert "article_id" not in found

    def test_unknown_article_returns_none(self, repo: PaperRepository) -> None:
        assert repo.find_article("2099-01-01-g_fri") is None

    def test_re_record_replaces_instead_of_appending(self, repo: PaperRepository) -> None:
        repo.record_article(make_record(volume=1))
        repo.record_article(make_record(volume=1))
        assert len(repo.all_articles()) == 1

    def test_next_volume_advances(self, repo: PaperRepository) -> None:
        assert repo.next_volume() == 1
        repo.record_article(make_record(volume=1))
        assert repo.next_volume() == 2


class TestDedupeIndex:
    def test_indexes_both_arxiv_and_doi_keys(self, repo: PaperRepository) -> None:
        repo.record_article(make_record())
        keys = repo.known_paper_keys()
        assert "arxiv:2609.21554" in keys
        assert "doi:10.1000/abc" in keys

    def test_second_article_adds_to_existing_entry(self, repo: PaperRepository) -> None:
        repo.record_article(make_record())
        repo.record_article(make_record(article_id="2026-09-28-g_mon", volume=2))
        entry = repo.find_paper("arxiv:2609.21554")
        assert entry is not None
        assert entry["articles"] == ["2026-09-21-g_mon", "2026-09-28-g_mon"]


class TestPersistence:
    def test_save_then_load_round_trip(self, tmp_path) -> None:
        path = tmp_path / "published.json"
        first = PaperRepository(path)
        first.load()
        first.record_article(make_record())
        first.save()

        second = PaperRepository(path)
        second.load()
        assert second.find_article("2026-09-21-g_mon") is not None
        assert "arxiv:2609.21554" in second.known_paper_keys()

    def test_missing_file_starts_empty(self, tmp_path) -> None:
        repo = PaperRepository(tmp_path / "nested" / "published.json")
        repo.load()
        assert repo.all_articles() == []

    def test_corrupt_file_raises_instead_of_overwriting(self, tmp_path) -> None:
        path = tmp_path / "published.json"
        path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        repo = PaperRepository(path)
        with pytest.raises(RepositoryError, match="无法解析"):
            repo.load()
        # 原文件必须原样保留，不能被清空
        assert "这不是合法 JSON" in path.read_text(encoding="utf-8")

    def test_no_tmp_file_left_behind(self, tmp_path) -> None:
        path = tmp_path / "published.json"
        repo = PaperRepository(path)
        repo.load()
        repo.record_article(make_record())
        repo.save()
        assert not (tmp_path / "published.json.tmp").exists()
        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


class TestMarkStatus:
    def test_mark_status_persists(self, tmp_path) -> None:
        path = tmp_path / "published.json"
        repo = PaperRepository(path)
        repo.load()
        repo.record_article(make_record())
        assert repo.mark_status("2026-09-21-g_mon", "published", "2026-09-22 09:00") is True

        reloaded = PaperRepository(path)
        reloaded.load()
        article = reloaded.find_article("2026-09-21-g_mon")
        assert article is not None
        assert article["status"] == "published"
        assert article["published_at"] == "2026-09-22 09:00"

    def test_mark_missing_article_returns_false(self, repo: PaperRepository) -> None:
        assert repo.mark_status("2099-01-01-g_fri", "published") is False