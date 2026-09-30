"""从 arXiv HTML 版抓取论文配图（每篇只取 Figure 1）。

为什么不直接从会议论文集抓：会议页面只给 PDF，抽图要么装一整条 PDF 工具链，
要么抽出一堆碎图。而 arXiv 的 HTML 版把插图拆成了独立 PNG，还带
<figcaption> 原文图注。所以会议论文也要先按标题反查 arXiv 版本再取图。

三条底线：
1. **图注逐字引用原文**，不改写也不翻译——大模型看不懂图，描述图就是编造；
2. **只取 Figure 1**，取不到就跳过，不拿别的图顶替，否则「每篇取 Figure 1」
   这件事就不可预期了；
3. **图片必须真正落盘且非空**，才允许被写进文章。
"""

from __future__ import annotations

import io
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import feedparser

from models import Figure, Paper

try:  # Pillow 只在压缩图片时用得到，缺了也不该让整期跑不起来
    from PIL import Image
except ImportError:  # pragma: no cover - 取决于运行环境
    Image = None

LOGGER = logging.getLogger(__name__)

# arXiv 会对没有正常 UA / Accept 的请求返回 406，必须装成浏览器
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HTML_HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
IMAGE_HEADERS = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8"}

HTML_URL = "https://arxiv.org/html/{arxiv_id}"
API_URL = "https://export.arxiv.org/api/query"
# arXiv 要求请求间隔 ≥3 秒，标题反查是逐篇串行的，这里自己节流
MIN_API_INTERVAL_SECONDS = 3.0

FIGURE_BLOCK_RE = re.compile(r"<figure[^>]*>(?P<body>.*?)</figure>", re.S | re.I)
IMG_SRC_RE = re.compile(r'<img[^>]+src="(?P<src>[^"]+)"', re.I)
CAPTION_RE = re.compile(r"<figcaption[^>]*>(?P<text>.*?)</figcaption>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
# 只认「Figure 1」，且后面不能紧跟数字（否则 Figure 10 也会命中）
FIGURE1_RE = re.compile(r"^figure\s*1(?!\d)", re.I)

# 缩到多少还超标就再缩一档，最多试这么多次
SHRINK_STEPS = 5
# 再缩也不能窄于这个宽度，否则图里的字就看不清了
MIN_WIDTH = 320


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _caption_text(body: str) -> str:
    match = CAPTION_RE.search(body)
    if not match:
        return ""
    return " ".join(TAG_RE.sub("", match.group("text")).split())


def _to_rgb(image):
    """转成白底 RGB。

    arXiv 里不少插图带透明通道（RGBA），直接贴到公众号很容易变成黑底，
    所以先合到白底上再输出。
    """
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


class FigureFetcher:
    """按配置抓取配图。未启用时 fetch() 直接返回 None。"""

    def __init__(
        self,
        config: Optional[dict[str, Any]] = None,
        *,
        timeout: int = 30,
        mailto: str = "",
    ) -> None:
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.max_width = int(cfg.get("max_width", 1200))
        self.max_bytes = int(cfg.get("max_bytes", 900_000))
        self.timeout = timeout
        self.mailto = (mailto or "").strip()
        self._last_api_call = 0.0
        # 本期成功取到几张、失败几次，跑完写进日志，方便判断配图覆盖率
        self.fetched = 0
        self.skipped = 0

    # ------------------------------------------------------------------
    def fetch(
        self,
        paper: Paper,
        *,
        dest_dir: Path,
        name: str,
        rel_prefix: str,
    ) -> Optional[Figure]:
        """抓取 paper 的 Figure 1。取不到就返回 None，绝不抛异常。"""
        if not self.enabled:
            return None

        arxiv_id = paper.arxiv_id or self._lookup_arxiv_id(paper.title)
        if not arxiv_id:
            LOGGER.info("配图跳过（arXiv 上找不到这篇论文）：%s", paper.display_title[:50])
            self.skipped += 1
            return None

        page_url, image_url, caption = self._find_figure1(arxiv_id)
        if not image_url:
            self.skipped += 1
            return None

        blob = self._download(image_url)
        if not blob:
            self.skipped += 1
            return None
        shrunk = self._shrink(blob)
        if not shrunk:
            self.skipped += 1
            return None

        path = Path(dest_dir) / f"{name}.png"
        if not self._save(shrunk, path):
            self.skipped += 1
            return None

        self.fetched += 1
        LOGGER.info(
            "配图已就绪：%s（%d KB，Figure 1）",
            path.name,
            len(shrunk) // 1024,
        )
        return Figure(
            rel_path=f"{rel_prefix.strip('/')}/{name}.png",
            caption=caption,
            page_url=page_url,
            arxiv_id=arxiv_id,
        )

    # ------------------------------------------------------------------
    def _lookup_arxiv_id(self, title: str) -> str:
        """按标题精确反查 arXiv ID。只认归一化后完全相同的标题。"""
        if not title.strip():
            return ""
        self._throttle()
        query = urllib.parse.urlencode(
            {"search_query": f'ti:"{title}"', "max_results": 5}
        )
        try:
            feed = feedparser.parse(f"{API_URL}?{query}")
        except Exception as exc:  # noqa: BLE001 反查失败只是没图，不该中断整期
            LOGGER.warning("arXiv 标题反查失败：%s", exc)
            return ""
        wanted = _normalize_title(title)
        for entry in feed.entries:
            if _normalize_title(entry.get("title", "")) == wanted:
                return str(entry.get("id", "")).rsplit("/abs/", 1)[-1]
        return ""

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_api_call
        if elapsed < MIN_API_INTERVAL_SECONDS:
            time.sleep(MIN_API_INTERVAL_SECONDS - elapsed)
        self._last_api_call = time.time()

    def _find_figure1(self, arxiv_id: str) -> tuple[str, str, str]:
        """返回 (页面地址, 图片地址, 原文图注)。找不到时后两项为空串。"""
        for candidate in (HTML_URL.format(arxiv_id=arxiv_id),
                          HTML_URL.format(arxiv_id=f"{arxiv_id}v1")):
            html, final_url = self._get_html(candidate)
            if not html:
                continue
            for match in FIGURE_BLOCK_RE.finditer(html):
                body = match.group("body")
                caption = _caption_text(body)
                if not FIGURE1_RE.match(caption):
                    continue
                img = IMG_SRC_RE.search(body)
                if not img:
                    # 有些论文的 Figure 1 在 HTML 版里没有图片（例如是视频或表格）
                    LOGGER.info(
                        "Figure 1 在 arXiv HTML 版中没有可下载的图片：%s", arxiv_id
                    )
                    return final_url, "", ""
                # 必须用重定向后的地址当基准，否则图片路径会拼重复
                return (
                    final_url,
                    urllib.parse.urljoin(final_url, img.group("src")),
                    caption,
                )
        return "", "", ""

    def _get_html(self, url: str) -> tuple[str, str]:
        try:
            request = urllib.request.Request(url, headers=HTML_HEADERS)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "ignore"), response.geturl()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOGGER.debug("arXiv HTML 版不可用 %s：%s", url, exc)
            return "", ""

    def _download(self, url: str) -> bytes:
        try:
            request = urllib.request.Request(url, headers=IMAGE_HEADERS)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOGGER.warning("配图下载失败 %s：%s", url, exc)
            return b""

    def _shrink(self, blob: bytes) -> bytes:
        """转白底 RGB、缩到宽度上限，再压到体积上限以内。

        公众号正文单图上限约 1MB，arXiv 原图经常有 2MB 以上，不压会被拒。
        """
        if Image is None:
            LOGGER.warning(
                "未安装 Pillow，无法压缩配图，本期不配图。"
                "请执行 pip install -r requirements.txt"
            )
            return b""
        try:
            with Image.open(io.BytesIO(blob)) as raw:
                image = _to_rgb(raw)
        except Exception as exc:  # noqa: BLE001 个别图片格式怪异，跳过即可
            LOGGER.warning("配图无法解析：%s", exc)
            return b""

        width = min(image.width, self.max_width)
        for _ in range(SHRINK_STEPS):
            height = max(1, round(image.height * width / image.width))
            scaled = image.resize((width, height), Image.LANCZOS)
            buffer = io.BytesIO()
            scaled.save(buffer, format="PNG", optimize=True)
            data = buffer.getvalue()
            if len(data) <= self.max_bytes:
                return data
            if width <= MIN_WIDTH:
                break
            width = max(MIN_WIDTH, int(width * 0.75))

        # 缩到底仍超标就不配这张图：文章里的图必须一定在体积上限内，
        # 否则公众号会拒收或者压糊，不如不放。
        LOGGER.warning(
            "配图压缩后仍超过 %d KB，本期不放这张图", self.max_bytes // 1024
        )
        return b""

    def _save(self, data: bytes, path: Path) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            LOGGER.warning("配图写入失败 %s：%s", path, exc)
            return False
        # 落盘后回读校验：写坏了宁可不配图，也不能在文章里留个坏引用
        return path.exists() and path.stat().st_size > 0