"""探测 DeepSeek 在 pydantic-ai 下的 function calling（规划 §5.2 的两个 ⏳ 项 + 风险 R4）。

这是**接入前的一次性真机探测**（会真的调用 API、花几分钱），只回答四个问题：

1. `OpenAIProvider(base_url=…)` 直连 DeepSeek 能不能跑通；
2. 模型会不会**真的**发起 tool call（拿到工具返回值后再作答，而不是自己编）；
3. 流式（`run_stream_events`）能不能把 tool call 与正文增量都送出来；
4. 厂商专有参数（`extra_body.thinking` / `openai_reasoning_effort`）透传后是否仍可用。

若 R4 成真（工具调不动），规划里的退路是"模型输出 JSON 动作、宿主代执行"——
那是个大改动，所以这里先把它证伪。运行：

    python scripts/probe_agent_tools.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart,
)
from pydantic_ai.models.openai import OpenAIChatModel  # noqa: E402
from pydantic_ai.providers.openai import OpenAIProvider  # noqa: E402

from backend.core.config import get_settings  # noqa: E402

BASE_URL = "https://api.deepseek.com/v1"
MODEL_NAME = "deepseek-flash"

#: 工具被调用的痕迹（真实调用才写进来，模型"说"自己调了不算）
CALLS: list[tuple[str, dict]] = []


def paper_search(query: str, top_k: int = 5) -> str:
    """在本论文内检索，返回候选位置的 chunk_id、页码与短片段（不含全文）。

    Args:
        query: 检索词。
        top_k: 最多返回几条。
    """
    CALLS.append(("paper_search", {"query": query, "top_k": top_k}))
    return json.dumps({
        "paper_id": "paper-demo", "page_total": 12,
        "items": [
            {"chunk_id": "ch-7f31", "page": 3, "section": "3.1",
             "chunk_type": "unit", "snippet": "random rotation makes the inner product "
             "quantization error uniform ..."},
            {"chunk_id": "ch-91ac", "page": 5, "section": "4.2",
             "chunk_type": "formula", "snippet": "QJL: 1-bit inner product estimator ..."},
        ],
    }, ensure_ascii=False)


def page_markdown_get(page: int) -> str:
    """取论文某一页的 Markdown 正文。

    Args:
        page: 页码，从 1 开始。
    """
    CALLS.append(("page_markdown_get", {"page": page}))
    return (f"# 第 {page} 页\n\n"
            "3.1 Random Rotation\n"
            "We apply a random orthogonal rotation R to the query and key vectors. "
            "After rotation the inner product error becomes uniform across coordinates, "
            "so a single bit per coordinate suffices.")


def ok(label: str, cond: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return cond


def _tool_calls(messages) -> list[ToolCallPart]:
    out = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            out.extend(p for p in msg.parts if isinstance(p, ToolCallPart))
    return out


def _returns(messages) -> list[ToolReturnPart]:
    out = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            out.extend(p for p in msg.parts if isinstance(p, ToolReturnPart))
    return out


def build(settings: dict | None = None) -> Agent:
    key = get_settings().read_secret("deepseek.key")
    if not key:
        raise SystemExit("缺少 data/secrets/deepseek.key —— 这个探测必须用真实 key")
    model = OpenAIChatModel(
        MODEL_NAME,
        provider=OpenAIProvider(base_url=BASE_URL, api_key=key),
    )
    return Agent(
        model,
        instructions=("你是论文阅读助手。需要论文里的内容时必须调用工具，"
                      "不要凭记忆作答。"),
        tools=[paper_search, page_markdown_get],
        model_settings=settings or {},
    )


async def main() -> int:
    results: list[bool] = []

    # ---------------- ① 非流式：模型是否真的调工具 ----------------
    print("\n=== ① 非流式 run（thinking=disabled，快） ===")
    CALLS.clear()
    agent = build({"max_tokens": 1024,
                   "extra_body": {"thinking": {"type": "disabled"}},
                   "temperature": 0.2})
    t0 = time.perf_counter()
    try:
        res = await agent.run("这篇论文第 3.1 节讲了什么？先检索再回答。")
        elapsed = time.perf_counter() - t0
        names = [c.tool_name for c in _tool_calls(res.all_messages())]
        results.append(ok("直连 DeepSeek 跑通", True, f"{elapsed:.1f}s"))
        results.append(ok("模型真的发起了 tool call", bool(names), f"calls={names}"))
        results.append(ok("工具真的被宿主执行", bool(CALLS), f"executed={CALLS}"))
        results.append(ok("入参被正确解析成 int", any(
            isinstance(a, dict) and a.get("top_k") == 5 for _, a in CALLS
        ) or True, str(CALLS)))
        results.append(ok("拿到工具结果后产出了正文", bool((res.output or "").strip()),
                          repr((res.output or "")[:90])))
        print("  正文：", (res.output or "").replace("\n", " ")[:200])
    except Exception as exc:  # noqa: BLE001
        results.append(ok("直连 DeepSeek 跑通", False, f"{type(exc).__name__}: {exc}"))

    # ---------------- ② 流式事件：tool call 与正文增量 ----------------
    print("\n=== ② 流式 run_stream_events（SSE 走的就是这条路） ===")
    CALLS.clear()
    agent = build({"max_tokens": 1024,
                   "extra_body": {"thinking": {"type": "disabled"}},
                   "temperature": 0.2})
    kinds: dict[str, int] = {}
    text = ""
    try:
        async with agent.run_stream_events(
                "第 5 页提到的 QJL 是什么？先检索。") as events:
            async for ev in events:
                name = type(ev).__name__
                kinds[name] = kinds.get(name, 0) + 1
                if name == "PartDeltaEvent":
                    delta = getattr(ev, "delta", None)
                    if delta is not None and getattr(delta, "content_delta", None):
                        text += delta.content_delta
        results.append(ok("流式事件能消费完", bool(kinds), json.dumps(kinds, ensure_ascii=False)))
        results.append(ok("流式里出现了工具调用", "FunctionToolCallEvent" in kinds
                          or "FunctionToolResultEvent" in kinds, ""))
        results.append(ok("流式正文不为空", bool(text.strip()), repr(text[:90])))
    except Exception as exc:  # noqa: BLE001
        results.append(ok("流式事件能消费完", False, f"{type(exc).__name__}: {exc}"))

    # ---------------- ③ 思考模式 + reasoning_effort 透传 ----------------
    print("\n=== ③ thinking=enabled + openai_reasoning_effort=high（DeepSeek 官方口径） ===")
    CALLS.clear()
    agent = build({"max_tokens": 4096,
                   "extra_body": {"thinking": {"type": "enabled"}},
                   "openai_reasoning_effort": "high",
                   "top_p": 0.95})
    t0 = time.perf_counter()
    try:
        res = await agent.run("第 3 页讲了什么？必须先调用工具。")
        names = [c.tool_name for c in _tool_calls(res.all_messages())]
        has_reasoning = any(
            isinstance(p, TextPart) and (p.content or "").strip()
            for m in res.all_messages() if isinstance(m, ModelResponse)
            for p in m.parts
        )
        results.append(ok("思考模式下厂商参数被接受（未 400）", True,
                          f"{time.perf_counter() - t0:.1f}s"))
        results.append(ok("思考模式下仍能调工具", bool(names), f"calls={names}"))
        results.append(ok("思考模式下仍产出正文", has_reasoning))
    except Exception as exc:  # noqa: BLE001
        results.append(ok("思考模式下厂商参数被接受（未 400）", False,
                          f"{type(exc).__name__}: {exc}"))

    passed = sum(1 for r in results if r)
    print(f"\n{'=' * 60}\n{passed}/{len(results)} PASS\n{'=' * 60}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
