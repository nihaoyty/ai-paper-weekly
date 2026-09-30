"""配图抓取的单元测试（不联网）。

重点守三条底线：
* 图注必须逐字来自原文，不能被改写；
* 只认 Figure 1，取不到就跳过，不拿别的图顶替；
* 图片要转白底 RGB 并压到体积上限内（公众号正文单图上限约 1MB）。
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from collectors.arxiv_figures import (
    FigureFetcher,
    _caption_text,
)
from models import Figure, Paper


def noisy_png(width: int, height: int, mode: str = "RGB") -> bytes:
    """生成一张不易压缩的图，用来测体积上限。"""
    channels = len(mode)
    image = Image.frombytes(mode, (width, height), os.urandom(width * height * channels))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def figure_html(fid: str, caption: str, src: str = "2601.00001v1/fig1.png") -> str:
    """照 arXiv HTML 的结构拼一个 figure 块：fid 形如 S0.F1。"""
    number = fid.split(".F")[-1]
    return (
        f'<figure id="{fid}"><img src="{src}" id="{fid}.g1">'
        f'<figcaption class="ltx_caption"><span id="{fid}.4">Figure {number}</span>: '
        f"{caption}</figcaption></figure>"
    )


class TestCaptionText:
    def test_tags_are_stripped(self) -> None:
        body = (
            '<figcaption><span id="a">Figure 1</span>: '
            '<span class="b">PointWorld</span> is a model.</figcaption>'
        )
        assert _caption_text(body) == "Figure 1: PointWorld is a model."

    def test_missing_caption_is_empty(self) -> None:
        assert _caption_text("<img src='x.png'>") == ""


class TestFigureOneDetection:
    def _fetcher(self) -> FigureFetcher:
        return FigureFetcher({"enabled": True})

    def _html(self, html: str, fetcher: FigureFetcher):
        fetcher._get_html = lambda url: (html, "https://arxiv.org/html/2601.00001v1/")
        return fetcher._find_figure1("2601.00001")

    def test_finds_figure_one(self) -> None:
        page, image, caption = self._html(
            figure_html("S0.F1", "Overview of our method."), self._fetcher()
        )
        assert image.endswith("2601.00001v1/fig1.png")
        assert caption == "Figure 1: Overview of our method."
        assert page.endswith("/2601.00001v1/")

    def test_figure_without_image_is_skipped(self) -> None:
        """有些论文的 Figure 1 在 HTML 版里没有图片（例如是视频或表格）。"""
        html = (
            '<figure id="S0.F1"><figcaption>Figure 1: A video teaser.</figcaption></figure>'
            + figure_html("S1.F2", "Second figure.")
        )
        _, image, caption = self._html(html, self._fetcher())
        assert image == "" and caption == ""

    def test_figure_ten_is_not_mistaken_for_figure_one(self) -> None:
        html = figure_html("S5.F10", "Scaling study.", src="f10.png")
        _, image, _ = self._html(html, self._fetcher())
        assert image == ""

    def test_returns_nothing_when_no_figure_one(self) -> None:
        html = figure_html("S1.F2", "Second figure.")
        assert self._html(html, self._fetcher()) == ("", "", "")


class TestShrink:
    def test_transparent_image_becomes_white_rgb(self) -> None:
        """带透明通道的图直接贴到公众号会变黑底，必须先合到白底。"""
        image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        out = FigureFetcher({"enabled": True})._shrink(buffer.getvalue())

        result = Image.open(io.BytesIO(out))
        assert result.mode == "RGB"
        assert result.getpixel((20, 20)) == (255, 255, 255)

    def test_oversized_image_is_shrunk_under_the_limit(self) -> None:
        fetcher = FigureFetcher({"enabled": True, "max_width": 400, "max_bytes": 200_000})
        out = fetcher._shrink(noisy_png(2000, 1500))
        assert 0 < len(out) <= 200_000
        result = Image.open(io.BytesIO(out))
        assert result.width <= 400
        assert result.mode == "RGB"

    def test_small_image_is_kept_readable(self) -> None:
        fetcher = FigureFetcher({"enabled": True, "max_width": 1200, "max_bytes": 900_000})
        out = fetcher._shrink(noisy_png(300, 200))
        assert Image.open(io.BytesIO(out)).size == (300, 200)

    def test_broken_bytes_are_rejected(self) -> None:
        assert FigureFetcher({"enabled": True})._shrink(b"not an image") == b""


class TestFetch:
    def _fetcher_with(self, html: str, image: bytes) -> FigureFetcher:
        fetcher = FigureFetcher({"enabled": True, "max_width": 800, "max_bytes": 500_000})
        fetcher._get_html = lambda url: (html, "https://arxiv.org/html/2601.00001v1/")
        fetcher._download = lambda url: image
        fetcher._lookup_arxiv_id = lambda title: "2601.00001"
        return fetcher

    def test_saves_file_and_returns_relative_path(self, tmp_path) -> None:
        fetcher = self._fetcher_with(
            figure_html("S0.F1", "Overview of our method."), noisy_png(600, 400)
        )
        figure = fetcher.fetch(
            Paper(title="A Paper"),
            dest_dir=tmp_path / "2026-10-02-vol02-g_fri" / "figures",
            name="01",
            rel_prefix="2026-10-02-vol02-g_fri/figures",
        )
        assert isinstance(figure, Figure)
        assert figure.rel_path == "2026-10-02-vol02-g_fri/figures/01.png"
        assert (tmp_path / figure.rel_path).exists()
        assert fetcher.fetched == 1

    def test_caption_is_verbatim(self, tmp_path) -> None:
        caption = "Figure 1: We propose RawVLA, an embodied and adaptive neural ISP module."
        fetcher = self._fetcher_with(
            figure_html("S0.F1", caption[len("Figure 1: "):]), noisy_png(300, 200)
        )
        figure = fetcher.fetch(
            Paper(title="A Paper"), dest_dir=tmp_path, name="01", rel_prefix="x"
        )
        assert figure is not None
        assert figure.caption == caption

    def test_disabled_fetcher_does_nothing(self, tmp_path) -> None:
        fetcher = FigureFetcher({"enabled": False})
        assert fetcher.fetch(
            Paper(title="A Paper"), dest_dir=tmp_path, name="01", rel_prefix="x"
        ) is None

    def test_paper_without_arxiv_id_is_skipped(self, tmp_path) -> None:
        fetcher = FigureFetcher({"enabled": True})
        fetcher._lookup_arxiv_id = lambda title: ""
        assert fetcher.fetch(
            Paper(title="Not On arXiv"), dest_dir=tmp_path, name="01", rel_prefix="x"
        ) is None
        assert fetcher.skipped == 1

    def test_download_failure_is_not_fatal(self, tmp_path) -> None:
        fetcher = self._fetcher_with(figure_html("S0.F1", "Overview."), b"")
        assert fetcher.fetch(
            Paper(title="A Paper"), dest_dir=tmp_path, name="01", rel_prefix="x"
        ) is None

    def test_uses_the_papers_own_arxiv_id_without_lookup(self, tmp_path, monkeypatch) -> None:
        called = []
        fetcher = self._fetcher_with(figure_html("S0.F1", "Overview."), noisy_png(300, 200))
        fetcher._lookup_arxiv_id = lambda title: called.append(title) or ""

        figure = fetcher.fetch(
            Paper(title="RawVLA", arxiv_id="2609.37530"),
            dest_dir=tmp_path,
            name="01",
            rel_prefix="x",
        )
        assert figure is not None
        assert called == []