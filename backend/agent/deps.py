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
    #: 工具轨迹（审计 + 自检 A5/A13/A14 读它）
    trace: list[dict] = field(default_factory=list)
    #: 模型轮次（含工具轮）：超上限就停，见 §11 的 `rounds_exhausted`
    model_calls: int = 0
    #: 这一轮读过的页（自检用：验证"先检索定位、再读页"的顺序）
    pages_read: list[int] = field(default_factory=list)

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
