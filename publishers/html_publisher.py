"""HTML 产物输出。

面向微信公众号编辑器做了取舍：
* 所有样式内联，不依赖 <style> / class —— 公众号会清掉外部样式；
* 不使用 JavaScript，不使用公众号不支持的标签；
* 段落间距、行高按手机阅读设置，方便直接全选复制到后台。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import markdown as markdown_lib
from jinja2 import Environment, FileSystemLoader

from utils import write_text_atomic

LOGGER = logging.getLogger(__name__)

MARKDOWN_EXTENSIONS = ["extra", "sane_lists"]

# 公众号兼容的内联样式表
STYLE_MAP: dict[str, str] = {
    "h1": "font-size:21px;font-weight:bold;color:#1a1a1a;margin:0 0 18px;line-height:1.5;text-align:center;",
    "h2": "font-size:19px;font-weight:bold;color:#1a1a1a;margin:30px 0 12px;padding-left:10px;border-left:4px solid #07c160;line-height:1.6;",
    "h3": "font-size:17px;font-weight:bold;color:#333333;margin:20px 0 8px;line-height:1.6;",
    "p": "font-size:16px;line-height:1.8;color:#3f3f3f;margin:0 0 14px;letter-spacing:0.4px;",
    "ul": "margin:0 0 14px;padding-left:22px;",
    "ol": "margin:0 0 14px;padding-left:22px;",
    "li": "font-size:16px;line-height:1.8;color:#3f3f3f;margin:0 0 6px;",
    "blockquote": "margin:0 0 18px;padding:10px 14px;border-left:3px solid #d9d9d9;background-color:#f7f7f7;color:#666666;font-size:15px;line-height:1.7;",
    "a": "color:#576b95;text-decoration:none;word-break:break-all;",
    "strong": "font-weight:bold;color:#1a1a1a;",
    "em": "font-style:italic;color:#555555;",
    "code": "background-color:#f2f2f2;padding:1px 5px;border-radius:3px;font-size:14px;color:#c7254e;font-family:Menlo,Consolas,monospace;",
    "pre": "background-color:#2d2d2d;color:#f8f8f2;padding:12px;border-radius:4px;overflow-x:auto;font-size:13px;line-height:1.6;",
    "hr": "border:none;border-top:1px solid #e5e5e5;margin:24px 0;",
    "table": "border-collapse:collapse;width:100%;margin:0 0 16px;font-size:15px;",
    "th": "border:1px solid #e5e5e5;padding:6px 8px;background-color:#fafafa;text-align:left;",
    "td": "border:1px solid #e5e5e5;padding:6px 8px;",
}


class HtmlPublisher:
    def __init__(self, output_dir: Path, template_path: Path) -> None:
        self.output_dir = Path(output_dir)
        template_path = Path(template_path)
        self._env = Environment(
            loader=FileSystemLoader(str(template_path.parent)),
            autoescape=False,
        )
        self._template = self._env.get_template(template_path.name)

    def render(self, markdown_text: str, title: str, generated_at: str) -> str:
        body = markdown_lib.markdown(markdown_text, extensions=MARKDOWN_EXTENSIONS)
        return self._template.render(
            title=title, generated_at=generated_at, content=_inline_styles(body)
        )

    def render_body(self, markdown_text: str) -> str:
        """只要正文片段，不含 <html> / <head>。

        公众号草稿箱的 content 要的是正文片段，整份文档会被拒；
        外层那层灰底白卡是网页阅读用的，公众号自己会排版，不需要。
        """
        body = markdown_lib.markdown(markdown_text, extensions=MARKDOWN_EXTENSIONS)
        return _inline_styles(body)

    def save_article(
        self, slug: str, markdown_text: str, title: str, generated_at: str
    ) -> Path:
        html = self.render(markdown_text, title, generated_at)
        path = write_text_atomic(self.output_dir / f"{slug}.html", html)
        LOGGER.info("已保存 HTML：%s", path)
        return path


def _inline_styles(html: str) -> str:
    """给 Markdown 生成的标签补上内联样式。"""
    for tag, style in STYLE_MAP.items():
        pattern = re.compile(rf"<{tag}(\s[^>]*)?>", re.IGNORECASE)
        html = pattern.sub(
            lambda match, t=tag, s=style: f'<{t}{match.group(1) or ""} style="{s}">',
            html,
        )
    return html