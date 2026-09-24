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
    paper_id = payload.get("paper_id")
    include = bool(payload.get("include", True))
    return envelope(knowledge_service.set_kb_membership(paper_id, include),
                    request=request)


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
    """列出 chunk 元数据（**不返回全文**：全文在 LanceDB，规划 §8 三条禁止）。"""
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
            if lang and (meta.get("lang") or "en") != lang:
                continue
            items.append({"id": c.id, "paper_id": c.paper_id, "block_id": c.block_id,
                          "chunk_type": c.chunk_type, "page": c.page_no,
                          "lang": meta.get("lang") or "en",
                          "section": c.section, "preview": c.content_preview,
                          "hash": c.content_hash, "tokens": c.token_count,
                          "index_version": c.index_version,
                          "embedding_model_id": c.embedding_model_id})
            if len(items) >= limit:
                break
    return envelope({"items": items, "count": len(items)}, request=request)


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
