"""会议论文集采集器的单元测试（不联网）。

fixture 里的 HTML 都是从真实页面摘下来的片段，
结构一旦变化这些测试会失败，正好起到「页面改版告警」的作用。
"""

from __future__ import annotations

import gzip
import io
from pathlib import Path

import pytest

from collectors.venues.acl_anthology import (
    AclAnthologyClient,
    iter_bibtex_entries,
    parse_anthology_url,
    venue_from_collection,
)
from collectors.venues.base import SeedCache, clean_bibtex_value, split_bibtex_authors
from collectors.venues.cvf import CvfClient
from collectors.venues.iclr_virtual import IclrVirtualClient
from collectors.venues.neurips import NeuripsClient
from collectors.venues.pmlr import PmlrClient
from models import PaperStatus

# ----------------------------------------------------------------------
# 真实页面片段
# ----------------------------------------------------------------------
ICLR_INDEX = """
<ul>
<li><a href="/virtual/2026/poster/10006831">Escaping Policy Contraction: Contraction-Aware PPO for Stable Language Model Fine-Tuning</a></li>
<li><a href="/virtual/2026/poster/10009179">VITA: Zero-Shot Value Functions via Test-Time Adaptation of Vision–Language Models</a></li>
<li><a href="/virtual/2026/poster/10006831">重复链接应被忽略</a></li>
</ul>
"""

ICLR_POSTER = """
<h3>Abstract</h3>
<div class="abstract-text collapsed" id="abstractText"><div class="inner">
Reinforcement learning from human feedback (RLHF) with PPO is widely used.
</div></div>
<script type="application/ld+json">
{"@type": "ScholarlyArticle", "name": "InftyThink", "creditText": "ICLR 2026",
 "author": [{"@type": "Person", "name": "Yuchen Yan"}, {"@type": "Person", "name": "Yongliang Shen"},
            {"@type": "Person", "name": "Yuchen Yan"}]}
</script>
"""

CVF_INDEX = """
<dt class="ptitle"><br><a href="/content/CVPR2026/html/Xiao_Tracking_CVPR_2026_paper.html">Generalizable Structure-Aware Keypoint Correspondence</a></dt>
<dd>
<form id="f1" action="/CVPR2026" method="post" class="authsearch"><input type="hidden" name="query_author" value="Jie Xiao"><a href="#">Jie Xiao</a>,</form>
</dd>
<div class="link2">[<a href="/content/CVPR2026/papers/Xiao_Tracking_CVPR_2026_paper.pdf">pdf</a>][<a href="/content/CVPR2026/supplemental/Xiao_Tracking_CVPR_2026_supplemental.pdf">supp</a>]
<div class="bibref">@InProceedings{Xiao_2026_CVPR,
    author    = {Xiao, Jie and Ma, Yinchao and Zhang, Tianzhu},
    title     = {Generalizable Structure-Aware Keypoint Correspondence},
    booktitle = {Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)},
    year      = {2026},
    pages     = {28156-28166}
}</div></div>
<dt class="ptitle"><br><a href="/content/CVPR2026/html/Li_Second_CVPR_2026_paper.html">A Second Paper</a></dt>
<dd><form id="f2"><input type="hidden" name="query_author" value="Wei Li"></form></dd>
"""

NEURIPS_INDEX = """
<li><a href="/paper_files/paper/2024/hash/0001-Abstract-Conference.html" title="MicroAdam: Accurate Adaptive Optimization">MicroAdam: Accurate Adaptive Optimization</a></li>
<li><a class="btn" href="/paper_files/paper/2024/hash/0002-Abstract-Conference.html" title="Second Paper Title">Second Paper Title</a></li>
"""

PMLR_HOME = """
<li><a href="v339"><b>Volume 339</b></a> Proceedings of MLIC 2025</li>
<li><a href="v267"><b>Volume 267</b></a> Proceedings of ICML 2025</li>
<li><a href="v296"><b>Volume 296</b></a> Proceedings of COLM 2025</li>
"""

PMLR_VOLUME = """
<div class="paper">
  <p class="title">Aggregation of Dependent Expert Distributions</p>
  <p class="details">
    <span class="authors">Rogelio A. Mancisidor,&nbsp;Robert Jenssen,&nbsp;Shujian Yu</span>;
    <span class="info"><i>Proceedings of the 42nd International Conference on Machine Learning</i>, PMLR 267:1-26</span>
  </p>
  <p class="links">
    [<a href="https://proceedings.mlr.press/v267/a-mancisidor25a.html">abs</a>][<a href="https://raw.githubusercontent.com/mlresearch/v267/main/assets/a-mancisidor25a/a-mancisidor25a.pdf">pdf</a>]
  </p>
</div>
"""

ACL_BIB = """@inproceedings{yung-etal-2026-semi,
  title = {Semi-automatic Approach for {Tamil} Discourse Relation Annotation},
  author = {Yung, Fiona and Vajjala, Sowmya},
  year = {2026},
  url = {https://aclanthology.org/2026.wildre-1.2/},
  abstract = {We present a semi-automatic approach.}
}
@inproceedings{octotools-2026,
  title = {{OctoTools}: A Multi-Agent Framework},
  author = {Lu, Pan and Chen, Bowen},
  year = {2026},
  url = {https://aclanthology.org/2026.acl-long.1/},
  abstract = {We build an agentic framework.}
}
@inproceedings{2026-acl-front,
  title = {Proceedings of the 64th Annual Meeting of the Association for Computational Linguistics},
  year = {2026},
  url = {https://aclanthology.org/2026.acl-long.0/}
}
@inproceedings{findings-2026,
  title = {A Findings Paper},
  year = {2026},
  url = {https://aclanthology.org/2026.findings-acl.7/}
}
"""


def make_cache(tmp_path: Path, ttl_days: int = 7) -> SeedCache:
    return SeedCache(tmp_path / "cache", ttl_days=ttl_days)


# ----------------------------------------------------------------------
class TestSeedCache:
    def test_first_call_fetches_and_second_hits_cache(self, tmp_path):
        cache = make_cache(tmp_path)
        calls = []

        def fetch() -> bytes:
            calls.append(1)
            return b"payload"

        assert cache.get_or_fetch("key", fetch) == b"payload"
        assert cache.get_or_fetch("key", fetch) == b"payload"
        assert len(calls) == 1
        assert cache.hits == 1 and cache.misses == 1

    def test_expired_cache_is_refetched(self, tmp_path):
        cache = make_cache(tmp_path, ttl_days=0)
        cache.get_or_fetch("key", lambda: b"old")
        assert cache.get_or_fetch("key", lambda: b"new") == b"new"

    def test_failed_fetch_does_not_write_cache(self, tmp_path):
        cache = make_cache(tmp_path)

        def boom() -> bytes:
            raise RuntimeError("网络挂了")

        with pytest.raises(RuntimeError):
            cache.get_or_fetch("key", boom)
        assert list((tmp_path / "cache").glob("*.cache")) == []


class TestIclrVirtual:
    def test_parse_index_extracts_titles_and_dedupes(self, tmp_path):
        client = IclrVirtualClient(make_cache(tmp_path), years=[2026], venues=["ICLR"])
        records = client._parse_index(ICLR_INDEX, 2026)
        assert len(records) == 2
        assert records[0].venue == "ICLR"
        assert records[0].year == 2026
        assert records[0].page_url == "https://iclr.cc/virtual/2026/poster/10006831"
        assert records[0].title.startswith("Escaping Policy Contraction")

    def test_extract_abstract(self, tmp_path):
        client = IclrVirtualClient(make_cache(tmp_path), years=[2026], venues=["ICLR"])
        abstract = client.extract_abstract(ICLR_POSTER)
        assert "Reinforcement learning from human feedback" in abstract
        assert "<div" not in abstract

    def test_extract_authors_from_jsonld(self, tmp_path):
        """索引页只有标题，作者必须从单篇页的 JSON-LD 里取。"""
        client = IclrVirtualClient(make_cache(tmp_path), years=[2026], venues=["ICLR"])
        assert client.extract_authors(ICLR_POSTER) == ["Yuchen Yan", "Yongliang Shen"]

    def test_extract_authors_without_jsonld_is_empty(self, tmp_path):
        client = IclrVirtualClient(make_cache(tmp_path), years=[2026], venues=["ICLR"])
        assert client.extract_authors("<html><body>no ld+json</body></html>") == []

    def test_fill_details_fills_abstract_and_authors(self, tmp_path):
        client = IclrVirtualClient(make_cache(tmp_path), years=[2026], venues=["ICLR"])
        client.fetch_text = lambda url, retries=3: ICLR_POSTER  # type: ignore[assignment]
        records = client._parse_index(ICLR_INDEX, 2026)
        paper = client.to_paper(records[0])
        assert paper.abstract == "" and paper.authors == []
        assert client.fill_details(paper) is True
        assert "Reinforcement learning" in paper.abstract
        assert paper.authors == ["Yuchen Yan", "Yongliang Shen"]

    def test_fill_details_failure_is_not_fatal(self, tmp_path):
        """单篇抓不到只返回 False，不能抛异常拖垮整期。"""
        client = IclrVirtualClient(make_cache(tmp_path), years=[2026], venues=["ICLR"])
        client.fetch_text = lambda url, retries=3: ""  # type: ignore[assignment]
        records = client._parse_index(ICLR_INDEX, 2026)
        paper = client.to_paper(records[0])
        assert client.fill_details(paper) is False
        assert paper.abstract == ""


class TestCvf:
    def test_parse_index_reads_official_bibtex_authors(self, tmp_path):
        client = CvfClient(make_cache(tmp_path), years=[2026], venues=["CVPR"])
        records = client._parse_index(CVF_INDEX, "CVPR", 2026)
        assert len(records) == 2
        first = records[0]
        assert first.venue == "CVPR 2026"
        assert first.authors == ["Xiao, Jie", "Ma, Yinchao", "Zhang, Tianzhu"]
        assert first.page_url.endswith("/content/CVPR2026/html/Xiao_Tracking_CVPR_2026_paper.html")
        # 只取主论文 PDF，不能抓成 supplemental
        assert first.pdf_url.endswith("Xiao_Tracking_CVPR_2026_paper.pdf")

    def test_falls_back_to_author_input_when_no_bibtex(self, tmp_path):
        client = CvfClient(make_cache(tmp_path), years=[2026], venues=["CVPR"])
        records = client._parse_index(CVF_INDEX, "CVPR", 2026)
        assert records[1].authors == ["Wei Li"]


class TestNeurips:
    def test_parse_index_uses_title_attribute(self, tmp_path):
        client = NeuripsClient(make_cache(tmp_path), years=[2024], venues=["NeurIPS"])
        records = client._parse_index(NEURIPS_INDEX, 2024)
        assert len(records) == 2
        assert records[0].title == "MicroAdam: Accurate Adaptive Optimization"
        assert records[0].page_url.startswith("https://proceedings.neurips.cc/paper_files/paper/2024/hash/")
        assert records[0].year == 2024


class TestPmlr:
    def test_find_volume_matches_venue_and_year(self, tmp_path):
        client = PmlrClient(make_cache(tmp_path), years=[2025, 2026], venues=["ICML"])
        assert client._find_volume(PMLR_HOME, "ICML", 2025) == 267
        assert client._find_volume(PMLR_HOME, "COLM", 2025) == 296
        # 2026 卷尚未发布，必须返回 None，不允许猜一个卷号
        assert client._find_volume(PMLR_HOME, "ICML", 2026) is None

    def test_parse_volume_keeps_only_author_span(self, tmp_path):
        client = PmlrClient(make_cache(tmp_path), years=[2025], venues=["ICML"])
        records = client._parse_volume(PMLR_VOLUME, 267, "ICML", 2025)
        assert len(records) == 1
        record = records[0]
        assert record.authors == ["Rogelio A. Mancisidor", "Robert Jenssen", "Shujian Yu"]
        # 卷期信息不能被当成作者
        assert all("PMLR" not in name for name in record.authors)
        assert record.abstract_url == "https://proceedings.mlr.press/v267/a-mancisidor25a.html"


class TestAclAnthology:
    def test_parse_anthology_url(self):
        assert parse_anthology_url("https://aclanthology.org/2026.acl-long.1/") == (2026, "acl-long", 1)
        assert parse_anthology_url("https://aclanthology.org/2026.emnlp-main.42/") == (2026, "emnlp-main", 42)
        assert parse_anthology_url("https://example.org/x") is None

    def test_venue_from_collection(self):
        assert venue_from_collection("acl-long") == "ACL"
        assert venue_from_collection("emnlp-main") == "EMNLP"
        assert venue_from_collection("naacl-short") == "NAACL"
        # Findings 与教程不是主会研究论文
        assert venue_from_collection("findings-acl") == ""
        assert venue_from_collection("acl-tutorials") == ""
        assert venue_from_collection("wildre-1") == ""

    def test_iter_bibtex_entries(self):
        stream = (line.encode() + b"\n" for line in ACL_BIB.splitlines())
        entries = list(iter_bibtex_entries(stream))
        assert [key for key, _ in entries] == [
            "yung-etal-2026-semi",
            "octotools-2026",
            "2026-acl-front",
            "findings-2026",
        ]

    def test_fetch_index_filters_front_matter_and_findings(self, tmp_path):
        client = AclAnthologyClient(
            make_cache(tmp_path), years=[2026], venues=["ACL", "EMNLP", "NAACL"]
        )
        payload = gzip.compress(ACL_BIB.encode())
        client.cache.get_or_fetch = lambda *a, **k: payload  # type: ignore[assignment]
        records = client.fetch_index()
        titles = [record.title for record in records]
        assert titles == ["OctoTools: A Multi-Agent Framework"]
        assert records[0].venue == "ACL 2026"
        assert records[0].authors == ["Lu, Pan", "Chen, Bowen"]
        assert records[0].abstract == "We build an agentic framework."


class TestToPaper:
    def test_conference_papers_are_published_and_have_no_fake_date(self, tmp_path):
        client = IclrVirtualClient(make_cache(tmp_path), years=[2026], venues=["ICLR"])
        records = client._parse_index(ICLR_INDEX, 2026)
        paper = client.to_paper(records[0])
        assert paper.status == PaperStatus.PUBLISHED
        assert paper.source == "conference"
        assert paper.publication_year == 2026
        # 会议论文集只给得出年份，绝不能编造月日
        assert paper.publication_date is None
        assert paper.year == 2026
        assert paper.is_published
        # 会议论文没有 DOI / arXiv ID，用官方论文页地址做稳定标识
        assert paper.dedupe_keys == [f"venue:{paper.venue_url}"]
        assert paper.source_url == paper.venue_url


class TestBibtexHelpers:
    def test_clean_bibtex_value_strips_protective_braces(self):
        assert clean_bibtex_value("{OctoTools}: A {Tamil} Study") == "OctoTools: A Tamil Study"

    def test_split_bibtex_authors(self):
        assert split_bibtex_authors("{A, B and C, D and E F}") == ["A, B", "C, D", "E F"]