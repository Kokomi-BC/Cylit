"""论文 / 解析 / 翻译 / 复核 API（规划 §20.1–§20.3）。"""

from __future__ import annotations

from fastapi import APIRouter, Body, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse

from backend.api.response import envelope
from backend.core.errors import ValidationError
from backend.services.paper import paper_service
from backend.services.translation import translation_service

router = APIRouter(tags=["papers"])


# --------------------------------------------------------------------------
# §20.1 论文与解析
# --------------------------------------------------------------------------


@router.post("/papers")
async def upload_paper(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    authors: str | None = Form(None),
    venue: str | None = Form(None),
    year: int | None = Form(None),
    tags: str | None = Form(None),
    paper_id: str | None = Form(None),
    auto_parse: bool = Form(True),
):
    """上传 PDF → 落盘 + 建 paper + 建 PARSE 任务（规划 §21.1）。"""
    data = await file.read()
    if not data:
        raise ValidationError("上传文件为空")
    tmp = paper_service.save_upload(data, file.filename or "upload.pdf")
    result = paper_service.import_pdf(
        tmp, file.filename or "upload.pdf", title=title, authors=authors,
        venue=venue, year=year, tags=tags, paper_id=paper_id,
        auto_parse=auto_parse,
    )
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    return envelope(result, message="导入成功，已创建解析任务", request=request)


@router.get("/papers")
async def list_papers(
    request: Request,
    q: str | None = Query(None, description="按标题/作者模糊搜索"),
    status: str | None = Query(None),
    tag: str | None = Query(None),
    sort: str = Query("last_read_at"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    include_deleted: bool = Query(False),
):
    data = paper_service.list_papers(q=q, status=status, tag=tag, sort=sort,
                                     order=order, page=page, page_size=page_size,
                                     include_deleted=include_deleted)
    return envelope(data, request=request)


@router.get("/papers/{paper_id}")
async def paper_detail(paper_id: str, request: Request):
    return envelope(paper_service.get_paper(paper_id), request=request)


@router.put("/papers/{paper_id}")
async def update_paper(paper_id: str, request: Request, payload: dict = Body(...)):
    return envelope(paper_service.update_paper(paper_id, payload), request=request)


@router.delete("/papers/{paper_id}")
async def delete_paper(paper_id: str, request: Request,
                       hard: bool = Query(False, description="彻底删除（含 trash 与 MySQL 行）")):
    """删除论文（先删索引，最后删文件；规划 §21.12）。"""
    return envelope(paper_service.delete_paper(paper_id, hard=hard), request=request)


@router.post("/papers/{paper_id}/parse")
async def trigger_parse(paper_id: str, request: Request,
                        force: bool = Query(False),
                        llm_repair: bool = Query(False,
                                                description="启用 LLM 公式修复（需真实模型）"),
                        vision: bool = Query(True,
                                             description="扫描页交视觉模型读页（默认开）"),
                        vision_model: str | None = Query(None,
                                                         description="指定视觉模型 id")):
    task = paper_service.create_parse_task(paper_id, force=force, params={
        "llm_repair": llm_repair, "vision": vision, "vision_model": vision_model,
    })
    return envelope(task, message="已创建解析任务", request=request)


@router.get("/papers/{paper_id}/file")
async def paper_pdf(paper_id: str):
    """PDF 流：阅读器「原 PDF」栏由 pdf.js 渲染，也支持新标签页直接打开。

    `content_disposition_type="inline"` 是必需的：带 `filename` 的 FileResponse
    默认发 `attachment`，浏览器会**下载**而不是就地浏览（新标签页里也开不了阅读器）。
    """
    path = paper_service.resolve_pdf(paper_id)
    return FileResponse(str(path), media_type="application/pdf",
                        filename=f"{paper_id}.pdf",
                        content_disposition_type="inline")


@router.get("/papers/{paper_id}/pages/{page_no}")
async def paper_page_image(paper_id: str, page_no: int):
    path = paper_service.resolve_page_image(paper_id, page_no)
    return FileResponse(str(path), media_type="image/png")


@router.get("/papers/{paper_id}/figures/region/{page_no}/{index}")
async def paper_figure_region(paper_id: str, page_no: int, index: int):
    """插图裁图（**按图注锚定**重算，600dpi 按需渲染并落盘缓存）。

    为什么不用解析阶段切的 figure 裁图：那些裁图会把相邻的另一张图/小标题一起裁进来，
    前端就会看到同一张图出现两次。这里按"紧贴图注的那一簇图块"重新裁剪。
    """
    from backend.services.paper import figure_regions

    path = figure_regions.ensure_region_crop(paper_id, page_no, index)
    return FileResponse(str(path), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@router.get("/papers/{paper_id}/markdown")
async def paper_markdown(paper_id: str, request: Request):
    """Markdown 单一事实源（规划 §15.1 ③）。"""
    text = paper_service.get_markdown(paper_id)
    return envelope({"paper_id": paper_id, "markdown": text,
                     "chars": len(text or "")}, request=request)


@router.get("/papers/{paper_id}/reconcile")
async def reconcile(paper_id: str, request: Request):
    """对账（只报告不自动删，规划 §8）。"""
    return envelope(paper_service.reconcile(paper_id), request=request)


# --------------------------------------------------------------------------
# §20.3 翻译（产线 A）
# --------------------------------------------------------------------------


@router.post("/papers/{paper_id}/translate")
async def trigger_translate(paper_id: str, request: Request,
                            payload: dict = Body(default={})):
    """触发产线 A 翻译（**逐页**：每页一次调用，同时产出原文与译文）。

    - ``mode=resume``（默认）：只翻还没有产物的页（断点续跑）；
    - ``mode=retranslate``：忽略已有产物全部重来；
    - ``brief=false`` 关闭文档简报（少一次调用，但术语一致性下降）；
    - ``pages=[3,4]`` 只翻指定页（重跑个别页用）。
    """
    mode = payload.get("mode") or "resume"
    if mode not in ("resume", "retranslate", "retry_failed"):
        raise ValidationError("mode 必须是 resume / retranslate / retry_failed")
    if mode == "retry_failed":
        mode = "resume"
    pages = payload.get("pages") or None
    task = translation_service.start_translation(
        paper_id, mode=mode, force=bool(payload.get("force")),
        brief=payload.get("brief", True),
        pages=[int(p) for p in pages] if pages else None,
    )
    return envelope(task, message="已创建逐页翻译任务（产线 A）", request=request)


@router.get("/papers/{paper_id}/brief")
async def get_brief(paper_id: str, request: Request):
    """文档简报（全文脉络 + 统一术语，注入每次翻译调用）。"""
    from backend.services.translation import brief as brief_mod

    data = brief_mod.load_brief(paper_id)
    return envelope({"paper_id": paper_id,
                     "brief": data.to_dict(include_prompt=True) if data else None,
                     "path": str(brief_mod.brief_path(paper_id))}, request=request)


@router.post("/papers/{paper_id}/brief")
async def regenerate_brief(paper_id: str, request: Request,
                           payload: dict = Body(default={})):
    """（重新）生成文档简报；``{"apply": {...}}`` 可直接写入人工修订的术语。"""
    from backend.services.translation import brief as brief_mod

    apply = payload.get("apply")
    if apply:
        current = brief_mod.load_brief(paper_id) or brief_mod.DocumentBrief(
            paper_id=paper_id)
        if apply.get("domain"):
            current.domain = apply["domain"]
        if apply.get("style"):
            current.style = apply["style"]
        if apply.get("notes"):
            current.notes = list(apply["notes"])
        if apply.get("glossary"):
            current.glossary.update({str(k): str(v)
                                     for k, v in apply["glossary"].items()})
        current.source = "manual"
        brief_mod.save_brief(current)
        return envelope(current.to_dict(include_prompt=True),
                        message="已写入人工修订的简报", request=request)

    data = brief_mod.generate_brief(paper_id, force=bool(payload.get("force", True)))
    return envelope(data.to_dict(include_prompt=True), request=request)


@router.get("/papers/{paper_id}/translation/cache")
async def translation_cache(paper_id: str, request: Request):
    """页级产物状态（已翻译页数 / 行数 / token 用量 / 对齐质量分布）。"""
    return envelope(translation_service.translation_cache_info(paper_id),
                    request=request)


@router.delete("/papers/{paper_id}/translation/cache")
async def clear_translation_cache(paper_id: str, request: Request):
    """清空页级产物（等于强制全篇重译）。"""
    return envelope(translation_service.clear_translation_cache(paper_id),
                    request=request)


@router.get("/papers/{paper_id}/translation/stats")
async def translation_stats(paper_id: str, request: Request):
    return envelope(paper_service.translation_stats(paper_id), request=request)


@router.get("/papers/{paper_id}/glossary")
async def get_glossary(paper_id: str, request: Request):
    terms = translation_service.load_glossary(paper_id, limit=200)
    return envelope({"items": terms, "count": len(terms)}, request=request)


@router.post("/papers/{paper_id}/glossary")
async def post_glossary(paper_id: str, request: Request, payload: dict = Body(...)):
    terms = payload.get("terms") or []
    n = translation_service.save_terms(paper_id, terms)
    return envelope({"saved": n, "terms": terms}, request=request)
