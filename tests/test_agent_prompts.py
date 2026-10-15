"""Agent 提示词的两堆内容：**稳定的 system** 与**每轮会变的备注**（纯函数，零模型调用）。

为什么值得单独锁住：system 在请求最前面，provider 的前缀缓存**从第 0 个 token 起比前缀** ——
system 里只要有一个"这一轮才有的值"（选区原文、图片张数、检索分），
`system + 全部历史` 就整段按未命中计费；而且下一轮那个块消失、前缀又是另一个，
于是连续两轮都 miss。用户 2026-10-03 问的"命中不了缓存"就是这个。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.agent import prompts
from backend.agent.deps import Deps

pytest.importorskip("pydantic_ai")

from backend.agent.runtime import AgentRuntime  # noqa: E402


def _deps(**kw) -> Deps:
    """最小 Deps：`build_instructions` / `turn_notes` 只看这几个字段。"""
    kws = dict(cfg=SimpleNamespace(max_rounds=60, deadline_seconds=600.0), host=None,
               paper_id="demo-paper", registry=None, page_total=12, title="Demo Paper")
    kws.update(kw)
    return Deps(**kws)  # type: ignore[arg-type]


def _rich() -> Deps:
    """带齐"每轮会变"的那两样的 deps：划词选区 + 图片。"""
    return _deps(selection="Instead of performing a single attention function", selection_ref=7,
                 selection_page=4, attachments=3, attachments_shown=1)


def test_system_prompt_is_byte_stable_for_a_paper() -> None:
    """同一篇论文：不管这一轮有没有选区 / 图片 / 低相关度提示，system 必须逐字节一样。"""
    plain = prompts.build_instructions(_deps())
    rich = prompts.build_instructions(_rich())
    assert plain == rich
    assert plain.startswith(prompts.BASE)
    # 反过来确认 rich 的那些值**确实有地方去**（否则就是把内容弄丢了）
    assert "Instead of performing" not in plain
    assert "Instead of performing" in prompts.turn_notes(_rich())


def test_turn_notes_carry_the_per_turn_values() -> None:
    assert prompts.turn_notes(_deps()) == "", "没有选区/图片/提示时不该凭空多一段备注"
    notes = prompts.turn_notes(_rich())
    assert notes.startswith("【本轮备注】")
    assert "第 4 页" in notes and "ref=7" in notes and "Instead of performing" in notes
    assert "3 张图片" in notes and "1 张就在这条消息里" in notes
    assert "0.42" in notes, "低相关度提示里的分数是给模型判断用的，不能丢"


def test_system_prompt_keeps_the_rules() -> None:
    """规则（选区别跑题、图片别当论文正文、别复述备注）必须常驻 system。"""
    text = prompts.build_instructions(_deps())
    for name in ("SELECTION_RULES", "IMAGE_RULES", "NOTES_RULE"):
        assert getattr(prompts, name) in text
    assert "不要跑去总结全篇" in text and "不要把它当作论文正文去引用" in text


def test_system_prompt_does_not_preload_the_archive() -> None:
    """**不预注入论文档案**（用户口径 2026-10-07："agent 不需要每轮预读材料"）。

    档案改成模型按需调 `paper_archive_get` 去拿：不看的轮次一个 token 都不花。
    """
    text = prompts.build_instructions(_deps())
    assert "【论文档案" not in text
    assert "主要论断" not in text and "章节结构" not in text
    # 提示词里要**告诉它怎么拿**（否则就是单纯把能力删了）
    assert "paper_archive_get" in text

    # 工具那一侧：档案工具在挂载清单里，而且明确写着"不是可引用的材料"
    from backend.agent import tools as agent_tools

    assert agent_tools.paper_archive_get in agent_tools.AGENT_TOOLS
    doc = agent_tools.paper_archive_get.__doc__ or ""
    assert "不是可引用的材料" in doc


def test_user_prompt_puts_notes_before_the_question() -> None:
    """备注拼在**这一轮的 user prompt** 里（缓存的最后一段），问题仍然清晰可辨。"""
    req = SimpleNamespace(question="多头注意力为什么有用？", selection="", attachments=[])
    plain = asyncio.run(AgentRuntime._user_prompt(SimpleNamespace(), req, _deps()))
    assert plain == "多头注意力为什么有用？", "没有备注时不许改用户的原话"

    rich_req = SimpleNamespace(question="这段话什么意思？", selection=_rich().selection, attachments=[])
    text = asyncio.run(AgentRuntime._user_prompt(SimpleNamespace(), rich_req, _rich()))
    assert text.startswith("【本轮备注】")
    assert "【用户这一轮的问题】\n这段话什么意思？" in text
    assert text.index("【本轮备注】") < text.index("这段话什么意思？")
