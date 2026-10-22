"""Anthropic（Claude）**原生协议**适配器 —— `POST /v1/messages`。

用户口径 2026-12-07：Claude 不按 OpenAI 兼容层接（那是 beta、字段语义也不一样），
改用官方原生协议。这里只做**备用实现**：报文形状按官方文档写、离线单测打桩验证，
真实账号能否调通要用户配好 key 后自己点一次「测试」。

要点：
  · 认证是 `x-api-key` + `anthropic-version`，不是 `Authorization: Bearer`；
  · `system` 是**顶层字段**，不在 messages 里；
  · 回复是 content blocks 数组，要拼回纯文本；
  · `max_tokens` 必填（原生接口不填直接 400）。
"""

from __future__ import annotations

from typing import Any

from backend.core.errors import ProviderError, RateLimitError
from backend.providers.base import GenerationProvider, GenerationResult

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicGeneration(GenerationProvider):
    """Claude 原生 Messages API。只实现 `generate`（含 system）与 `structured`（用 prompt 约束 JSON）。"""

    def __init__(self, config: dict | None = None, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self._client = None

    def _endpoint(self) -> str:
        base = (self.config.get("base_url") or "https://api.anthropic.com").rstrip("/")
        # 允许用户把 base_url 填成带 /v1 或带 /v1/messages 的形式，统一收敛到 /v1/messages
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ProviderError(
                f"模型 {self.model_name or '(未命名)'} 缺少 API Key：请在 data/secrets/ 里放入密钥文件。",
                retryable=False,
            )
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("未安装 httpx", retryable=False) from exc
        self._client = httpx.Client(timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0))
        return self._client

    def _post(self, body: dict) -> dict:
        client = self._ensure_client()
        headers = {
            "x-api-key": self.api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        try:
            resp = client.post(self._endpoint(), json=body, headers=headers)
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一成可重试错误
            raise ProviderError(f"Claude 请求失败（{type(exc).__name__}）", retryable=True) from exc
        if resp.status_code == 429:
            raise RateLimitError("Claude 限流（429）")
        if resp.status_code in (401, 403):
            raise ProviderError("Claude 认证失败：x-api-key 无效或无权限", retryable=False)
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str((resp.json().get("error") or {}).get("message") or "")[:200]
            except Exception:  # noqa: BLE001 - 错误体不是 JSON 就只报状态码
                detail = ""
            raise ProviderError(f"Claude 返回 HTTP {resp.status_code} {detail}".strip(), retryable=False)
        return resp.json()

    @staticmethod
    def _text_of(payload: dict) -> str:
        parts: list[str] = []
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)

    def generate(self, prompt: str, *, system: str | None = None,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> GenerationResult:
        params = self.config.get("parameters") or {}
        body: dict = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            # 原生 API 必须给 max_tokens：配置里没有就按 4096 兜底
            "max_tokens": int(max_tokens or params.get("max_tokens") or 4096),
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        payload = self._post(body)
        usage = payload.get("usage") or {}
        return GenerationResult(
            text=self._text_of(payload),
            model=str(payload.get("model") or self.model_name or ""),
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            raw=payload,
        )

    def structured(self, prompt: str, schema: dict, *, system: str | None = None,
                   **kwargs: Any) -> dict:
        """原生接口没有 `response_format`，就把 schema 写进 system 再按 JSON 解析。"""
        from backend.core.utils import json_loads

        guard = (system or "") + "\n\n只输出一个 JSON 对象，符合下面 schema，不要解释、不要代码块：\n" \
            + str(schema)
        text = self.generate(prompt, system=guard, **kwargs).text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[1] if "\n" in text else text
        try:
            data = json_loads(text)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("Claude 返回的不是合法 JSON", retryable=False) from exc
        if not isinstance(data, dict):
            raise ProviderError("Claude 返回的 JSON 不是对象", retryable=False)
        return data
