"""拉取模型服务端的模型列表（`GET {base_url}/models`，OpenAI 兼容）。

用户口径（改造计划 §2.1）：官方提供方要「从服务端获取模型列表并选择 Model ID，
取不到时允许手填」。所以这个模块只做一件事：尽力列出来，失败了给**结构化、不泄密**的原因。

三条硬约束（同计划 §7）：

1. **先过 `endpoint_guard`**：地址不可信就不发请求；重定向一律不跟随（跟着跳就可能跳到内网）。
2. **超时 + 响应体上限**：`timeout` 默认 8s，边读边数，超过 `MAX_BODY` 立即断开。
3. **错误里不出现密钥、完整 URL、请求头、堆栈**：只报状态码 / 异常类别 / 一句人话。
"""

from __future__ import annotations

import json

import httpx

from backend.core.errors import ProviderError, ValidationError
from backend.services.model import endpoint_guard

# 响应体上限：模型列表一般几十 KB，512KB 已经非常宽松
MAX_BODY = 512 * 1024
# 连接 / 读取超时（秒）
DEFAULT_TIMEOUT = 8.0


def _models_url(base_url: str) -> str:
    """`{base}/models` —— base 已经去掉尾斜杠，也不会带 query（见 guard）。"""
    return f"{base_url.rstrip('/')}/models"


def _pick_ids(payload) -> list[dict]:
    """从几种常见形状里取 `id` 列表：`{data:[…]}` / `{models:[…]}` / `[…]`。"""
    rows = None
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("data", "models", "items"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if rows is None:
        raise ValidationError("端点返回的不是模型列表（既没有 data 也没有 models 字段）")
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            mid, owner = row.strip(), None
        elif isinstance(row, dict):
            mid = str(row.get("id") or row.get("model") or row.get("name") or "").strip()
            owner = row.get("owned_by") or row.get("owner")
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        item: dict = {"id": mid}
        if owner:
            item["owned_by"] = str(owner)
        out.append(item)
    out.sort(key=lambda x: x["id"])
    return out


async def list_models(base_url: str, *, api_key: str | None = None,
                      allow_private: bool = False,
                      timeout: float = DEFAULT_TIMEOUT) -> dict:
    """拉模型列表。成功返回 `{items: [{id, owned_by?}], count, host}`；
    失败抛 `ValidationError`（400）或 `ProviderError`（502），消息里**不含密钥与完整地址**。"""
    base = endpoint_guard.normalize_base_url(base_url)
    await endpoint_guard.guard_url(base, allow_private=allow_private)
    url = _models_url(base)
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    chunks: list[bytes] = []
    total = 0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), follow_redirects=False,
        ) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code in (401, 403):
                    raise ValidationError("认证失败：端点拒绝了这把密钥（401 / 403）")
                if resp.status_code == 404:
                    raise ValidationError("端点没有 /models 接口（404），请手填 Model ID")
                if resp.status_code >= 400:
                    raise ValidationError(f"端点返回 HTTP {resp.status_code}")
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_BODY:
                        raise ValidationError("端点返回体过大（超过 512KB），已断开")
                    chunks.append(chunk)
    except httpx.TimeoutException as exc:
        raise ProviderError("连接超时，请检查 Base URL 与网络", retryable=True) from exc
    except httpx.HTTPError as exc:
        # 只报异常类别：不把完整 URL / 请求头 / 内网地址带进日志和响应
        raise ProviderError(f"连接失败（{type(exc).__name__}）", retryable=True) from exc
    body = b"".join(chunks)
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ValidationError("端点返回的不是合法 JSON") from exc
    items = _pick_ids(payload)
    host = url.split("/")[2] if "://" in url else ""
    return {"items": items, "count": len(items), "host": host}
