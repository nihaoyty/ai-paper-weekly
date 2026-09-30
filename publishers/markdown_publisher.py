"""Markdown 产物输出。

Phase 1 的交付物就是这个文件：可以直接本地阅读、修改，再手工贴进
公众号后台。它不依赖任何外部服务，是方案 C 的核心。
"""

from __future__ import annotations

import logging
from pathlib import Path

from utils import write_text_atomic

LOGGER = logging.getLogger(__name__)


class MarkdownPublisher:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)

    def save_article(self, slug: str, markdown: str) -> Path:
        path = write_text_atomic(self.output_dir / f"{slug}.md", markdown)
        LOGGER.info("已保存 Markdown：%s", path)
        return path

    def save_report(self, slug: str, report: str) -> Path:
        path = write_text_atomic(self.output_dir / f"{slug}.validation.md", report)
        LOGGER.info("已保存校验报告：%s", path)
        return path