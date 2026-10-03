"""发给模型的历史怎么组装（**纯函数，不连库、不调模型**）。

为什么值得单独一个文件：这里出过的两个问题都是"静默丢东西"——库里明明有、模型却看不到，
而所有自检只断言"库里存了什么"（`agent_check` 的"助手消息带引用 + 工具轨迹"就是查库），
于是丢在路上的部分一直没人发现：

1. `role="summary"`（压缩摘要）被 `_history` 的 user/assistant 两支漏掉 → 压缩之后模型记忆断点；
2. 助手历史只带正文，不带 `citations` / `tool_trace` → 模型看到自己上一轮的 `[1]…[17]`
   查不到出处，判定"这些编号是我编的"，于是不认上面的对话。
"""

from __future__ import annotations

import pytest

from backend.services.agent.conversations import history_from_chain

pytest.importorskip("pydantic_ai")

from backend.agent.runtime import SUMMARY_LABEL, AgentRuntime  # noqa: E402


class _Req:
    """只带 `history` 的最小请求（`_history` 只看这一个字段）。"""

    def __init__(self, history: list[dict]) -> None:
        self.history = history


def _cites() -> list[dict]:
    return [
        {"ref": 1, "page": 3, "label": "第 3 页 · caption", "snippet": "Figure 1: The Transformer"},
        {"ref": 2, "page": 4, "label": "第 4 页 · 段落", "snippet": "Instead of performing a single"},
    ]


def test_summary_reaches_the_model() -> None:
    """压缩摘要必须真的进模型：以前它在这条路上被静默丢掉。"""
    chain = [
        {"role": "summary", "content": "【摘要】用户先问 X，又问 Y，结论是 Z。"},
        {"role": "user", "content": "那 Z 的公式是什么？"},
        {"role": "assistant", "content": "Z 的公式是 [1]。", "citations": [], "tool_trace": []},
    ]
    history = history_from_chain(chain)
    assert [m["role"] for m in history] == ["summary", "user", "assistant"]

    msgs = AgentRuntime._history(_Req(history))
    parts = [p for m in msgs for p in m.parts]
    kinds = [type(p).__name__ for p in msgs[0].parts]
    assert kinds == ["SystemPromptPart"], "摘要要走 system 侧（不是用户说的话）"
    joined = "\n".join(getattr(p, "content", "") for p in parts)
    assert "结论是 Z" in joined, "摘要正文必须原样到达"
    assert joined.startswith(SUMMARY_LABEL), "摘要要有抬头，模型才知道那是更早对话的要点"
    assert [type(m).__name__ for m in msgs] == ["ModelRequest", "ModelRequest", "ModelResponse"]


def test_assistant_history_carries_material_index() -> None:
    """助手的旧回答后面要跟"那一轮用到了什么材料"（编号才有出处）。"""
    chain = [
        {"role": "user", "content": "结合图片讲一下架构图。"},
        {"role": "assistant", "content": "见图 1[1]，注意力部分见[2]。",
         "citations": _cites(),
         "tool_trace": [{"tool": "__presearch__"}, {"tool": "figure_list"},
                        {"tool": "figure_list"}, {"tool": "page_markdown_get"}]},
        {"role": "user", "content": "那多头注意力呢？"},
    ]
    history = history_from_chain(chain)
    body = history[1]["content"]
    assert body.startswith("见图 1[1]，注意力部分见[2]。"), "正文一个字都不许改"
    assert "[1] 第 3 页 · caption：Figure 1: The Transformer" in body
    assert "[2] 第 4 页 · 段落：Instead of performing a single" in body
    assert "figure_list×2" in body and "page_markdown_get" in body
    assert "__presearch__" not in body, "内部标记（__presearch__ 这种）不进历史"
    # 没有材料的两条不许凭空长尾巴
    assert history[0]["content"] == "结合图片讲一下架构图。"
    assert history[2]["content"] == "那多头注意力呢？"


def test_provenance_is_deterministic_and_bounded() -> None:
    """同一行库数据必须拼出**同样的字节**（否则前缀缓存每轮都会被自己打掉），且条目有上限。"""
    item = {"role": "assistant", "content": "答。", "citations": _cites(), "tool_trace": []}
    once = history_from_chain([item, {"role": "user", "content": "q"}])[0]["content"]
    twice = history_from_chain([item, {"role": "user", "content": "q"}])[0]["content"]
    assert once == twice

    many = [{"ref": i, "page": i, "label": f"第 {i} 页 · 段落", "snippet": "x"} for i in range(1, 41)]
    long_item = {"role": "assistant", "content": "答。", "citations": many, "tool_trace": []}
    body = history_from_chain([long_item, {"role": "user", "content": "q"}])[0]["content"]
    assert "还有 20 条" in body
    assert len(body) < 2400, f"材料索引要截断，实际 {len(body)} 字"
