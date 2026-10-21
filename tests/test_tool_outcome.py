"""过程轨迹的**实时反馈**：工具返回的成败与摘要要能跟着 SSE 出去。

用户口径（2026-12-06）「模型调用工具时，实时显示效果」：步骤行以前只有工具名 + 入参摘要 +
耗时，工具成功没有、改了什么，要等整轮回答出来才知道。`runtime._tool_outcome` 从工具返回体里
取 `ok` 与一句摘要，前端据此标 ✓/✗ 并把失败原因直接写在那一行上。
"""

from __future__ import annotations

import json

from backend.agent.runtime import _tool_outcome


class _Part:
    def __init__(self, content=None, is_error: bool = False) -> None:
        self.content = content
        self.is_error = is_error


def test_successful_write_reports_its_effect():
    ok, detail = _tool_outcome(_Part(json.dumps({"ok": True, "note": "已建立高亮"},
                                         ensure_ascii=False)))
    assert ok is True and detail == "已建立高亮"


def test_refusal_is_reported_as_failure_with_its_reason():
    """工具"拒绝"是**正常返回**（ok=false），所以要看返回体里的 ok，而不只看异常标记。"""
    ok, detail = _tool_outcome(_Part(json.dumps({"ok": False, "error": "这段文字找不到"},
                                                 ensure_ascii=False)))
    assert ok is False and "找不到" in detail


def test_exception_flag_wins():
    ok, detail = _tool_outcome(_Part("boom", is_error=True))
    assert ok is False and detail == "boom"


def test_plain_text_result_is_clipped():
    ok, detail = _tool_outcome(_Part("x" * 500))
    assert ok is True and len(detail) == 120


def test_missing_result_still_reports_success():
    ok, detail = _tool_outcome(_Part(None))
    assert ok is True and detail == ""
