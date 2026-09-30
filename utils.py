"""跨模块共用的小工具。"""

from __future__ import annotations

import os
import re
from pathlib import Path


def write_text_atomic(path: Path, content: str) -> Path:
    """先写临时文件再原子替换，避免中途失败留下半个文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


def safe_slug(text: str, max_length: int = 48) -> str:
    """把任意文字压成适合做文件名的片段（保留中英文与数字）。"""
    cleaned = re.sub(r"[^\w]+", "-", str(text or ""), flags=re.UNICODE).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned[:max_length].strip("-") or "article")