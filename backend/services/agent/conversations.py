"""会话与消息的落库（MySQL，主后端是唯一写入方）。

**为什么会话不落在 agent 服务侧**（规划 §4.3）：消息要带 `citations` 与 `paper_id`，
主后端已经在管这些字段；放两侧就会出现两处都存、迁移改两边，而 agent 那一侧
还要为此获得数据库凭据 —— 与"agent 不碰数据库"的边界直接冲突。

**为什么消息是树**：`regenerate` 与"编辑我的提问"都是在同一个父节点下挂兄弟节点，
线性数组表达不了；等真的要做时再改数据模型，历史数据里的分叉信息是补不回来的。
可见分支 = 从 `head_id` 沿 `parent_id` 回溯再反转（没有 `parent_id` 的老数据退化为按 `seq`）。
"""

from __future__ import annotations

import uuid

from backend.core.errors import NotFoundError
from backend.core.logging import get_logger
from backend.core.utils import iso_utc, json_dumps, json_loads, now

log = get_logger(__name__)

#: 首条用户消息截多长当会话标题
TITLE_CHARS = 40
#: 单条消息的框架开销估算（角色标签 + 分隔符），参与历史预算扣减
HISTORY_MESSAGE_OVERHEAD = 8


def _row_to_message(row) -> dict:
    return {
        "message_id": row.id, "conversation_id": row.conversation_id,
        "parent_id": row.parent_id, "seq": row.seq, "role": row.role,
        "content": row.content or "",
        "citations": json_loads(row.citations_json, []) or [],
        "tool_trace": json_loads(row.tool_trace_json, []) or [],
        "attachments": json_loads(getattr(row, "attachments_json", None), []) or [],
        "thinking": row.thinking or "",
        "thinking_ms": int(row.thinking_ms or 0),
        # 过程轨迹（多轮思考 + 工具调用）：老消息没有这一列 → 空对象，前端回退到旧画法
        "run": json_loads(getattr(row, "run_json", None), {}) or {},
        "finish_reason": row.finish_reason,
        # 时间一律走 `iso_utc`（库里是 UTC naive）：直接 `.isoformat()` 会让前端按本地时区
        # 解析，东八区里刚发的消息显示成"8 小时前"（用户 2026-10-03 报的"对话时间有问题"）
        "created_at": iso_utc(row.created_at),
    }


def _row_to_conversation(row, *, messages: list[dict] | None = None) -> dict:
    data = {
        "conversation_id": row.id, "paper_id": row.paper_id, "title": row.title or "",
        "head_id": row.head_id,
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
    }
    if messages is not None:
        data["messages"] = messages
    return data


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------


def create_conversation(conversation_id: str, paper_id: str = "",
                        *, title: str = "") -> str:
    """**按给定 id** 建会话（已存在就把它缺的字段补上，绝不换 id）。

    ⚠️ 与 `ensure_conversation` 的区别只有一条，但很关键：那个在"id 查不到"时会**换一个新
    uuid**（前端手里的会话没了时的兜底），所以调用方拿不到它想要的 id。内部落库接口要的是
    "就用这个 id"（自检造临时会话），所以这里是另一个函数。

    建会话时顺手把**此刻的风格**冻结到这一行上（用户口径 2026-10-15："风格只对新对话生效"）——
    之后设置页怎么改都追不到它，见 `services/agent/style.py` 的文件头。
    """
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope
    from backend.services.agent import style as style_service

    with session_scope() as sess:
        row = sess.get(Conversation, conversation_id)
        if row is None:
            sess.add(Conversation(id=conversation_id, paper_id=paper_id or None,
                                  title=(title or "")[:255], head_id=None,
                                  agent_style=style_service.effective()))
        else:
            if paper_id and not row.paper_id:
                row.paper_id = paper_id
            if title and not row.title:
                row.title = title[:255]
    return conversation_id


def frozen_style(conversation_id: str) -> str:
    """这条会话该用的风格（**冻结**语义：新建时就定下来，之后设置页怎么改都不动它）。

    老会话（升级前建的、`agent_style` 还是 NULL）在这里判一次并**写回** —— 判一次就够了，
    不写回的话每轮都要重判，而设置页一改重判结果就变，正是要避免的那种"历史会话被追着改"。
    """
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope
    from backend.services.agent import style as style_service

    with session_scope() as sess:
        row = sess.get(Conversation, conversation_id)
        if row is None:                      # 正常到不了（调用方刚 ensure/建过）
            return style_service.effective()
        text = style_service.pick(row.agent_style, style_service.stored(),
                                  created_at=row.created_at,
                                  changed_at=style_service.changed_at())
        if not (row.agent_style or "").strip():
            row.agent_style = text            # 冻结：这条会话从此只用这一份
        return text


def ensure_conversation(conversation_id: str | None, paper_id: str,
                        *, title: str = "") -> str:
    """取会话；不存在就建。返回 conversation_id。"""
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope

    if conversation_id:
        with session_scope() as sess:
            row = sess.get(Conversation, conversation_id)
            if row is not None:
                if paper_id and not row.paper_id:
                    row.paper_id = paper_id
                if title and not row.title:
                    row.title = title[:255]
                return conversation_id
        # 前端手里的会话不见了（换库 / 清库）→ 建一个新的，比 500 好
        conversation_id = None

    return create_conversation(conversation_id or str(uuid.uuid4()), paper_id,
                               title=title)


def list_conversations(paper_id: str | None = None, *, limit: int = 50,
                       query: str = "", kind: str = "all") -> list[dict]:
    """会话列表（侧栏一级页）。

    ``query`` 同时搜**标题与消息正文**：用户记不住标题，记得住"我当时问的是
    query plan 那段"，只搜标题等于搜不到。
    ``kind`` 按**有没有划词消息**自动分类（``selection`` / ``normal``）—— 划词解释
    与普通提问的入口、上下文、用途都不一样，混在一列里翻起来很累。
    """
    from sqlalchemy import func, or_

    from backend.repositories.mysql.models import Conversation, ConversationMessage
    from backend.repositories.mysql.session import session_scope

    q_text = (query or "").strip()
    with session_scope() as sess:
        q = sess.query(Conversation)
        if paper_id:
            q = q.filter(Conversation.paper_id == paper_id)
        if q_text:
            like = f"%{q_text}%"
            hit = (sess.query(ConversationMessage.conversation_id)
                   .filter(ConversationMessage.content.like(like))
                   .subquery())
            q = q.filter(or_(Conversation.title.like(like),
                             Conversation.id.in_(hit)))
        rows = q.order_by(Conversation.updated_at.desc()).limit(limit).all()
        ids = [r.id for r in rows]
        counts: dict[str, int] = {}
        selections: set[str] = set()
        if ids:
            # 一次分组查询拿全部计数（会话列表是每次开面板都要拉的，别在这里 N+1）
            for cid, n in (sess.query(ConversationMessage.conversation_id,
                                      func.count(ConversationMessage.id))
                           .filter(ConversationMessage.conversation_id.in_(ids))
                           .group_by(ConversationMessage.conversation_id).all()):
                counts[str(cid)] = int(n)
            # 划词判定：用户消息里带 `> 引用` 开头的那一段（`api/agent.py` 就是这么写的）
            for (cid,) in (sess.query(ConversationMessage.conversation_id)
                           .filter(ConversationMessage.conversation_id.in_(ids),
                                   ConversationMessage.role == "user",
                                   ConversationMessage.content.like("> %"))
                           .distinct().all()):
                selections.add(str(cid))
        items = []
        for r in rows:
            data = {**_row_to_conversation(r), "messages": counts.get(r.id, 0),
                    "has_selection": r.id in selections,
                    "kind": "selection" if r.id in selections else "normal"}
            if kind in ("selection", "normal") and data["kind"] != kind:
                continue
            items.append(data)
        return items


def delete_conversations(ids: list[str]) -> dict:
    """批量删（侧栏一级页的"批量管理"）。不存在的 id 直接跳过，不报错。"""
    from backend.repositories.mysql.models import Conversation, ConversationMessage
    from backend.repositories.mysql.session import session_scope

    wanted = [str(i) for i in (ids or []) if i]
    if not wanted:
        return {"deleted": [], "messages": 0}
    with session_scope() as sess:
        found = [r.id for r in sess.query(Conversation.id)
                 .filter(Conversation.id.in_(wanted)).all()]
        if not found:
            return {"deleted": [], "messages": 0}
        deleted = (sess.query(ConversationMessage)
                   .filter(ConversationMessage.conversation_id.in_(found))
                   .delete(synchronize_session=False))
        (sess.query(Conversation).filter(Conversation.id.in_(found))
         .delete(synchronize_session=False))
    return {"deleted": found, "messages": int(deleted or 0)}


def purge_paper(paper_id: str) -> dict:
    """删掉**这篇论文的全部会话**（删论文时由 `paper_service.delete_paper` 调用）。

    为什么必须做（用户 2026-10-03 问的"删掉这篇论文，历史记录能不能被正确清除"）：
    会话是按 `paper_id` 挂在论文上的，论文删掉之后它们**既进不了列表**（前端只列当前论文），
    **也永远不会被清理**；更糟的是论文 id 是标题派生的 slug ——
    **同一篇重新导入会拿到同一个 id**，于是那些旧对话会挂到新论文上"复活"，
    消息里的引用还指着已经变过的行号，点下去跳到别的段落。

    顺带清掉这些会话引用过的附件图片：附件是按消息引用的，消息没了文件就是垃圾
    （`attachments.remove`）。
    """
    from backend.repositories.mysql.models import Conversation, ConversationMessage
    from backend.repositories.mysql.session import session_scope
    from backend.services.agent import attachments as attachment_store

    with session_scope() as sess:
        ids = [r.id for r in sess.query(Conversation.id)
               .filter(Conversation.paper_id == paper_id).all()]
        if not ids:
            return {"conversations": 0, "messages": 0, "attachments": 0}
        attachment_ids: list[str] = []
        for (raw,) in (sess.query(ConversationMessage.attachments_json)
                       .filter(ConversationMessage.conversation_id.in_(ids)).all()):
            for item in json_loads(raw, []) or []:
                aid = str((item or {}).get("attachment_id") or "")
                if aid:
                    attachment_ids.append(aid)
        messages = (sess.query(ConversationMessage)
                    .filter(ConversationMessage.conversation_id.in_(ids))
                    .delete(synchronize_session=False))
        (sess.query(Conversation).filter(Conversation.id.in_(ids))
         .delete(synchronize_session=False))
    files = attachment_store.remove(attachment_ids)
    log.info("清空论文 %s 的会话：%d 个会话 / %d 条消息 / %d 个附件文件",
             paper_id, len(ids), int(messages or 0), files)
    return {"conversations": len(ids), "messages": int(messages or 0),
            "attachments": files}


def set_head(conversation_id: str, message_id: str) -> dict:
    """把可见分支切到某条消息上（**分叉导航**：◀ 1/2 ▶）。

    只改 `head_id`，一个字节都不删：另一条分支仍在树里，随时能切回去。
    """
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        conv = sess.get(Conversation, conversation_id)
        if conv is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        rows = _all_messages(sess, conversation_id)
        if message_id not in {r.id for r in rows}:
            raise NotFoundError(f"这条消息不在会话里：{message_id}")
        conv.head_id = message_id
    return {"conversation_id": conversation_id, "head_id": message_id}


def get_conversation(conversation_id: str) -> dict:
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Conversation, conversation_id)
        if row is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        data = _row_to_conversation(row, messages=visible_messages(conversation_id))
    data["usage"] = conversation_usage(conversation_id)
    return data


def delete_conversation(conversation_id: str) -> dict:
    from backend.repositories.mysql.models import Conversation, ConversationMessage
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Conversation, conversation_id)
        if row is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        deleted = (sess.query(ConversationMessage)
                   .filter(ConversationMessage.conversation_id == conversation_id)
                   .delete(synchronize_session=False))
        sess.delete(row)
    return {"deleted": conversation_id, "messages": int(deleted or 0)}


def rename_conversation(conversation_id: str, title: str) -> dict:
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Conversation, conversation_id)
        if row is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        row.title = (title or "")[:255]
    return {"conversation_id": conversation_id, "title": title}


# --------------------------------------------------------------------------
# 消息
# --------------------------------------------------------------------------


def _all_messages(sess, conversation_id: str) -> list:
    from backend.repositories.mysql.models import ConversationMessage

    return (sess.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.seq.asc()).all())


def _siblings_map(rows: list) -> dict[str, list[str]]:
    """``parent_id → [子消息 id…]``（按 seq 排；根节点的父记成 ``""``）。

    分叉导航（◀ 2/3 ▶）就是拿它算的：同一父节点下的兄弟 = 同一处的不同版本
    （重新生成 / 改过问法），前端只显示"当前是第几个"。
    """
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.parent_id or "", []).append(r)
    return {parent: [r.id for r in sorted(items, key=lambda x: x.seq)]
            for parent, items in groups.items()}


def visible_messages(conversation_id: str) -> list[dict]:
    """当前可见分支（`head_id` 沿 `parent_id` 回溯再反转）。"""
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Conversation, conversation_id)
        if row is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        rows = _all_messages(sess, conversation_id)
        by_id = {r.id: r for r in rows}
        siblings = _siblings_map(rows)
        chain: list = []
        cursor = row.head_id
        guard = 0
        while cursor and cursor in by_id and guard < 1000:
            node = by_id[cursor]
            chain.append(node)
            cursor = node.parent_id
            guard += 1
        if not chain:
            # 老数据 / head_id 丢了：退化为按 seq 串联（可见顺序就是写入顺序）
            chain = sorted(rows, key=lambda x: x.seq)
        else:
            chain.reverse()
        out: list[dict] = []
        for node in chain:
            item = _row_to_message(node)
            group = siblings.get(node.parent_id or "") or [node.id]
            item["siblings"] = group
            item["sibling_index"] = group.index(node.id) + 1 if node.id in group else 1
            out.append(item)
        return out


def message_of(conversation_id: str, message_id: str) -> dict:
    """取一条消息（含 ``attachments``）—— 重新生成 / 编辑提问要按 id 找它。"""
    from backend.repositories.mysql.models import ConversationMessage
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(ConversationMessage, message_id)
        if row is None or row.conversation_id != conversation_id:
            raise NotFoundError(f"消息不存在：{message_id}")
        return _row_to_message(row)


def branch_history(conversation_id: str, upto_id: str | None) -> list[dict]:
    """``upto_id`` **之前**那条链上的消息（不含它自己）。

    重放某一轮要看的是"当时模型看到了什么"，而不是"现在 head 在哪" ——
    重新生成第 3 轮时，第 4 轮之后的对话不能进去（否则模型会以为自己已经答过了）。
    """
    if not upto_id:
        return []
    from backend.repositories.mysql.models import ConversationMessage
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        rows = _all_messages(sess, conversation_id)
        by_id = {r.id: r for r in rows}
        target = by_id.get(upto_id)
        if target is None:
            return []
        chain: list = []
        cursor = target.parent_id
        guard = 0
        while cursor and cursor in by_id and guard < 1000:
            node = by_id[cursor]
            chain.append(node)
            cursor = node.parent_id
            guard += 1
        chain.reverse()
        return [_row_to_message(r) for r in chain]


def history_for_agent(conversation_id: str, *,
                      max_tokens: int | None = None) -> list[dict]:
    """给 agent 的历史（只保留 user/assistant 的正文，不含 tool/summary 之外的杂项）。

    **压缩边界**：从 `head` 往回走到最近一条 `summary` 就停 —— 那条摘要就是"之前所有对话"
    的等价物（见 `compress`）。压缩之后模型的记忆就只有"摘要 + 之后的几轮"，这正是用户
    点圆环时期待的效果。

    摘要本身**永远保留**（它可能是那批老内容的唯一记忆），轮数只裁摘要之后的对话。
    """
    return history_from_chain(visible_messages(conversation_id), max_tokens=max_tokens)


#: 助手历史里"材料索引"最多列几条（挡住极端长的引用表）
PROVENANCE_LIMIT = 20
#: 材料索引里每条摘要截多长
PROVENANCE_CHARS = 50


def _provenance(m: dict) -> str:
    """一条**助手历史**接一个"那一轮用到了什么材料"的尾巴（没有材料就返回空串）。

    为什么必须拼回去：正文里的 `[n]` 是**那一轮自己的编号**（`_renumber` 把 registry 的号压成
    1..M），而库里另存着 `citations` 与 `tool_trace`。以前发给模型的历史只有 `role` + `content`，
    于是模型看到自己上一轮写着 `[1]…[17]`，却**查不到任何出处、也看不到工具调用记录** ——
    它的结论只能是"这些编号是我编的"，接着就不认上面的对话（用户 2026-10-03 贴的那段思维链
    正是这个推理）。

    **缓存安全**：这段文本只由库里的行拼出来（同一行永远拼出同样的字节），且拼在每条消息
    自己后面 —— 历史前缀逐字节不变，provider 的前缀缓存照旧命中。
    """
    cites = list(m.get("citations") or [])
    trace = [t for t in (m.get("tool_trace") or [])
             if not str(t.get("tool") or "").startswith("__")]
    if not cites and not trace:
        return ""
    lines: list[str] = []
    if trace:
        counts: dict[str, int] = {}
        for t in trace:
            name = str(t.get("tool") or "?")
            counts[name] = counts.get(name, 0) + 1
        lines.append("〔上一轮调用过的工具〕"
                     + "、".join(f"{k}×{v}" if v > 1 else k for k, v in counts.items()))
    if cites:
        lines.append("〔上一轮回答里的编号指向这些材料 —— 编号只属于那一轮，"
                     "本轮要引用哪条材料就自己调工具拿本轮的编号〕")
        for c in cites[:PROVENANCE_LIMIT]:
            where = c.get("label") or (f"第 {c.get('page')} 页" if c.get("page") else "相关片段")
            snippet = " ".join(str(c.get("snippet") or "").split())[:PROVENANCE_CHARS]
            lines.append(f"- [{c.get('ref')}] {where}：{snippet}")
        if len(cites) > PROVENANCE_LIMIT:
            lines.append(f"- …（还有 {len(cites) - PROVENANCE_LIMIT} 条）")
    return "\n".join(lines)


def history_from_chain(chain: list[dict], *,
                       max_tokens: int | None = None) -> list[dict]:
    """把一条可见链裁成给模型的历史（压缩边界同样落在最近一条 `summary` 上）。

    分叉（重新生成 / 编辑提问）走的就是这条：链换成"目标节点之前的那条"，
    压缩边界与预算口径一个字都不改。

    **按预算裁，不按轮数裁**（2026-10-03 改）：以前是固定 12 轮，一篇论文聊到第 13 个
    问题，模型就再也看不到开头那几轮了 —— 而它自己**看不出来**自己被截断过，于是出现
    "按最近两轮反推"这种答案（用户拿这个当"模型不认记忆"的证据）。预算由
    `agent_context_budget` 决定，压缩（90% 触发线）才是唯一的记忆压缩点：压缩之后
    历史自然变短，没到线就不该偷偷截。``max_tokens`` 只在测试/特殊场景覆盖默认预算。
    """
    from backend.core.config import get_settings

    start = _summary_index(chain)
    active = chain[start:] if start >= 0 else chain
    head = [active[0]] if active and active[0]["role"] == "summary" else []
    turns = [m for m in active
             if m["role"] in ("user", "assistant") and (m.get("content") or "").strip()]
    budget = int(max_tokens if max_tokens is not None
                 else get_settings().agent_context_budget)
    kept: list[tuple[dict, str]] = []
    used = 0
    for item in reversed(turns):
        extra = _provenance(item) if item["role"] == "assistant" else ""
        cost = (count_tokens(str(item.get("content") or "")) + count_tokens(extra)
                + HISTORY_MESSAGE_OVERHEAD)
        # 最近一轮**永远保留**：截掉它就等于这一轮没有上下文，比截断更糟
        if kept and used + cost > budget:
            break
        kept.append((item, extra))
        used += cost
    kept.reverse()
    out = [{"role": m["role"], "content": m["content"]} for m in head]
    for m, extra in kept:
        content = str(m.get("content") or "")
        out.append({"role": m["role"], "content": f"{content}\n\n{extra}" if extra else content})
    return out


# --------------------------------------------------------------------------
# 用量 / 压缩
# --------------------------------------------------------------------------

_ENCODER: object = None


def count_tokens(text: str) -> int:
    """tiktoken 估算（拿不到编码器就退化成"字符数 / 2"，中文大致是这个量级）。"""
    global _ENCODER
    if _ENCODER is None:
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:  # noqa: BLE001 —— 离线/首次下载失败都走退化路径
            log.warning("tiktoken 不可用，用量改用字符数估算：%s", exc)
            _ENCODER = False
    if _ENCODER:
        try:
            return len(_ENCODER.encode(text or ""))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text or "") // 2)


def _summary_index(chain: list[dict]) -> int:
    """最近一条 `summary` 在可见链里的下标（-1 = 没压缩过）。"""
    for i in range(len(chain) - 1, -1, -1):
        if chain[i]["role"] == "summary":
            return i
    return -1


def conversation_usage(conversation_id: str) -> dict:
    """会话用量：**从最近一次压缩之后**算起（压缩过的部分不再进上下文）。

    前端那个圆环显示的就是它；点圆环 → 压缩 → 圆环归零。
    """
    from backend.core.config import get_settings

    chain = visible_messages(conversation_id)
    start = _summary_index(chain)
    active = chain[start:] if start >= 0 else chain
    tokens = count_tokens("\n".join(str(m.get("content") or "") for m in active))
    budget = int(get_settings().agent_context_budget)
    return {
        "tokens": tokens,
        "budget": budget,
        "percent": round(min(100.0, tokens * 100.0 / budget), 1) if budget > 0 else 0.0,
        "messages": len(chain),
        "active": len(active),
        "compressed": start >= 0,
        "compressed_at": chain[start]["created_at"] if start >= 0 else None,
    }


def transcript_for_compress(conversation_id: str) -> tuple[str, int]:
    """给压缩用的对话记录；返回 ``(文本, 参与压缩的消息数)``。

    **上一次的摘要要一起带上**：压缩是可累加的，不带就得把老内容重新讲一遍，
    压第二次等于把第一次的记忆丢掉。
    """
    chain = visible_messages(conversation_id)
    start = _summary_index(chain)
    lines: list[str] = []
    if start >= 0:
        lines.append(f"【此前摘要】{chain[start]['content']}")
    active = chain[start + 1:] if start >= 0 else chain
    for item in active:
        if item["role"] not in ("user", "assistant"):
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"【{'用户' if item['role'] == 'user' else '助手'}】{content}")
    return "\n\n".join(lines), len(active)


def update_message_content(conversation_id: str, message_id: str,
                           content: str) -> dict:
    """**就地**改掉某条消息的正文（用户口径 2026-10-03：编辑＝在本对话内改，不再开分支）。

    会话标题跟着走：改的是**根**那条（第一条提问）时，标题本来就是从它来的，
    不同步就会出现"标题写着旧问法、消息里是新问法"。
    """
    from backend.repositories.mysql.models import Conversation, ConversationMessage
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(ConversationMessage, message_id)
        if row is None or row.conversation_id != conversation_id:
            raise NotFoundError(f"消息不存在：{message_id}")
        row.content = content
        conv = sess.get(Conversation, conversation_id)
        if conv is not None:
            if row.parent_id is None:
                conv.title = content.strip()[:TITLE_CHARS]
            conv.updated_at = now()
    return {"message_id": message_id, "content": content}


def drop_subtree(conversation_id: str, message_id: str, *, keep: bool) -> dict:
    """删掉以某条消息为根的那棵子树（`keep=True` 时保留这条消息本身）。

    为什么要连子树一起删：链是 `head → parent` 回溯出来的，中间断一环后面就全成孤儿。
    入参语义：
    - ``keep=True``（**就地编辑**）：留下这条（正文已被改写），把它之后的问答一起作废，
      `head` 挪到它身上 —— 用户看到的就是"改了这句话，后面的答案重来"；
    - ``keep=False``（**就地重新生成**）：连它自己一起删（它就是要被替换的那条回答），
      `head` 挪到它的父上。

    只删**它自己的子树**：兄弟分支（同一个父下的另一条）不受影响。
    """
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        conv = sess.get(Conversation, conversation_id)
        if conv is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        rows = _all_messages(sess, conversation_id)
        by_id = {r.id: r for r in rows}
        target = by_id.get(message_id)
        if target is None:
            raise NotFoundError(f"消息不存在：{message_id}")
        children: dict[str, list] = {}
        for r in rows:
            children.setdefault(r.parent_id or "", []).append(r)
        doomed: list = []
        stack = list(children.get(message_id, []))
        while stack:
            node = stack.pop()
            doomed.append(node)
            stack.extend(children.get(node.id, []))
        if not keep:
            # `keep=False` 是"**连它一起**删"（它就是要被替换的那条回答）——
            # 漏了这一行就会只删子节点，于是旧回答留着、新回答成了它的兄弟（踩过）
            doomed.append(target)
        head = target.parent_id if not keep else target.id
        for node in doomed:
            sess.delete(node)
        conv.head_id = head
        conv.updated_at = now()
    return {"deleted": len(doomed), "head_id": head,
            "kept": target.id if keep else None}


def append_message(conversation_id: str, *, role: str, content: str,
                   parent_id: str | None = None, root: bool = False,
                   citations: list | None = None,
                   tool_trace: list | None = None,
                   attachments: list | None = None,
                   thinking: str = "", thinking_ms: int = 0,
                   run: dict | None = None,
                   finish_reason: str | None = None) -> dict:
    """追加一条消息并把 `head_id` 移到它上面（父节点默认取当前 head）。

    ``parent_id`` 显式给定时就是**分叉**：新消息与原来的那条成为兄弟，
    两条都留在树里，`head_id` 决定现在看哪一条（见 `set_head`）。
    ``root=True`` 表示"挂在根上"（没有父）—— 与"没指定父（跟着 head）"是两件事：
    编辑**第一句**提问时，新提问必须成为根上的兄弟，而不是接到当前末尾去
    （踩过：不给这个开关，`parent_id=None` 会被当成"用 head"，编辑第一句就等于追加一轮）。
    """
    from backend.repositories.mysql.models import Conversation, ConversationMessage
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        conv = sess.get(Conversation, conversation_id)
        if conv is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        rows = _all_messages(sess, conversation_id)
        if root:
            parent = None
        elif parent_id is not None:
            parent = parent_id
        else:
            parent = conv.head_id
        if parent and parent not in {r.id for r in rows}:
            parent = conv.head_id        # 前端给了个不存在的父节点 → 挂到当前 head
        message_id = str(uuid.uuid4())
        sess.add(ConversationMessage(
            id=message_id, conversation_id=conversation_id, parent_id=parent,
            seq=(max((r.seq for r in rows), default=0) + 1),
            role=role, content=content,
            citations_json=json_dumps(citations or []),
            tool_trace_json=json_dumps(tool_trace or []),
            attachments_json=json_dumps(attachments or []),
            thinking=thinking or None,
            thinking_ms=int(thinking_ms or 0) or None,
            run_json=json_dumps(run) if run else None,
            finish_reason=finish_reason,
        ))
        conv.head_id = message_id
        conv.updated_at = now()
        if not conv.title and role == "user":
            conv.title = content.strip()[:TITLE_CHARS]
        return {"message_id": message_id, "conversation_id": conversation_id,
                "parent_id": parent, "seq": len(rows) + 1, "persisted": True,
                "attachments": attachments or []}

