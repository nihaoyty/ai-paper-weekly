"""微信通知与公众号草稿箱的单元测试（不联网）。

两个模块都涉及第三方接口，这里全部用假 session / 假响应驱动，
重点验证：
* 通知失败绝不能影响出稿（一律返回 False，不抛异常）；
* 微信「HTTP 200 但 errcode 非 0」也算失败，必须识别出来；
* 超限字段在本地就被拦住，不必等服务端拒绝。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import requests

from publishers import notifier
from publishers.notifier import build_failure_message, build_issue_message, issue_link, notify
from publishers.wechat_mp import WeChatDraftClient, WeChatError


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """记录每次调用，按预置队列返回响应。"""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def _next(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, **kwargs):
        kwargs["url"] = url
        return self._next(**kwargs)

    def request(self, method, url, **kwargs):
        kwargs.update({"url": url, "method": method})
        return self._next(**kwargs)


# ----------------------------------------------------------------------
class TestHtmlStyling:
    def test_self_closing_image_keeps_valid_markup(self, tmp_path) -> None:
        """Markdown 输出 <img ... />，插样式时不能把斜杠留在属性中间。"""
        from publishers.html_publisher import HtmlPublisher

        template = Path(__file__).resolve().parent.parent / "templates" / "article.html"
        html = HtmlPublisher(tmp_path, template).render(
            "![图](a/b.png)\n", "标题", "2026-10-01 09:00"
        )
        tag = re.search(r"<img[^>]*>", html)
        assert tag is not None
        assert " / style" not in tag.group(0)
        assert 'style="max-width:100%' in tag.group(0)
        assert 'src="a/b.png"' in tag.group(0)


class TestNotify:
    def test_without_token_nothing_is_sent(self, monkeypatch) -> None:
        monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)
        called = []
        monkeypatch.setattr(notifier.requests, "post", lambda *a, **k: called.append(1))
        assert notify("标题", "正文") is False
        assert called == []

    def test_success_returns_true(self, monkeypatch) -> None:
        monkeypatch.setenv("PUSHPLUS_TOKEN", "tok")
        monkeypatch.setattr(
            notifier.requests, "post", lambda *a, **k: FakeResponse({"code": 200, "msg": "请求成功"})
        )
        assert notify("标题", "正文") is True

    def test_service_rejects_returns_false(self, monkeypatch) -> None:
        monkeypatch.setenv("PUSHPLUS_TOKEN", "tok")
        monkeypatch.setattr(
            notifier.requests,
            "post",
            lambda *a, **k: FakeResponse({"code": 903, "msg": "未实名认证"}),
        )
        assert notify("标题", "正文") is False

    def test_network_failure_does_not_raise(self, monkeypatch) -> None:
        # 推送是附加品：第三方服务挂了也不能让一期文章白跑
        monkeypatch.setenv("PUSHPLUS_TOKEN", "tok")
        monkeypatch.setattr(
            notifier.requests, "post", lambda *a, **k: FakeResponse({}, status_code=502)
        )
        assert notify("标题", "正文") is False

    def test_token_is_never_logged(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("PUSHPLUS_TOKEN", "super-secret-token")
        monkeypatch.setattr(
            notifier.requests, "post", lambda *a, **k: FakeResponse({"code": 200})
        )
        with caplog.at_level("INFO"):
            notify("标题", "正文")
        assert "super-secret-token" not in caplog.text


class TestIssueMessage:
    def test_lists_papers_and_counts(self) -> None:
        title, content = build_issue_message(
            volume=12,
            theme="长上下文推理与智能体",
            group_name="大模型推理与 AI Agent",
            papers=[
                ("会议", "正式发表", "OrchestrationBench"),
                ("arXiv", "预印本", "Some Preprint"),
            ],
            warning_count=2,
        )
        assert title == "AI 论文周报 Vol.12 已生成"
        assert "本期论文：2 篇" in content
        assert "1. [会议][正式发表] OrchestrationBench" in content
        assert "待核实项：2 条" in content

    def test_clean_issue_says_so(self) -> None:
        _, content = build_issue_message(
            volume=1, theme="t", group_name="g", papers=[], warning_count=0
        )
        assert "未发现可疑项" in content

    def test_issue_link_points_at_the_committed_file(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", "someone/ai-paper")
        monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
        monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
        monkeypatch.delenv("GITEE_REPO", raising=False)
        assert issue_link("2026-10-05-vol12-g_mon") == (
            "https://github.com/someone/ai-paper/blob/main/output/2026-10-05-vol12-g_mon.md"
        )

    def test_issue_link_uses_the_actual_branch(self, monkeypatch) -> None:
        # 仓库默认分支不叫 main 时，写死 main 会让链接 404
        monkeypatch.setenv("GITHUB_REPOSITORY", "someone/ai-paper")
        monkeypatch.setenv("GITHUB_REF_NAME", "master")
        monkeypatch.delenv("GITEE_REPO", raising=False)
        assert issue_link("slug") == (
            "https://github.com/someone/ai-paper/blob/master/output/slug.md"
        )

    def test_gitee_wins_over_github(self, monkeypatch) -> None:
        """github.com 国内直连不通，配了 Gitee 就必须优先给 Gitee 链接。"""
        monkeypatch.setenv("GITHUB_REPOSITORY", "someone/ai-paper")
        monkeypatch.setenv("GITHUB_REF_NAME", "main")
        monkeypatch.setenv("GITEE_REPO", "someone/ai-paper-weekly")
        assert issue_link("slug") == (
            "https://gitee.com/someone/ai-paper-weekly/blob/main/output/slug.md"
        )

    def test_gitee_only_setup_needs_no_github_vars(self, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.setenv("GITEE_REPO", "someone/ai-paper-weekly")
        assert issue_link("slug").startswith("https://gitee.com/")

    def test_trailing_slash_in_gitee_repo_is_tolerated(self, monkeypatch) -> None:
        monkeypatch.setenv("GITEE_REPO", "someone/ai-paper-weekly/")
        assert "gitee.com/someone/ai-paper-weekly/blob" in issue_link("slug")

    def test_issue_link_is_empty_off_actions(self, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITEE_REPO", raising=False)
        assert issue_link("2026-10-05-vol12-g_mon") == ""

    def test_empty_slug_yields_no_link(self, monkeypatch) -> None:
        monkeypatch.setenv("GITEE_REPO", "someone/ai-paper-weekly")
        assert issue_link("") == ""

    def test_link_is_included_when_available(self) -> None:
        _, content = build_issue_message(
            volume=12,
            theme="t",
            group_name="g",
            papers=[],
            warning_count=0,
            link="https://example.com/a.md",
        )
        assert "[打开全文](https://example.com/a.md)" in content

    def test_failure_message_names_the_stage(self) -> None:
        title, content = build_failure_message(scope="论文检索与筛选", reason="候选不足")
        assert title == "AI 论文周报运行失败"
        assert "论文检索与筛选" in content and "候选不足" in content


# ----------------------------------------------------------------------
TOKEN_OK = {"access_token": "ACCESS", "expires_in": 7200}


class TestWeChatDraftClient:
    def test_access_token_is_parsed(self) -> None:
        session = FakeSession([FakeResponse(TOKEN_OK)])
        client = WeChatDraftClient("appid", "secret", session=session)
        assert client.access_token() == "ACCESS"

    def test_access_token_is_cached(self) -> None:
        session = FakeSession([FakeResponse(TOKEN_OK)])
        client = WeChatDraftClient("appid", "secret", session=session)
        client.access_token()
        client.access_token()
        assert len(session.calls) == 1

    def test_http_200_with_error_code_is_a_failure(self) -> None:
        # 微信出错时也返回 200，只看 HTTP 状态码会误判成功
        session = FakeSession([FakeResponse({"errcode": 40164, "errmsg": "invalid ip"})])
        client = WeChatDraftClient("appid", "secret", session=session)
        with pytest.raises(WeChatError) as excinfo:
            client.access_token()
        assert "40164" in str(excinfo.value)
        assert "IP 白名单" in str(excinfo.value)

    def test_missing_cover_file_is_rejected_before_upload(self, tmp_path) -> None:
        session = FakeSession([])
        client = WeChatDraftClient("appid", "secret", session=session)
        with pytest.raises(WeChatError) as excinfo:
            client.upload_cover(tmp_path / "nope.png")
        assert "封面图不存在" in str(excinfo.value)
        assert session.calls == []

    def test_upload_cover_returns_media_id(self, tmp_path) -> None:
        cover = tmp_path / "cover.png"
        cover.write_bytes(b"png")
        session = FakeSession(
            [FakeResponse(TOKEN_OK), FakeResponse({"media_id": "MEDIA", "url": "http://x"})]
        )
        client = WeChatDraftClient("appid", "secret", session=session)
        assert client.upload_cover(cover) == "MEDIA"

    def test_create_draft_returns_media_id(self) -> None:
        session = FakeSession(
            [FakeResponse(TOKEN_OK), FakeResponse({"media_id": "DRAFT"})]
        )
        client = WeChatDraftClient("appid", "secret", session=session)
        media_id = client.create_draft(
            title="AI 论文周报 Vol.12", content="<p>正文</p>", thumb_media_id="MEDIA"
        )
        assert media_id == "DRAFT"
        body = session.calls[-1]["json"]
        assert body["articles"][0]["thumb_media_id"] == "MEDIA"
        # 评论开关交给人工在后台决定，程序不替用户做主
        assert body["articles"][0]["need_open_comment"] == 0

    def test_over_long_title_is_rejected_locally(self) -> None:
        session = FakeSession([])
        client = WeChatDraftClient("appid", "secret", session=session)
        with pytest.raises(WeChatError) as excinfo:
            client.create_draft(title="标" * 33, content="<p>x</p>", thumb_media_id="M")
        assert "超过微信上限 32 字" in str(excinfo.value)
        assert session.calls == []

    def test_over_long_content_is_rejected_locally(self) -> None:
        session = FakeSession([])
        client = WeChatDraftClient("appid", "secret", session=session)
        with pytest.raises(WeChatError) as excinfo:
            client.create_draft(title="标题", content="<p>" + "字" * 20001, thumb_media_id="M")
        assert "超过微信上限 20000 字符" in str(excinfo.value)
        assert session.calls == []

    def test_empty_title_is_rejected(self) -> None:
        client = WeChatDraftClient("appid", "secret", session=FakeSession([]))
        with pytest.raises(WeChatError):
            client.create_draft(title="  ", content="<p>x</p>", thumb_media_id="M")