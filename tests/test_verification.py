"""发表状态核验与文章事实校验的单元测试（不联网）。"""

from __future__ import annotations

from models import Paper, PaperAnalysis, PaperStatus
from collectors.crossref_client import CrossrefError
from collectors.publication_verifier import PublicationVerifier
from services.article_validator import REQUIRED_FIELDS, ArticleValidator
from services.paper_reader import build_context

# _apply_crossref 是 main 里的私有步骤，直接拿来测比用假的重跑整条流程更直接
from main import _apply_crossref

NEURIPS = {
    "name": "NeurIPS",
    "aliases": ["Neural Information Processing Systems", "NIPS"],
}


def make_paper(comment: str = "", journal_ref: str = "", doi: str = "") -> Paper:
    return Paper(
        arxiv_id="2509.00001",
        arxiv_version="1",
        title="A Test Paper",
        abstract="We propose something and report 12.5% improvement.",
        published_at="2026-09-01",
        arxiv_url="https://arxiv.org/abs/2509.00001",
        pdf_url="https://arxiv.org/pdf/2509.00001",
        comment=comment,
        journal_ref=journal_ref,
        arxiv_doi=doi,
    )


class TestPublicationVerifier:
    def test_whitelisted_venue_marks_published(self) -> None:
        verifier = PublicationVerifier([NEURIPS])
        paper = verifier.apply(
            make_paper(),
            {
                "venue": "Advances in Neural Information Processing Systems 38",
                "venue_type": "conference",
                "openalex_id": "https://openalex.org/W1",
                "doi": "10.1/x",
                "publication_date": "2026-08-01",
                "cited_by_count": 7,
            },
        )
        assert paper.status == PaperStatus.PUBLISHED
        assert "白名单" in paper.status_evidence
        assert paper.doi == "10.1/x"

    def test_unknown_venue_is_unverified(self) -> None:
        verifier = PublicationVerifier([NEURIPS])
        paper = verifier.apply(
            make_paper(),
            {
                "venue": "Journal of Irreproducible Results",
                "venue_type": "journal",
                "openalex_id": "https://openalex.org/W2",
            },
        )
        assert paper.status == PaperStatus.UNVERIFIED
        assert "不在顶会/顶刊白名单内" in paper.status_evidence

    def test_no_record_and_no_hint_is_preprint(self) -> None:
        verifier = PublicationVerifier([NEURIPS])
        paper = verifier.apply(make_paper(), None)
        assert paper.status == PaperStatus.PREPRINT

    def test_author_comment_only_yields_unverified(self) -> None:
        verifier = PublicationVerifier([NEURIPS])
        paper = verifier.apply(make_paper(comment="Accepted at ICML 2027"), None)
        assert paper.status == PaperStatus.UNVERIFIED
        assert "未经会议/期刊官方渠道核验" in paper.status_evidence

    def test_arxiv_doi_hint_does_not_upgrade_status(self) -> None:
        verifier = PublicationVerifier([NEURIPS])
        paper = verifier.apply(make_paper(doi="10.9999/fake"), None)
        assert paper.status == PaperStatus.UNVERIFIED

    def test_openalex_down_is_reported_honestly(self) -> None:
        verifier = PublicationVerifier([NEURIPS])
        paper = verifier.apply(make_paper(), None, enrichment_available=False)
        assert paper.status == PaperStatus.UNVERIFIED
        assert "OpenAlex 本次不可用" in paper.status_evidence

    def test_shifted_keyword_does_not_match(self) -> None:
        """ACL 不应匹配 NAACL，否则会把非目标会议算成白名单。"""
        verifier = PublicationVerifier([{"name": "ACL"}])
        paper = verifier.apply(
            make_paper(),
            {"venue": "NAACL 2026", "venue_type": "conference"},
        )
        assert paper.status == PaperStatus.UNVERIFIED

    def test_mark_unverified(self) -> None:
        verifier = PublicationVerifier([NEURIPS])
        paper = verifier.mark_unverified(make_paper(), "未执行核验")
        assert paper.status == PaperStatus.UNVERIFIED
        assert paper.status_evidence == "未执行核验"


NMI = {"name": "Nature Machine Intelligence", "match": "exact"}


def make_journal_paper(doi: str = "10.1038/s42256-026-01234-5") -> Paper:
    """期刊通道产出的论文：来自 OpenAlex，状态只有 OpenAlex 作为依据。"""
    paper = Paper(
        title="A Published Paper",
        abstract="We propose something.",
        doi=doi or None,
        venue="Nature Machine Intelligence",
        venue_type="journal",
        publication_date="2026-09-14",
        source="journal",
    )
    paper.status = PaperStatus.PUBLISHED
    paper.status_evidence = "经 OpenAlex 期刊通道检索到"
    return paper


class FakeCrossref:
    """替身：只实现 lookup。"""

    def __init__(self, record=None, error: Exception | None = None) -> None:
        self.record = record
        self.error = error
        self.calls: list[str] = []

    def lookup(self, doi: str):
        self.calls.append(doi)
        if self.error is not None:
            raise self.error
        return self.record


CROSSREF_RECORD = {
    "doi": "10.1038/s42256-026-01234-5",
    "title": "A Published Paper",
    "container_title": "Nature Machine Intelligence",
    "type": "journal-article",
    "publisher": "Springer Science and Business Media LLC",
    "published": "2026-08-01",
    "url": "https://doi.org/10.1038/s42256-026-01234-5",
}


class TestCrossrefVerification:
    """期刊论文必须由 Crossref 复核，不能只凭 OpenAlex 的来源名定案。"""

    def test_registered_doi_with_whitelisted_venue_is_published(self) -> None:
        verifier = PublicationVerifier([NMI])
        paper = _apply_crossref(make_journal_paper(), verifier, FakeCrossref(CROSSREF_RECORD))
        assert paper.status == PaperStatus.PUBLISHED
        assert "Crossref 注册记录" in paper.status_evidence
        assert "白名单" in paper.status_evidence
        # 正式发表日期以出版商注册的为准，不用聚合结果
        assert paper.publication_date == "2026-08-01"
        assert paper.status_source_url == "https://doi.org/10.1038/s42256-026-01234-5"

    def test_missing_registration_is_downgraded(self) -> None:
        verifier = PublicationVerifier([NMI])
        paper = _apply_crossref(make_journal_paper(), verifier, FakeCrossref(None))
        assert paper.status == PaperStatus.UNVERIFIED
        assert "没有 DOI" in paper.status_evidence

    def test_venue_outside_whitelist_is_unverified(self) -> None:
        verifier = PublicationVerifier([NMI])
        record = dict(CROSSREF_RECORD, container_title="Journal of Irreproducible Results")
        paper = _apply_crossref(make_journal_paper(), verifier, FakeCrossref(record))
        assert paper.status == PaperStatus.UNVERIFIED
        assert "不在顶会/顶刊白名单内" in paper.status_evidence

    def test_crossref_outage_keeps_original_conclusion(self) -> None:
        """核验服务挂了不等于这篇论文没发表，不能因为故障改判。"""
        verifier = PublicationVerifier([NMI])
        fake = FakeCrossref(error=CrossrefError("Crossref 持续不可用"))
        paper = _apply_crossref(make_journal_paper(), verifier, fake)
        assert paper.status == PaperStatus.PUBLISHED
        assert paper.status_evidence == "经 OpenAlex 期刊通道检索到"

    def test_paper_without_doi_cannot_be_verified(self) -> None:
        verifier = PublicationVerifier([NMI])
        fake = FakeCrossref(CROSSREF_RECORD)
        paper = _apply_crossref(make_journal_paper(doi=""), verifier, fake)
        assert paper.status == PaperStatus.UNVERIFIED
        assert fake.calls == []


def complete_content() -> dict[str, str]:
    return {key: f"{label}的内容" for key, label in REQUIRED_FIELDS}


class TestArticleValidator:
    def setup_method(self) -> None:
        self.validator = ArticleValidator()
        self.paper = make_paper()
        self.context = build_context(self.paper)

    def _analysis(self, **overrides) -> PaperAnalysis:
        content = complete_content()
        content.update(overrides)
        return PaperAnalysis(paper=self.paper, content=content)

    def test_complete_content_passes(self) -> None:
        result = self.validator.validate_analysis(self._analysis(), self.context)
        assert result.ok
        assert result.fatal == []

    def test_missing_field_is_fatal(self) -> None:
        analysis = self._analysis()
        del analysis.content["method"]
        result = self.validator.validate_analysis(analysis, self.context)
        assert not result.ok
        assert any("技术原理" in item for item in result.fatal)

    def test_invented_url_is_dropped(self) -> None:
        analysis = self._analysis(repo_url="https://github.com/nobody/invented")
        result = self.validator.validate_analysis(analysis, self.context)
        assert not result.ok
        assert analysis.content["repo_url"] is None
        assert any("无法追溯" in item for item in result.fatal)

    def test_url_from_source_is_kept(self) -> None:
        paper = make_paper()
        paper.abstract += " Code: https://github.com/real/repo"
        context = build_context(paper)
        analysis = PaperAnalysis(
            paper=paper,
            content={**complete_content(), "repo_url": "https://github.com/real/repo"},
        )
        result = self.validator.validate_analysis(analysis, context)
        assert result.ok
        assert analysis.content["repo_url"] == "https://github.com/real/repo"

    def test_number_not_in_source_is_flagged(self) -> None:
        analysis = self._analysis(method="实验显示性能提升 42.5%")
        result = self.validator.validate_analysis(analysis, self.context)
        assert any("42.5" in item for item in result.warnings)

    def test_number_from_source_is_not_flagged(self) -> None:
        analysis = self._analysis(method="实验显示性能提升 12.5%")
        result = self.validator.validate_analysis(analysis, self.context)
        assert not any("12.5" in item for item in result.warnings)

    def test_year_followed_by_capital_word_is_not_a_number(self) -> None:
        """「2025 MAS-2025」里的 M 不是「百万」单位，不应被当成数字提取。"""
        analysis = self._analysis(
            limitations="作者备注称被 ICML 2025 MAS-2025 研讨会接收"
        )
        result = self.validator.validate_analysis(analysis, self.context)
        assert not any("2025" in item for item in result.warnings)

    def test_latex_times_is_normalized_before_comparison(self) -> None:
        """arXiv 摘要里的 5.5$\\times$ 与输出里的 5.5× 是同一个数据。"""
        paper = make_paper()
        paper.abstract = "DRT achieves 5.5$\\times$ token efficiency improvement."
        context = build_context(paper)
        analysis = PaperAnalysis(paper=paper, content=complete_content())
        analysis.content["experiments"] = "相比基线取得 5.5× 的 token 效率提升"
        result = self.validator.validate_analysis(analysis, context)
        assert not any("5.5" in item for item in result.warnings)

    def test_localized_unit_is_not_a_mismatch(self) -> None:
        """把 5.5x 写成中文的 5.5 倍属于正常表述，不是编造数据。"""
        paper = make_paper()
        paper.abstract = "DRT achieves 5.5$\\times$ token efficiency improvement."
        context = build_context(paper)
        analysis = PaperAnalysis(paper=paper, content=complete_content())
        analysis.content["experiments"] = "相比基线取得 5.5 倍的 token 效率提升"
        result = self.validator.validate_analysis(analysis, context)
        assert not any("5.5" in item for item in result.warnings)

    def test_genuinely_wrong_number_is_still_flagged(self) -> None:
        """抹平排版差异后，真正编造的数字仍必须被拦下。"""
        paper = make_paper()
        paper.abstract = "DRT achieves 5.5$\\times$ token efficiency improvement."
        context = build_context(paper)
        analysis = PaperAnalysis(paper=paper, content=complete_content())
        analysis.content["experiments"] = "相比基线取得 8.9 倍的 token 效率提升"
        result = self.validator.validate_analysis(analysis, context)
        assert any("8.9" in item for item in result.warnings)

    def test_latex_escaped_percent_is_normalized(self) -> None:
        """arXiv 摘要常把百分号写成 86.3\\%，与 86.3% 是同一个数据。"""
        paper = make_paper()
        paper.abstract = "final success rises from 86.3\\% to 98.7\\%."
        context = build_context(paper)
        analysis = PaperAnalysis(paper=paper, content=complete_content())
        analysis.content["experiments"] = "最终成功率从 86.3% 提升到 98.7%"
        result = self.validator.validate_analysis(analysis, context)
        assert not any("86.3" in item or "98.7" in item for item in result.warnings)

    def test_latex_escaped_percent_plus_times_together(self) -> None:
        """真实案例：摘要里同时有 8.0$\\times$ 和 95.5\\%。"""
        paper = make_paper()
        paper.abstract = (
            "This represents an 8.0$\\times$ end-to-end speedup "
            "and a 95.5\\% reduction in API usage."
        )
        context = build_context(paper)
        analysis = PaperAnalysis(paper=paper, content=complete_content())
        analysis.content["experiments"] = "端到端提速 8.0 倍，API 用量下降 95.5%"
        result = self.validator.validate_analysis(analysis, context)
        assert not any("8.0" in item or "95.5" in item for item in result.warnings)

    def test_negated_acceptance_claim_is_not_flagged(self) -> None:
        """真实案例：模型写「不能视为已被录用」，是澄清而非断言，不应报警。"""
        self.paper.status = PaperStatus.UNVERIFIED
        analysis = self._analysis(
            limitations="arXiv 页面备注只有作者提供的网站链接，"
            "未经会议或期刊官方渠道核验，因此不能视为已被录用或正式发表。"
        )
        result = self.validator.validate_analysis(analysis, self.context)
        assert not any("录用/发表类表述" in item for item in result.warnings)

    def test_unnegated_acceptance_claim_still_flagged(self) -> None:
        """去掉否定后，真实断言仍必须被拦下。"""
        self.paper.status = PaperStatus.UNVERIFIED
        analysis = self._analysis(innovation="该方法已被 ICML 2027 录用")
        result = self.validator.validate_analysis(analysis, self.context)
        assert any("录用/发表类表述" in item for item in result.warnings)

    def test_negated_top_venue_claim_is_not_flagged(self) -> None:
        self.paper.status = PaperStatus.PREPRINT
        analysis = self._analysis(
            limitations="该论文目前是预印本，不能当作顶会论文引用。"
        )
        result = self.validator.validate_analysis(analysis, self.context)
        assert not any("顶会/顶刊" in item for item in result.warnings)

    def test_preprint_claiming_acceptance_is_flagged(self) -> None:
        self.paper.status = PaperStatus.PREPRINT
        analysis = self._analysis(innovation="该方法已被 NeurIPS 录用")
        result = self.validator.validate_analysis(analysis, self.context)
        assert any("录用/发表类表述" in item for item in result.warnings)

    def test_preprint_claiming_top_venue_is_flagged(self) -> None:
        self.paper.status = PaperStatus.UNVERIFIED
        analysis = self._analysis(background="这是一篇顶会论文")
        result = self.validator.validate_analysis(analysis, self.context)
        assert any("顶会/顶刊" in item for item in result.warnings)

    def test_article_level_url_check(self) -> None:
        markdown = (
            "# 标题\n\n"
            f"{self.paper.arxiv_url}\n\n"
            f"{self.paper.status}\n\n"
            "https://evil.example.com/made-up\n"
        )
        issues = self.validator.validate_article(markdown, [self.context])
        assert any("evil.example.com" in item for item in issues)


class TestValidationReport:
    def test_conference_paper_has_no_empty_arxiv_line(self) -> None:
        """会议论文没有 arXiv 地址，报告里不能留一行空的「arXiv：」。"""
        conference = Paper(
            title="OrchestrationBench",
            venue="ACL 2026",
            venue_url="https://aclanthology.org/2026.acl-long.1/",
            source="conference",
        )
        report = ArticleValidator.render_report(
            "AI 论文周报 Vol.12",
            "2026-09-28 23:22",
            [(build_context(conference), ["[提醒] 示例问题"])],
            [],
        )
        assert "arXiv：" not in report
        assert "- 来源：会议" in report
        assert "https://aclanthology.org/2026.acl-long.1/" in report

    def test_arxiv_paper_still_shows_its_source(self) -> None:
        paper = make_paper()
        report = ArticleValidator.render_report(
            "AI 论文周报 Vol.12",
            "2026-09-28 23:22",
            [(build_context(paper), ["[提醒] 示例问题"])],
            [],
        )
        assert "- 来源：arXiv" in report
        assert paper.arxiv_url in report