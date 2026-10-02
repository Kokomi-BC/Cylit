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
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        q = sess.query(Conversation)
        if paper_id:
            q = q.filter(Conversation.paper_id == paper_id)
        rows = q.order_by(Conversation.updated_at.desc()).limit(limit).all()
        return [_row_to_conversation(r) for r in rows]


def get_conversation(conversation_id: str) -> dict:
    from backend.repositories.mysql.models import Conversation
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Conversation, conversation_id)
        if row is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        return _row_to_conversation(row, messages=visible_messages(conversation_id))


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


def history_for_agent(conversation_id: str, *, limit: int = 6) -> list[dict]:
    """给 agent 的历史（只保留 user/assistant 的正文，不含 tool/summary）。

    P0 是单轮问答，这份历史通常为空；但要**从第一天就接对**：等 P1 加多轮时，
    agent 侧不需要任何改动。
    """
    out: list[dict] = []
    for item in visible_messages(conversation_id):
        if item["role"] not in ("user", "assistant"):
            continue
        if not (item.get("content") or "").strip():
            continue
        out.append({"role": item["role"], "content": item["content"]})
    return out[-limit * 2:]


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
