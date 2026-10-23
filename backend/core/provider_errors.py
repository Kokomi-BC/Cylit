"""上游（模型供应商）错误的统一解析层 —— SSE error 帧与 HTTP 错误信封的**唯一**来源。

为什么要单独一层：模型调用抛出来的是 pydantic_ai / openai / httpx 三类异常，它们各自的
字段（HTTP 状态码、上游 error.code / error.type、Retry-After）在往上抛的过程里被折成一句
str，最后只能以 code=INTERNAL 的形状给到前端 —— 状态码、上游错误码全丢，还看起来像我们
自己的 bug。这里把"异常 → 结构化字段"收成**纯函数**：

- classify(exc) 一次产出 A 契约的全部字段（不含 type）；
- SSE 走 error_frame(exc)（= {"type": "error", **字段}）；
- HTTP 信封走 ProviderError.to_dict() 里的 detail.provider（同一份字段）。

前端因此只有一处可读：kind 分类、http_status / provider_code 显示、retryable 决定要不要
给"重试"按钮。两处各拼一份正是本文件存在的原因，别在调用点再拼第二次。

约定：
- code / kind 一一对应，映射表就是 CODE_KINDS（C 契约），不许在别处新造；
- **绝不伪造**上游的 code/type：上游没给就是 None；
- http_status 只表示**上游**的 HTTP 状态；本地限额是 None（那次请求根本没发出去）；
- 本模块纯函数、无 IO、无全局可变状态；第三方包的 import 全在函数内并吞 ImportError
  （装不上包的环境不该因为这里炸掉）。

一处**有意保留的非一一对应**：我们自己的通用包装异常 ProviderError（code=PROVIDER_ERROR，
手里没有上游响应）保留它原有的 code、kind 记 unknown。改名会让已有日志与调用方的 code
断言失效，而它本来就没有可分类的信息。
"""

from __future__ import annotations

import json
import re
import socket
from typing import Any, Iterable, Mapping

#: 一条 error 帧 / 一个 detail.provider 的**字段集**（前后端契约，顺序无关但别增删）
FIELDS = ("code", "kind", "message", "http_status", "provider_code",
          "provider_type", "provider_message", "retryable", "retry_after",
          "model", "provider_label")

#: code → kind 的**唯一**映射（C 契约）。想加种类先改这里，并同步前端。
CODE_KINDS = {
    "LIMIT_EXCEEDED": "limit",                    # 本地额度（不是上游！）
    "PROVIDER_RATE_LIMIT": "rate_limit",          # 上游 429
    "PROVIDER_QUOTA": "quota",                    # 上游 402 / 欠费
    "PROVIDER_AUTH": "auth",                      # 401 / 403
    "PROVIDER_BAD_REQUEST": "bad_request",        # 400 / 404 / 422
    "PROVIDER_SERVER": "server",                  # 5xx / 529
    "PROVIDER_TIMEOUT": "timeout",                # 连接或读超时
    "PROVIDER_NETWORK": "network",                # 连不上 / DNS / 断网
    "PROVIDER_STREAM": "stream",                  # 流被截断
    "MODEL_NOT_CONFIGURED": "not_configured",     # 没配模型（沿用旧语义）
    "INTERNAL": "unknown",                        # 其它
}
KIND_CODES = {kind: code for code, kind in CODE_KINDS.items()}

#: 各 kind 默认是否值得重试。上游说"你参数错 / 密钥错 / 额度不足"时重试永远没用，
#: 而 429 / 5xx / 超时 / 断流是真的等一下就可能好 —— 这个判断直接决定前端给不给按钮。
KIND_RETRYABLE = {
    "limit": False, "rate_limit": True, "quota": False, "auth": False,
    "bad_request": False, "server": True, "timeout": True, "network": True,
    "stream": True, "not_configured": False, "unknown": False,
}

#: 上游 body 里的"额度"标记。只认 code/type，**不看 message** ——
#: 429 的提示语里常带 quota 字样（"Rate limit ... quota"），拿 message 判会把
#: "等一会儿就能好"读成"充钱才能好"。
QUOTA_MARKERS = ("insufficient_quota", "insufficient_balance", "insufficient_fund",
                 "quota", "billing", "arrears", "payment_required", "balance")

#: 消息上限：帧里的中文整句 300，上游原文 500（前端横幅会直接显示，太长等于日志）
MESSAGE_LIMIT = 300
PROVIDER_MESSAGE_LIMIT = 500

#: 常见供应商的显示名。取不到就原样回 provider id（绝不编造一个"某某云"）
PROVIDER_LABELS = {
    "deepseek": "DeepSeek", "openai": "OpenAI", "azure": "Azure OpenAI",
    "dashscope": "通义千问", "qwen": "通义千问", "aliyun": "通义千问",
    "moonshot": "Kimi", "zhipu": "智谱", "bigmodel": "智谱",
    "anthropic": "Anthropic", "claude": "Anthropic",
    "ollama": "Ollama", "vllm": "vLLM", "siliconflow": "SiliconFlow",
    "openrouter": "OpenRouter", "mineru": "MinerU", "mock": "Mock 模型",
    "local": "本地模型", "hash": "伪向量模型",
}

# 脱敏：命中即替换。**绝不泄露密钥**是硬约束，代价是上游原文里这些位置变成 ***
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]+")
_SK_RE = re.compile(r"(?i)\bsk[_-][A-Za-z0-9_\-]{4,}")
_KEY_VAL_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|authorization|"
    r"x-api-key|secret[_-]?key|secret)\b\s*([=:])\s*\"?([^\s\"',;&}\]]+)")


# --------------------------------------------------------------------------
# 字段组装
# --------------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def label_of(provider: str | None) -> str:
    """供应商显示名（provider_label 字段）。未知名原样返回。"""
    key = str(provider or "").strip()
    if not key:
        return ""
    return PROVIDER_LABELS.get(key.lower(), key)


def redact(text: Any, secrets: Iterable[str] = ()) -> str:
    """把密钥从任意文本里抹掉（sk-xxx / Bearer xxx / api_key=xxx / 真实 key 原文）。

    secrets 是当次调用**真的用了**的那些 key（能拿到就传：模式匹配兜不住自定义格式）。
    先替换已知原文再跑模式，否则 "sk-abc123" 会被模式切成 "sk-***" 而留下残片。
    """
    out = str(text or "")
    if not out:
        return ""
    for secret in secrets or ():
        secret = str(secret or "")
        if len(secret) >= 4:
            out = out.replace(secret, "***")
    out = _SK_RE.sub("sk-***", out)
    out = _BEARER_RE.sub(r"\1 ***", out)
    out = _KEY_VAL_RE.sub(r"\1\2***", out)
    return out


def fields(*, code: str, message: str, kind: str | None = None,
           http_status: Any = None, provider_code: Any = None,
           provider_type: Any = None, provider_message: Any = None,
           retryable: bool | None = None, retry_after: Any = None,
           model: str = "", provider_label: str = "",
           secrets: Iterable[str] = ()) -> dict:
    """组装一条结构化错误（A 契约去掉 type）。

    kind 由 code 决定（映射表是唯一真相）；只有 code 不在表里的历史 code
    （HOST_ERROR / AGENT_DOWN / EMPTY_ANSWER …）才用调用方给的 kind，默认 unknown ——
    它们语义不变，只是补上了字段。
    """
    kind = CODE_KINDS.get(code, kind or "unknown")
    if kind == "limit":
        # 本地限额没有上游：这几个字段必须是 None，留着上游值就是把两种 429 混成一种
        http_status = provider_code = provider_type = provider_message = None
    if retryable is None:
        retryable = KIND_RETRYABLE.get(kind, False)
    return {
        "code": code,
        "kind": kind,
        "message": _clip(redact(message, secrets), MESSAGE_LIMIT),
        "http_status": _int_or_none(http_status),
        "provider_code": _str_or_none(provider_code),
        "provider_type": _str_or_none(provider_type),
        "provider_message": (_clip(redact(provider_message, secrets), PROVIDER_MESSAGE_LIMIT)
                             if provider_message else None),
        "retryable": bool(retryable),
        "retry_after": _float_or_none(retry_after),
        "model": str(model or ""),
        "provider_label": str(provider_label or ""),
    }


def frame(payload: dict) -> dict:
    """把字段包成一条 SSE 终态 error 事件体。"""
    return {"type": "error", **payload}


def error_frame(exc: BaseException, **kwargs) -> dict:
    """异常 → SSE error 事件体（{"type": "error", **classify(exc)}）。"""
    return frame(classify(exc, **kwargs))


# --------------------------------------------------------------------------
# 上游 body 解析
# --------------------------------------------------------------------------
def _snippet(text: str, limit: int = PROVIDER_MESSAGE_LIMIT) -> str:
    return _clip(str(text or "").strip(), limit)


def _json_snippet(value: Any) -> str:
    try:
        return _snippet(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return _snippet(str(value))


def _first(obj: Mapping, *keys: str) -> Any:
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_body(body: Any) -> dict:
    """上游 body → {"code", "type", "message"}（原样，不翻译、不编造）。

    覆盖的形状（都是真实见过的）：
    OpenAI 兼容 {"error": {"message", "type", "code", "param"}}、
    {"error": "字符串"}、DashScope {"code", "message"}、只有 {"message"}、
    我们自己的 {"detail": ...}、body 是 str 且不是 JSON（截片段）、
    None / 空 / list（不猜，全 None）。
    """
    return _parse_body(body, 0)


def _parse_body(body: Any, depth: int) -> dict:
    empty = {"code": None, "type": None, "message": None}
    if body is None or depth > 3:
        return empty
    if isinstance(body, (bytes, bytearray)):
        return _parse_body(bytes(body).decode("utf-8", "ignore"), depth + 1)
    if isinstance(body, str):
        raw = body.strip()
        if not raw:
            return empty
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            # 不是 JSON：上游常直接回一段 HTML / 纯文本错误页，留片段比留空有用
            return {**empty, "message": _snippet(raw)}
        if isinstance(data, (Mapping, list)):
            return _parse_body(data, depth + 1)
        return {**empty, "message": _snippet(raw)}
    if isinstance(body, (list, tuple)):
        # list 的形状没有共识（可能是 [{"error":…}]，也可能是一串无关对象）：
        # 猜错就是把别人的话当上游错误码，宁可为空
        return empty
    if not isinstance(body, Mapping):
        return {**empty, "message": _snippet(str(body))}
    if not body:
        # 空对象 = 上游什么都没说：编一句"未知错误"就是把 None 变成假信息
        return empty

    code = type_ = message = None
    err = body.get("error")
    if isinstance(err, Mapping):
        code = _first(err, "code", "type", "error_code")
        type_ = _first(err, "type")
        message = _first(err, "message", "msg", "error_description")
    elif isinstance(err, str):
        message = err
    detail = body.get("detail")
    if message is None and detail is not None:
        if isinstance(detail, Mapping):
            code = code or _first(detail, "code", "type")
            type_ = type_ or _first(detail, "type")
            message = _first(detail, "message", "msg")
            nested = detail.get("error")
            if message is None and isinstance(nested, str):
                message = nested
            elif isinstance(nested, Mapping):
                code = code or _first(nested, "code", "type")
                type_ = type_ or _first(nested, "type")
                message = _first(nested, "message", "msg", "error_description")
        elif isinstance(detail, str):
            message = detail
    code = code or _first(body, "code", "error_code", "errcode")
    type_ = type_ or _first(body, "type")
    message = message or _first(body, "message", "msg", "error_msg", "error_description")
    if message is None:
        # 一个字段都认不出来：整段留片段即可（排查用），不硬塞一种"标准形状"
        message = _json_snippet(body) or None
    return {"code": _str_or_none(code), "type": _str_or_none(type_),
            "message": _snippet(message) if message else None}


# --------------------------------------------------------------------------
# 异常 → 分类
# --------------------------------------------------------------------------
def _chain(exc: BaseException, depth: int = 6) -> list[BaseException]:
    """异常因果链（外层 → 内层）。

    供应商适配层普遍是 raise ProviderError("..."[:200]) from exc：真正的状态码与上游
    错误码在 __cause__ 上，只看最外层等于什么都没看。
    """
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and len(out) < depth and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        nxt = cur.__cause__
        if nxt is None and not cur.__suppress_context__:
            nxt = cur.__context__
        cur = nxt
    return out


def _headers_of(exc: BaseException) -> Any:
    headers = getattr(exc, "headers", None)
    if headers is not None:
        return headers
    return getattr(getattr(exc, "response", None), "headers", None)


def _response_body(exc: BaseException) -> Any:
    """没带 body 的错误（httpx / 不带 body 的构造）才去读响应体。"""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        text = getattr(resp, "text", None)
    except Exception:  # noqa: BLE001 —— 流式响应读 .text 会抛，读不到就当没有
        return None
    return _snippet(text) if text else None


def _empty_hit(kind: str | None) -> dict:
    return {"kind": kind, "status": None, "body": None, "headers": None, "model_name": ""}


def _from_status_error(exc: BaseException) -> dict:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    body = getattr(exc, "body", None)
    return {"kind": None, "status": status,
            "body": body if body is not None else _response_body(exc),
            "headers": _headers_of(exc), "model_name": ""}


def _from_model_http(exc: BaseException) -> dict:
    return {"kind": None, "status": getattr(exc, "status_code", None),
            "body": getattr(exc, "body", None),
            "headers": getattr(exc, "headers", None),
            "model_name": str(getattr(exc, "model_name", "") or "")}


def _rules() -> list[tuple[type, str | None, Any]]:
    """（类型, 固定 kind 或 None=按状态码判, 字段提取器）。顺序即优先级。

    ConnectTimeout 同时是超时与网络错误 → 先判超时（"等一下"比"检查网络"更准）；
    RemoteProtocolError 是 TransportError 的子类 → 先判"流被截断"（重试就能好）。
    """
    rules: list[tuple[type, str | None, Any]] = []
    try:
        import httpx
    except ImportError:
        httpx = None  # noqa: N806 —— 缺包就不装规则，内建异常照样能分类
    if httpx is not None:
        rules += [
            (httpx.TimeoutException, "timeout", _empty_hit),
            (httpx.RemoteProtocolError, "stream", _empty_hit),
            (httpx.ProtocolError, "stream", _empty_hit),
            (httpx.HTTPStatusError, None, _from_status_error),
            (httpx.TransportError, "network", _empty_hit),
        ]
    try:
        import openai
    except ImportError:
        openai = None  # noqa: N806
    if openai is not None:
        rules += [
            (openai.APITimeoutError, "timeout", _empty_hit),
            (openai.APIConnectionError, "network", _empty_hit),
            (openai.APIStatusError, None, _from_status_error),
        ]
    try:
        from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
    except ImportError:
        pass
    else:
        rules += [
            (UnexpectedModelBehavior, "stream", _empty_hit),
            (ModelHTTPError, None, _from_model_http),
        ]
    return rules


def _kind_of_status(status: int | None, parsed: dict) -> tuple[str, str]:
    """HTTP 状态码 + 上游 code/type → (code, kind)。"""
    blob = f"{parsed.get('code') or ''} {parsed.get('type') or ''}".lower()
    if any(marker in blob for marker in QUOTA_MARKERS):
        return KIND_CODES["quota"], "quota"
    if status in (401, 403):
        return KIND_CODES["auth"], "auth"
    if status == 402:
        return KIND_CODES["quota"], "quota"
    if status == 429:
        return KIND_CODES["rate_limit"], "rate_limit"
    if status in (400, 404, 422):
        return KIND_CODES["bad_request"], "bad_request"
    if isinstance(status, int) and 500 <= status <= 599:
        return KIND_CODES["server"], "server"
    if isinstance(status, int) and 400 <= status <= 499:
        # 405 / 409 / 413 之类：仍是"这次请求不合法"，只是没那么常见
        return KIND_CODES["bad_request"], "bad_request"
    return KIND_CODES["unknown"], "unknown"


def kind_of_status(status: Any) -> str:
    """只有 HTTP 状态码、没有上游 body 时的分类。

    bridge 转发 agent 进程的 HTTP 错误用得着它：那一层只拿到状态码，
    但前端要显示的 kind 必须与 provider_errors 的分类一致，不能各判一套。
    """
    return _kind_of_status(_int_or_none(status), {"code": None, "type": None})[1]


def _template(kind: str, label: str, status: int | None) -> str:
    http = f"HTTP {status}" if status else "连接层"
    return {
        "rate_limit": f"{label} 限流了（{http}）：请求过于频繁，稍等一会儿再试。",
        "quota": f"{label} 说额度或余额不足（{http}）：请到厂商后台充值，或换一条模型配置。",
        "auth": f"{label} 不认这把密钥（{http}）：请到「设置 · 模型」检查 API Key 与地址。",
        "bad_request": f"{label} 说这次请求不合法（{http}）：参数、模型名或上下文长度不对。",
        "server": f"{label} 服务端出错（{http}）：这是对方的问题，稍后重试。",
        "timeout": f"等 {label} 的响应超时了：网络慢或对方没回，稍后重试。",
        "network": f"连不上 {label}：网络断了、域名解析不了或连接被拒。",
        "stream": f"{label} 的响应流被提前截断：连接中途断了，重试这一轮通常就好。",
    }.get(kind, f"{label} 调用失败。")


def _retry_after_of(headers: Any) -> float | None:
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # noqa: BLE001 —— headers 不是 Mapping 就当没有
        return None
    # 只认秒数：HTTP-date 换算成秒对前端没有意义（它要的是"等多久"）
    return _float_or_none(str(raw).strip()) if raw is not None else None


def classify(exc: BaseException, *, model: str = "", provider_label: str = "",
             secrets: Iterable[str] = (), retry_after: float | None = None) -> dict:
    """异常 → A 契约字段（**唯一**的分类入口）。"""
    from backend.core.errors import LimitExceededError, ProviderError  # 延迟：errors 依赖本模块

    secrets = tuple(str(s) for s in (secrets or ()) if s)
    label = provider_label or "上游"

    if isinstance(exc, LimitExceededError):
        # **本地**限额：请求根本没发出去，http_status / provider_* 一律 None，
        # code 仍是 LIMIT_EXCEEDED —— 与上游 429 严格分开
        return fields(code="LIMIT_EXCEEDED", kind="limit", message=exc.message,
                      retryable=False, model=model, provider_label=provider_label,
                      secrets=secrets)

    for node in _chain(exc):
        for cls, fixed_kind, extract in _rules():
            if not isinstance(node, cls):
                continue
            hit = _empty_hit(fixed_kind) if extract is _empty_hit else extract(node)
            parsed = parse_body(hit.get("body"))
            parsed_kind = fixed_kind
            if parsed_kind:
                code, kind = KIND_CODES[parsed_kind], parsed_kind
            else:
                code, kind = _kind_of_status(hit.get("status"), parsed)
            if kind == "unknown":
                break  # 认得异常类型但认不出状态码：往因果链里找更有信息的那一层
            return fields(
                code=code, kind=kind,
                message=_template(kind, label, hit.get("status")),
                http_status=hit.get("status"),
                provider_code=parsed["code"], provider_type=parsed["type"],
                provider_message=parsed["message"],
                retryable=KIND_RETRYABLE.get(kind, False),
                retry_after=(retry_after if retry_after is not None
                             else _retry_after_of(hit.get("headers"))),
                model=model or hit.get("model_name") or "",
                provider_label=provider_label, secrets=secrets)

    if isinstance(exc, TimeoutError):
        return fields(code=KIND_CODES["timeout"], kind="timeout",
                      message=_template("timeout", label, None), retryable=True,
                      model=model, provider_label=provider_label, secrets=secrets)
    if isinstance(exc, (ConnectionError, socket.gaierror)):
        return fields(code=KIND_CODES["network"], kind="network",
                      message=_template("network", label, None), retryable=True,
                      model=model, provider_label=provider_label, secrets=secrets)

    if isinstance(exc, ProviderError):
        code = exc.code or "PROVIDER_ERROR"
        if code == "RATE_LIMITED":      # 我们自己的 429 包装：语义就是上游限流
            code = KIND_CODES["rate_limit"]
        kind = CODE_KINDS.get(code, "unknown")
        return fields(code=code, kind=kind, message=exc.message,
                      retryable=bool(getattr(exc, "retryable", False)),
                      retry_after=(retry_after if retry_after is not None
                                   else getattr(exc, "retry_after", None)),
                      model=model, provider_label=provider_label, secrets=secrets)

    return fields(code="INTERNAL", kind="unknown",
                  message=f"{type(exc).__name__}: {exc}", retryable=False,
                  model=model, provider_label=provider_label, secrets=secrets)
