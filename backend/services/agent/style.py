"""Agent **风格**（设置页「论文问答 → 对话风格」里可改的那一段）。

用户口径 2026-10-15：设置里要能改 agent 提示词的**风格**部分，并且能恢复默认。

**它只改"你是谁 / 什么语气"**：护栏与工作方式（只管这篇论文、引用要 [ref]、没证据就说没有、
改产物要先读后写）写死在 `agent/prompts.py` 的常驻块里，风格改不掉它们 ——
用户能调的是口吻，不是底线（把风格写成猫娘，也照样要按引用规则办事）。

存储：setting 表的裸键 `agent_style`（读法与其他设置同一条规则，见 `retrieval.setting`）。
**空 = 没设过**：`PUT /settings` 收到空串就把那一行删掉，读取时自然回落到
`prompts.DEFAULT_STYLE` —— 而不是存一段空风格（那会让模型失去身份设定）。
"""

from __future__ import annotations

#: setting 表里的键名（裸键；`retrieval.setting` 会先试 `kb.` 前缀那一行）。
KEY = "agent_style"


def default() -> str:
    """代码里那份默认风格。设置页的「恢复默认」与 `values` 的兜底都用它。"""
    from backend.agent.prompts import DEFAULT_STYLE

    return DEFAULT_STYLE


def stored() -> str:
    """设置里存的那一份；没设过（或存的是空白）返回空串。"""
    from backend.services.knowledge import retrieval

    return str(retrieval.setting(KEY, "") or "").strip()


def effective() -> str:
    """**生效**的风格：设置里有就用它，否则默认。"""
    from backend.agent.prompts import style_of

    return style_of(stored())


def describe() -> dict:
    """`GET /agent/style` 的载荷（agent 进程每轮来取这个）。

    ``custom`` 让前端与诊断能一眼看出"用户改过没有" —— 只看 ``persona`` 与默认值是否相同
    是不行的：用户完全可能自己把默认那段抄进去。
    """
    text = stored()
    return {"style": text or default(), "custom": bool(text), "default": default()}
