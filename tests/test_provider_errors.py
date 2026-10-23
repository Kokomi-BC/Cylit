"""上游错误解析层：SSE error 帧与 HTTP 错误信封的字段契约。

覆盖两件事：
1. 分类（哪种异常 → 哪个 code/kind，状态码与上游错误码有没有丢）；
2. 一条链路上的**真实**帧：agent 运行时抛的异常 → SSE 帧，
   HTTP 层的限额 → 429 信封（两者必须能区分，前端只按一种读法取）。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

from backend.api import agent as api_agent
from backend.agent.citations import RefRegistry
from backend.agent.deps import Deps
from backend.agent.runtime import AgentRuntime, RunState
from backend.core import provider_errors
from backend.core.errors import (
    LimitExceededError, ProviderError, RateLimitError, provider_detail_of,
)
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

API = "https://api.example.com/v1/chat/completions"


def _req() -> httpx.Request:
    return httpx.Request("POST", API)


def _openai_error(kind, status: int, body: dict):
    """openai 的构造体（**不发真请求**，response 只是装着状态码与 body）。"""
    response = httpx.Response(status, request=_req(), json=body)
    return kind("boom", response=response, body=body)


# ---------------------------------------------------------------------------
# 上游 body 的形状
# ---------------------------------------------------------------------------
def test_parse_openai_compatible_error_object():
    parsed = provider_errors.parse_body(
        {"error": {"message": "Rate limit reached", "type": "requests",
                   "param": None, "code": "rate_limit_exceeded"}})
    assert parsed == {"code": "rate_limit_exceeded", "type": "requests",
                      "message": "Rate limit reached"}


def test_parse_error_as_string():
    assert provider_errors.parse_body({"error": "model not found"})["message"] == "model not found"


def test_parse_dashscope_code_message():
    parsed = provider_errors.parse_body({"code": "InvalidApiKey", "message": "Invalid API-key provided."})
    assert parsed["code"] == "InvalidApiKey"
    assert parsed["message"] == "Invalid API-key provided."
    assert parsed["type"] is None


def test_parse_non_json_text_keeps_snippet():
    parsed = provider_errors.parse_body("<html><body>502 Bad Gateway</body></html>")
    assert parsed["code"] is None and parsed["type"] is None
    assert "502 Bad Gateway" in parsed["message"]


def test_parse_empty_and_list_and_our_own_detail():
    for body in (None, "", {}, [], [{"error": {"message": "x"}}]):
        parsed = provider_errors.parse_body(body)
        assert parsed["message"] is None and parsed["code"] is None
    parsed = provider_errors.parse_body({"detail": "内部令牌不正确"})
    assert parsed["message"] == "内部令牌不正确"


# ---------------------------------------------------------------------------
# ModelHTTPError：状态码映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status,code,kind", [
    (401, "PROVIDER_AUTH", "auth"),
    (403, "PROVIDER_AUTH", "auth"),
    (429, "PROVIDER_RATE_LIMIT", "rate_limit"),
    (500, "PROVIDER_SERVER", "server"),
    (503, "PROVIDER_SERVER", "server"),
    (529, "PROVIDER_SERVER", "server"),
    (400, "PROVIDER_BAD_REQUEST", "bad_request"),
    (404, "PROVIDER_BAD_REQUEST", "bad_request"),
    (422, "PROVIDER_BAD_REQUEST", "bad_request"),
])
def test_model_http_error_maps_status(status, code, kind):
    exc = ModelHTTPError(status, "gpt-4o", {"error": {"message": "upstream said no",
                                                      "type": "requests",
                                                      "code": "provider_" + str(status)}})
    fields = provider_errors.classify(exc, provider_label="OpenAI")
    assert (fields["code"], fields["kind"]) == (code, kind)
    assert fields["http_status"] == status
    assert fields["provider_code"] == "provider_" + str(status)
    assert fields["provider_type"] == "requests"
    assert fields["model"] == "gpt-4o"
    assert fields["retryable"] is (kind in ("rate_limit", "server"))


def test_quota_is_not_a_rate_limit():
    """429 的 message 里带 quota 不算额度；error.code 说 insufficient_quota 才算。"""
    rate = ModelHTTPError(429, "m", {"error": {"message": "Rate limit reached for quota",
                                              "type": "requests",
                                              "code": "rate_limit_exceeded"}})
    assert provider_errors.classify(rate)["kind"] == "rate_limit"
    quota = ModelHTTPError(429, "m", {"error": {"message": "quota exhausted",
                                               "type": "insufficient_quota",
                                               "code": "insufficient_quota"}})
    assert provider_errors.classify(quota)["kind"] == "quota"
    billing = ModelHTTPError(402, "m", {"error": {"message": "请充值"}})
    assert provider_errors.classify(billing)["kind"] == "quota"


def test_retry_after_header_is_carried():
    exc = ModelHTTPError(429, "m", {"error": {"message": "slow down"}},
                         headers={"retry-after": "17"})
    assert provider_errors.classify(exc)["retry_after"] == 17.0


# ---------------------------------------------------------------------------
# openai / httpx / pydantic_ai 的其它异常
# ---------------------------------------------------------------------------
def test_openai_rate_limit_error():
    exc = _openai_error(openai.RateLimitError, 429,
                        {"error": {"message": "Rate limit reached", "type": "requests",
                                   "code": "rate_limit_exceeded"}})
    fields = provider_errors.classify(exc)
    assert (fields["code"], fields["kind"], fields["http_status"]) == \
        ("PROVIDER_RATE_LIMIT", "rate_limit", 429)
    assert fields["provider_code"] == "rate_limit_exceeded"


def test_openai_connection_error_is_network():
    fields = provider_errors.classify(openai.APIConnectionError(request=_req()))
    assert (fields["code"], fields["kind"]) == ("PROVIDER_NETWORK", "network")
    assert fields["http_status"] is None
    assert fields["retryable"] is True


def test_httpx_connect_error_read_timeout_and_remote_protocol():
    connect = provider_errors.classify(httpx.ConnectError("getaddrinfo failed", request=_req()))
    assert (connect["code"], connect["kind"]) == ("PROVIDER_NETWORK", "network")

    timeout = provider_errors.classify(httpx.ReadTimeout("read timed out", request=_req()))
    assert (timeout["code"], timeout["kind"]) == ("PROVIDER_TIMEOUT", "timeout")

    broken = provider_errors.classify(
        httpx.RemoteProtocolError("peer closed connection without sending complete message body",
                                  request=_req()))
    assert (broken["code"], broken["kind"]) == ("PROVIDER_STREAM", "stream")


def test_connect_timeout_counts_as_timeout():
    """ConnectTimeout 同时是超时与网络错误：先判超时（"等一下"比"检查网络"准）。"""
    fields = provider_errors.classify(httpx.ConnectTimeout("t", request=_req()))
    assert fields["kind"] == "timeout"


def test_unexpected_model_behaviour_is_stream():
    fields = provider_errors.classify(UnexpectedModelBehavior("stream ended early"))
    assert (fields["code"], fields["kind"]) == ("PROVIDER_STREAM", "stream")


def test_cause_chain_is_walked():
    """适配层普遍是 raise ProviderError(...) from exc：状态码在内层，不能只看最外层。"""
    inner = httpx.ReadTimeout("read timed out", request=_req())
    try:
        try:
            raise inner
        except httpx.ReadTimeout as exc:
            raise ProviderError("翻译请求失败：read timed out") from exc
    except ProviderError as outer:
        fields = provider_errors.classify(outer)
    assert fields["kind"] == "timeout"
    assert fields["retryable"] is True


# ---------------------------------------------------------------------------
# 本地限额 vs 上游 429
# ---------------------------------------------------------------------------
def test_limit_exceeded_is_its_own_kind():
    exc = LimitExceededError("今日额度已用完（模型级）",
                             detail={"limit": {"period": "day", "used": 12}})
    fields = provider_errors.classify(exc)
    assert (fields["code"], fields["kind"], fields["retryable"]) == ("LIMIT_EXCEEDED", "limit", False)
    # 本地限额没有上游：这几个字段必须是 None，否则前端会把两种 429 显示成一回事
    assert fields["http_status"] is None and fields["provider_code"] is None
    assert not isinstance(exc, ProviderError)


def test_plain_value_error_is_unknown_internal():
    fields = provider_errors.classify(ValueError("bad math"))
    assert (fields["code"], fields["kind"], fields["retryable"]) == ("INTERNAL", "unknown", False)
    assert "ValueError" in fields["message"]


def test_legacy_provider_error_keeps_its_code():
    fields = provider_errors.classify(ProviderError("压缩没有产出内容，请重试"))
    assert fields["code"] == "PROVIDER_ERROR" and fields["kind"] == "unknown"
    fields = provider_errors.classify(RateLimitError("上游限流"))
    assert (fields["code"], fields["kind"]) == ("PROVIDER_RATE_LIMIT", "rate_limit")


# ---------------------------------------------------------------------------
# 脱敏与长度
# ---------------------------------------------------------------------------
def test_secrets_are_redacted_and_message_is_clipped():
    body = {"error": {"message": "bad key Bearer sk-abc123 for request api_key=xyz",
                      "type": "invalid_request_error", "code": "invalid_api_key"}}
    fields = provider_errors.classify(ModelHTTPError(401, "gpt-4o", body),
                                      secrets=("super-secret-value",))
    text = json.dumps(fields, ensure_ascii=False)
    assert "sk-abc123" not in text
    assert "xyz" not in text
    assert len(fields["provider_message"]) <= 500
    assert len(fields["message"]) <= 300

    long_body = {"error": {"message": "x" * 4000, "code": "invalid_api_key"}}
    clipped = provider_errors.classify(ModelHTTPError(401, "m", long_body))
    assert len(clipped["provider_message"]) == 500


def test_real_key_string_from_config_is_redacted():
    exc = ModelHTTPError(401, "m", {"error": {"message": "Incorrect API key provided: k-9f3d77zz"}})
    fields = provider_errors.classify(exc, secrets=("k-9f3d77zz",))
    assert "k-9f3d77zz" not in json.dumps(fields, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 帧的形状
# ---------------------------------------------------------------------------
def test_frame_carries_every_contract_field():
    frame = provider_errors.error_frame(ModelHTTPError(429, "gpt-4o", None))
    assert frame["type"] == "error"
    assert set(frame) == {"type", *provider_errors.FIELDS}


# ---------------------------------------------------------------------------
# 端到端：agent 运行时 → SSE 帧
# ---------------------------------------------------------------------------
class _BoomStream:
    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *args):
        return False


class _BoomAgent:
    def __init__(self, exc):
        self.exc = exc

    def run_stream_events(self, *args, **kwargs):
        return _BoomStream(self.exc)


def _runtime(exc, monkeypatch):
    """一个只差"模型这一跳"的 runtime：真实的 _stream_model，假的 agent。"""
    runtime = AgentRuntime.__new__(AgentRuntime)     # 跳过 HostClient / Skills 的构造
    runtime.cfg = SimpleNamespace(heartbeat_seconds=1.0, max_rounds=8)
    runtime._workspace = None

    async def model_config(*args, **kwargs):
        return {"usable": True, "model_name": "gpt-4o", "provider": "openai",
                "api_key": "sk-live-real-key", "parameters": {}}

    async def user_prompt(*args, **kwargs):
        return "hello"

    monkeypatch.setattr(runtime, "model_config", model_config)
    monkeypatch.setattr(runtime, "_user_prompt", user_prompt)
    monkeypatch.setattr(runtime, "_history", lambda req: [])
    monkeypatch.setattr(runtime, "_build_agent", lambda cfg: _BoomAgent(exc))
    return runtime


def _drive(runtime):
    req = SimpleNamespace(model_id=None, reasoning_effort=None)
    deps = Deps(cfg=None, host=None, paper_id="p1", registry=RefRegistry("p1"))
    state = RunState()

    async def run():
        return [event async for event in
                runtime._stream_model(req, deps, state, float("inf"), time.perf_counter())]

    return asyncio.run(run()), state


def test_runtime_emits_classified_frame_for_upstream_429(monkeypatch):
    exc = ModelHTTPError(429, "gpt-4o", {"error": {
        "message": "Rate limit reached", "type": "requests",
        "code": "rate_limit_exceeded"}})
    events, state = _drive(_runtime(exc, monkeypatch))
    assert len(events) == 1
    frame = events[0]
    assert frame["type"] == "error"
    assert (frame["code"], frame["kind"], frame["http_status"]) == \
        ("PROVIDER_RATE_LIMIT", "rate_limit", 429)
    assert frame["provider_code"] == "rate_limit_exceeded"
    assert frame["provider_type"] == "requests"
    assert frame["model"] == "gpt-4o"
    assert frame["provider_label"] == "OpenAI"
    # 真实 key 已进脱敏集合：帧里搜不到它
    assert "sk-live-real-key" not in json.dumps(frame, ensure_ascii=False)
    assert state.incomplete_reason == "error"


def test_runtime_reraises_unknown_errors(monkeypatch):
    """认不出来的异常仍然上抛：真 bug 不该在分类层被吞成一条"上游错误"。"""
    runtime = _runtime(ValueError("bad math"), monkeypatch)
    with pytest.raises(ValueError):
        _drive(runtime)


def test_finalize_empty_answer_frame_has_fields():
    """EMPTY_ANSWER 帧在 _finalize 里产出：模型信息只能从 state.model_cfg 拿。"""
    runtime = AgentRuntime.__new__(AgentRuntime)
    state = RunState()
    state.model_cfg = {"model_name": "gpt-4o", "provider": "openai"}
    deps = Deps(cfg=None, host=None, paper_id="p1", registry=RefRegistry("p1"))
    req = SimpleNamespace(conversation_id="c1", parent_id=None)

    async def run():
        return [event async for event in runtime._finalize(req, deps, state)]

    frames = asyncio.run(run())
    assert len(frames) == 1
    frame = frames[0]
    assert (frame["code"], frame["kind"], frame["retryable"]) == \
        ("EMPTY_ANSWER", "unknown", True)
    assert frame["model"] == "gpt-4o"
    assert frame["provider_label"] == "OpenAI"


# ---------------------------------------------------------------------------
# 端到端：主后端泵 → SSE 帧；HTTP 层 → 错误信封
# ---------------------------------------------------------------------------
def _parse_frames(chunks):
    out = []
    for chunk in chunks:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        for block in text.split("\n\n"):
            if not block.strip():
                continue
            lines = block.strip().splitlines()
            event = lines[0].split(":", 1)[1].strip()
            payload = json.loads(lines[1].split(":", 1)[1].strip())
            out.append((event, payload))
    return out


def test_bridge_ask_stream_error_becomes_classified_frame(monkeypatch):
    exc = ModelHTTPError(429, "gpt-4o", {"error": {
        "message": "Rate limit reached", "type": "requests",
        "code": "rate_limit_exceeded"}})

    async def boom(body):
        raise exc
        yield b""            # pragma: no cover —— 让它是个 async generator

    async def no_compress(conversation_id):
        return False

    monkeypatch.setattr(api_agent.bridge, "stream_ask", boom)
    monkeypatch.setattr(api_agent, "_auto_compress", no_compress)

    async def collect():
        return [chunk async for chunk in api_agent._ask_stream({"question": "q"}, "c1")]

    frames = _parse_frames(asyncio.run(collect()))
    assert frames and frames[0][0] == "error"
    payload = frames[0][1]
    assert (payload["code"], payload["kind"], payload["http_status"]) == \
        ("PROVIDER_RATE_LIMIT", "rate_limit", 429)
    assert payload["provider_code"] == "rate_limit_exceeded"
    assert payload["provider_type"] == "requests"
    assert payload["retryable"] is True


def test_usage_recorder_attaches_provider_detail(monkeypatch):
    """model_manager 的 generate 包装器：失败时补 detail.provider，重试语义一个字不动。"""
    from backend.providers.base import GenerationProvider
    from backend.services.model import limits
    from backend.services.model import model_manager as mm

    class Boom(GenerationProvider):
        def generate(self, prompt, *args, **kwargs):
            raise ProviderError("翻译请求失败（ReadTimeout）") from httpx.ReadTimeout(
                "read timed out", request=_req())

    # 限额与落账要 DB：这两处不是本用例的对象，换成空实现
    monkeypatch.setattr(limits, "check_and_reserve", lambda config, prompt=None: None)
    monkeypatch.setattr(limits, "release", lambda reservation: None)

    provider = mm._usage_recorder(Boom)({"model_name": "deepseek-chat",
                                         "provider": "deepseek"},
                                        "sk-deepseek-secret")
    with pytest.raises(ProviderError) as info:
        provider.generate("hi")
    exc = info.value
    payload = exc.to_dict()["detail"]["provider"]
    assert payload["kind"] == "timeout"                    # 顺着 __cause__ 认出来的
    assert payload["model"] == "deepseek-chat"
    assert payload["provider_label"] == "DeepSeek"
    assert "sk-deepseek-secret" not in json.dumps(payload, ensure_ascii=False)
    assert exc.retryable is True                           # 原有重试策略没被改


def test_http_envelope_keeps_limit_shape_and_adds_provider():
    """限额走 429 + code=LIMIT_EXCEEDED + detail.limit（原有快照形状不动）。

    前端只按一种读法取分类：error.detail.provider.kind（limit / rate_limit / …），
    detail.limit 只用来渲染额度数值。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.core.errors import PaperMindError
    from backend.main import _error_response

    app = FastAPI()

    @app.exception_handler(PaperMindError)
    async def handler(request, exc):
        return _error_response(exc, "trace-1")

    @app.get("/limit")
    def limit():
        raise LimitExceededError("今日额度已用完（模型级）",
                                 detail={"limit": {"period": "day", "used": 12}})

    @app.get("/upstream")
    def upstream():
        raise ProviderError("上游限流了", model="gpt-4o", provider_label="OpenAI")

    @app.get("/upstream429")
    def upstream429():
        try:
            raise ModelHTTPError(429, "gpt-4o", {"error": {
                "message": "Rate limit reached", "type": "requests",
                "code": "rate_limit_exceeded"}})
        except ModelHTTPError as exc:
            raise ProviderError("上游限流了", model="gpt-4o",
                                provider_label="OpenAI") from exc

    client = TestClient(app)
    resp = client.get("/limit")
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "LIMIT_EXCEEDED"
    detail = body["error"]["detail"]
    assert detail["limit"] == {"period": "day", "used": 12}       # 现有快照形状
    assert detail["provider"]["kind"] == "limit"
    assert detail["provider"]["http_status"] is None
    assert body["trace_id"] == "trace-1"

    # 同一份字段也在普通上游错误上（code 沿用旧值，kind/字段补上）
    resp = client.get("/upstream")
    detail = resp.json()["error"]["detail"]
    assert resp.json()["error"]["code"] == "PROVIDER_ERROR"
    assert detail["provider"]["kind"] == "unknown"
    assert detail["provider"]["model"] == "gpt-4o"
    assert detail["provider"]["provider_label"] == "OpenAI"
    # 上游 429 与本地限额必须一眼分得开：本地是 HTTP 429 + code=LIMIT_EXCEEDED；
    # 上游是本层包装的 502，真实状态码在 detail.provider.http_status=429、
    # 分类在 detail.provider.kind=rate_limit。信封的 error.code 保持旧值（不改名）。
    resp = client.get("/upstream429")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "PROVIDER_ERROR"          # 旧 code 不动
    assert body["error"]["detail"]["provider"]["code"] == "PROVIDER_RATE_LIMIT"
    assert body["error"]["detail"]["provider"]["kind"] == "rate_limit"
    assert body["error"]["detail"]["provider"]["http_status"] == 429
    assert "limit" not in body["error"]["detail"]      # 额度快照只属于本地限额

    assert provider_detail_of(LimitExceededError("x", detail={"limit": {}}))["kind"] == "limit"
