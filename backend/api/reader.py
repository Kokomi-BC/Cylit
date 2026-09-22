"""阅读器数据 API（规划 §20.2）与 SSE 推送（规划 §20 SSE 事件类型）。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import StreamingResponse

from backend.api.response import envelope
from backend.core.events import (
    EVENT_ANNOTATION, EVENT_BLOCK_TRANSLATED, EVENT_PAGE_TRANSLATED,
    EVENT_PAPER_STATUS, EVENT_PDF_PAGE_DONE, EVENT_RAG_TOKEN, EVENT_TASK_PROGRESS,
    bus,
)
from backend.services.paper import reader_service

router = APIRouter(tags=["reader"])


# --------------------------------------------------------------------------
# §20.2 阅读器数据
# --------------------------------------------------------------------------


@router.get("/papers/{paper_id}/reader")
async def reader_payload(paper_id: str, request: Request,
                         since: int | None = Query(None, description="增量：只取该 order 之后的块"),
                         include_annotations: bool = Query(True)):
    """装配 reader.json（逐页译文行 + 解析块 + 双语目录 + 进度 + 批注 + 任务状态）。"""
    data = reader_service.build_reader(paper_id, since=since,
                                       include_annotations=include_annotations)
    return envelope(data, request=request)


@router.get("/papers/{paper_id}/content")
async def paper_content(paper_id: str, request: Request,
                        since: int = Query(0), limit: int = Query(200, le=1000)):
    return envelope(reader_service.content_since(paper_id, since=since, limit=limit),
                    request=request)


@router.get("/papers/{paper_id}/translations")
async def paper_translations(paper_id: str, request: Request,
                             since_page: int = Query(0),
                             page: list[int] | None = Query(None)):
    """页级译文增量（前端 SSE 断线时的轮询兜底）。"""
    return envelope(
        reader_service.translations_since(paper_id, since_page=since_page,
                                          pages=page),
        request=request)


@router.get("/papers/{paper_id}/markdown/translated")
async def paper_translated_markdown(paper_id: str, request: Request,
                                    write: bool = Query(True)):
    """**带插图的双语 Markdown**（逐页原文/译文 + 整篇合并版）。

    页级翻译产物只有文字，模型不会自己写 ``![]()``；这里按图号把插图行插到对应图注
    上方，并保证裁图已落盘，于是任何 Markdown 渲染器都能看到图。
    同时落盘 ``data/papers/<id>/md/translated.md``（图片用相对路径）。
    """
    data = reader_service.bilingual_markdown(paper_id, write=write)
    return envelope(data, request=request)


@router.get("/papers/{paper_id}/blocks/{block_id}")
async def block_detail(paper_id: str, block_id: str, request: Request):
    return envelope(reader_service.block_detail(paper_id, block_id), request=request)


@router.get("/papers/{paper_id}/formulas")
async def paper_formulas(paper_id: str, request: Request,
                         source: str = Query("markdown", pattern="^(markdown|parse)$"),
                         grade: str | None = Query(None, pattern="^[ABC]$")):
    """公式列表：**默认从 Markdown（页级产物）里提取**，只读不复核。

    ``source=parse`` 才回解析阶段的公式卡（含裁图与三重校验证据），仅作解析质检参考。
    """
    del grade  # 分级只对解析阶段的公式卡有意义；从 Markdown 抽出来的公式不分级
    return envelope(reader_service.formulas_payload(paper_id, source), request=request)


@router.get("/papers/{paper_id}/figures")
async def paper_figures(paper_id: str, request: Request):
    """插图列表（含体检状态）。"""
    return envelope(reader_service.figures_payload(paper_id), request=request)


@router.get("/papers/{paper_id}/outline")
async def paper_outline(paper_id: str, request: Request):
    """目录（按 **Markdown 标题结构**，规划 §18 结构导航）。

    层级与 Markdown 事实源同源：原生页来自解析器标题判定，扫描页来自视觉模型返回的
    `## / ###`。返回 `{items（扁平）, tree（嵌套）, count}`；伪代码假标题已被剔除。
    """
    return envelope(reader_service.build_outline(paper_id), request=request)


@router.get("/papers/{paper_id}/progress")
async def get_progress(paper_id: str, request: Request):
    return envelope(reader_service.get_progress(paper_id), request=request)


@router.put("/papers/{paper_id}/progress")
async def put_progress(paper_id: str, request: Request, payload: dict = Body(...)):
    """保存阅读进度（含视图模式与中英顺序，规划 §10 ⑧）。"""
    return envelope(reader_service.save_progress(paper_id, payload), request=request)


# --------------------------------------------------------------------------
# SSE（规划 §20：block_translated / pdf_page_done / task_progress / rag_token）
# --------------------------------------------------------------------------

ALL_EVENTS = {EVENT_PAGE_TRANSLATED, EVENT_BLOCK_TRANSLATED, EVENT_PDF_PAGE_DONE,
              EVENT_TASK_PROGRESS, EVENT_RAG_TOKEN, EVENT_PAPER_STATUS,
              EVENT_ANNOTATION}


@router.get("/events/stream")
async def events_stream(request: Request,
                        topic: str = Query("global"),
                        since_seq: int = Query(0),
                        types: str | None = Query(None, description="逗号分隔的事件类型过滤"),
                        heartbeat: float = Query(15.0)):
    """通用 SSE 流（任务进度 / 译文 / 批注）。"""
    wanted = {t.strip() for t in types.split(",") if t.strip()} if types else None
    if topic not in ("global", "tasks", "papers", "rag"):
        topic = "global"
    generator = bus.stream(topic, since_seq=since_seq, heartbeat=heartbeat,
                           wanted=wanted)
    return StreamingResponse(
        generator, media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@router.get("/papers/{paper_id}/translations/stream")
async def translation_stream(paper_id: str, request: Request,
                             since_seq: int = Query(0),
                             heartbeat: float = Query(15.0)):
    """产线 A 逐页译文流（前端 LiveUpdater 订阅，规划 §22 关键点 2）。"""

    async def filtered():
        async for frame in bus.stream("papers", since_seq=since_seq,
                                      heartbeat=heartbeat,
                                      wanted={EVENT_PAGE_TRANSLATED,
                                              EVENT_BLOCK_TRANSLATED,
                                              EVENT_PAPER_STATUS}):
            if paper_id in frame:
                yield frame

    return StreamingResponse(filtered(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
