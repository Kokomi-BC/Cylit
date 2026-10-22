"""Claude 原生适配器（离线单测：桩 httpx，不联网）。

用户口径 2026-12-07：Claude 走原生 Messages API，**不在**这一版做真实连通性验证；
这里只钉住三件事：认证头、报文形状、回复解析。
"""

from __future__ import annotations

import pytest

from backend.core.errors import ProviderError
from backend.providers.generation.anthropic import AnthropicGeneration


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, resp: _Resp):
        self.resp = resp
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.resp


def _make(resp: _Resp, base_url: str = "https://api.anthropic.com"):
    p = AnthropicGeneration({"base_url": base_url, "model_name": "claude-sonnet-4-5"}, "sk-ant-test")
    c = _Client(resp)
    p._client = c
    return p, c


def test_endpoint_normalization():
    for base in ("https://api.anthropic.com", "https://api.anthropic.com/",
                 "https://api.anthropic.com/v1"):
        p, _ = _make(_Resp(200, {}), base)
        assert p._endpoint() == "https://api.anthropic.com/v1/messages"


def test_request_shape_and_auth_header():
    resp = _Resp(200, {"model": "claude-sonnet-4-5", "content": [{"type": "text", "text": "hi"}],
                       "usage": {"input_tokens": 3, "output_tokens": 5}})
    p, c = _make(resp)
    out = p.generate("hello", system="be brief", max_tokens=64)
    assert out.text == "hi"
    assert out.prompt_tokens == 3 and out.completion_tokens == 5
    call = c.calls[0]
    assert call["headers"]["x-api-key"] == "sk-ant-test"      # 不是 Bearer
    assert call["headers"]["anthropic-version"]
    assert call["json"]["model"] == "claude-sonnet-4-5"
    assert call["json"]["max_tokens"] == 64
    assert call["json"]["system"] == "be brief"                # system 是顶层字段
    assert call["json"]["messages"] == [{"role": "user", "content": "hello"}]


def test_auth_error_is_not_retryable():
    p, _ = _make(_Resp(401, {"error": {"message": "invalid x-api-key"}}))
    with pytest.raises(ProviderError) as exc:
        p.generate("x")
    assert exc.value.retryable is False
    assert "sk-ant-test" not in str(exc.value)


def test_text_blocks_joined_and_missing_key_rejected():
    resp = _Resp(200, {"content": [{"type": "text", "text": "a"},
                                  {"type": "tool_use"},
                                  {"type": "text", "text": "b"}]})
    p, _ = _make(resp)
    assert p.generate("x").text == "ab"
    p2 = AnthropicGeneration({"base_url": "https://api.anthropic.com", "model_name": "m"}, None)
    with pytest.raises(ProviderError):
        p2.generate("x")
