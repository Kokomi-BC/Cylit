"""主后端 → Agent 服务的**字节级转发**（规划 §4.2：主后端不解释 agent 事件）。

为什么不让主后端"看懂"SSE 再重新组装成前端协议：那样每次调整 agent 的事件载荷都要
改两处，而且中间层一旦开始解析就会开始"顺手补全"，两边的语义迟早分叉。
这里只做三件事：带上内部令牌、把请求转过去、把响应字节原样吐出来。

唯一例外是**失败**：agent 进程没起、或令牌不对时，必须由主后端合成一个合法的
终态事件（`error`），否则前端在等一个永远不会来的 `done`。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.utils import json_dumps

log = get_logger(__name__)

#: 连不上 agent 时的护栏文案（前端直接显示）
AGENT_DOWN = ("问答服务没有在运行。请先启动它：`pr.py agent`"
              "（或在设置页确认问答功能已开启）。")


def _frame(event_type: str, **payload: Any) -> bytes:
    return f"event: {event_type}\ndata: {json_dumps(payload)}\n\n".encode("utf-8")


def frame(event_type: str, **payload: Any) -> bytes:
    """合成一帧 SSE（公开版）。

    **只用于我们自己产生的帧**（自动压缩提示、追问建议）—— agent 的字节仍然原样透传，
    这一层不去解析它们（§4.2）。
    """
    return _frame(event_type, **payload)


def _base_url() -> str:
    s = get_settings()
    return f"http://{s.agent_host}:{s.agent_port}"


def _timeout() -> httpx.Timeout:
    """流式问答的读超时必须**不设上限**：agent 可能想很久才吐第一个字。"""
    return httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)


def agent_enabled() -> bool:
    return bool(get_settings().agent_enabled)


async def stream_ask(payload: dict) -> AsyncIterator[bytes]:
    """转发一轮问答（SSE 逐块透传）。"""
    s = get_settings()
    token = s.agent_token()
    if not agent_enabled():
        yield _frame("error", code="AGENT_DISABLED",
                     message="问答功能已在设置里关闭。", retryable=False)
        return
    if not token:
        yield _frame("error", code="AGENT_NO_TOKEN",
                     message="内部令牌缺失，请重启主后端以生成它。", retryable=False)
        return
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            async with client.stream("POST", f"{_base_url()}/v1/ask", json=payload,
                                     headers={"X-Internal-Token": token}) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                    log.warning("agent 拒绝了请求：HTTP %s %s", resp.status_code, body)
                    yield _frame("error", code=f"AGENT_HTTP_{resp.status_code}",
                                 message=f"问答服务报错：{body}", retryable=True)
                    return
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
    except httpx.HTTPError as exc:
        log.warning("连不上 agent 服务：%s", exc)
        yield _frame("error", code="AGENT_DOWN", message=AGENT_DOWN, retryable=True)


async def agent_get(path: str) -> dict:
    """读 agent 的诊断接口（`/v1/skills` 等）。失败返回 ``{"ok": False, ...}``。"""
    s = get_settings()
    token = s.agent_token()
    if not token:
        return {"ok": False, "error": "内部令牌缺失"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(f"{_base_url()}{path}",
                                    headers={"X-Internal-Token": token})
            if resp.status_code >= 400:
                return {"ok": False, "status": resp.status_code,
                        "error": resp.text[:200]}
            return {"ok": True, **(resp.json() or {})}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


async def agent_post(path: str, payload: dict, *, timeout: float = 300.0) -> dict:
    """POST 到 agent 的 JSON 接口（非 SSE，如 `/v1/compress`）。

    失败**抛 `ProviderError`**而不是返回 `{"ok": False}`：这类调用在主后端是同步等待的，
    调用方需要的是一句能直接回给用户的话，错误码走统一信封。
    """
    from backend.core.errors import ProviderError

    s = get_settings()
    token = s.agent_token()
    if not token:
        raise ProviderError("内部令牌缺失，请重启主后端以生成它。", retryable=False)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(f"{_base_url()}{path}", json=payload,
                                     headers={"X-Internal-Token": token})
    except httpx.HTTPError as exc:
        raise ProviderError(AGENT_DOWN, retryable=True, detail={"cause": str(exc)[:200]}) from exc
    if resp.status_code >= 400:
        detail = resp.text[:300]
        try:
            detail = str((resp.json() or {}).get("detail") or detail)
        except Exception:  # noqa: BLE001
            pass
        raise ProviderError(f"问答服务报错：{detail}", retryable=True)
    return resp.json() or {}


async def agent_up() -> bool:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
            resp = await client.get(f"{_base_url()}/health")
            return resp.status_code < 400
    except Exception:  # noqa: BLE001
        return False
