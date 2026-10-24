"""论文 / 解析 / 翻译 / 复核 API（规划 §20.1–§20.3）。"""

from __future__ import annotations

from fastapi import APIRouter, Body, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse

from backend.api.response import envelope
from backend.core.errors import ValidationError
from backend.services.paper import folder_service, paper_service
from backend.services.translation import translation_service

router = APIRouter(tags=["papers"])


def _mineru_params(*, model_version=None, language=None, is_ocr=None,
                   enable_formula=None, enable_table=None, page_ranges=None) -> dict:
    """在线解析的可选参数 → PARSE 任务参数（`mineru_*`）。

    只把**显式传了的**放进结果：`None` 表示"这次不动这一项"，由服务层按
    "设置默认值 > 官方默认值"补齐。校验（取值闭集合、页码范围格式、布尔字面量）
    统一放在 `mineru_service`，这里不重复一遍规则 —— 两处各写一套一定会漂。
    """
    from backend.services.paper import mineru_service

    return mineru_service.normalize_params({
        "mineru_model_version": model_version,
        "mineru_language": language,
        "mineru_is_ocr": is_ocr,
        "mineru_enable_formula": enable_formula,
        "mineru_enable_table": enable_table,
        "mineru_page_ranges": page_ranges,
    })


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
    # 这两个开关都是 None 哨兵：没传 = 用设置里存的默认值（在线服务 → 本地解析），
    # 显式 true/false 仍然覆盖它。
    figure_caption: bool | None = Form(None),
    deep_figure_search: bool | None = Form(None),
    parse_engine: str | None = Form(None),
    mineru_model_version: str | None = Form(None),
    mineru_language: str | None = Form(None),
    mineru_is_ocr: str | None = Form(None),
    mineru_enable_formula: str | None = Form(None),
    mineru_enable_table: str | None = Form(None),
    mineru_page_ranges: str | None = Form(None),
):
    """上传 PDF → 落盘 + 建 paper + 建 PARSE 任务（规划 §21.1）。

    ``figure_caption`` / ``deep_figure_search``：导入弹窗的两个插图开关（均默认关，
    **导入即重解析**，所以只在本次解析生效）。语义见 ``PaperBuilder``。

    ``parse_engine``：解析引擎（``local`` / ``mineru``，不传 = 用设置里的默认引擎）。
    ``mineru_*``：在线解析的可选参数（模型版本 / OCR 语言 / 强制 OCR / 公式 / 表格 /
    页码范围），**不传的项由设置默认值与官方默认值补齐**，见
    ``mineru_service.effective_params``。
    """
    data = await file.read()
    if not data:
        raise ValidationError("上传文件为空")
    tmp = paper_service.save_upload(data, file.filename or "upload.pdf")
    result = paper_service.import_pdf(
        tmp, file.filename or "upload.pdf", title=title, authors=authors,
        venue=venue, year=year, tags=tags, paper_id=paper_id,
        auto_parse=auto_parse,
        figure_caption=figure_caption, deep_figure_search=deep_figure_search,
        parse_engine=parse_engine,
        parse_params=_mineru_params(
            model_version=mineru_model_version, language=mineru_language,
            is_ocr=mineru_is_ocr, enable_formula=mineru_enable_formula,
            enable_table=mineru_enable_table, page_ranges=mineru_page_ranges),
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
    folder: str | None = Query(None, description="按自定义分类过滤：分类 id，或 none = 未分类"),
):
    data = paper_service.list_papers(q=q, status=status, tag=tag, sort=sort,
                                     order=order, page=page, page_size=page_size,
                                     include_deleted=include_deleted, folder=folder)
    return envelope(data, request=request)


@router.post("/papers/folder")
async def assign_papers_folder(request: Request, payload: dict = Body(...)):
    """批量归类（``folder_id=null`` = 移回未分类），返回 ``updated`` = 实际改到的篇数。

    ⚠️ 这个路由必须排在 ``/papers/{paper_id}`` **之前**：FastAPI 按声明顺序匹配，
    放到后面时 ``folder`` 会被当成论文 id 吃掉（404 "论文不存在：folder"）。
    """
    return envelope(folder_service.assign_papers(payload.get("paper_ids"),
                                                payload.get("folder_id")),
                    request=request)


@router.get("/papers/trash")
async def list_trash(request: Request):
    """回收站列表（只含已软删的论文），最后删的排最前。

    ⚠️ 这个路由必须排在 `/papers/{paper_id}` **之前**：放到后面时 `trash` 会被当成论文 id 吃掉。
    """
    return envelope(paper_service.list_trash(), request=request)


@router.post("/papers/trash/clear")
async def clear_trash(request: Request):
    """清空回收站：逐篇彻底删除（trash 目录 + MySQL 行），返回 `purged` 与失败明细。"""
    return envelope(paper_service.clear_trash(), request=request)


@router.post("/papers/{paper_id}/restore")
async def restore_paper(paper_id: str, request: Request):
    """从回收站恢复（文件搬回 + 清 deleted_at + 按需重开索引）。

    批注 / 笔记 / 对话 / 解读产物在软删时就已清掉，恢复不会带回。
    """
    return envelope(paper_service.restore_paper(paper_id), request=request)


@router.get("/papers/{paper_id}")
async def paper_detail(paper_id: str, request: Request):
    """论文详情：元数据 + **参考文献**（列表页不带它 —— 一篇论文几十上百条，太重）。"""
    data = paper_service.get_paper(paper_id)
    refs = paper_service.references(paper_id)
    return envelope({**data, "references": refs, "reference_count": len(refs)},
                    request=request)


@router.get("/papers/{paper_id}/references")
async def paper_references(paper_id: str, request: Request):
    """这篇论文的参考文献（按论文里的顺序）。

    阅读器「引用文献」侧栏用它：正文里的引用标号（`[2, 4, 10]`）点开就只显示被提到的那几条，
    所以侧栏要能**单独**取这份列表 —— 详情接口（`GET /papers/{id}`）那份带着**整份元数据**，
    读者只是点了个标号，不该顺带拉一坨元数据过来。
    """
    refs = paper_service.references(paper_id)
    return envelope({"items": refs, "count": len(refs)}, request=request)


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
                                                         description="指定视觉模型 id"),
                        figure_caption: bool | None = Query(
                            None, description="插图带图注（不传 = 沿用上次解析的值）"),
                        deep_figure_search: bool | None = Query(
                            None, description="深度图片搜索：无图注页兜底裁图（不传 = 沿用）"),
                        engine: str | None = Query(
                            None, description="解析引擎：local（默认）/ mineru（在线，需 token）",
                            pattern="^(local|mineru|auto)$"),
                        mineru_model_version: str | None = Query(
                            None, description="MinerU 模型：pipeline（默认）/ vlm"),
                        mineru_language: str | None = Query(
                            None, description="MinerU OCR 语言（默认取设置，en）"),
                        mineru_is_ocr: bool | None = Query(
                            None, description="强制 OCR（扫描件/字体异常时开）"),
                        mineru_enable_formula: bool | None = Query(
                            None, description="识别公式（默认开）"),
                        mineru_enable_table: bool | None = Query(
                            None, description="识别表格（默认开）"),
                        mineru_page_ranges: str | None = Query(
                            None, description='页码范围，如 "1-10" / "2,4-6" / "2--2"（留空 = 全部）')):
    params: dict = {
        "llm_repair": llm_repair, "vision": vision, "vision_model": vision_model,
        "then_translate": then_translate,
    }
    # 三个"解析口径"开关只在**显式传了**的时候覆盖（不传则由 `create_parse_task` 沿用
    # 这篇论文上一次解析固化的值 —— 用户点「重新解析」不该把开关悄悄关掉）
    if figure_caption is not None:
        params["figure_caption"] = figure_caption
    if deep_figure_search is not None:
        params["deep_figure_search"] = deep_figure_search
    if engine is not None:
        params["engine"] = engine
    # 在线解析的可选参数：同样"传了才覆盖"（不传 = 用设置默认值/官方默认值）
    params.update(_mineru_params(
        model_version=mineru_model_version, language=mineru_language,
        is_ocr=mineru_is_ocr, enable_formula=mineru_enable_formula,
        enable_table=mineru_enable_table, page_ranges=mineru_page_ranges))
    task = paper_service.create_parse_task(paper_id, force=force, params=params)
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
    - ``blocks_from_a``：**块模式**（只对 B 路生效，**默认开**）—— B 的正文换成 A 路已转录好的
      块清单，逐块译、逐块回（对齐由"生成时约定"保证，见 `BLOCK_TRANSLATE_SYSTEM`）；
      B 会**等** A 领先（本页 + 下一页开头），上限 `block_wait_seconds`。
      传 ``false`` 退回旧契约（两路各自分块 + 事后配对）；
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
        blocks_from_a=payload.get("blocks_from_a"),
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
    """触发**第二阶段「论文理解」**（论文精读）。

    - ``paths=["a"]``（默认，也是唯一取值）：全篇精读一次；「阅读地图」已删除；
    - ``force=true``：已有结果也重跑（默认幂等，直接返回库里那份）。

    前置条件：这篇论文**已经建过索引**（理解读的是 chunk，不是页图）。
    理解与知识库问答是两套规则：理解**允许**用通用知识且不必区分来源；
    问答严格"只依据来源"。
    """
    from backend.services.understanding import understanding_service as us

    paths = payload.get("paths") or ["a"]
    result = us.start_understanding(paper_id, paths=list(paths),
                                    force=bool(payload.get("force")))
    if not result["tasks"]:
        raise ValidationError("这篇论文还没有可理解的 chunk：先建索引（知识库页）再来理解")
    roads = "、".join(us.PATH_LABELS.get(t["path"], t["path"]) for t in result["tasks"])
    return envelope(result, message=f"已创建理解任务（{roads}）", request=request)


@router.get("/papers/{paper_id}/understanding")
async def get_understanding(paper_id: str, request: Request,
                            path: str | None = Query(None, pattern="^a$")):
    """取解读结果（默认打包返回，前端一次取全）。

    结果里有：自由 Markdown、引用短码、**解析后的 chunk id**（前端据此"跳转到原文"）、
    无效引用（``refs_ok=false`` 时前端要标"引用不可信"）。
    """
    from backend.services.understanding import understanding_service as us

    return envelope(us.get_understanding(paper_id, path), request=request)


@router.get("/papers/{paper_id}/understanding/status")
async def understanding_status(paper_id: str, request: Request):
    """解读的状态（按钮与"能不能理解"的判断）。"""
    from backend.services.understanding import understanding_service as us

    return envelope(us.understanding_status(paper_id), request=request)


@router.post("/papers/{paper_id}/metadata/auto")
async def auto_metadata(paper_id: str, request: Request,
                        payload: dict = Body(default={})):
    """自动补全论文元数据（**离线**那一段：读 PDF 首页 + 页产物）。

    - 默认**只读 PDF / 页产物**（离线、不花钱）：标题 / 作者 / 会议 / 年份 / 原文摘要 /
      DOI / 开源地址 / **参考文献** / **推荐阅读时间** 都能直接读出来；
    - ``model=true``：仍缺的字段再交给模型（读抽样内容，判断不出就留空）；
    - ``overwrite=true``：连已有值一起覆盖（默认只填空字段 —— 每个字段的来源记在
      `meta_sources_json` 里，但覆盖掉用户手改过的值仍然不可逆）。
    """
    from backend.services.paper import metadata_fill

    report = metadata_fill.auto(
        paper_id,
        model=bool(payload.get("model", False)),
        overwrite=bool(payload.get("overwrite", False)),
    )
    applied = report.get("applied") or {}
    errors = report.get("errors") or []
    refs = int(report.get("references") or 0)
    if applied:
        msg = "已补全：" + "、".join(_META_LABELS.get(k, k) for k in applied)
    elif errors:
        # 别把"读取失败"说成"没有可补全的"：扫描件/加密 PDF 走的就是这里
        msg = "自动补全没成功：" + str(errors[0])[:120]
    elif refs:
        msg = f"已抽出 {refs} 条参考文献"
    else:
        msg = "没有可自动补全的字段（论文里读不出来，可试试点「模型补全」）"
    return envelope(report, message=msg, request=request)


@router.post("/papers/{paper_id}/metadata/agent")
async def agent_metadata(paper_id: str, request: Request, payload: dict = Body(default={})):
    """「模型补全论文信息」：交给 **agent 读全文**填写元数据（后台任务）。

    为什么是任务而不是同步接口：agent 要翻页、检索、再判断，实测 1~3 分钟 ——
    同步等会把请求挂在那儿，用户也看不到进度。任务面板里能看到它，卡片刷新即可
    （用户口径：**完整 agent 功能，不用显示细节**）。

    任务内部顺序是：离线（不花钱）→ agent（读全文）→ 失败再回落一次单次模型调用。
    """
    from backend.services.task.task_service import task_service

    paper_service.get_paper(paper_id)
    task, created = task_service.get_or_create(
        "META_FILL", "knowledge", paper_id, {"via": "ui"}, total_items=1)
    return envelope({**task, "created": created},
                    message=("已在后台开始补全论文信息（agent 会先读这篇论文）"
                             if created else "已经有一个补全任务在跑了"),
                    request=request)


# 元数据字段的中文名（只用于回执文案）
_META_LABELS = {
    "title": "标题", "authors": "作者", "venue": "会议 / 期刊", "year": "年份",
    "tags": "中文关键词", "keywords_en": "英文关键词", "abstract": "原文摘要",
    "abstract_zh": "中文摘要", "domain": "所属领域", "one_liner": "一句话概括",
    "contributions": "核心创新点", "prereq": "前置知识", "difficulty": "难度",
    "read_time_min": "推荐阅读时间", "paper_type": "论文类型",
    "doi": "DOI", "code_url": "开源地址",
}


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
