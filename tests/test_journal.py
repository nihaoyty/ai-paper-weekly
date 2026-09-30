"""期刊通道的单元测试（不联网）。"""

from __future__ import annotations

from datetime import date

from models import Paper, PaperStatus
from collectors.openalex_client import (
    OpenAlexClient,
    _extract_authors,
    _reconstruct_abstract,
)
from collectors.journal_client import JournalClient

SINCE = date(2026, 6, 1)
UNTIL = date(2026, 9, 22)


def openalex_work(**overrides) -> dict:
    work = {
        "id": "https://openalex.org/W123456",
        "doi": "https://doi.org/10.1038/s42256-026-01234-5",
        "display_name": "Quantum neural operators with implicit quadrature",
        "publication_date": "2026-09-14",
        "publication_year": 2026,
        "type": "article",
        "cited_by_count": 3,
        "abstract_inverted_index": {
            "We": [0],
            "propose": [1],
            "a": [2],
            "method": [3],
        },
        "authorships": [
            {"author": {"display_name": "Alice Zhang"}},
            {"author": {"display_name": "Bob Li"}},
        ],
        "primary_location": {
            "landing_page_url": "https://www.nature.com/articles/s42256-026-01234-5",
            "source": {
                "display_name": "Nature Machine Intelligence",
                "type": "journal",
                "homepage_url": "https://www.nature.com/natmachintell/",
            },
        },
        "locations": [],
    }
    work.update(overrides)
    return work


class TestAbstractReconstruction:
    def test_rebuilds_words_in_order(self) -> None:
        assert _reconstruct_abstract(openalex_work()) == "We propose a method"

    def test_missing_index_returns_empty(self) -> None:
        assert _reconstruct_abstract({"abstract_inverted_index": None}) == ""

    def test_handles_multi_position_words(self) -> None:
        work = openalex_work(abstract_inverted_index={"the": [0, 2], "cat": [1], "sat": [3]})
        assert _reconstruct_abstract(work) == "the cat the sat"


class TestAuthorExtraction:
    def test_extracts_display_names(self) -> None:
        assert _extract_authors(openalex_work()) == ["Alice Zhang", "Bob Li"]

    def test_skips_malformed_entries(self) -> None:
        work = openalex_work(authorships=[{"author": {}}, "garbage", {"author": {"display_name": "C"}}])
        assert _extract_authors(work) == ["C"]


class FakeOpenAlex:
    """替身：只实现 JournalClient 用到的方法。"""

    def __init__(self, works: list[dict], sources: dict[str, str]) -> None:
        self._works = works
        self._sources = sources
        self.source_calls = 0

    def find_source_id(self, name: str):
        self.source_calls += 1
        return self._sources.get(name)

    def recent_works(self, source_id, since, until, per_page=50):
        return list(self._works)


class TestJournalClient:
    def test_builds_published_paper(self) -> None:
        client = JournalClient(
            FakeOpenAlex([openalex_work()], {"Nature Machine Intelligence": "S1"}),
            ["Nature Machine Intelligence"],
        )
        papers = client.search(SINCE, UNTIL)
        assert len(papers) == 1
        paper = papers[0]
        assert paper.source == "journal"
        assert paper.arxiv_id == ""
        assert paper.status == PaperStatus.PUBLISHED
        assert paper.venue == "Nature Machine Intelligence"
        assert paper.doi == "10.1038/s42256-026-01234-5"
        assert paper.abstract == "We propose a method"
        assert paper.authors == ["Alice Zhang", "Bob Li"]
        assert paper.uid == "doi:10.1038/s42256-026-01234-5"
        assert paper.source_url == "https://doi.org/10.1038/s42256-026-01234-5"
        assert "OpenAlex" in paper.status_evidence

    def test_source_resolution_is_cached(self) -> None:
        fake = FakeOpenAlex([], {"Nature Machine Intelligence": "S1"})
        client = JournalClient(fake, ["Nature Machine Intelligence"])
        client.resolve_sources()
        client.resolve_sources()
        assert fake.source_calls == 1

    def test_unknown_source_is_skipped(self) -> None:
        client = JournalClient(FakeOpenAlex([], {}), ["Nonexistent Journal"])
        assert client.search(SINCE, UNTIL) == []

    def test_work_without_title_is_skipped(self) -> None:
        client = JournalClient(
            FakeOpenAlex([openalex_work(display_name="")], {"X": "S1"}), ["X"]
        )
        assert client.search(SINCE, UNTIL) == []

    def test_venue_falls_back_to_configured_name(self) -> None:
        work = openalex_work(primary_location={"source": None}, locations=[])
        client = JournalClient(
            FakeOpenAlex([work], {"Nature Machine Intelligence": "S1"}),
            ["Nature Machine Intelligence"],
        )
        papers = client.search(SINCE, UNTIL)
        assert papers[0].venue == "Nature Machine Intelligence"


class TestPaperIdentity:
    def test_journal_paper_has_no_arxiv_identity(self) -> None:
        paper = Paper(title="T", abstract="A", doi="10.1/x")
        assert paper.is_arxiv is False
        assert paper.uid == "doi:10.1/x"
        assert paper.source_url == "https://doi.org/10.1/x"
        assert paper.dedupe_keys == ["doi:10.1/x"]

    def test_arxiv_paper_uses_arxiv_identity(self) -> None:
        paper = Paper(
            arxiv_id="2509.00001",
            title="T",
            abstract="A",
            arxiv_url="https://arxiv.org/abs/2509.00001",
        )
        assert paper.is_arxiv is True
        assert paper.uid == "arxiv:2509.00001"
        assert paper.source_url == "https://arxiv.org/abs/2509.00001"

    def test_normalize_work_fills_required_fields(self) -> None:
        record = OpenAlexClient.normalize_work(openalex_work())
        assert record["type"] == "article"
        assert record["venue"] == "Nature Machine Intelligence"
        assert record["venue_type"] == "journal"
        assert record["abstract"] == "We propose a method"