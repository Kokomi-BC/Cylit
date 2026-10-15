"""Agent **风格**（设置页「论文问答 → 对话风格」里可改的那一段）。

用户口径 2026-10-15：设置里要能改 agent 提示词的**风格**部分、能恢复默认，
而且**只对新对话生效** —— 已经开着的会话不因为一次改动就换口吻。

**它只改"你是谁 / 什么语气"**：护栏与工作方式（只管这篇论文、引用要 [ref]、没证据就说没有、
改产物要先读后写）写死在 `agent/prompts.py` 的常驻块里，风格改不掉它们 ——
用户能调的是口吻，不是底线（把风格写成猫娘，也照样要按引用规则办事）。

## 为什么风格要**冻结在会话行上**（`conversation.agent_style`）

风格拼在 system 的**最前面**，而 provider 的前缀缓存是**从第 0 个 token 起比前缀**的：
每轮都按"当前设置"取一次，等于"设置页一改，所有历史会话的 `system + 全部历史`
下一轮同时作废"（一次改动把整个库的缓存打掉，之后又各自重建）。

所以：**建会话时把它那一刻的风格抄进会话行**，之后这一条会话永远用那一份；
设置页的改动只影响**新建**的会话。老会话（升级前建的、这一列还是 NULL）在首次提问时
按"谁更晚"判一次再写回（见 `pick`）：建得比风格最后一次改动晚的，说明它出生时就见的
这一份，冻结它；更早的按默认算 —— 它出生时还没有自定义风格。

存储（设置侧）：setting 表裸键 `agent_style`（读法与其他设置同一条规则，见 `retrieval.setting`）。
**空 = 没设过**：`PUT /settings` 收到空串就把那一行删掉，读取时自然回落到
`prompts.DEFAULT_STYLE` —— 而不是存一段空风格（那会让模型失去身份设定）。
"""

from __future__ import annotations

from datetime import datetime

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
    """**新建会话**该冻结的那一份：设置里有就用它，否则默认。"""
    from backend.agent.prompts import style_of

    return style_of(stored())


def changed_at() -> datetime | None:
    """风格最后一次改动的时间；没设过 → ``None``（见 `pick` 用它判老会话）。"""
    try:
        from backend.repositories.mysql.models import Setting
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            for name in (f"kb.{KEY}", KEY):          # 与 `retrieval.setting` 同一条优先级
                row = sess.get(Setting, name)
                if row is not None:
                    return row.updated_at
    except Exception:  # noqa: BLE001 —— 判不了就按"没有自定义风格"算，不影响问答
        pass
    return None


def pick(frozen: str | None, current: str, *, created_at: datetime | None = None,
         changed_at: datetime | None = None) -> str:
    """一条会话该用**哪一份**风格（纯函数，便于测试；数据库那一半在 `conversations`）。

    - ``frozen`` 有值（新会话）→ 就是它，谁都改不动；
    - 老会话（NULL）→ 看谁更晚：会话建得**不早于**风格最后一次改动，说明它出生时就见的
      这一份（比如"设好猫娘之后才建的那条对话"）→ 用当前这份；更早 → 用**默认**，
      因为它出生时还没有自定义风格。
    """
    from backend.agent.prompts import style_of

    text = str(frozen or "").strip()
    if text:
        return style_of(text)
    if current and changed_at and created_at and created_at >= changed_at:
        return style_of(current)
    return style_of("")
