"""一轮问答的**过程轨迹**：多轮思考 + 工具调用，按**发生的顺序**排成一条流水。

为什么要单独一个模块：这是"这一轮它到底干了什么"的**唯一形状定义**，三方共用 ——

- 运行时按事件往里写（`runtime._translate` / `_presearch` / `ask` 的阶段提示）；
- SSE 的 `done` 把它整份发给前端（前端流式期间自己拼一份，收到权威值后覆盖）；
- 落库（`conversation_message.run_json`），重开会话、翻旧消息时读的就是它。

形状（前端照着渲染，别再各自发明）::

    {"steps": [{"kind": "stage",    "round": 1, "text": "正在这篇论文里检索…"},
               {"kind": "thinking", "round": 1, "text": "用户问的是…"},
               {"kind": "tool",     "round": 1, "tool": "paper_search",
                "digest": "query=随机旋转", "status": "done", "ms": 312}],
     "elapsed_ms": 1179000, "rounds": 12}

三条口径：

1. **思考按"轮"合并**：模型一轮里会吐几百个 thinking 增量块，一个块一条步骤等于刷屏 ——
   同一轮的相邻思考合并成一条（换轮或中间插了工具就另起一条）。
2. **工具步骤"先出现、后补状态"**：`FunctionToolCallEvent` 建一条 `running`（此刻还不知道要跑多久），
   `FunctionToolResultEvent` 找到它补上 `done` + 毫秒数。找不到就补一条新的（宁可多一条，不要丢）。
3. **不做展示文案**：`tool` 只存工具名与参数摘要，中文短语与图标在**前端**（`ReaderChat.vue`）——
   存文案就等于把"以后想改叫法"变成一次数据迁移。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: 单条思考步骤最多存多少字（一轮思考可能几万字，存满一条消息就废了）
STEP_CHARS = 6000
#: 一条流水最多多少步（模型 60 轮 × 每轮 2~3 步，留够余量）
MAX_STEPS = 80
#: 工具参数摘要长度上限（与 SSE 的 `args_digest` 一致）
DIGEST_CHARS = 120


@dataclass
class StepLog:
    """一条流水（可写、可序列化）。"""

    steps: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0
    rounds: int = 0

    # ---------------- 写 ----------------
    def stage(self, text: str, *, round: int = 0) -> dict | None:
        """阶段提示（"正在准备论文档案…"这类宿主自己的动作）。相邻重复的丢掉。"""
        text = (text or "").strip()
        if not text:
            return None
        if self.steps and self.steps[-1].get("kind") == "stage" \
                and self.steps[-1].get("text") == text:
            return None
        return self._push({"kind": "stage", "round": round, "text": text[:200]})

    def thinking(self, text: str | None, *, round: int) -> dict | None:
        """思考增量：并进**当前这一轮的思考步骤**（不是每块一条）。"""
        if not text:
            return None
        last = self.steps[-1] if self.steps else None
        if (last is not None and last.get("kind") == "thinking"
                and last.get("round") == round and len(last.get("text") or "") < STEP_CHARS):
            last["text"] = (last.get("text") or "") + text
            if len(last["text"]) > STEP_CHARS:
                last["text"] = last["text"][:STEP_CHARS] + "…"
                last["truncated"] = True
            return last
        truncated = len(text) > STEP_CHARS
        return self._push({"kind": "thinking", "round": round,
                           "text": text[:STEP_CHARS], **({"truncated": True} if truncated else {})})

    def tool_start(self, tool: str, *, round: int, digest: str = "") -> dict:
        """工具调用开始（此刻还不知道要跑多久）。"""
        return self._push({"kind": "tool", "round": round, "tool": str(tool or "?"),
                           "digest": str(digest or "")[:DIGEST_CHARS],
                           "status": "running", "ms": None,
                           # `_t0` 只在内存里活一会儿（`tool_done` 用它算毫秒），
                           # `payload()` 会把下划线开头的键全部剥掉，不落库
                           "_t0": time.perf_counter()})

    def tool_done(self, tool: str, *, round: int, ms: int | None = None) -> dict:
        """工具调用结束：补上状态与耗时；找不到对应的开始步骤就补一条。"""
        name = str(tool or "?")
        for step in reversed(self.steps):
            if (step.get("kind") == "tool" and step.get("tool") == name
                    and step.get("status") == "running"):
                step["status"] = "done"
                started = step.pop("_t0", None)
                step["ms"] = (int(ms) if ms is not None
                              else (int((time.perf_counter() - started) * 1000)
                                    if started else None))
                return step
        return self._push({"kind": "tool", "round": round, "tool": name, "digest": "",
                           "status": "done", "ms": int(ms) if ms is not None else None})

    def text(self, text: str | None, *, round: int) -> dict | None:
        """正文增量：并进**当前这一轮的正文步骤**。

        为什么要记正文步骤：正常的 agent 就是"想 → 说一句 → 调工具 → 接着想 → 说完"，
        思考、正文、工具是**交织**的。只存一条最终回答就丢掉了这个结构
        （用户 2026-10-03 的口径："允许思考和正文夹杂显示"）。
        `content` 仍然是全部正文的拼接（历史/复制/检索都用它），这里只是把它按轮切开。

        ⚠️ **正文段不截断**（思考段才截）：正文段是要渲染的**回答本身**，截了就是回答缺了一截
        （用户报过"最终正文被截断"）。它本来就与 `content` 同量级，重复存这一份是设计代价。
        """
        if not text:
            return None
        last = self.steps[-1] if self.steps else None
        if (last is not None and last.get("kind") == "text"
                and last.get("round") == round):
            last["text"] = (last.get("text") or "") + text
            return last
        return self._push({"kind": "text", "round": round, "text": text})

    def _push(self, step: dict) -> dict:
        self.steps.append(step)
        if len(self.steps) > MAX_STEPS:
            # 砍掉最早的**非工具**步骤（工具调用是"它干了什么"的骨架，不能丢）
            for i, item in enumerate(self.steps):
                if item.get("kind") != "tool":
                    del self.steps[i]
                    break
            else:
                del self.steps[0]
        return step

    # ---------------- 读 ----------------
    def payload(self) -> dict:
        """落库 / 下发用的那份（剥掉内部键，补上收尾状态）。"""
        steps = [{k: v for k, v in s.items() if not k.startswith("_")}
                 for s in self.steps]
        return {"steps": steps, "elapsed_ms": int(self.elapsed_ms or 0),
                "rounds": int(self.rounds or 0)}

    @property
    def tools(self) -> list[str]:
        return [s["tool"] for s in self.steps
                if s.get("kind") == "tool" and s.get("tool")]
