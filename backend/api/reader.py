"""阅读器数据 API（规划 §20.2）与 SSE 推送（规划 §20 SSE 事件类型）。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
# 单个后台任务（响应发完后删掉临时 zip）：这一个类 fastapi 没有转出，从 starlette 拿
from starlette.background import BackgroundTask

from backend.api.response import envelope
from backend.core.config import get_settings
from backend.core.events import (
    EVENT_PAGE_TRANSCRIBED, EVENT_PAGE_TRANSLATED, EVENT_PAPER_STATUS, bus,
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


@router.get("/papers/{paper_id}/markdown/export")
async def paper_markdown_export(
    paper_id: str, request: Request,
    mode: str = Query("bilingual", pattern="^(original|translation|bilingual)$"),
):
    """下载**某一版**整篇 Markdown（原文 / 译文 / 中英对照）。

    - **不带页标识**（用户口径 2026-09-28）：没有 ``<!-- page N -->``、也没有 ``## 第 N 页``；
    - 图片补成**绝对**地址（按本次请求的 host 拼），下载下来点开就能看图；
    - 内容与 Markdown 视图里看到的一致（同一个 ``bilingual_markdown`` 口径）。

    走服务端而不是前端拼字符串：下载与打包（zip）共用同一份"三个版本"的实现，不会各写一套。
    """
    s = get_settings()
    base = str(request.base_url).rstrip("/")
    text = reader_service.export_markdown(paper_id, mode, img_prefix=s.api_prefix,
                                          absolute_base=base)
    return Response(
        content=text.encode("utf-8"), media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{paper_id}-{mode}.md"'})


@router.get("/papers/{paper_id}/export/zip")
async def paper_export_zip(paper_id: str):
    """打包下载：原 PDF + 三个版本的 Markdown + 被 Markdown 引用的插图（zip）。

    包内图片是**相对路径**，解压后离线可看；临时 zip 在响应发完后就删掉。
    """
    path = reader_service.export_bundle(paper_id)
    return FileResponse(str(path), media_type="application/zip",
                        filename=f"{paper_id}.zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


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


@router.get("/papers/{paper_id}/tables")
async def paper_tables(paper_id: str, request: Request):
    """表格列表：从**页级译文的 Markdown** 里抽（只读，不复核）。

    含表格说明（中文译文优先）与行/列数；未翻译的页不出条目 —— 解析产物里的 table 块
    只是按坐标拼起来的纯文本，画不成表（口径见 `reader_service.markdown_tables`）。
    """
    return envelope(reader_service.tables_payload(paper_id), request=request)


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

@router.get("/papers/{paper_id}/translations/stream")
async def translation_stream(paper_id: str, request: Request,
                             since_seq: int = Query(0),
                             heartbeat: float = Query(15.0)):
    """逐页产物流（前端 LiveUpdater 订阅，规划 §22 关键点 2）。

    发 **三个**事件：
    - `page_transcribed` —— A 路（英文转录）完成一页；
    - `page_translated` —— B 路（中文翻译）或融合模式完成一页；
    - `paper_status` —— 整篇状态。
    两路的 `rows` 都是**读时合并好的**那一页（见 `translation_service.emit`），
    所以前端拿到就能直接替换。
    ⚠️ `page_transcribed` 以前不在白名单里（只有 `page_translated`），于是 **A 路的事件
    在服务端就被丢掉了** —— 前端即使订阅也收不到，表现为"英文转录结果要刷新页面才出现"
    （2026-09-27 用户实测报的）。旧版逐块事件 `block_translated` 仍然不发（没有发布方）。
    """

    async def filtered():
        async for frame in bus.stream("papers", since_seq=since_seq,
                                      heartbeat=heartbeat,
                                      wanted={EVENT_PAGE_TRANSCRIBED,
                                              EVENT_PAGE_TRANSLATED,
                                              EVENT_PAPER_STATUS}):
            if paper_id in frame:
                yield frame

    return StreamingResponse(filtered(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
