"""知识库 API（规划 §20.5）。

本轮范围：概览 / 纳入移出 / 触发 EMBED+INDEX / 索引状态。
检索与 RAG 问答（§20.4）在阶段 14–17 实现，接口给出明确未实现提示。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

from backend.api.response import envelope
from backend.services.knowledge import knowledge_service
from backend.services.task.task_service import task_service

router = APIRouter(tags=["knowledge"])


@router.get("/knowledge/overview")
async def overview(request: Request):
    return envelope(knowledge_service.overview(), request=request)


@router.post("/knowledge/papers")
async def set_membership(request: Request, payload: dict = Body(...)):
    """兼容保留：只翻"已纳入"标记（不动索引）。

    界面已经不用它了 —— 用户口径是**建索引即纳入**、要退出就用 ``/knowledge/remove``
    （那条会把索引一起清掉，两侧不会长期不一致）。
    """
    paper_id = payload.get("paper_id")
    include = bool(payload.get("include", True))
    return envelope(knowledge_service.set_kb_membership(paper_id, include),
                    request=request)


@router.post("/knowledge/remove")
async def remove_from_kb(request: Request, payload: dict = Body(...)):
    """**从知识库移除**：清向量 + 清 chunk 元数据 + 取消"已纳入"（论文与译文不动）。"""
    paper_id = payload.get("paper_id")
    if not paper_id:
        from backend.core.errors import ValidationError

        raise ValidationError("缺少 paper_id")
    result = knowledge_service.remove_from_kb(paper_id)
    return envelope(result, message=f"已从知识库移除（清 {result['removed_chunks']} 条 chunk）",
                    request=request)


@router.post("/knowledge/reconcile")
async def reconcile_knowledge(request: Request, payload: dict = Body(default={})):
    """对账：MySQL chunk 元数据 ↔ 向量库；``{fix: true}`` 时顺手清掉孤儿。"""
    return envelope(knowledge_service.reconcile(
        paper_id=payload.get("paper_id"), fix=bool(payload.get("fix"))), request=request)


@router.post("/knowledge/index")
async def trigger_index(request: Request, payload: dict = Body(default={})):
    paper_id = payload.get("paper_id")
    rebuild = bool(payload.get("rebuild"))
    task, created = task_service.get_or_create(
        "INDEX", "knowledge", paper_id,
        params={"rebuild": rebuild}, total_items=0,
        message="排队中（产线 C：chunk + embedding + 入库）",
    )
    return envelope(task, message=("已创建索引任务" if created else "已有进行中的索引任务"),
                    request=request)


@router.get("/knowledge/index/status")
async def index_status(request: Request, paper_id: str | None = Query(None)):
    return envelope(knowledge_service.index_status(paper_id), request=request)


@router.get("/knowledge/chunks")
async def list_chunks(request: Request, paper_id: str = Query(...),
                      chunk_type: str | None = Query(None),
                      lang: str | None = Query(None),
                      limit: int = Query(50, le=500)):
    """列出 chunk 元数据（**不返回全文**：全文在 LanceDB，规划 §8 三条禁止）。

    `image_url` 是插图/表格/公式的裁图（浏览器可直接取，用于知识库页的缩略图 / 图库）；
    正文条目为 `None`。
    """
    from backend.core.utils import json_loads
    from backend.repositories.mysql.models import Chunk
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        q = sess.query(Chunk).filter(Chunk.paper_id == paper_id)
        if chunk_type:
            q = q.filter(Chunk.chunk_type == chunk_type)
        rows = q.order_by(Chunk.order_global.asc()).limit(limit if not lang else 5000).all()
        items = []
        for c in rows:
            meta = json_loads(c.metadata_json, {}) or {}
            row_lang = meta.get("lang") or "en"
            # `any` = 语言无关（公式原子行）：按中文、英文过滤时都要出现
            if lang and row_lang not in (lang, "any"):
                continue
            items.append({"id": c.id, "paper_id": c.paper_id, "block_id": c.block_id,
                          "chunk_type": c.chunk_type, "page": c.page_no,
                          "lang": row_lang,
                          "section": c.section, "preview": c.content_preview,
                          "caption": meta.get("caption"),
                          "hash": c.content_hash, "tokens": c.token_count,
                          "index_version": c.index_version,
                          "embedding_model_id": c.embedding_model_id,
                          "image_url": knowledge_service.chunk_image_url(
                              c.paper_id, c.chunk_type, int(c.page_no or 0), meta)})
            if len(items) >= limit:
                break
    return envelope({"items": items, "count": len(items)}, request=request)


@router.get("/knowledge/chunk")
async def chunk_detail(request: Request, chunk_id: str = Query(...)):
    """**单个 chunk 的完整正文**（知识库页「展开全文」）。

    列表接口只给 200 字 preview（规划 §8：全文只在向量库）；这条只读那一条向量行，
    不触发任务、不写状态。不做中英对照 —— 完整阅读回阅读器。
    """
    return envelope(knowledge_service.chunk_detail(chunk_id), request=request)


# ---------------- §20.4 相似检索（RAG 问答见下一轮的 /rag/query） ----------------


@router.post("/rag/similar")
async def rag_similar(request: Request, payload: dict = Body(default={})):
    """跨文献 / 全库相似检索：向量 + 词法 → RRF → 重排（任务书功能⑥）。

    请求体：``{query?}`` 或 ``{chunk_id?}``；``scope`` = all | cross | paper；
    可选 ``paper_id`` / ``chunk_types`` / ``langs`` / ``top_k``。
    """
    return envelope(knowledge_service.similar(payload), request=request)


@router.post("/rag/query")
async def rag_query(request: Request, payload: dict = Body(...)):
    """RAG 问答（§20.4）：混合检索 → ContextBuilder → 生成 → AnswerValidator。

    请求体：``{question, scope: current_paper|knowledge_base|cross, paper_id?, top_k?}``；
    响应：``{implemented, answer, sources[], grounded, validation, retrieval_metadata, usage}``。
    """
    from backend.services.knowledge import rag_service

    return envelope(rag_service.answer(payload), request=request)


@router.post("/rag/query/stream")
def rag_query_stream(payload: dict = Body(...)):
    """RAG 流式问答（SSE）：``rag_meta`` → ``rag_token``* → ``rag_done`` / ``rag_error``。

    同步生成器 + ``StreamingResponse``：同步 SDK 的流式接口在线程池里迭代，
    不阻塞事件循环。
    """
    from fastapi.responses import StreamingResponse

    from backend.services.knowledge import rag_service

    return StreamingResponse(rag_service.stream_answer(payload),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
