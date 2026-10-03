"""一轮问答的运行时依赖（pydantic-ai 的 deps 注入点）。

工具是**模块级函数**（pydantic-ai 靠函数签名生成工具 schema），一轮问答的可变状态
（当前论文、引用表、工具轨迹）因此只能挂在 deps 上。这样同一个 agent 实例可以并发
服务多个请求，不会互相串状态 —— 把 paper_id 存成模块全局是这类服务最典型的越权来源。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from backend.agent.citations import RefRegistry
from backend.agent.config import AgentConfig
from backend.agent.host import HostClient


@dataclass
class Deps:
    cfg: AgentConfig
    host: HostClient
    paper_id: str
    registry: RefRegistry
    page_total: int = 0
    title: str = ""
    #: 预注入的档案块（第一轮之前放进 system/instructions，§9.3）
    archive_text: str = ""
    #: 本轮检索的低相关度提醒（**软信号**：只告诉模型"这批材料可能不相关"，
    #: 不替它决定"有没有依据" —— 阈值实测会误伤，见 `config.agent_evidence_floor`）
    evidence_note: str = ""
    #: 划词解释：用户选中的原文 + 它对应的 ref（ref 用来让模型自己展开上下文）
    selection: str = ""
    #: 本轮用户上传的图片张数 / 其中真正随消息发出去的那几张（见 `prompts.IMAGE_NOTE`）
    attachments: int = 0
    attachments_shown: int = 0
    selection_ref: int | None = None
    selection_page: int | None = None
    #: 用户对选区的提问（划词追问）；为空时是"解释这段内容"
    selection_question: str = ""
    #: 工具轨迹（审计 + 自检 A5/A13/A14 读它）
    trace: list[dict] = field(default_factory=list)
    #: 模型轮次（含工具轮）：超上限就停，见 §11 的 `rounds_exhausted`
    model_calls: int = 0
    #: 这一轮读过的页（自检用：验证"先检索定位、再读页"的顺序）
    pages_read: list[int] = field(default_factory=list)
    #: 归一化检索词 → 上一次的返回。模型会把同一句话问两遍（实测跨库问题连发 9 次
    #: `kb_search`，最后把轮数用满），重复的查询不必再打一次后端。
    query_cache: dict[str, dict] = field(default_factory=dict)
    #: 已用掉的检索预算（M7：与轮数预算**分开**扣，见 `Settings.agent_search_budget`）
    search_calls: int = 0
    #: 已用掉的**改动**次数（`page_markdown_edit`）。写比读更需要闸：写错了要人来收拾，
    #: 所以它有自己的预算（`Settings.agent_edit_budget`），与检索、轮数三者互不顶替。
    edit_calls: int = 0
    #: 本轮改过的产物（`done` 事件带给前端做提示，也是自检的断言口）
    edits: list[dict] = field(default_factory=list)
    #: 本轮**读过**其产物原文的 ``(页, 侧)``：`page_markdown_edit` 的"先读后写"判据
    viewed_sides: set[tuple[int, str]] = field(default_factory=set)

    def spend_search(self) -> bool:
        """扣一次检索预算；返回 ``False`` 表示已用尽（调用方直接拒绝，不打后端）。"""
        if self.search_calls >= self.cfg.search_budget:
            return False
        self.search_calls += 1
        return True

    def spend_edit(self) -> bool:
        """扣一次改动预算；返回 ``False`` 表示已用尽（调用方直接拒绝，不打后端）。"""
        if self.edit_calls >= self.cfg.edit_budget:
            return False
        self.edit_calls += 1
        return True

    @property
    def edit_exhausted(self) -> bool:
        return self.edit_calls >= self.cfg.edit_budget

    @property
    def search_exhausted(self) -> bool:
        return self.search_calls >= self.cfg.search_budget

    # ------------------------------------------------------------------
    def note_tool(self, name: str, args: dict, status: str,
                  started: float, detail: str = "") -> None:
        self.trace.append({
            "tool": name, "args": args, "status": status,
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "detail": detail[:200],
        })

    def note_model_call(self) -> int:
        self.model_calls += 1
        return self.model_calls

    @property
    def rounds_exhausted(self) -> bool:
        return self.model_calls >= self.cfg.max_rounds
