"""OpenAI 兼容生成 Provider（DeepSeek / 通义 / vLLM / Ollama 等，规划 §12.1）。

- 走 ``/chat/completions``，支持 ``response_format=json_object`` 的结构化输出；
- 429 / 5xx / 超时 → ``RateLimitError`` / ``ProviderError(retryable=True)``；
- 401 / 403 / 400 → 不可重试错误；
- 遵守 ``Retry-After`` 响应头（规划 §3.1 A3-3），交由调度器执行退避。
"""

from __future__ import annotations

import ast
import time
from typing import Any, Iterator

from backend.core.errors import ProviderError, RateLimitError
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads
from backend.providers.base import GenerationProvider, GenerationResult, ModelInfo

log = get_logger(__name__)


class OpenAICompatibleGeneration(GenerationProvider):
    def __init__(self, config: dict | None = None, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self._client = None

    # ------------------------------------------------------------------
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ProviderError(
                f"模型 {self.model_name or '(未命名)'} 缺少 API Key。"
                f"请在 data/secrets/ 下创建密钥文件（见 README）。",
                retryable=False,
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("未安装 openai 包：pip install openai",
                                retryable=False) from exc
        base_url = (self.config.get("base_url") or "").strip() or None
        timeout = float(self.config.get("timeout") or 120.0)
        self._client = OpenAI(api_key=self.api_key, base_url=base_url,
                              timeout=timeout, max_retries=0)
        return self._client

    def _params(self, **overrides: Any) -> dict:
        """组装请求参数。

        ``parameters`` 里可以放任意 OpenAI 兼容字段；厂商专有字段
        （DeepSeek 的 ``thinking`` / ``reasoning_effort``）由 ``_extra_body``
        单独处理，不混在标准参数里。本系统内部使用的键不转发给模型。
        """
        p = dict(self.config.get("parameters") or {})
        for internal in ("concurrency", "dim", "batch", "timeout",
                         "thinking", "reasoning_effort", "reasoning_levels",
                         "extra_body", "cache_prefix"):
            p.pop(internal, None)
        p.update({k: v for k, v in overrides.items() if v is not None})
        return self._apply_thinking_constraints(p)

    def _thinking_disabled(self) -> bool:
        cfg = dict(self.config.get("parameters") or {})
        body = dict(cfg.get("extra_body") or {})
        if (body.get("thinking") or {}).get("type") == "disabled":
            return True
        return cfg.get("thinking") == "disabled"

    def _apply_thinking_constraints(self, params: dict) -> dict:
        """思考模式下的参数约束（官方文档明确，传了不报错也不生效）。

        - 思考模式：``temperature`` / ``presence_penalty`` / ``frequency_penalty``
          **恒不生效**；``top_p`` 生效但下限 0.95。这里把无效参数剥掉，
          避免"设了 temperature=0.2 以为在控随机性"的错觉。
        - 非思考模式：``top_p`` 恒为 1.0，同样剥掉。
        """
        if self._thinking_disabled():
            params.pop("top_p", None)
            return params
        for ineffective in ("temperature", "presence_penalty", "frequency_penalty"):
            params.pop(ineffective, None)
        if "top_p" in params:
            try:
                params["top_p"] = max(0.95, min(1.0, float(params["top_p"])))
            except (TypeError, ValueError):
                params.pop("top_p", None)
        return params

    def _extra_body(self) -> dict:
        """厂商专有参数（DeepSeek 的思考模式开关等）。

        支持两种写法：
        - ``parameters.extra_body = {"thinking": {"type": "disabled"}}``（原样透传）
        - ``parameters.thinking = "disabled" | "enabled"``（简写）

        DeepSeek 的思考模式**默认开启**且 effort=high，思维链计入
        ``completion_tokens``；翻译这类任务应当关闭，否则 ``max_tokens``
        会被推理吃掉、``content`` 返回空串。
        """
        cfg = dict(self.config.get("parameters") or {})
        body: dict = dict(cfg.get("extra_body") or {})
        thinking = cfg.get("thinking")
        if thinking in ("enabled", "disabled") and "thinking" not in body:
            body["thinking"] = {"type": thinking}
        return body

    def _reasoning_effort(self) -> str | None:
        cfg = dict(self.config.get("parameters") or {})
        effort = cfg.get("reasoning_effort")
        return str(effort) if effort else None

    def _apply_vendor_params(self, params: dict) -> dict:
        extra_body = self._extra_body()
        if extra_body:
            params["extra_body"] = extra_body
        effort = self._reasoning_effort()
        if effort:
            params["reasoning_effort"] = effort
        return params

    # ------------------------------------------------------------------
    def generate(self, prompt: str, *, system: str | None = None,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> GenerationResult:
        client = self._ensure_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        params = self._params(temperature=temperature, max_tokens=max_tokens, **kwargs)
        params.setdefault("model", self.model_name)
        self._apply_vendor_params(params)

        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(messages=messages, **params)
        except Exception as exc:  # noqa: BLE001
            raise self._translate_error(exc) from exc
        latency = (time.perf_counter() - t0) * 1000
        return self._to_result(resp, latency, params=params)

    def generate_vision(self, prompt: str, images: list[bytes], *,
                        system: str | None = None,
                        max_tokens: int | None = None,
                        **kwargs: Any) -> GenerationResult:
        """带图生成（扫描件读页）：图片按 base64 data URL 放进 user 消息。

        DeepSeek 的多模态端点与文本端点同一个 ``/chat/completions``，
        只是消息体里多一个 ``image_url`` part（实测单图 ≤384 tokens）。
        """
        import base64

        client = self._ensure_client()
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for raw in images:
            b64 = base64.b64encode(raw).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        params = self._params(max_tokens=max_tokens, **kwargs)
        params.setdefault("model", self.model_name)
        self._apply_vendor_params(params)

        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(messages=messages, **params)
        except Exception as exc:  # noqa: BLE001
            raise self._translate_error(exc) from exc
        latency = (time.perf_counter() - t0) * 1000
        return self._to_result(resp, latency, params=params)

    def stream(self, prompt: str, *, system: str | None = None,
               **kwargs: Any) -> Iterator[str]:
        client = self._ensure_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        params = self._params(**kwargs)
        params.setdefault("model", self.model_name)
        params["stream"] = True
        self._apply_vendor_params(params)
        try:
            for chunk in client.chat.completions.create(messages=messages, **params):
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
        except Exception as exc:  # noqa: BLE001
            raise self._translate_error(exc) from exc

    def structured(self, prompt: str, schema: dict, *, system: str | None = None,
                   **kwargs: Any) -> dict:
        """要求返回 JSON object；解析失败时返回 ``{}`` 并记录告警。

        两个踩过的坑都修在这里：
        1. schema 必须用 ``json.dumps`` 展开 —— 直接 f-string 插 Python 字典会得到**单引号**，
           模型会照着抄成 ``{'venue': ''}``，``json.loads`` 当场失败（实测一次真实调用就中招）；
        2. 即便如此，仍用 ``ast.literal_eval`` 兜一层：模型偶尔交回 Python 风格的字典。
        """
        sys_prompt = (system or "") + (
            "\n\n只输出一个 JSON 对象，不要输出解释或 Markdown 代码块。"
            f"\nJSON 必须满足以下结构：{json_dumps(schema)}"
        )
        result = self.generate(prompt, system=sys_prompt.strip(), **kwargs)
        data = _loads_loose(_strip_code_fence(result.text))
        if not isinstance(data, dict):
            log.warning("结构化输出解析失败，原始文本前 200 字：%s", result.text[:200])
            return {}
        return data

    # ------------------------------------------------------------------
    def test(self) -> ModelInfo:
        """连通性探测。

        注意：**思考模式下不能用极小的 max_tokens** —— 思维链会先把预算吃光，
        正文为空（finish=length），探测会假失败。这里给足预算再问一个极短问题。
        """
        t0 = time.perf_counter()
        try:
            res = self.generate("回答一个词：ok", max_tokens=2048)
            return ModelInfo(ok=True, latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                             model_name=res.model or self.model_name,
                             detail=(res.text or "").strip()[:80] or "（空回复）")
        except ProviderError as exc:
            return ModelInfo(ok=False, detail=exc.message[:200])

    # ------------------------------------------------------------------
    def _to_result(self, resp: Any, latency_ms: float,
                   *, params: dict | None = None) -> GenerationResult:
        reasoning = ""
        try:
            choice = resp.choices[0]
            text = (choice.message.content or "") if choice.message else ""
            reasoning = getattr(choice.message, "reasoning_content", "") or ""
            finish = getattr(choice, "finish_reason", "") or ""
        except Exception:  # noqa: BLE001
            text, finish = "", ""
        usage = getattr(resp, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = int(getattr(details, "reasoning_tokens", 0) or 0)
        raw = {}
        if usage is not None:
            for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
                val = getattr(usage, key, None)
                if val is not None:
                    raw.setdefault("usage", {})[key] = int(val)
        result = GenerationResult(
            text=text,
            model=str(getattr(resp, "model", "") or self.model_name),
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            latency_ms=round(latency_ms, 1),
            finish_reason=finish,
            raw=raw,
        )
        result.reasoning_tokens = reasoning_tokens
        result.reasoning_content = reasoning
        # ---------------- 思考模式的两种"正文为空"----------------
        # 思考模式默认开启且思维链计入 completion_tokens，因此：
        #   (a) finish_reason=length：推理把 max_tokens 吃光，正文没来得及产出；
        #   (b) 推理正常结束但 content 仍为空（罕见）。
        # 两者都**必须显式失败**，绝不能把空串当成译文往上层传——
        # 上层拿到空串会写出空译文段，污染 Markdown 事实源。
        if not (text or "").strip():
            if reasoning_tokens or reasoning:
                # ⚠️ **不要**再写"请提高 max_tokens（建议 ≥8192）"：预算已经是**模型上限**
                # （`page_translate.MAX_OUTPUT_TOKENS` = 384K），那句话只会把人带偏
                # （用户 2026-10-08 就是照着它去找"思考模式的上限"）。
                # 两种形态要分清，处置也不同：
                #   (a) finish=length：思维链把预算吃光 —— 上限已经抬不动了，重试同样预算；
                #   (b) finish=stop：模型自己想完就停了、没写正文（**偶发**，实测同一份任务
                #       隔一分钟就能成功）—— 重试通常就好。
                # 两者都标 `retryable=True`：调度器/调用方重试一次是唯一可行的补救。
                budget = (params or {}).get("max_tokens")
                what = f"max_tokens={budget}" if budget else "未设 max_tokens"
                tail = ("预算被思维链吃光（已经是模型上限，抬不动了）"
                        if finish == "length" else
                        "这次是模型想完就停了、没写正文（偶发）")
                raise ProviderError(
                    f"模型只产出思维链（reasoning {reasoning_tokens} tokens，"
                    f"finish={finish or '?'}）而正文为空（本次 {what}）：{tail}。"
                    f"会自动重试；连续几次都这样就关掉思考模式"
                    f"（parameters.thinking=disabled）。",
                    retryable=True,
                )
            raise ProviderError(
                f"模型返回空正文（finish={finish or '?'}）", retryable=True)
        return result

    def _translate_error(self, exc: Exception) -> ProviderError:
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        detail = str(exc)[:400]
        retry_after = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            headers = getattr(resp, "headers", {}) or {}
            ra = headers.get("retry-after") or headers.get("Retry-After")
            if ra:
                try:
                    retry_after = float(ra)
                except (TypeError, ValueError):
                    retry_after = None
        if status == 429 or "RateLimit" in name:
            return RateLimitError(f"限流：{detail}", retry_after=retry_after)
        if status in (500, 502, 503, 504) or "Timeout" in name or "Connect" in name:
            return ProviderError(f"服务端/网络错误：{detail}", retryable=True,
                                 retry_after=retry_after)
        if status in (400, 401, 403, 404, 422):
            return ProviderError(f"请求被拒绝（{status}）：{detail}", retryable=False)
        return ProviderError(f"{name}: {detail}", retryable=True)


def _loads_loose(text: str) -> Any:
    """宽松 JSON 解析：先按 JSON 解，失败再用 ``ast.literal_eval`` 兜单引号/Python 字面量。"""
    data = json_loads(text, None)
    if data is not None:
        return data
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def _strip_code_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        return t[start:end + 1]
    return t
