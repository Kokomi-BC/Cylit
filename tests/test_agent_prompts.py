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
    assert plain.startswith(prompts.DEFAULT_STYLE)
    assert prompts.BASE in plain
    # 反过来确认 rich 的那些值**确实有地方去**（否则就是把内容弄丢了）
    assert "Instead of performing" not in plain
    assert "Instead of performing" in prompts.turn_notes(_rich())


def test_turn_notes_carry_the_per_turn_values() -> None:
    assert prompts.turn_notes(_deps()) == "", "没有选区/图片时不该凭空多一段备注"
    notes = prompts.turn_notes(_rich())
    assert notes.startswith("【本轮备注】")
    assert "第 4 页" in notes and "ref=7" in notes and "Instead of performing" in notes
    assert "3 张图片" in notes and "1 张就在这条消息里" in notes


def test_system_prompt_keeps_the_rules() -> None:
    """规则（选区别跑题、图片别当论文正文、别复述备注）必须常驻 system。"""
    text = prompts.build_instructions(_deps())
    for name in ("SELECTION_RULES", "IMAGE_RULES", "NOTES_RULE", "OFF_TOPIC",
                 "ABSTRACT_RULES"):
        assert getattr(prompts, name) in text
    assert "不要跑去总结全篇" in text and "不要把它当作论文正文去引用" in text
    # 跑题拒答（用户口径 2026-10-15）：既要说"拒绝"，也要防住"顺手把问题答了"
    assert "直接用一句话拒绝" in text and "不要顺手把那个问题答了" in text


def test_style_is_the_only_editable_part_of_the_prompt() -> None:
    """设置页能改的只有**风格**那一段：改得掉口吻，改不掉护栏。"""
    custom = prompts.build_instructions(_deps(style="你是猫娘助手，每句话结尾都要说喵。"))
    assert custom.startswith("你是猫娘助手，每句话结尾都要说喵。")
    for name in ("BASE", "SCOPE", "OFF_TOPIC", "ABSTRACT_RULES", "CITING",
                 "NO_EVIDENCE", "EDIT_RULES"):
        assert getattr(prompts, name) in custom, f"风格改掉之后 {name} 也不许丢"

    # 空白 = 没设过 → 默认风格（这就是设置页「恢复默认」的语义：删掉那一行）
    assert prompts.build_instructions(_deps(style="   ")).startswith(prompts.DEFAULT_STYLE)
    assert prompts.build_instructions(_deps(style=None)).startswith(prompts.DEFAULT_STYLE)

    # 过长要截断：别让一段人设把规则挤出上下文
    persona = prompts.build_instructions(_deps(style="喵" * (prompts.STYLE_MAX_CHARS + 500)))
    assert persona.startswith("喵" * prompts.STYLE_MAX_CHARS)
    assert persona.split("\n")[0] == "喵" * prompts.STYLE_MAX_CHARS


def test_abstract_rides_the_first_user_message_only() -> None:
    """论文摘要**只在首轮**带上，而且拼在**用户消息**里（用户口径 2026-10-15）。

    为什么不是 system：只有首轮有它 → 第 1 轮与第 2 轮的 system 差一个字节 →
    第 2 轮"system + 全部历史"整段前缀缓存作废。放进首轮的用户消息，它从第 2 轮起
    就在历史里，坐在缓存前缀**内部**，不再花一分钱。
    """
    first = _deps(first_turn=True, abstract_zh="本文提出 TurboQuant。",
                  abstract_en="We propose TurboQuant.")
    assert "【论文摘要" not in prompts.build_instructions(first), "摘要不许进 system"
    text = prompts.turn_notes(first)
    assert text.startswith("【论文摘要")
    assert "本文提出 TurboQuant。" in text and "We propose TurboQuant." in text
    assert "Demo Paper" in text, "标题一起带上（模型才知道这是哪一篇）"

    # 第二轮起：历史里已经有了，不许再附
    assert prompts.turn_notes(_deps(abstract_zh="本文提出 TurboQuant。")) == ""
    # 没有摘要的论文：一个字都不加（不要出现一个空抬头）
    assert prompts.turn_notes(_deps(first_turn=True)) == ""


def test_style_service_reads_the_setting_and_falls_back(monkeypatch) -> None:
    """风格存 setting 表（裸键 `agent_style`）：**空白 = 没设过 → 默认**

    这就是设置页「恢复默认」的语义（`PUT /settings` 收到空串就把那一行删掉），
    与"存了一段空风格"不是一回事。全部离线：`retrieval._setting_raw` 是唯一的数据库入口。
    """
    from backend.services.agent import style as style_service
    from backend.services.knowledge import retrieval

    monkeypatch.setattr(retrieval, "_setting_raw",
                        lambda key: {"agent_style": "你是猫娘助手，喵。"}.get(
                            key, retrieval._MISSING))
    assert style_service.stored() == "你是猫娘助手，喵。"
    assert style_service.effective() == "你是猫娘助手，喵。"

    monkeypatch.setattr(retrieval, "_setting_raw",
                        lambda key: {"agent_style": "   "}.get(key, retrieval._MISSING))
    assert style_service.stored() == ""
    assert style_service.effective() == prompts.DEFAULT_STYLE


def test_style_is_frozen_per_conversation() -> None:
    """**风格只对新对话生效**（用户口径 2026-10-15）：冻结在会话行上，之后谁都追不到它。

    为什么必须冻：风格在 system 最前面，而前缀缓存是从第 0 个 token 起比的 ——
    "每轮按当前设置取一次"等于"设置页改一下，所有历史会话的 `system + 全部历史`
    下一轮同时作废"。冻在会话行上，改动就只影响新建的会话。
    """
    from datetime import datetime, timedelta

    from backend.services.agent import style as style_service

    cat = "你是猫娘助手，喵。"
    t0 = datetime(2026, 10, 15, 10, 0, 0)
    changed = t0 + timedelta(hours=1)
    old_conv, new_conv = t0, changed + timedelta(minutes=5)

    # 新会话（行上已经有冻结值）：设置里改成别的，它也只用自己的那一份
    assert style_service.pick(cat, "换了个风格") == cat
    assert style_service.pick(cat, "") == cat

    # 老会话（这一列还是空的，升级前建的）：
    # 建得**晚于**风格最后一次改动 → 它出生时就见的这一份 → 用当前的
    assert style_service.pick(None, cat, created_at=new_conv, changed_at=changed) == cat
    # 建得更早 → 默认（它出生时还没有自定义风格）
    assert style_service.pick(None, cat, created_at=old_conv,
                              changed_at=changed) == prompts.DEFAULT_STYLE
    # 从没设过自定义风格 / 会话比改动晚但设置里是空的 → 一律默认
    assert style_service.pick(None, cat, created_at=new_conv) == prompts.DEFAULT_STYLE
    assert style_service.pick(None, "", created_at=new_conv, changed_at=changed) \
        == prompts.DEFAULT_STYLE
    assert prompts.build_instructions(_deps(style="")).startswith(prompts.DEFAULT_STYLE)


def test_style_default_import_stays_light() -> None:
    """主后端要读默认风格（设置页要显示它、要能恢复），而 `services/agent/__init__` 写着
    "本包不能把 pydantic-ai 拉进主后端的导入图"。

    这条捷径成立的前提是 `prompts.py` 的**导入链**里没有 pydantic-ai，这里静态守着它 ——
    哪天有人在 `prompts/deps/citations/config/host` 里加一行 `from pydantic_ai import ...`，
    主后端的启动图就悄悄变重了，而那种回归不会有任何报错。
    （不用起子进程验证：Windows 沙箱里管道会 EPERM。）
    """
    import pathlib

    root = pathlib.Path(prompts.__file__).parent
    for name in ("prompts.py", "deps.py", "citations.py", "config.py", "host.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "pydantic_ai" not in text, f"{name} 把 pydantic-ai 拖进了 prompts 的导入链"


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

    # 首轮：摘要在最前面（背景先给、再给这一轮的定位信息），问题始终在最后一句
    first_req = SimpleNamespace(question="这篇讲了什么？", selection="", attachments=[])
    first = asyncio.run(AgentRuntime._user_prompt(
        SimpleNamespace(), first_req,
        _deps(first_turn=True, abstract_zh="本文提出 TurboQuant。")))
    assert first.startswith("【论文摘要")
    assert first.endswith("【用户这一轮的问题】\n这篇讲了什么？")

# --------------------------------------------------------------------------
# 档案渲染：**章节结构先出现**，且不被 brief 的长字段挤掉
#
# 线上实测（2026-12）：archive 接口的 outline 有 24 条，agent 读到的档案里「章节结构」
# 却是空的 —— 它当时渲染在最后，brief 的「主要论断 / 统一译法（16 条）」把 6000 字预算
# 吃光，整段被 \`text[:cfg_chars]\` 一刀切掉。而 summarize-paper 第 1 步恰恰依赖它。
# --------------------------------------------------------------------------

_FAT_BRIEF = {
    "domain": "向量量化与 KV cache 压缩",
    "key_claims": ["论断" + "很长的说明" * 12] * 6,
    "glossary": {f"term-{i}": f"译法{i}" for i in range(16)},
    "keep_as_is": [f"KEEP{i}" for i in range(16)],
}
#: **真实 outline 形状**（见 `reader_service.outline_payload`）：键是
#: `block_id / page / level / number / text / text_zh / children`，**没有 title**。
#: 上一版的测试自己捏了 `title`，于是"取错字段导致 24 条全被过滤"这个 bug 一条都没照出来。
_OUTLINE = [
    {"level": 1, "number": "0", "page": 1, "text": "ABSTRACT", "text_zh": "摘要"},
    {"level": 2, "number": "0.1", "page": 1, "text": "0.1 PROBLEM DEFINITION",
     "text_zh": "0.1 问题定义"},
    {"level": 2, "number": "0.2", "page": 3, "text": "0.2 OUR APPROACH", "text_zh": ""},
    {"level": 1, "number": "1", "page": 9, "text": "1 CONCLUSION", "text_zh": "1 结论"},
]


def _archive(budget: int = 6000, *, brief=None, outline=None) -> str:
    return prompts.archive_block(budget,
                                 _FAT_BRIEF if brief is None else brief,
                                 _OUTLINE if outline is None else outline,
                                 title="TurboQuant", page_total=22)


def test_archive_keeps_the_structure_even_when_the_brief_is_fat() -> None:
    """brief 塞满 key_claims / glossary 时，章节结构**仍然完整出现**（它排在最前面）。"""
    text = _archive()
    assert "章节结构：" in text
    for node in _OUTLINE:
        assert node["text"] in text          # 英文标题必须出现（就是这一条曾经全丢）
    # 中文并排：有 text_zh 的用全角竖线接在后面，空的只留英文
    assert "ABSTRACT｜摘要" in text
    assert "0.2 OUR APPROACH" in text and "0.2 OUR APPROACH｜" not in text
    # 结构必须在可选的 brief 字段之前 —— 否则又会被挤到截断线外
    assert text.index("章节结构：") < text.index("主要论断：")
    assert text.index("章节结构：") < text.index("统一译法：")


def test_archive_omits_the_heading_when_there_is_no_outline() -> None:
    text = _archive(outline=[])
    assert "章节结构" not in text
    assert "标题：TurboQuant" in text


def test_archive_marks_truncation_but_the_structure_survives() -> None:
    """预算不够时：结尾带截断标记、结构一条不少、可选字段整块停掉（不留半句）。"""
    small = _archive(budget=600)
    assert small.endswith("…（档案过长已截断）")
    for node in _OUTLINE:
        assert node["text"] in small
    # 大纲永远在，被牺牲的是后面的长字段
    assert small.index("章节结构：") < small.index("领域与核心方法")


def test_archive_without_truncation_has_no_marker() -> None:
    """没被截就别加标记：加了模型会以为结构也被切了。"""
    text = _archive(budget=6000, brief={"domain": "量化"},
                    outline=[{"level": 1, "text": "ABSTRACT", "text_zh": "摘要"}])
    assert "已截断" not in text
    assert "章节结构：\n  - ABSTRACT｜摘要" in text

#: 线上那条论文的 outline 就是 24 条，键是 text / text_zh（其中一条没有中文）。
_REAL_OUTLINE = [
    {"level": 1 if i % 4 == 0 else 2, "number": str(i // 4), "page": i + 1,
     "text": f"{i // 4}.{i % 4} SECTION HEADING {i}",
     "text_zh": "" if i == 7 else f"{i // 4}.{i % 4} 小节标题 {i}"}
    for i in range(24)
]


def test_archive_renders_the_outline_from_the_real_shape() -> None:
    """**24 条真实形状**的 outline 必须整段出现：有条数、有层级缩进、没有空行与 None。

    这是这次 bug 的正面用例：`text` / `text_zh`、没有 `title`，
    旧实现按 `title` 过滤 → 24 条全丢，档案里连「章节结构：」都不出现。
    """
    text = _archive(outline=_REAL_OUTLINE)
    assert "章节结构：" in text

    struct = text.split("章节结构：\n", 1)[1].split("\n领域与核心方法", 1)[0]
    lines = struct.split("\n")
    assert len(lines) == 24, f"应为 24 条，实际 {len(lines)} 条"
    assert all(line.strip() for line in lines), "章节结构里出现了空行"
    assert "None" not in struct
    # 层级缩进：level=1 顶格 2 空格，level=2 四空格
    assert lines[0].startswith("  - 0.0 SECTION HEADING 0")
    assert lines[1].startswith("    - 0.1 SECTION HEADING 1")
    # 中文并排；第 7 条没有中文，就只留英文
    assert "0.0 SECTION HEADING 0｜0.0 小节标题 0" in struct
    assert "1.3 SECTION HEADING 7" in struct and "1.3 SECTION HEADING 7｜" not in struct
