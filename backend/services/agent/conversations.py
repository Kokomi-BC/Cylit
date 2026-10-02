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
from backend.core.utils import json_dumps, json_loads, now

log = get_logger(__name__)

#: 首条用户消息截多长当会话标题
TITLE_CHARS = 40
#: 发给 agent 的历史轮数上限（一轮 = 一问一答）。百万上下文下没必要抠，
#: 超过这个量级该由用户点圆环压缩，而不是我们偷偷截断。
HISTORY_TURNS = 12


def _row_to_message(row) -> dict:
    return {
        "message_id": row.id, "conversation_id": row.conversation_id,
        "parent_id": row.parent_id, "seq": row.seq, "role": row.role,
        "content": row.content or "",
        "citations": json_loads(row.citations_json, []) or [],
        "tool_trace": json_loads(row.tool_trace_json, []) or [],
        "finish_reason": row.finish_reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _row_to_conversation(row, *, messages: list[dict] | None = None) -> dict:
    data = {
        "conversation_id": row.id, "paper_id": row.paper_id, "title": row.title or "",
        "head_id": row.head_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if messages is not None:
        data["messages"] = messages
    return data


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------


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

    cid = conversation_id or str(uuid.uuid4())
    with session_scope() as sess:
        sess.add(Conversation(id=cid, paper_id=paper_id or None,
                              title=(title or "")[:255], head_id=None))
    return cid


def list_conversations(paper_id: str | None = None, *, limit: int = 50) -> list[dict]:
    from sqlalchemy import func

    from backend.repositories.mysql.models import Conversation, ConversationMessage
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        q = sess.query(Conversation)
        if paper_id:
            q = q.filter(Conversation.paper_id == paper_id)
        rows = q.order_by(Conversation.updated_at.desc()).limit(limit).all()
        ids = [r.id for r in rows]
        counts: dict[str, int] = {}
        if ids:
            # 一次分组查询拿全部计数（会话列表是每次开面板都要拉的，别在这里 N+1）
            for cid, n in (sess.query(ConversationMessage.conversation_id,
                                      func.count(ConversationMessage.id))
                           .filter(ConversationMessage.conversation_id.in_(ids))
                           .group_by(ConversationMessage.conversation_id).all()):
                counts[str(cid)] = int(n)
        return [{**_row_to_conversation(r), "messages": counts.get(r.id, 0)} for r in rows]


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
            return [_row_to_message(r) for r in sorted(rows, key=lambda x: x.seq)]
        chain.reverse()
        return [_row_to_message(r) for r in chain]


def history_for_agent(conversation_id: str, *, limit: int = HISTORY_TURNS) -> list[dict]:
    """给 agent 的历史（只保留 user/assistant 的正文，不含 tool/summary 之外的杂项）。

    **压缩边界**：从 `head` 往回走到最近一条 `summary` 就停 —— 那条摘要就是"之前所有对话"
    的等价物（见 `compress`）。压缩之后模型的记忆就只有"摘要 + 之后的几轮"，这正是用户
    点圆环时期待的效果。

    摘要本身**永远保留**（它可能是那批老内容的唯一记忆），轮数只裁摘要之后的对话。
    """
    chain = visible_messages(conversation_id)
    start = _summary_index(chain)
    active = chain[start:] if start >= 0 else chain
    head = [active[0]] if active and active[0]["role"] == "summary" else []
    turns = [m for m in active
             if m["role"] in ("user", "assistant") and (m.get("content") or "").strip()]
    out = [{ "role": m["role"], "content": m["content"]} for m in head + turns[-limit * 2:]]
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


def append_message(conversation_id: str, *, role: str, content: str,
                   parent_id: str | None = None, citations: list | None = None,
                   tool_trace: list | None = None,
                   finish_reason: str | None = None) -> dict:
    """追加一条消息并把 `head_id` 移到它上面（父节点默认取当前 head）。"""
    from backend.repositories.mysql.models import Conversation, ConversationMessage
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        conv = sess.get(Conversation, conversation_id)
        if conv is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        rows = _all_messages(sess, conversation_id)
        parent = parent_id if parent_id is not None else conv.head_id
        if parent and parent not in {r.id for r in rows}:
            parent = conv.head_id        # 前端给了个不存在的父节点 → 挂到当前 head
        message_id = str(uuid.uuid4())
        sess.add(ConversationMessage(
            id=message_id, conversation_id=conversation_id, parent_id=parent,
            seq=(max((r.seq for r in rows), default=0) + 1),
            role=role, content=content,
            citations_json=json_dumps(citations or []),
            tool_trace_json=json_dumps(tool_trace or []),
            finish_reason=finish_reason,
        ))
        conv.head_id = message_id
        conv.updated_at = now()
        if not conv.title and role == "user":
            conv.title = content.strip()[:TITLE_CHARS]
        return {"message_id": message_id, "conversation_id": conversation_id,
                "parent_id": parent, "seq": len(rows) + 1, "persisted": True}
