"""知识库服务（产线 C，本轮只做"可运行骨架 + LanceDB 入库"）。

规划 §11 / §21.6：chunk 化 → Embedding → 写入 MySQL chunk 元数据 → 写 LanceDB。
检索（向量/词法/混合/重排/RAG 问答）在下一轮按阶段 14–17 实现；
本模块先把 chunk 与索引链路打通，保证「删除论文 → 清索引」与「重建索引」可用。
"""

from __future__ import annotations

from backend.core.errors import NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, new_id, now, sha256_text, truncate
from backend.repositories.mysql.models import Block, Chunk, Paper
from backend.repositories.mysql.session import session_scope
from backend.services.model.model_manager import model_manager
from backend.services.paper import paper_service, reader_service

log = get_logger(__name__)

# 四类 chunk（规划 §10 ⑫ chunk_type）
CHUNK_TYPES = ("text", "formula", "figure", "table")
# 目标 chunk 字符数（段合并上限）
TARGET_CHUNK_CHARS = 900
MIN_CHUNK_CHARS = 40


# --------------------------------------------------------------------------
# Chunk 化（规划 §21.6 步骤 1）
# --------------------------------------------------------------------------


def build_chunks_for_paper(paper_id: str) -> list[dict]:
    """按 IR + Markdown 生成四类 chunk。"""
    md_map = reader_service.load_markdown_map(paper_id)
    from backend.parsing.ir.ir_reader import IRReader

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    formulas = {f["block_id"]: f for f in ir.formulas()}
    figures = {f["block_id"]: f for f in ir.figures()}
    tables = {t["block_id"]: t for t in ir.tables()}

    with session_scope() as sess:
        blocks = (sess.query(Block).filter(Block.paper_id == paper_id)
                  .order_by(Block.order_global.asc()).all())
        rows = [_block_row(b, md_map) for b in blocks]

    chunks: list[dict] = []
    buffer: list[dict] = []
    buffer_chars = 0

    def flush_text() -> None:
        nonlocal buffer, buffer_chars
        if not buffer:
            return
        text = "\n\n".join(b["text"] for b in buffer if b["text"])
        if len(text) >= MIN_CHUNK_CHARS:
            chunks.append(_make_chunk(
                paper_id, "text", text, buffer[0], buffer[-1],
                section=buffer[0].get("section") or "",
                metadata={"block_ids": [b["id"] for b in buffer],
                          "pages": sorted({b["page"] for b in buffer})},
            ))
        buffer = []
        buffer_chars = 0

    for b in rows:
        bid = b["id"]
        if b["type"] == "heading":
            flush_text()
            buffer = [b]
            buffer_chars = len(b["text"])
            continue
        if b["type"] in ("ref",):
            continue
        if bid in formulas:
            flush_text()
            f = formulas[bid]
            latex = f.get("latex_final") or f.get("latex_repaired") or ""
            cands = f.get("latex_candidate") or []
            text = f"公式 {bid}：{latex or (cands[0] if cands else f.get('raw_text') or '')}"
            chunks.append(_make_chunk(
                paper_id, "formula", text, b, b, section=b.get("section") or "",
                metadata={"latex": latex, "grade": f.get("grade"),
                          "raw_text": f.get("raw_text"),
                          "crop": f.get("image_path")},
            ))
            continue
        if bid in figures:
            flush_text()
            f = figures[bid]
            text = f"插图 {bid}：{f.get('caption') or ''}"
            chunks.append(_make_chunk(
                paper_id, "figure", text, b, b, section=b.get("section") or "",
                metadata={"caption": f.get("caption"), "image_path": f.get("image_path")},
            ))
            continue
        if bid in tables:
            flush_text()
            t = tables[bid]
            text = f"表格 {bid}：{t.get('caption') or ''}"
            chunks.append(_make_chunk(
                paper_id, "table", text, b, b, section=b.get("section") or "",
                metadata={"caption": t.get("caption"), "image_path": t.get("image_path")},
            ))
            continue

        text = b["text"] or ""
        if not text:
            continue
        buffer.append(b)
        buffer_chars += len(text)
        if buffer_chars >= TARGET_CHUNK_CHARS:
            flush_text()
    flush_text()
    return chunks


def _block_row(b: Block, md_map: dict | None = None) -> dict:
    import json

    meta = {}
    try:
        meta = json.loads(b.meta_json or "{}")
    except ValueError:
        meta = {}
    # 正文只在 Markdown（规划 §8 铁律），MySQL 里不存正文
    text = ((md_map or {}).get(b.id, {}) or {}).get("text") or b.preview or ""
    return {"id": b.id, "page": b.page_no, "type": b.type, "order": b.order_global,
            "text": text, "hash": b.content_hash,
            "section": meta.get("section_title") or b.section_id or ""}


def _make_chunk(paper_id: str, chunk_type: str, content: str, first: dict,
                last: dict, *, section: str = "", metadata: dict | None = None) -> dict:
    cid = f"{paper_id}:{chunk_type}:{first['id']}"
    if chunk_type == "text" and first["id"] != last["id"]:
        cid = f"{paper_id}:text:{first['id']}-{last['id']}"
    return {
        "id": cid, "paper_id": paper_id, "chunk_type": chunk_type,
        "page_no": first["page"], "order_global": first["order"],
        "block_id": first["id"], "end_block_id": last["id"],
        "section": section, "content": content,
        "content_hash": sha256_text(content),
        "content_preview": truncate(content, 200),
        "token_count": _approx_tokens(content),
        "metadata": metadata or {},
    }


def _approx_tokens(text: str) -> int:
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return int(cjk / 1.5 + (len(text) - cjk) / 4) + 1


# --------------------------------------------------------------------------
# 入库（规划 §21.6 步骤 2–5）
# --------------------------------------------------------------------------


def index_paper(paper_id: str, *, rebuild: bool = False,
                embed: bool = True, progress=None) -> dict:
    """EMBED + INDEX：写 MySQL chunk 元数据 + 生成向量 + 写 LanceDB。"""
    paper_service.get_paper(paper_id)
    chunks = build_chunks_for_paper(paper_id)
    if not chunks:
        raise ValidationError(
            f"没有可用 chunk：{paper_id} 是否尚未解析？")

    cfg = model_manager.get_config(None, "embedding")
    model_id = cfg.get("id")
    dim = (cfg.get("parameters") or {}).get("dim")

    # 索引版本号（换模型即重建，规划 §11.3）
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        prev = sess.query(Chunk).filter(Chunk.paper_id == paper_id).first()
        index_version = int(prev.index_version or 0) + 1 if prev else 1
        sess.query(Chunk).filter(Chunk.paper_id == paper_id).delete()
        for c in chunks:
            sess.add(Chunk(
                id=c["id"], paper_id=paper_id, block_id=c["block_id"],
                chunk_type=c["chunk_type"], page_no=c["page_no"],
                order_global=c["order_global"], section=c["section"],
                content_preview=c["content_preview"],
                content_hash=c["content_hash"], token_count=c["token_count"],
                index_version=index_version, embedding_model_id=model_id,
                metadata_json=json_dumps(c["metadata"]),
            ))
        if row is not None and row.status not in ("translated", "indexed"):
            if row.status == "parsed":
                row.status = "parsed"

    embedded = 0
    vector_error = None
    if embed:
        try:
            provider = model_manager.get_embedding()
            batch = int((cfg.get("parameters") or {}).get("batch") or 32)
            vectors: list[list[float]] = []
            for i in range(0, len(chunks), batch):
                part = chunks[i:i + batch]
                vectors.extend(provider.embed([c["content"] for c in part]))
                if progress:
                    progress(min(i + batch, len(chunks)), len(chunks))
            dim = len(vectors[0]) if vectors else dim
            model_manager.remember_dim(cfg, dim)
            from backend.repositories.vector.lancedb_store import vector_store

            vector_store.upsert_chunks(chunks, vectors, index_version,
                                       embedding_model_id=model_id)
            embedded = len(vectors)
        except Exception as exc:  # noqa: BLE001
            vector_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("向量入库失败（MySQL chunk 元数据已写入）：%s", exc)

    return {
        "paper_id": paper_id, "chunks": len(chunks),
        "embedded": embedded, "index_version": index_version,
        "model_id": model_id, "dim": dim,
        "by_type": _count_by_type(chunks),
        "vector_error": vector_error,
        "rebuilt": rebuild,
        "at": now().isoformat(timespec="seconds"),
    }


def _count_by_type(chunks: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in chunks:
        out[c["chunk_type"]] = out.get(c["chunk_type"], 0) + 1
    return out


def overview() -> dict:
    """知识库概览（规划 §20.5）。"""
    from sqlalchemy import func

    with session_scope() as sess:
        papers = sess.query(Paper).filter(Paper.deleted_at.is_(None)).all()
        chunk_count = sess.query(func.count(Chunk.id)).scalar() or 0
        by_type = dict(sess.query(Chunk.chunk_type, func.count(Chunk.id))
                       .group_by(Chunk.chunk_type).all())
        index_versions = dict(sess.query(Chunk.paper_id, func.max(Chunk.index_version))
                              .group_by(Chunk.paper_id).all())
        model_ids = [m for (m,) in sess.query(Chunk.embedding_model_id).distinct().all()]
    vector = None
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        vector = vector_store.stats()
    except Exception:  # noqa: BLE001
        vector = {"available": False}
    return {
        "papers_total": len(papers),
        "papers_in_kb": sum(1 for p in papers if p.in_knowledge_base),
        "chunks": int(chunk_count),
        "chunks_by_type": by_type,
        "embedding_models": [m for m in model_ids if m],
        "index_versions": index_versions,
        "vector_store": vector,
        "items": _overview_items(papers),
    }


def _overview_items(papers: list[Paper]) -> list[dict]:
    from sqlalchemy import func

    with session_scope() as sess:
        counts = dict(sess.query(Chunk.paper_id, func.count(Chunk.id))
                      .group_by(Chunk.paper_id).all())
    return [{"id": p.id, "title": p.title, "status": p.status,
             "in_knowledge_base": bool(p.in_knowledge_base),
             "chunks": int(counts.get(p.id, 0))} for p in papers]


def set_kb_membership(paper_id: str, include: bool) -> dict:
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is None or row.deleted_at is not None:
            raise NotFoundError(f"论文不存在：{paper_id}")
        row.in_knowledge_base = include
        sess.flush()
        return {"paper_id": paper_id, "in_knowledge_base": include}


def index_status(paper_id: str | None = None) -> dict:
    from sqlalchemy import func

    with session_scope() as sess:
        q = sess.query(Chunk)
        if paper_id:
            q = q.filter(Chunk.paper_id == paper_id)
        total = q.count()
        with_hash = q.filter(Chunk.content_hash.isnot(None)).count()
        versions = (sess.query(Chunk.paper_id, func.max(Chunk.index_version))
                    .group_by(Chunk.paper_id).all())
    vector = None
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        vector = {"available": True, "count": vector_store.count(paper_id=paper_id)}
    except Exception as exc:  # noqa: BLE001
        vector = {"available": False, "error": str(exc)[:200]}
    return {"paper_id": paper_id, "chunks": total, "chunks_with_hash": with_hash,
            "index_versions": dict(versions), "vector": vector}


def new_chunk_id(paper_id: str, chunk_type: str) -> str:
    return f"{paper_id}:{chunk_type}:{new_id('c')[-8:]}"
