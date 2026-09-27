"""批注 / 笔记 / 高亮服务（规划 §10 ⑤⑥⑦、§21.10 划词 AI 解释）。

- 高亮锚定 ``block_id + 字符偏移``（规划 §10 ⑤），重译后按 ``anchor_hash`` 重定位；
- 划词解释：缓存命中则秒回；否则小上下文 + LLM → 浮动卡片 → 可存为批注。
"""

from __future__ import annotations

from backend.core.config import get_settings
from backend.core.errors import NotFoundError, ValidationError
from backend.core.events import EVENT_ANNOTATION, bus
from backend.core.logging import get_logger
from backend.core.utils import (
    iso_utc, json_dumps, json_loads, new_id, now, sha256_text, short_hash, truncate,
)
from backend.repositories.mysql.models import (
    HIGHLIGHT_SCOPES, HIGHLIGHT_SIDES, Annotation, Block, Highlight, Note, Paper,
)
from backend.repositories.mysql.session import session_scope
from backend.services.paper import reader_service
from backend.services.model.model_manager import model_manager

log = get_logger(__name__)

HIGHLIGHT_COLORS = ("yellow", "green", "blue", "pink", "purple")

EXPLAIN_SYSTEM = """你是论文阅读助手。基于给定的段落与上下文，用简体中文解释用户选中的内容：
- 说明该内容在论文中的作用与含义；
- 涉及公式时解释符号含义；
- 只依据给定文本，不要引入外部知识；不确定就说不确定。
输出 3~6 句，可用 Markdown 列表。"""


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


def _highlight_dict(row: Highlight) -> dict:
    return {"id": row.id, "paper_id": row.paper_id, "block_id": row.block_id,
            "text": row.selected_text, "start": row.start_offset,
            "end": row.end_offset, "color": row.color,
            "side": row.side or "en", "scope": row.scope or "row",
            "group_id": row.group_id,
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
# 划词 AI 解释（规划 §21.10）
# --------------------------------------------------------------------------


def explain_selection(payload: dict, user_id: int = 1) -> dict:
    """选中文本 → 缓存命中秒回；否则小上下文 + LLM。"""
    paper_id = payload.get("paper_id")
    block_id = payload.get("block_id")
    selection = (payload.get("selected_text") or "").strip()
    if not paper_id or not selection:
        raise ValidationError("paper_id 与 selected_text 必填")

    cache_key = short_hash(f"{paper_id}|{block_id}|{selection}", 32)
    cached = _read_cache(cache_key)
    if cached:
        return {**cached, "cached": True}

    md_map = reader_service.load_markdown_map(paper_id)
    context = ""
    source_text = ""
    if block_id:
        source_text = md_map.get(block_id, {}).get("text", "")
        if not source_text:
            with session_scope() as sess:
                b = sess.get(Block, block_id)
                source_text = (b.preview if b else "") or ""
        context = _neighbor_context(paper_id, block_id)
    translation = md_map.get(block_id or "", {}).get("translation", "")

    prompt = f"""【所在段落原文】
{truncate(source_text or '(未知)', 1500)}

【该段译文】
{truncate(translation or '(暂无)', 800)}

【相邻段落（上下文）】
{truncate(context or '(无)', 800)}

【用户选中的内容】
{selection}

请解释用户选中的内容。"""

    provider = model_manager.get_generation()
    cfg = model_manager.get_config(None, "generation")
    try:
        res = provider.generate(prompt, system=EXPLAIN_SYSTEM, temperature=0.2)
        answer = res.text
        tokens = res.total_tokens
        model_name = res.model
    except Exception as exc:  # noqa: BLE001
        log.warning("划词解释失败：%s", exc)
        answer = f"（解释失败：{type(exc).__name__}：{str(exc)[:200]}）"
        tokens = 0
        model_name = cfg.get("model_name") or ""

    result = {
        "paper_id": paper_id, "block_id": block_id, "selected_text": selection,
        "explanation": answer, "model_id": cfg.get("id"),
        "model_name": model_name,
        "grounded": False,
        "sources": [{"block_id": block_id, "paper_id": paper_id,
                     "snippet": truncate(source_text, 200)}] if block_id else [],
        "prompt_hash": short_hash(prompt + EXPLAIN_SYSTEM, 16),
    }
    _write_cache(cache_key, result)
    _ = tokens
    return {**result, "cached": False}


def _neighbor_context(paper_id: str, block_id: str, window: int = 1) -> str:
    with session_scope() as sess:
        blk = sess.get(Block, block_id)
        if blk is None:
            return ""
        rows = (sess.query(Block)
                .filter(Block.paper_id == paper_id,
                        Block.order_global >= blk.order_global - window,
                        Block.order_global <= blk.order_global + window,
                        Block.id != block_id)
                .order_by(Block.order_global.asc()).all())
        md_map = reader_service.load_markdown_map(paper_id)
        return " ".join(truncate(md_map.get(r.id, {}).get("text", "") or
                                 (r.preview or ""), 300) for r in rows)


def save_explanation_as_annotation(payload: dict, user_id: int = 1) -> dict:
    """把划词解释存为批注（规划 §21.10 末段）。"""
    data = dict(payload)
    data["type"] = payload.get("type") or "qa"
    data["source"] = "selection"
    data["content_md"] = payload.get("content_md") or payload.get("explanation") or ""
    return create_annotation(data, user_id)


# --- 解释缓存（内存级；重启即失效，避免为演示引入额外存储）----------------

_explain_cache: dict[str, dict] = {}


def _read_cache(key: str) -> dict | None:
    return _explain_cache.get(key)


def _write_cache(key: str, value: dict) -> None:
    if len(_explain_cache) > 500:
        _explain_cache.clear()
    _explain_cache[key] = value


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
