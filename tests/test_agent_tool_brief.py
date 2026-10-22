r"""过程行上的**一句话人话**：`修订产物 · 第 4 页 · 第 4 行 → 「\frac{\sqrt{3}\,\pi^2}」`。

线上实测 2026-12-07（用户报「工具调用看不出在干什么」）：过程行显示的是
``{"find": "\\frac{\\sqrt{3\\pi^2}...`` —— 框架给的入参是 JSON **串**，老代码直接
`str(args)[:120]`；跑完之后连这一截也没了，只剩「取公式 ✓ 0.6 秒」。

两条口径：

1. `digest`（参数）与 `detail`（结果）都由**工具层**写人话 —— 只有它知道参数叫什么、截到哪儿
   才不像乱码；中文短语（`修订产物`）仍在前端。
2. 结果里 `summary` 优先于 `note`：`note` 是给模型看的长解释，过程行要的是「第 4 页 · 第 4 行」。
"""
from __future__ import annotations

import json

import pytest

from backend.agent.runtime import _tool_outcome
from backend.agent.tools import tool_brief


@pytest.mark.parametrize("args", [
    '{"pages": "4,7"}',            # 框架实测就是这种 JSON 串
    {"pages": "4,7"},
])
def test_args_may_arrive_as_json_string_or_dict(args) -> None:
    assert tool_brief("page_markdown_get", args) == "第 4,7 页"


def test_pages_all_is_spelled_out() -> None:
    assert tool_brief("translate_get", {"pages": "all"}) == "整篇"


def test_edit_replace_shows_what_replaces_what() -> None:
    brief = tool_brief("page_markdown_edit", {
        "op": "replace", "page": 4,
        "find": r"\frac{\sqrt{3\pi^2} \|\boldsymbol{y}\|_2^2}{d}",
        "replace": r"\frac{\sqrt{3}\,\pi^2 \|\boldsymbol{y}\|_2^2}{d}",
    })
    assert brief.startswith("第 4 页 · ")
    assert "→" in brief and "\\frac" in brief


def test_edit_view_and_undo_stay_short() -> None:
    assert tool_brief("page_markdown_edit", {"op": "view", "page": 4}) == "第 4 页"
    assert tool_brief("page_markdown_edit", {"op": "undo", "page": 4}) == "第 4 页 · 撤回上一处"


def test_delete_has_no_arrow() -> None:
    brief = tool_brief("page_markdown_edit", {"op": "replace", "page": 4, "find": "旧的"})
    assert "删掉「旧的」" in brief and "→" not in brief


def test_highlight_brief_names_the_anchor() -> None:
    assert tool_brief("highlight_manage", {"op": "create", "anchor": "md:p5"}) == "新增高亮 · 第 5 页"
    assert tool_brief("highlight_manage", {"op": "delete", "highlight_id": "h-1"}) == "删掉一条高亮"


def test_search_brief_quotes_the_query() -> None:
    assert tool_brief("paper_search", {"query": "随机旋转 量化", "top_k": 8}) == "「随机旋转 量化」"
    assert tool_brief("figure_image_get", {}) == ""            # 有这工具但没给页 → 空


def test_unknown_tool_falls_back_to_key_value_pairs() -> None:
    assert tool_brief("some_new_tool", {"alpha": 1, "beta": "x"}) == "alpha=1、beta=x"


def test_no_args_is_empty_not_a_crash() -> None:
    assert tool_brief("page_markdown_get", None) == ""
    assert tool_brief("page_markdown_edit", "{oops") == ""


class _Part:
    """`FunctionToolResultEvent.part` 的最小替身。"""

    def __init__(self, content, is_error: bool = False):
        self.content = content
        self.is_error = is_error


def test_summary_beats_the_long_note() -> None:
    """`note` 是给模型看的长解释（「已落盘。这一页的阅读器内容…」），过程行要的是 summary。"""
    payload = {"ok": True, "summary": "第 4 页 · 第 4 行 → 「新」",
               "note": "已落盘。这一页的阅读器内容与检索索引都已更新…"}
    ok, detail = _tool_outcome(_Part(json.dumps(payload, ensure_ascii=False)))
    assert ok is True and detail == "第 4 页 · 第 4 行 → 「新」"


def test_success_without_summary_keeps_the_row_quiet() -> None:
    """读工具的成功返回是一大段 JSON：塞进过程行只是噪音（以前它被压成 120 字垃圾）。"""
    ok, detail = _tool_outcome(_Part(json.dumps({"pages": [{"page": 5, "blocks": []}]})))
    assert ok is True and detail == ""


def test_failure_still_carries_the_reason() -> None:
    ok, detail = _tool_outcome(_Part(json.dumps({"ok": False, "error": "没有第 99 页（这篇共 22 页）。"})))
    assert ok is False and "第 99 页" in detail