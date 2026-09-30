"""微信公众号草稿箱客户端。

只做「建草稿」，不做发布与群发：个人订阅号没有这两个权限
（freepublish / message/mass 的适用范围是「仅认证」），
能自动化的上限就是把文章放进草稿箱，再由人工点群发。

两个前提都是微信侧的限制，程序绕不过去：
1. 调用来源 IP 必须在公众号后台的 IP 白名单里，否则返回 40164；
   所以 GitHub Actions 的动态出口 IP 无法直接调用（见 README）；
2. 图文消息必须带封面，封面得是「永久素材」的 media_id，不能用外部图片地址。

正文里也不要用外部图片：微信会过滤非本平台的外链图片。
本项目的文章模板不含任何图片，这一点天然满足。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import requests

LOGGER = logging.getLogger(__name__)

API_ROOT = "https://api.weixin.qq.com/cgi-bin"
TOKEN_ENDPOINT = f"{API_ROOT}/token"
MATERIAL_ENDPOINT = f"{API_ROOT}/material/add_material"
DRAFT_ENDPOINT = f"{API_ROOT}/draft/add"

TIMEOUT_SECONDS = 30

# 微信侧的字段上限，超了会被直接拒绝，所以在本地就拦住
MAX_TITLE_CHARS = 32
MAX_AUTHOR_CHARS = 16
MAX_DIGEST_CHARS = 120
MAX_CONTENT_CHARS = 20000

# 只解释真正会踩到的几个错误码，其余按原样抛给调用方
ERROR_HINTS = {
    40001: "access_token 失效，多为 AppSecret 被重置或冻结",
    40164: (
        "调用来源 IP 不在公众号的 IP 白名单里。到「微信开发者平台 → 我的业务 → "
        "公众号 → 开发信息」把出口 IP 加进去（支持 172.0.0.1/24 这种写法）"
    ),
    40007: "thumb_media_id 无效，封面图必须走 material/add_material 上传成永久素材",
    44003: "正文为空或缺少段落标签，content 里至少要有一个 <p> 段落",
    47001: "请求体格式错误，检查 JSON 是否合法",
}


class WeChatError(RuntimeError):
    """公众号接口调用失败。"""


class WeChatDraftClient:
    def __init__(
        self,
        appid: str,
        secret: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.appid = appid.strip()
        self.secret = secret.strip()
        self._session = session or requests.Session()
        self._token = ""

    # ------------------------------------------------------------------
    def access_token(self) -> str:
        """取接口调用凭据。同一实例内缓存，避免短期内重复获取把旧 token 顶掉。"""
        if self._token:
            return self._token
        payload = self._request(
            "GET",
            TOKEN_ENDPOINT,
            params={
                "grant_type": "client_credential",
                "appid": self.appid,
                "secret": self.secret,
            },
            action="获取 access_token",
        )
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise WeChatError(f"获取 access_token 失败：{_describe(payload)}")
        self._token = token
        return token

    def upload_cover(self, image_path: Path) -> str:
        """上传封面图，返回永久素材 media_id。"""
        path = Path(image_path)
        if not path.exists():
            raise WeChatError(f"封面图不存在：{path}")
        token = self.access_token()
        try:
            with path.open("rb") as handle:
                response = self._session.post(
                    MATERIAL_ENDPOINT,
                    params={"access_token": token, "type": "image"},
                    files={"media": (path.name, handle)},
                    timeout=TIMEOUT_SECONDS,
                )
        except requests.RequestException as exc:
            raise WeChatError(f"封面图上传失败：{exc}") from exc

        payload = _parse(response, "封面图上传")
        media_id = str(payload.get("media_id") or "").strip()
        if not media_id:
            raise WeChatError(f"封面图上传未返回 media_id：{_describe(payload)}")
        return media_id

    def create_draft(
        self,
        *,
        title: str,
        content: str,
        thumb_media_id: str,
        author: str = "",
        digest: str = "",
        source_url: str = "",
    ) -> str:
        """新增图文草稿，返回草稿的 media_id。"""
        _check_limits(title=title, author=author, digest=digest, content=content)
        article: dict[str, Any] = {
            "title": title,
            "author": author,
            "digest": digest,
            "content": content,
            "content_source_url": source_url,
            "thumb_media_id": thumb_media_id,
            # 评论开关交给人工在后台决定，程序不替你做主
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }
        payload = {"articles": [article]}
        result = self._request(
            "POST",
            DRAFT_ENDPOINT,
            params={"access_token": self.access_token()},
            json=payload,
            action="新增草稿",
        )
        media_id = str(result.get("media_id") or "").strip()
        if not media_id:
            raise WeChatError(f"新增草稿未返回 media_id：{_describe(result)}")
        return media_id

    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        action: str,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        try:
            response = self._session.request(
                method, url, params=params, json=json, timeout=TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise WeChatError(f"{action}请求失败：{exc}") from exc
        return _parse(response, action)


# ----------------------------------------------------------------------
def draft_client_from_env() -> Optional[WeChatDraftClient]:
    """按环境变量创建客户端。没配置时返回 None，调用方据此跳过。"""
    appid = os.getenv("WECHAT_APPID", "").strip()
    secret = os.getenv("WECHAT_APPSECRET", "").strip()
    if not appid or not secret:
        LOGGER.info("未配置 WECHAT_APPID / WECHAT_APPSECRET，跳过创建公众号草稿")
        return None
    return WeChatDraftClient(appid, secret)


def cover_image_from_env() -> Optional[Path]:
    raw = os.getenv("WECHAT_COVER_IMAGE", "").strip()
    return Path(raw).expanduser() if raw else None


def _check_limits(*, title: str, author: str, digest: str, content: str) -> None:
    """在本地先把超限拦住，比等服务端拒绝再排查快得多。"""
    if not title.strip():
        raise WeChatError("标题不能为空")
    for value, limit, label in (
        (title, MAX_TITLE_CHARS, "标题"),
        (author, MAX_AUTHOR_CHARS, "作者"),
        (digest, MAX_DIGEST_CHARS, "摘要"),
    ):
        if len(value) > limit:
            raise WeChatError(f"{label} {len(value)} 字，超过微信上限 {limit} 字：{value[:20]}…")
    if len(content) > MAX_CONTENT_CHARS:
        raise WeChatError(
            f"正文 {len(content)} 字符，超过微信上限 {MAX_CONTENT_CHARS} 字符，"
            "请调小 config/topics.yaml 中 article 段的字数预算"
        )


def _parse(response: requests.Response, action: str) -> dict[str, Any]:
    """微信即使出错也返回 HTTP 200，必须看 errcode 才算真的成功。"""
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise WeChatError(f"{action}失败：{exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise WeChatError(f"{action}返回的不是 JSON：{response.text[:200]}") from exc
    if not isinstance(payload, dict):
        raise WeChatError(f"{action}返回结构异常：{payload}")

    errcode = payload.get("errcode")
    if errcode not in (None, 0):
        hint = ERROR_HINTS.get(errcode, "")
        raise WeChatError(
            f"{action}失败：微信返回 {errcode} {payload.get('errmsg') or ''}"
            + (f"。{hint}" if hint else "")
        )
    return payload


def _describe(payload: dict[str, Any]) -> str:
    return str({k: v for k, v in payload.items() if k != "access_token"})