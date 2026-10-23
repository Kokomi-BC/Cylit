"""Agent **口吻 / 自定义指令 / 回复长度**（设置页「Cyli 偏好」里能改的那几段）。

用户口径 2026-10-15：设置里要能改 agent 提示词的**风格**部分、能恢复默认，而且**只对
新对话生效** —— 已经开着的会话不因为一次改动就换口吻。2026-12「Cyli 偏好」页把它拆成
**三项各自独立**的设置：口吻预设 / 自定义指令 / 回复长度。

**它们只改"语气"和"篇幅"**：身份（你是谁、只管这一篇）与护栏、工作方式写死在
`agent/prompts.py` 的 `IDENTITY` 与常驻块里，风格改不掉它们 —— 用户能调的是口吻，
不是底线（把口吻写成猫娘，也照样要按引用规则办事）。

## 为什么这些值要**冻结在会话行上**

（`conversation.agent_style` / `agent_style_preset` / `agent_reply_length`）

它们都拼在 system 的**最前面**跟**最后面**，而 provider 的前缀缓存是**从第 0 个 token
起比前缀**的：每轮都按"当前设置"取一次，等于"设置页一改，所有历史会话的
`system + 全部历史` 下一轮同时作废"（一次改动把整个库的缓存打掉，之后又各自重建）。

所以：**建会话时把它那一刻的三项抄进会话行**，之后这一条会话永远用那一份；
设置页的改动只影响**新建**的会话。老会话（升级前建的、这几列还是 NULL）在首次提问时
按"谁更晚"判一次再写回（见 `pick` / `pick_preset` / `pick_length`）。

## 存储（设置侧，setting 表裸键）

- `agent_style`：**自定义指令**文本（上限 `prompts.STYLE_MAX_CHARS`），
  **只在 `agent_style_preset = custom` 时生效**；
- `agent_style_preset`：`default | rigorous | concise | plain | custom`；
- `agent_reply_length`：`short | normal | detailed`。

**空 = 没设过**：`PUT /settings` 收到空串就把那一行删掉，读取时自然回落到代码里的默认 ——
而不是存一段空值（那会让模型失去身份设定 / 让设置页显示一个假的"已自定义"）。
**向后兼容**：老设置只有 `agent_style` 一段文本、没有 preset 行，这种库读出来就是
`preset = custom`（那段文本当年就是唯一的口吻来源）。
"""

from __future__ import annotations

from datetime import datetime

#: setting 表里的键名（裸键；`retrieval.setting` 会先试 `kb.` 前缀那一行）。
KEY = "agent_style"
KEY_PRESET = "agent_style_preset"
KEY_LENGTH = "agent_reply_length"


def default() -> str:
    """代码里那份默认风格。设置页的「恢复默认」与 `values` 的兜底都用它。"""
    from backend.agent.prompts import DEFAULT_STYLE

    return DEFAULT_STYLE


def default_preset() -> str:
    """口吻预设的默认值（= `default`，即"就用默认口吻那一段"）。"""
    from backend.agent.prompts import DEFAULT_PRESET

    return DEFAULT_PRESET


def default_length() -> str:
    """回复长度的默认值（= `normal`，即不追加任何篇幅要求）。"""
    from backend.agent.prompts import DEFAULT_REPLY_LENGTH

    return DEFAULT_REPLY_LENGTH


def _read(key: str) -> str:
    """读一条设置原文；没设过 `None`（与"存了空串"区分开，见 `stored_preset`）。"""
    from backend.services.knowledge import retrieval

    value = str(retrieval.setting(key, "") or "").strip()
    return value


def stored() -> str:
    """设置里存的那一份自定义指令；没设过（或存的是空白）返回空串。"""
    return _read(KEY)


def stored_preset() -> str:
    """设置里存的口吻预设；**没设过**返回空串（`effective_preset` 才回落到 default）。

    老设置（只有 `agent_style` 一段自定义文本、没有 preset 行）在这里读成 `custom`：
    那段文本当年就是唯一的口吻来源，不认它等于把用户写过的话悄悄丢掉。
    显式存过 `default` 的用户不受影响 —— 那时"自定义指令"本来就不生效（见文件头）。
    """
    from backend.agent.prompts import STYLE_PRESETS

    value = _read(KEY_PRESET).lower()
    if value in STYLE_PRESETS:
        return value
    return "custom" if stored() else ""


def stored_length() -> str:
    """设置里存的回复长度；没设过返回空串。"""
    from backend.agent.prompts import REPLY_LENGTHS

    value = _read(KEY_LENGTH).lower()
    return value if value in REPLY_LENGTHS else ""


def effective() -> str:
    """**新建会话**该冻结的那一份自定义指令：设置里有就用它，否则默认。"""
    from backend.agent.prompts import style_of

    return style_of(stored())


def effective_preset() -> str:
    """**新建会话**该冻结的口吻预设。"""
    return stored_preset() or default_preset()


def effective_length() -> str:
    """**新建会话**该冻结的回复长度。"""
    return stored_length() or default_length()


def changed_at(key: str = KEY) -> datetime | None:
    """某一项设置最后一次改动的时间；没设过 → `None`（见 `pick` 用它判老会话）。"""
    try:
        from backend.repositories.mysql.models import Setting
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            for name in (f"kb.{key}", key):          # 与 `retrieval.setting` 同一条优先级
                row = sess.get(Setting, name)
                if row is not None:
                    return row.updated_at
    except Exception:  # noqa: BLE001 —— 判不了就按"没有自定义风格"算，不影响问答
        pass
    return None


def is_custom_text(text: str | None) -> bool:
    """这一段是不是**用户自己写的指令**（非空、且不是代码里那份默认风格）。"""
    value = str(text or "").strip()
    return bool(value) and value != default()


def pick(frozen: str | None, current: str, *, created_at: datetime | None = None,
         changed_at: datetime | None = None) -> str:
    """一条会话该用**哪一份**自定义指令（纯函数，便于测试；数据库那一半在 `conversations`）。

    - `frozen` 有值（新会话）→ 就是它，谁都改不动；
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


def pick_preset(frozen: str | None, current: str, *, style_text: str = "",
                created_at: datetime | None = None,
                changed_at: datetime | None = None) -> str:
    """一条会话该用**哪个口吻预设**（纯函数）。

    三种情形：行上有值 → 就是它（冻结）；老会话（NULL）→ 谁更晚判一次；
    再兜一层**老语义**（升级前没有 preset 这一列，行上只有 `agent_style` 那段文本）——
    那是自定义指令就用 `custom`，否则 `default`。
    """
    from backend.agent.prompts import DEFAULT_PRESET, STYLE_PRESETS

    name = str(frozen or "").strip().lower()
    if name in STYLE_PRESETS:
        return name
    if current in STYLE_PRESETS and changed_at and created_at and created_at >= changed_at:
        return current
    return "custom" if is_custom_text(style_text) else DEFAULT_PRESET


def pick_length(frozen: str | None, current: str, *, created_at: datetime | None = None,
                changed_at: datetime | None = None) -> str:
    """一条会话该用**哪个回复长度**（纯函数；判法与 `pick_preset` 同一条"谁更晚"）。

    没有"老语义"兜底：升级前根本没有长度这一项，那些会话就是 `normal`。
    """
    from backend.agent.prompts import DEFAULT_REPLY_LENGTH, REPLY_LENGTHS

    name = str(frozen or "").strip().lower()
    if name in REPLY_LENGTHS:
        return name
    if current in REPLY_LENGTHS and changed_at and created_at and created_at >= changed_at:
        return current
    return DEFAULT_REPLY_LENGTH
