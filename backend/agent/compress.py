"""压缩：把一段对话记录压成要点（**由用户点圆环触发，作为多出来的一轮会话**）。

为什么不自动压缩：摘要是**不可逆**的 —— 它替换了后续对话看到的历史。自动触发时用户
既不知道发生了什么，也说不清"模型为什么忘了刚才那段"。做成一次显式动作 + 一条可见的
会话消息（`role=summary`），代价是用户得点一下，换来的是整个过程可解释、可回看。

为什么用**独立 Agent**（不带工具）：压缩是纯文本处理，让它有机会调工具只会多花钱、
还可能"为了压缩去读几页"。一个 `output_type=str` 的裸 agent 正好。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic_ai import Agent

from backend.agent.host import HostClient, HostError
from backend.agent.runtime import build_model, model_settings_from
from backend.core.logging import get_logger

log = get_logger(__name__)

#: 摘要里**不允许**出现 `[n]` 引用编号（见 `COMPRESS_INSTRUCTIONS` 第 1 条）。
#: 为什么在出口兜住而不是只写进提示词：实测模型偶发不遵守（自检里抓到过一次 `[1]`），
#: 而编号跨轮是**真的会点错地方**（`[1]` 在新的一轮指的是另一篇/另一块）。
#:
#: ⚠️ 引用现在有**合并组**写法（`[3,7]` / `[3-5]` / 全角逗号 `[3，7]`，见 prompts.CITING），
#: 只认 `[\d]` 会让 `[3,7]` 整组漏过去留在摘要里 —— 那一组编号同样只在原来那轮有效。
_REF_MARK = re.compile(r"\s*\[\s*\d{1,3}(?:\s*[,，、\-–~]\s*\d{1,3})*\s*\]")


def strip_ref_marks(text: str) -> str:
    """去掉摘要里的 `[n]` 编号，并把留下的双空格收一收。"""
    out = _REF_MARK.sub("", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"[ \t]+(\n)", r"\1", out).strip()

COMPRESS_INSTRUCTIONS = """你在整理一段论文问答对话，把它压缩成一份**要点笔记**，
供后续继续对话时当作背景。

要求：
1. 只保留三类信息：
   - 用户关心的问题与关注点；
   - 已经确认的结论 —— **每条都要保留出处**，写成「第 N 页 §X.X · 概念名」这种定位，
     **不要用 [n] 编号**（编号只在原来那一轮有效，写进来会让后面误点）；
   - 尚未解决的问题 / 还没查清的点。
2. 删掉寒暄、重复的检索过程、被推翻的中间推断、模型的自我说明。
3. 用简体中文分点写，**300~600 字**。直接给要点，不要前言、不要"以下是摘要"这类句子。
4. 论文里没有依据的东西不要写进结论；拿不准的就放到"尚未解决"里。
"""

#: 一次压缩最多处理多少字符（超了只取最近的）
TRANSCRIPT_LIMIT = 60000


async def compress(cfg, host: HostClient, payload: dict[str, Any]) -> dict:
    """``{paper_id?, transcript}`` → ``{summary, model, usage}``。"""
    transcript = str(payload.get("transcript") or "").strip()
    if not transcript:
        raise HostError("没有可压缩的对话内容")
    transcript = transcript[-TRANSCRIPT_LIMIT:]

    model_cfg = await host.model_config()
    if not model_cfg.get("usable"):
        raise HostError("问答模型没有配好，无法压缩（请到设置页给「问答」配模型与密钥）")

    agent = Agent(build_model(model_cfg), output_type=str,
                  instructions=COMPRESS_INSTRUCTIONS,
                  model_settings=model_settings_from(model_cfg.get("parameters") or {}))
    result = await agent.run(f"以下是问答记录：\n\n{transcript}")
    raw = (result.output or "").strip()
    summary = strip_ref_marks(raw)
    if summary != raw:
        log.info("摘要里的 [n] 编号已剥掉（模型偶发不遵守，见 strip_ref_marks）")
    usage: dict = {}
    try:
        snapshot = result.usage()
        usage = {"requests": int(getattr(snapshot, "requests", 0) or 0),
                 "input_tokens": int(getattr(snapshot, "input_tokens", 0) or 0),
                 "output_tokens": int(getattr(snapshot, "output_tokens", 0) or 0)}
    except Exception:  # noqa: BLE001
        pass
    log.info("压缩完成：输入 %d 字 → 摘要 %d 字（%s）",
             len(transcript), len(summary), usage)
    return {"summary": summary, "model": model_cfg.get("model_name"), "usage": usage}
