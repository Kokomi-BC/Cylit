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
                        then_translate: bool = Query(False,
                                                     description="解析完成后自动接着开始逐页翻译"),
                        vision_model: str | None = Query(None,
                                                         description="指定视觉模型 id")):
    task = paper_service.create_parse_task(paper_id, force=force, params={
        "llm_repair": llm_repair, "vision": vision, "vision_model": vision_model,
        "then_translate": then_translate,
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
async def paper_figure_region(paper_id: str, page_no: int, index: int,
                              card: bool = False):
    """插图裁图（**按图注锚定**重算，600dpi 按需渲染并落盘缓存）。

    为什么不用解析阶段切的 figure 裁图：那些裁图会把相邻的另一张图/小标题一起裁进来，
    前端就会看到同一张图出现两次。这里按"紧贴图注的那一簇图块"重新裁剪。

    ``card=1`` 出的是**图表卡片**（图 + 图注合成一张），放大看大图与导出用。
    同一张图被切成多段（内部夹着别的图注 / 跨页续图）时，各段是**独立的区域下标**，
    因此段号不需要出现在 URL 里（见 ``figure_regions.media_of_page``）。
    """
    from backend.services.paper import figure_regions

    path = figure_regions.ensure_region_crop(paper_id, page_no, index, card=card)
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
    """触发页级任务（默认**两路并发**：A 路英文转录 + B 路中文翻译）。

    - ``paths=["a","b"]``（默认）：两路各一个任务，**同时跑**；
      ``["b"]`` = 只补中文翻译；``["a"]`` = 只补英文转录；
      ``["fused"]`` = 融合模式（同一次调用产出两侧，**对照臂 / 回滚**用）；
    - ``mode=resume``（默认）：只做还没有产物的页（断点续跑）；
    - ``mode=retranslate``：忽略已有产物全部重来；
    - ``brief=false`` 关闭文档简报（少一次调用，但 B 路术语一致性下降；A 路本来就不用）；
    - ``pages=[3,4]`` 只做指定页（重跑个别页用）。
    """
    mode = payload.get("mode") or "resume"
    if mode not in ("resume", "retranslate", "retry_failed"):
        raise ValidationError("mode 必须是 resume / retranslate / retry_failed")
    if mode == "retry_failed":
        mode = "resume"
    pages = payload.get("pages") or None
    paths = payload.get("paths") or ["a", "b"]

    # 还没解析（没有页图）→ 解析是翻译的前置步骤，这里替用户串起来。
    # 只有**用户点了「翻译」**才会走到这里（导入时的自动解析不会串，见 §"解析完不自动翻译"），
    # 串起来的方式是给解析任务带上 `then_translate`，解析完成后由 handlers 自动开翻译任务。
    from backend.services.task import task_service
    from backend.services.translation.page_translate import list_pages

    if not list_pages(paper_id):
        running = task_service.find_active("PARSE", paper_id, "system") or {}
        if running.get("params", {}).get("then_translate"):
            msg = "这篇论文正在解析，完成后会自动开始翻译"
        elif running:
            msg = "这篇论文正在解析：等它完成后再点一次「翻译」"
        else:
            running = paper_service.create_parse_task(
                paper_id, force=False,
                params={"vision": True, "then_translate": True})
            msg = "这篇论文还没解析：已创建解析任务，完成后自动开始翻译"
        return envelope(running, message=msg, request=request)

    result = translation_service.start_translation(
        paper_id, paths=list(paths), mode=mode, force=bool(payload.get("force")),
        brief=payload.get("brief", True),
        pages=[int(p) for p in pages] if pages else None,
    )
    roads = "、".join({"a": "英文转录", "b": "中文翻译",
                       "fused": "融合（对照）"}.get(p, p)
                      for p in result["paths"])
    return envelope(result,
                    message=(f"已沿用已有任务（{roads}）" if result["reused"]
                             else f"已创建页级任务（{roads}）"),
                    request=request)


@router.post("/papers/{paper_id}/understand")
async def trigger_understand(paper_id: str, request: Request,
                             payload: dict = Body(default={})):
    """触发**第二阶段「论文理解」**（默认两路并发：A 技术解剖 + B 阅读地图与相关概念）。

    - ``paths=["a","b"]``（默认）：两路各一次全篇理解调用，**同时跑**；
      ``["b"]`` = 只做中文侧的阅读地图/相关概念；
    - ``force=true``：已有结果也重跑（默认幂等，直接返回库里那份）。

    前置条件：这篇论文**已经建过索引**（理解读的是 chunk，不是页图）。
    理解与知识库问答是两套规则：理解**允许**用通用知识且不必区分来源；
    问答严格"只依据来源"。
    """
    from backend.services.understanding import understanding_service as us

    paths = payload.get("paths") or ["a", "b"]
    result = us.start_understanding(paper_id, paths=list(paths),
                                    force=bool(payload.get("force")))
    if not result["tasks"]:
        raise ValidationError("这篇论文还没有可理解的 chunk：先建索引（知识库页）再来理解")
    roads = "、".join(us.PATH_LABELS.get(t["path"], t["path"]) for t in result["tasks"])
    return envelope(result, message=f"已创建理解任务（{roads}）", request=request)


@router.get("/papers/{paper_id}/understanding")
async def get_understanding(paper_id: str, request: Request,
                            path: str | None = Query(None, pattern="^(a|b)$")):
    """取理解结果（默认两路一起返回，前端一次拿两条）。

    每条结果里有：自由 Markdown、引用短码、**解析后的 chunk id**（前端据此"跳转到原文"）、
    无效/跨路引用（``refs_ok=false`` 时前端要标"引用不可信"）。
    """
    from backend.services.understanding import understanding_service as us

    return envelope(us.get_understanding(paper_id, path), request=request)


@router.get("/papers/{paper_id}/related")
async def related_concepts(paper_id: str, request: Request,
                           page: int = Query(..., ge=1),
                           tolerance: int = Query(1, ge=0, le=3)):
    """与某一页相关的概念（阅读器左栏「跟随阅读位置」用）。

    读的是 B 路理解结果里「相关概念」那一节（读时投影，见
    `understanding_service.parse_concepts`）。命中判据是"概念的引用里有 chunk 落在
    本页 ±`tolerance`"——引用的单元常常跨页，完全等值会漏。
    没有解读结果时返回空列表（前端静默退回"全篇概念列表"，不报错）。
    """
    from backend.services.understanding import understanding_service as us

    return envelope(us.related_concepts(paper_id, page, tolerance=tolerance),
                    request=request)


@router.get("/papers/{paper_id}/understanding/status")
async def understanding_status(paper_id: str, request: Request):
    """两路理解各自的状态（按钮与"能不能理解"的判断）。"""
    from backend.services.understanding import understanding_service as us

    return envelope(us.understanding_status(paper_id), request=request)


@router.post("/papers/{paper_id}/metadata/auto")
async def auto_metadata(paper_id: str, request: Request,
                        payload: dict = Body(default={})):
    """自动补全论文元数据（会议 / 年份 / 标签 / 摘要）。

    - 默认**只读 PDF**（离线、不花钱）：标题/作者/会议/年份/摘要都能从首页读出来；
    - ``model=true``：仍缺的字段再交给模型（读抽样内容，判断不出就留空）；
    - ``overwrite=true``：连已有值一起覆盖（默认只填空字段 —— 元数据没有来源标记，
      覆盖掉用户手改过的值是不可逆的）。
    """
    from backend.services.paper import metadata_fill

    report = metadata_fill.auto(
        paper_id,
        model=bool(payload.get("model", False)),
        overwrite=bool(payload.get("overwrite", False)),
    )
    applied = report.get("applied") or {}
    errors = report.get("errors") or []
    if applied:
        msg = "已补全：" + "、".join(_META_LABELS.get(k, k) for k in applied)
    elif errors:
        # 别把"读取失败"说成"没有可补全的"：扫描件/加密 PDF 走的就是这里
        msg = "自动补全没成功：" + str(errors[0])[:120]
    else:
        msg = "没有可自动补全的字段（论文里读不出来，可试试点「模型补全」）"
    return envelope(report, message=msg, request=request)


# 元数据字段的中文名（只用于回执文案）
_META_LABELS = {"title": "标题", "authors": "作者", "venue": "会议 / 期刊",
                "year": "年份", "tags": "标签", "abstract": "摘要"}


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
        # 一律用 `is not None` 判存在：编辑界面会**整份**提交，把某个字段清空也是合法修订
        # （以前用真值判断，"清空领域/要点"会被静默丢掉，用户以为改了其实没改）。
        if apply.get("domain") is not None:
            current.domain = str(apply["domain"]).strip()
        if apply.get("style") is not None:
            current.style = str(apply["style"]).strip()
        if apply.get("notes") is not None:
            current.notes = [str(x) for x in apply["notes"] if str(x).strip()][:6]
        if apply.get("keep_as_is") is not None:
            current.keep_as_is = [str(x) for x in apply["keep_as_is"] if str(x).strip()][:20]
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
