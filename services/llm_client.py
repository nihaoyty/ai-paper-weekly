"""大模型调用封装（OpenAI 兼容协议）。

默认对接阿里云百炼 DashScope 的兼容模式，改 .env 里的 base_url 与 model
即可切换到其他服务。这里自己控制重试，所以 SDK 自带重试关闭。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

LOGGER = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """大模型调用在重试后仍然失败。"""


class LLMClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 180,
        max_retries: int = 3,
        max_tokens: int | None = 32768,
    ) -> None:
        if not api_key:
            raise LLMError("缺少 LLM_API_KEY，请先复制 .env.example 为 .env 并填写密钥")
        self._model = model
        self._max_retries = max(1, max_retries)
        self._max_tokens = max_tokens
        self._client = OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0
        )
        LOGGER.info(
            "大模型就绪：model=%s base_url=%s max_tokens=%s",
            model, base_url, max_tokens if max_tokens else "（用服务端默认值）",
        )

    @property
    def model(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    def complete_json(
        self, system: str, user: str, temperature: float = 0.2
    ) -> dict[str, Any]:
        text = self._chat(system, user, temperature, json_mode=True)
        return _extract_json(text)

    def complete_text(
        self, system: str, user: str, temperature: float = 0.3
    ) -> str:
        return self._chat(system, user, temperature, json_mode=False)

    # ------------------------------------------------------------------
    def _chat(
        self, system: str, user: str, temperature: float, json_mode: bool
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self._max_tokens:
            kwargs["max_tokens"] = self._max_tokens

        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 统一转换为 LLMError
                if kwargs.get("response_format") and _unsupported_param(exc, "response_format"):
                    LOGGER.warning("当前模型不支持 JSON 模式，降级为普通文本后重试")
                    kwargs.pop("response_format")
                    continue
                if kwargs.get("max_tokens") and _unsupported_param(exc, "max_tokens"):
                    LOGGER.warning("服务端不接受 max_tokens=%s，改用服务端默认值后重试", kwargs["max_tokens"])
                    kwargs.pop("max_tokens")
                    continue
                if not _is_retryable(exc):
                    raise LLMError(f"大模型调用失败（不可重试）：{exc}") from exc
                last_error = exc
                LOGGER.warning(
                    "大模型调用失败（第 %d/%d 次）：%s", attempt, self._max_retries, exc
                )
                if attempt < self._max_retries:
                    time.sleep(min(30.0, 2.0**attempt))
                continue

            choice = response.choices[0]
            usage = _describe_usage(response)
            content = (choice.message.content or "").strip()

            # 推理模型的思考过程也计入 max_tokens。被截断时必须立刻报清楚，
            # 因为原样重试只会再次截断，白白烧掉额度。
            if choice.finish_reason == "length":
                raise LLMError(
                    f"输出被 max_tokens 截断（{usage}）。"
                    f"注意推理模型的思考过程同样占用 max_tokens，"
                    f"请调大 .env 中的 LLM_MAX_TOKENS（当前 {self._max_tokens}）。"
                )

            if content:
                LOGGER.debug("大模型返回正常（%s）", usage)
                return content

            last_error = LLMError(f"大模型返回空内容（{usage}）")
            LOGGER.warning(
                "大模型返回空内容（第 %d/%d 次，%s）", attempt, self._max_retries, usage
            )
            if attempt < self._max_retries:
                time.sleep(min(15.0, 2.0**attempt))

        raise LLMError(f"大模型调用失败，已重试 {self._max_retries} 次：{last_error}")


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500
    return False


def _describe_usage(response: Any) -> str:
    """把 token 用量整理成一行，便于排查截断与成本。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return "用量未知"
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details else None
    reasoning_part = f"，其中思考 {reasoning}" if reasoning else ""
    return (
        f"输入 {usage.prompt_tokens} / 输出 {usage.completion_tokens}"
        f"{reasoning_part} token"
    )


def _unsupported_param(exc: Exception, param: str) -> bool:
    """判断 400 是否由某个不受支持的参数引起。"""
    if not isinstance(exc, APIStatusError) or exc.status_code != 400:
        return False
    return param in str(exc).lower()


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise LLMError(f"无法从模型返回中解析出 JSON，原始内容前 400 字：{text[:400]}")