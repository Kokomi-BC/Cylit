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
    #: 这条会话**冻结**的风格（主后端按 `conversation.agent_style` 取好带过来）。
    #: 设置页的改动只对新对话生效 —— 已开着的会话继续用它出生时那一份，
    #: 免得一次改动把历史会话的 `system + 全部历史` 前缀缓存全打掉。空 = 用 `prompts.DEFAULT_STYLE`。
    style: str = ""
    #: 是否**本次对话的第一条消息**（判据 = 主后端没给历史）。只有首轮才附论文摘要。
    first_turn: bool = False
    #: 论文摘要原文 / 中文译文：**首轮**拼进用户消息当背景（`prompts.abstract_block`）。
    #: 它不是可引用的材料（没有 [ref]），规则常驻在 system 的 `ABSTRACT_RULES` 里。
    abstract_en: str = ""
    abstract_zh: str = ""
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
    #: 本轮检索次数（**纯记账**：检索预算 2026-11-26 已取消，只剩统计与自检在读它）
    search_calls: int = 0
    #: 本轮改动次数（高亮 / 笔记 / 页级产物都算，同样只是记账 —— 改动预算已取消）
    edit_calls: int = 0
    #: 本轮改过的产物（`done` 事件带给前端做提示，也是自检的断言口）
    edits: list[dict] = field(default_factory=list)
    #: 本轮**读过**其产物原文的 ``(页, 侧)``：`page_markdown_edit` 的"先读后写"判据
    viewed_sides: set[tuple[int, str]] = field(default_factory=set)
    #: 元数据填写（`paper_meta_write`）真正写进去的字段。一次会话只允许写一次：
    #: 元数据是"整篇的结论"，边读边改只会让前面写的几条与后面的判断互相矛盾。
    meta_applied: dict = field(default_factory=dict)
    #: 元数据**提交过**没有（与 `meta_applied` 不同：提交成功但所有字段都已经有值时，
    #: `applied` 是空的 —— 那也是一次成功的会话，不该报"没有提交"）。
    meta_submitted: bool = False

    def count_search(self) -> None:
        """记一次检索（**只记账，不拦截**）。

        检索预算 2026-11-26 已取消（长程任务需要）：那道"用尽后不打后端"的闸早就成了
        恒 False 的死分支，留着只会让人以为还有上限。`search_calls` 保留 —— 自检与统计在读它。
        """
        self.search_calls += 1

    def count_edit(self) -> None:
        """记一次改动（同样是纯记账，改动预算已取消）。"""
        self.edit_calls += 1

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
        return False        # 轮数也不再设限（长程任务）
