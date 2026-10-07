"""批注 / 笔记 / 高亮服务（规划 §10 ⑤⑥⑦）。

- 高亮锚定 ``block_id + 字符偏移``（规划 §10 ⑤），重译后按 ``anchor_hash`` 重定位；
- **划词解释已改由问答 agent 负责**（`/api/v1/agent/ask` + `selection`），本模块只负责
  把 agent 给出的解释**存成批注**（`source=selection`）—— 旧的那条"自己拼 prompt 调
  generation provider"的链路已删除，见 dev-log 第二十轮。
"""

from __future__ import annotations

import re

from backend.core.errors import NotFoundError, ValidationError
from backend.core.events import EVENT_ANNOTATION, bus
from backend.core.logging import get_logger
from backend.core.utils import (
    iso_utc, json_dumps, json_loads, new_id, sha256_text,
)
from backend.repositories.mysql.models import (
    HIGHLIGHT_SCOPES, HIGHLIGHT_SIDES, Annotation, Highlight, Note, Paper,
)
from backend.repositories.mysql.session import session_scope

log = get_logger(__name__)

HIGHLIGHT_COLORS = ("yellow", "green", "blue", "pink", "purple")


# --------------------------------------------------------------------------
# 高亮（规划 §10 ⑤）
# --------------------------------------------------------------------------


def create_highlight(payload: dict, user_id: int = 1) -> dict:
    """存一条高亮。

    **原文 / 译文分开存**（`side`）：一行对照里两侧共用同一个 `block_id`，
    只存偏移量的话原文的高亮会按同样的偏移量画到译文上。
    `scope` 区分在哪标的：正文行（`block_id` = 行 id）还是 Markdown 视图（`md:p<页号>`）。
    `group_id`：一次划词跨了多行时，每行一条记录共用同一个分组 id（删除时整组一起删）。
    """
    paper_id = payload.get("paper_id")
    block_id = payload.get("block_id")
    if not paper_id or not block_id:
        raise ValidationError("paper_id 与 block_id 必填")
    color = payload.get("color") or "yellow"
    if color not in HIGHLIGHT_COLORS:
        color = "yellow"
    side = str(payload.get("side") or "en").lower()
    if side not in HIGHLIGHT_SIDES:
        side = "en"
    scope = str(payload.get("scope") or "row").lower()
    if scope not in HIGHLIGHT_SCOPES:
        scope = "row"
    group_id = str(payload.get("group_id") or "").strip()[:32] or None
    with session_scope() as sess:
        row = Highlight(
            id=new_id("h"), user_id=user_id, paper_id=paper_id, block_id=block_id,
            selected_text=payload.get("selected_text"),
            start_offset=int(payload.get("start_offset") or 0),
            end_offset=int(payload.get("end_offset") or 0),
            color=color, side=side, scope=scope, group_id=group_id,
            anchor_hash=sha256_text(
                (payload.get("selected_text") or "") + block_id + side)[:64],
        )
        sess.add(row)
        sess.flush()
        return _highlight_dict(row)


def update_highlight(highlight_id: str, payload: dict) -> dict:
    """改一条高亮：现在只开放**颜色**（`color`）与 `selected_text`（引用文字）。

    为什么不动偏移量：偏移量是"渲染文字里的位置"，改了它就必须同时重画文字 ——
    那等于把这条高亮挪到别处，用户看不出来（agent 的"改高亮"需求是改颜色/改错标的文字，
    要换位置就删了重建，语义更清楚）。
    """
    with session_scope() as sess:
        row = sess.get(Highlight, highlight_id)
        if row is None:
            raise NotFoundError(f"高亮不存在：{highlight_id}")
        color = payload.get("color")
        if color is not None:
            row.color = str(color) if str(color) in HIGHLIGHT_COLORS else row.color
        if payload.get("selected_text") is not None:
            row.selected_text = str(payload["selected_text"])
        sess.flush()
        return _highlight_dict(row)


def list_highlights(paper_id: str, user_id: int = 1) -> dict:
    with session_scope() as sess:
        rows = (sess.query(Highlight)
                .filter(Highlight.paper_id == paper_id, Highlight.user_id == user_id)
                .order_by(Highlight.created_at.asc()).all())
        return {"items": [_highlight_dict(r) for r in rows], "count": len(rows)}


def delete_highlight(highlight_id: str) -> dict:
    """删一条高亮；如果它属于某个跨行分组，**整组一起删**（用户眼里那是一条高亮）。

    返回被删掉的 id 列表：前端据此把本地那几条一起摘掉，不用重新拉一遍列表。

    **幂等**：id 不存在时返回空列表，而不是 404。因为"整组删"这件事天然会带来重复请求 ——
    前端点一次删除可能对同组的几条各发一次（多标签页同开一篇也会撞车），
    重复删同一条不该算失败；以前会往日志里丢 404，看着像删除坏了（用户报过）。
    """
    with session_scope() as sess:
        row = sess.get(Highlight, highlight_id)
        if row is None:
            return {"deleted": highlight_id, "deleted_ids": [], "count": 0, "missing": True}
        rows = [row]
        if row.group_id:
            rows = (sess.query(Highlight)
                    .filter(Highlight.group_id == row.group_id,
                            Highlight.paper_id == row.paper_id,
                            Highlight.user_id == row.user_id).all())
        ids = [r.id for r in rows]
        for r in rows:
            sess.delete(r)
    return {"deleted": highlight_id, "deleted_ids": ids, "count": len(ids)}


def anchor_page(block_id: str | None) -> int | None:
    """锚点 → 页码（**三种面都能解析**，因为页码本来就编码在锚点里）。

    - 正文行 id：`p004-02` → 第 4 页（行 id 就是"位置编号"）；
    - Markdown 视图：`md:p8` → 第 8 页；
    - 原 PDF 页图：`pdf:p3` → 第 3 页。

    为什么放在这里而不是只靠 `reader_service` 那份映射：侧栏列表（`/notes`、`/highlights`）也要
    "点了跳到哪一页"，而那条链路上没有行/块映射表。老锚点（`attentionisa-p04-b06` 这种解析块 id）
    解不出来就返回 None，由前端退回"只按页提示"。
    """
    if not block_id:
        return None
    text = str(block_id).strip()
    if (m := re.match(r"^(?:md|pdf):p(\d+)$", text)):
        return int(m.group(1))
    if (m := re.match(r"^p(\d{1,5})-\d+$", text)):
        return int(m.group(1))
    return None


def _highlight_dict(row: Highlight) -> dict:
    return {"id": row.id, "paper_id": row.paper_id, "block_id": row.block_id,
            "text": row.selected_text, "start": row.start_offset,
            "end": row.end_offset, "color": row.color,
            "side": row.side or "en", "scope": row.scope or "row",
            "group_id": row.group_id,
            "page": anchor_page(row.block_id),
            "created_at": iso_utc(row.created_at)}



# --------------------------------------------------------------------------
# 笔记（规划 §10 ⑥）
# --------------------------------------------------------------------------


def create_note(payload: dict, user_id: int = 1) -> dict:
    if not payload.get("paper_id"):
        raise ValidationError("paper_id 必填")
    with session_scope() as sess:
        row = Note(
            id=new_id("n"), user_id=user_id, paper_id=payload["paper_id"],
            block_id=payload.get("block_id"),
            highlight_id=payload.get("highlight_id"),
            selected_text=payload.get("selected_text"),
            start_offset=payload.get("start_offset"),
            end_offset=payload.get("end_offset"),
            content_md=payload.get("content_md") or "",
            tags=",".join(payload.get("tags") or []) if isinstance(
                payload.get("tags"), list) else payload.get("tags"),
        )
        sess.add(row)
        sess.flush()
        return _note_dict(row)


def update_note(note_id: str, payload: dict) -> dict:
    with session_scope() as sess:
        row = sess.get(Note, note_id)
        if row is None:
            raise NotFoundError(f"笔记不存在：{note_id}")
        for field in ("content_md", "selected_text", "block_id"):
            if field in payload and payload[field] is not None:
                setattr(row, field, payload[field])
        if "tags" in payload and payload["tags"] is not None:
            tags = payload["tags"]
            row.tags = ",".join(tags) if isinstance(tags, list) else tags
        sess.flush()
        return _note_dict(row)


def list_notes(paper_id: str | None = None, user_id: int = 1,
               q: str | None = None) -> dict:
    with session_scope() as sess:
        query = sess.query(Note).filter(Note.user_id == user_id)
        if paper_id:
            query = query.filter(Note.paper_id == paper_id)
        if q:
            query = query.filter(Note.content_md.like(f"%{q}%"))
        rows = query.order_by(Note.updated_at.desc()).all()
        return {"items": [_note_dict(r) for r in rows], "count": len(rows)}


def delete_note(note_id: str) -> dict:
    with session_scope() as sess:
        row = sess.get(Note, note_id)
        if row is None:
            raise NotFoundError(f"笔记不存在：{note_id}")
        sess.delete(row)
    return {"deleted": note_id}


def _note_dict(row: Note) -> dict:
    return {"id": row.id, "paper_id": row.paper_id, "block_id": row.block_id,
            "highlight_id": row.highlight_id, "selected_text": row.selected_text,
            "content_md": row.content_md,
            # 页码与"面"都给出来：侧栏列表要显示"这条笔记挂在哪、点它能跳到哪"
            # （`scope` 从锚点前缀推：`pdf:` / `md:` / 其余 = 正文行）
            "page": anchor_page(row.block_id),
            "scope": ("pdf" if str(row.block_id or "").startswith("pdf:")
                      else "markdown" if str(row.block_id or "").startswith("md:") else "row"),
            "tags": [t for t in (row.tags or "").split(",") if t],
            "created_at": iso_utc(row.created_at),

            "updated_at": iso_utc(row.updated_at)}



# --------------------------------------------------------------------------
# AI 批注（规划 §10 ⑦、§21.9、§21.10）
# --------------------------------------------------------------------------


def list_annotations(paper_id: str, *, type_: str | None = None,
                     user_id: int = 1) -> dict:
    with session_scope() as sess:
        q = sess.query(Annotation).filter(Annotation.paper_id == paper_id,
                                          Annotation.user_id == user_id,
                                          Annotation.status == "active")
        if type_:
            q = q.filter(Annotation.type == type_)
        rows = q.order_by(Annotation.created_at.asc()).all()
        return {"items": [_annotation_dict(r) for r in rows], "count": len(rows)}


def create_annotation(payload: dict, user_id: int = 1) -> dict:
    if not payload.get("paper_id"):
        raise ValidationError("paper_id 必填")
    with session_scope() as sess:
        row = Annotation(
            id=new_id("a"), user_id=user_id, paper_id=payload["paper_id"],
            block_id=payload.get("block_id"),
            type=payload.get("type") or "explanation",
            content_md=payload.get("content_md") or "",
            source=payload.get("source") or "manual",
            sources_json=json_dumps(payload.get("sources") or []),
            model_id=payload.get("model_id"),
            confidence=payload.get("confidence"),
            task_id=payload.get("task_id"),
            status="active",
        )
        sess.add(row)
        sess.flush()
        data = _annotation_dict(row)
    bus.publish("papers", EVENT_ANNOTATION, data)
    return data


def update_annotation(annotation_id: str, payload: dict) -> dict:
    with session_scope() as sess:
        row = sess.get(Annotation, annotation_id)
        if row is None:
            raise NotFoundError(f"批注不存在：{annotation_id}")
        if payload.get("content_md") is not None:
            row.content_md = payload["content_md"]
        if payload.get("status") is not None:
            row.status = payload["status"]
        sess.flush()
        return _annotation_dict(row)


def delete_annotation(annotation_id: str) -> dict:
    with session_scope() as sess:
        row = sess.get(Annotation, annotation_id)
        if row is None:
            raise NotFoundError(f"批注不存在：{annotation_id}")
        row.status = "deleted"
    return {"deleted": annotation_id}


def _annotation_dict(row: Annotation) -> dict:
    return {"id": row.id, "paper_id": row.paper_id, "block_id": row.block_id,
            "type": row.type, "content_md": row.content_md, "source": row.source,
            "sources": json_loads(row.sources_json, []) or [],
            "model_id": row.model_id, "confidence": row.confidence,
            "status": row.status,
            "created_at": iso_utc(row.created_at)}



# --------------------------------------------------------------------------
# 划词解释的**落库**（解释本身由问答 agent 产出：`POST /api/v1/agent/ask` + `selection`）
#
# 旧实现 `explain_selection` 已删除：它自己拼 prompt（段落原文 + 译文 + 邻段 + 选区）
# 直接调 generation provider，产出物不带引用、不能跳转，与问答那条链路的口径也不一致。
# 现在划词走问答 agent —— 它会用 `block_get` 展开选区所在段落、按需读相邻页，答案带 `[n]`
# 引用可跳转，而且**作为一轮对话留在会话里**（这就是「划词解释历史」的数据来源）。
# --------------------------------------------------------------------------


def save_explanation_as_annotation(payload: dict, user_id: int = 1) -> dict:
    """把 agent 给出的划词解释存成批注（`source=selection`）。

    这里**不调用任何模型**：内容来自问答 agent 那一轮，本函数只负责"把这段话记下来"。
    所以它同时是前端「划词解释历史」的写入端。
    """
    data = dict(payload)
    data["type"] = payload.get("type") or "qa"
    data["source"] = "selection"
    data["content_md"] = payload.get("content_md") or payload.get("explanation") or ""
    return create_annotation(data, user_id)


def notes_overview(user_id: int = 1) -> dict:
    """跨论文笔记/高亮/批注聚合（规划 §22 /notes）。"""
    with session_scope() as sess:
        notes = (sess.query(Note).filter(Note.user_id == user_id)
                 .order_by(Note.updated_at.desc()).limit(200).all())
        highlights = (sess.query(Highlight).filter(Highlight.user_id == user_id)
                      .order_by(Highlight.created_at.desc()).limit(200).all())
        annotations = (sess.query(Annotation)
                       .filter(Annotation.user_id == user_id,
                               Annotation.status == "active")
                       .order_by(Annotation.created_at.desc()).limit(200).all())
        titles = {p.id: p.title for p in sess.query(Paper).all()}
    return {
        "notes": [{**_note_dict(n), "paper_title": titles.get(n.paper_id)} for n in notes],
        "highlights": [{**_highlight_dict(h), "paper_title": titles.get(h.paper_id)}
                       for h in highlights],
        "annotations": [{**_annotation_dict(a), "paper_title": titles.get(a.paper_id)}
                        for a in annotations],
    }
