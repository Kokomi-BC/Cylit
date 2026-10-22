"""过程轨迹（`agent/trace.py`）：多轮思考与工具调用**按发生顺序**排成一条流水。

为什么单独锁死它：这条流水是"这一轮它到底干了什么"的唯一形状 —— 前端照着它渲染
（思考 / 正文 / 工具夹杂）、`done` 整份下发、`run_json` 原样落库。它一旦串序或合并错，
界面上就是"思考跑到工具后面""正文被并进上一轮"这种说不清、又只有读回来才发现的错。

纯数据结构，零模型调用。
"""

from __future__ import annotations

from backend.agent.trace import MAX_STEPS, STEP_CHARS, StepLog


def test_steps_keep_the_order_they_happened() -> None:
    log = StepLog()
    log.stage("正在准备论文档案…")
    log.thinking("先看材料。", round=1)
    log.tool_start("paper_search", round=1, digest="query=随机旋转")
    log.tool_done("paper_search", round=1, ms=120)
    log.text("我先看一眼。", round=2)
    log.thinking("再翻翻书架。", round=2)
    log.tool_start("library_list", round=2, digest="query=")
    log.tool_done("library_list", round=2)
    log.text("结论是……", round=3)
    got = [(s["kind"], s.get("tool") or s.get("text")) for s in log.payload()["steps"]]
    assert got == [
        ("stage", "正在准备论文档案…"),
        ("thinking", "先看材料。"),
        ("tool", "paper_search"),
        ("text", "我先看一眼。"),
        ("thinking", "再翻翻书架。"),
        ("tool", "library_list"),
        ("text", "结论是……"),
    ]


def test_thinking_and_text_merge_within_a_round_only() -> None:
    """同一轮的相邻增量并成一条（一轮几百个块），换轮 / 换类就另起一条。"""
    log = StepLog()
    for piece in ("用户", "问的是", "核心贡献。"):
        log.thinking(piece, round=1)
    log.text("先答一句。", round=2)
    for piece in ("接着", "补充。"):
        log.text(piece, round=2)
    steps = log.payload()["steps"]
    assert [s["kind"] for s in steps] == ["thinking", "text"]
    assert steps[0]["text"] == "用户问的是核心贡献。"
    assert steps[1]["text"] == "先答一句。接着补充。"


def test_tool_step_records_ms_and_status() -> None:
    log = StepLog()
    log.tool_start("page_markdown_get", round=1, digest="pages=5")
    assert log.steps[-1]["status"] == "running" and log.steps[-1]["ms"] is None
    log.tool_done("page_markdown_get", round=1, ms=1234)
    step = log.payload()["steps"][0]
    assert step["status"] == "done" and step["ms"] == 1234
    assert "_t0" not in step                    # 内部计时键不得落库


def test_tool_done_without_start_still_appends() -> None:
    """框架事件顺序异常时宁可多一条，也不要丢一次工具调用。"""
    log = StepLog()
    log.tool_done("kb_search", round=3, ms=5)
    assert [s["kind"] for s in log.steps] == ["tool"]
    assert log.steps[0]["status"] == "done" and log.steps[0]["tool"] == "kb_search"


def test_long_thinking_is_capped_not_dropped() -> None:
    log = StepLog()
    log.thinking("字" * (STEP_CHARS + 100), round=1)
    step = log.payload()["steps"][0]
    assert len(step["text"]) <= STEP_CHARS + 1 and step.get("truncated") is True


def test_text_is_never_truncated() -> None:
    """正文段是**回答本体**：截了就是回答缺一截（用户报过"最终正文被截断"）；思考段才截。"""
    log = StepLog()
    body = "字" * (STEP_CHARS * 2)
    log.text(body, round=1)
    step = log.payload()["steps"][0]
    assert step["text"] == body and "truncated" not in step
    # 同一轮的后续增量继续并进同一条（不会被上限切断）
    log.text("尾巴。", round=1)
    assert log.payload()["steps"][0]["text"] == body + "尾巴。"


def test_step_cap_keeps_tool_skeleton() -> None:
    """步骤太多时先丢早期**非工具**步骤：工具调用是"它干了什么"的骨架。"""
    log = StepLog()
    for i in range(MAX_STEPS + 10):
        log.thinking(f"想第 {i} 轮。", round=i)
        log.tool_start("paper_search", round=i, digest=str(i))
        log.tool_done("paper_search", round=i, ms=1)
    steps = log.payload()["steps"]
    assert len(steps) <= MAX_STEPS
    assert all(s["kind"] == "tool" for s in steps[:2])


def test_payload_carries_elapsed_and_rounds() -> None:
    log = StepLog()
    log.thinking("想。", round=1)
    log.elapsed_ms = 1179000
    log.rounds = 4
    data = log.payload()
    assert data["elapsed_ms"] == 1179000 and data["rounds"] == 4
    assert log.tools == []


def test_stage_dedupes_consecutive_same_text() -> None:
    log = StepLog()
    log.stage("正在这篇论文里检索…")
    log.stage("正在这篇论文里检索…")
    log.stage("正在阅读并组织回答…")
    assert len(log.steps) == 2

def test_tool_step_keeps_outcome_for_history() -> None:
    """成败与一句话结果**必须落库**：只走 SSE 的话，翻回历史只剩工具名 + 毫秒数。"""
    log = StepLog()
    log.tool_start("page_markdown_edit", round=1, digest="第 4 页 · 「旧」→「新」")
    log.tool_done("page_markdown_edit", round=1, ms=1800, ok=True,
                  detail="第 4 页 · 第 4 行 → 「新」")
    step = log.payload()["steps"][0]
    assert step["ok"] is True
    assert step["detail"] == "第 4 页 · 第 4 行 → 「新」"
    assert step["digest"].startswith("第 4 页")
