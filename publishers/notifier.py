"""生成完成后的微信通知。

为什么需要它：一旦程序放到 GitHub Actions 上定时跑，就没人盯着终端了。
出稿成功要让你知道，失败更要让你知道——否则会静默漏掉一期。

实现走 PushPlus 的微信公众号渠道（免费、个人可用）：
拿到 token 后一次 HTTP POST 即可推到微信。token 只从环境变量读，
不进仓库、不进日志。

一条硬规矩：**推送失败绝不影响出稿本身**。通知是附加品，
不能因为第三方服务抖动就让一期文章白跑。
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

import requests

LOGGER = logging.getLogger(__name__)

ENDPOINT = "https://www.pushplus.plus/send"
TIMEOUT_SECONDS = 15
SUCCESS_CODE = 200


def pushplus_token() -> str:
    return os.getenv("PUSHPLUS_TOKEN", "").strip()


def notify(title: str, content: str, token: Optional[str] = None) -> bool:
    """把一条消息推到微信，返回是否发送成功。

    未配置 token 时返回 False 并记一条 info：这是「没开通知」，
    不是错误，所以不告警。
    """
    resolved = (token if token is not None else pushplus_token()).strip()
    if not resolved:
        LOGGER.info("未配置 PUSHPLUS_TOKEN，跳过微信通知")
        return False

    try:
        response = requests.post(
            ENDPOINT,
            json={
                "token": resolved,
                "title": title,
                "content": content,
                "template": "markdown",
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        LOGGER.warning("微信通知发送失败：%s", exc)
        return False

    if not isinstance(payload, dict) or payload.get("code") != SUCCESS_CODE:
        message = payload.get("msg") if isinstance(payload, dict) else payload
        LOGGER.warning("微信通知发送失败：%s", message)
        return False

    LOGGER.info("微信通知已发送：%s", title)
    return True


def issue_link(slug: str) -> str:
    """本期文章在仓库里的地址。

    优先给 Gitee：github.com 在国内直连不通，手机上点链接要挂代理，
    而 gitee.com 可直连，所以配了 Gitee 就优先用它。
    没配时退回 GitHub（本地跑时两者都为空串，本地文件在手机上点不开）。
    """
    if not slug:
        return ""
    branch = os.getenv("GITHUB_REF_NAME", "").strip() or "main"

    gitee_repo = os.getenv("GITEE_REPO", "").strip().strip("/")
    if gitee_repo:
        return f"https://gitee.com/{gitee_repo}/blob/{branch}/output/{slug}.md"

    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not repo:
        return ""
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    return f"{server}/{repo}/blob/{branch}/output/{slug}.md"


# ----------------------------------------------------------------------
def build_issue_message(
    *,
    volume: int,
    theme: str,
    group_name: str,
    papers: Iterable[tuple[str, str, str]],
    warning_count: int,
    link: str = "",
) -> tuple[str, str]:
    """本期完成的通知，返回 (标题, 正文)。

    papers 里每项是 (来源, 发表状态, 标题)，正文只列标题，
    全文仍在本地 output/ 与仓库里，通知本身不搬运长文。
    """
    items = list(papers)
    lines = [
        f"**{theme}**",
        "",
        f"- 主题组：{group_name}",
        f"- 本期论文：{len(items)} 篇",
        f"- 待核实项：{warning_count} 条" if warning_count else "- 校验：未发现可疑项",
        "",
    ]
    for index, (source, status, title) in enumerate(items, start=1):
        lines.append(f"{index}. [{source}][{status}] {title}")
    if link:
        lines.extend(["", f"[打开全文]({link})"])
    if warning_count:
        lines.extend(["", "发布前请先读校验报告。"])
    return f"AI 论文周报 Vol.{volume:02d} 已生成", "\n".join(lines)


def build_failure_message(*, scope: str, reason: str) -> tuple[str, str]:
    """无人值守运行失败的通知。"""
    content = f"- 阶段：{scope}\n- 原因：{reason}\n\n详见运行日志。"
    return "AI 论文周报运行失败", content