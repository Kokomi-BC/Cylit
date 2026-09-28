"""任务处理器：把 services 的业务函数挂到 TaskRunner 上（规划 §20.7 任务中心）。

任务类型（规划 §10 ⑨）：
``IMPORT / PARSE / TRANSLATE_MD / TRANSLATE_PDF / EMBED / INDEX / ANALYZE /
AI_READING / RECONCILE``
本轮实现 IMPORT / PARSE / TRANSLATE_MD / EMBED / INDEX / RECONCILE；
TRANSLATE_PDF / ANALYZE / AI_READING 属产线 B/C，下一轮接入（注册时明确提示）。
"""

from __future__ import annotations

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads, now
from backend.repositories.mysql.models import Paper
from backend.repositories.mysql.session import session_scope
from backend.services.knowledge import knowledge_service
from backend.services.paper import paper_service, reader_service
from backend.services.paper import parse_hooks
from backend.services.task.task_service import TaskContext, task_runner
from backend.services.translation import translation_service

log = get_logger(__name__)


# --------------------------------------------------------------------------
# IMPORT：上传后的登记（幂等，实际落盘在 API 层完成）
# --------------------------------------------------------------------------


def handle_import(ctx: TaskContext) -> dict:
    paper_id = ctx.paper_id
    ctx.progress(0, 1, force=True, message="校验并登记导入信息")
    if not paper_id:
        return {"message": "无 paper_id，忽略"}
    data = paper_service.get_paper(paper_id)
    ctx.progress(1, 1, force=True, message="导入完成")
    return {"message": f"已导入 {data['title']}（{data['page_count']} 页）",
            "paper_id": paper_id}


# --------------------------------------------------------------------------
# PARSE：解析（规划 §21.2 流程 2）
# --------------------------------------------------------------------------


PARSE_STAGES = ("render", "extract_text", "extract_vector", "extract_image",
                "vision_read", "structure", "formula", "figure", "persist")

#: 在线解析（MinerU）的阶段：前四步是"和云端打交道"，后四步与本地解析同构
#: （归一化成同一份 IR + Markdown，所以阅读器/翻译/知识库都不用改）。
MINERU_STAGES = ("mineru_apply", "mineru_upload", "mineru_poll", "mineru_download",
                 "render", "structure", "rebind", "persist")
MINERU_STAGE_LABELS = {
    "mineru_apply": "申请上传链接", "mineru_upload": "上传 PDF",
    "mineru_poll": "等待 MinerU 解析", "mineru_download": "下载结果",
    "render": "渲染页面", "structure": "整理结构",
    "rebind": "对齐块 ID", "persist": "写入产物",
}


def handle_parse(ctx: TaskContext) -> dict:
    """解析（按 ``params["engine"]`` 分派：本地 PyMuPDF 自研版面 / MinerU 在线解析）。

    **两条引擎产出的产物是同构的**（同一套 IR + Markdown），所以分派只发生在入口，
    下游一律不区分引擎 —— 这也是"选引擎"这件事只出现在导入/重解析两处的原因。
    """
    engine = str(ctx.params.get("engine") or "local").strip().lower()
    if engine == "mineru":
        return _handle_parse_online(ctx)
    return _handle_parse_local(ctx)


def _handle_parse_local(ctx: TaskContext) -> dict:
    """调用 parsing 层构建 IR + Markdown，再把结果回写 MySQL（规划 §6.2 硬约束）。"""
    from backend.parsing.pipeline import PaperBuilder

    paper_id = ctx.paper_id
    if not paper_id:
        raise ValueError("PARSE 任务缺少 paper_id")
    paths = paper_service.paper_paths(paper_id)
    pdf = paper_service.resolve_pdf(paper_id)
    _set_status(paper_id, "imported")

    stage_index = {name: i for i, name in enumerate(PARSE_STAGES)}
    control = {"value": None}

    def on_progress(stage: str, done: int, total: int) -> None:
        if control["value"]:
            return
        idx = stage_index.get(stage, 0)
        overall_total = len(PARSE_STAGES) * 100
        overall_done = idx * 100 + int(done / max(1, total) * 100)
        ctx.progress(overall_done, overall_total, message=f"解析中：{stage} {done}/{total}")
        ctrl = ctx.check_control()
        if ctrl:
            control["value"] = ctrl

    repair_fn = parse_hooks.build_repair_fn()
    builder = PaperBuilder(
        pdf, paper_id, paths["root"], progress=on_progress,
        repair_fn=repair_fn, use_llm_repair=bool(ctx.params.get("llm_repair")),
        vision_fn=parse_hooks.build_vision_fn(ctx.params),
        vision_dpi=int(ctx.params.get("vision_dpi")
                       or get_settings().parse_vision_dpi),
        # 导入弹窗的两个插图开关（均默认关，解析时生效；见 PaperBuilder）
        figure_caption=bool(ctx.params.get("figure_caption")),
        deep_figure_search=bool(ctx.params.get("deep_figure_search")),
    )
    result = builder.build()
    if result.stats.get("scanned_pages"):
        log.info("论文 %s 含扫描页 %s（正文由视觉模型读出）",
                 paper_id, result.stats["scanned_pages"])

    # 回写 MySQL：block 轻量副本 + formula 证据 + paper 统计（规划 §21.2 步骤 7）
    _sync_to_mysql(paper_id, result)
    reader_service.invalidate_cache(paper_id)

    ctx.progress(len(PARSE_STAGES) * 100, len(PARSE_STAGES) * 100,
                 message="解析完成", force=True)
    # 「解析并翻译」：解析完自动接着开翻译任务（前端只按一次，关掉页面也不影响）
    if ctx.params.get("then_translate"):
        _chain_translate(paper_id)
    summary = result.counts()
    return {
        "message": (f"解析完成：{summary['blocks']} 块（公式 {summary['formulas']}，"
                    f"插图 {summary['figures']}，表格 {summary['tables']}）"),
        "counts": summary,
        "grades": result.grade_distribution(),
        "duration_ms": result.duration_ms,
        "stats": {k: v for k, v in result.stats.items()
                  if k in ("stage_ms", "markdown", "rebind", "engine")},
    }


def _handle_parse_online(ctx: TaskContext) -> dict:
    """MinerU 在线解析：上传 → 等结果 → 归一化 → 落 IR/Markdown。

    与本地解析共用后半段（`_sync_to_mysql` / `reader_service.invalidate_cache` /
    `_chain_translate`），差别只在"结构从哪来"。

    ⚠️ 取消语义：取消只让**我们**不再等结果，MinerU 服务端的任务不会因此撤销
    （官方没有撤销接口），所以文案要说清楚，别让用户以为"点了取消就不占额度了"。
    """
    from backend.parsing.online.mineru_client import MineruCancelled
    from backend.services.paper import mineru_service

    paper_id = ctx.paper_id
    if not paper_id:
        raise ValueError("PARSE 任务缺少 paper_id")
    _set_status(paper_id, "imported")

    stage_index = {name: i for i, name in enumerate(MINERU_STAGES)}
    control = {"value": None}

    def on_progress(stage: str, done: int, total: int) -> None:
        if control["value"]:
            return
        idx = stage_index.get(stage, 0)
        overall_total = len(MINERU_STAGES) * 100
        overall_done = idx * 100 + int(done / max(1, total) * 100)
        label = MINERU_STAGE_LABELS.get(stage, stage)
        ctx.progress(overall_done, overall_total,
                     message=f"在线解析中：{label} {done}/{total}")
        ctrl = ctx.check_control()
        if ctrl:
            control["value"] = ctrl

    try:
        result, _bundle = mineru_service.parse_online(
            paper_id, params=ctx.params, progress=on_progress,
            should_stop=ctx.should_stop)
    except MineruCancelled as exc:
        # 取消不是失败：任务框架已经把它标成 cancelled，这里只给一句说明
        log.info("在线解析被取消：%s", exc.message)
        return {"message": exc.message, "cancelled": True, "engine": "mineru"}

    _sync_to_mysql(paper_id, result)
    reader_service.invalidate_cache(paper_id)
    ctx.progress(len(MINERU_STAGES) * 100, len(MINERU_STAGES) * 100,
                 message="在线解析完成", force=True)
    if ctx.params.get("then_translate"):
        _chain_translate(paper_id)
    summary = result.counts()
    mineru_stats = result.stats.get("mineru") or {}
    return {
        "message": (f"在线解析完成（MinerU {mineru_stats.get('model_version') or ''}）："
                    f"{summary['blocks']} 块（公式 {summary['formulas']}，"
                    f"插图 {summary['figures']}，表格 {summary['tables']}）"),
        "counts": summary,
        "grades": result.grade_distribution(),
        "duration_ms": result.duration_ms,
        "engine": "mineru",
        "mineru": {k: v for k, v in mineru_stats.items()
                   if k in ("model_version", "batch_id", "bbox_space",
                            "figures_written")},
        "stats": {k: v for k, v in result.stats.items()
                  if k in ("stage_ms", "markdown", "rebind", "engine")},
    }


def _chain_translate(paper_id: str) -> None:
    """解析完成后自动接着开**两路**页级任务。

    走 `translation_service.start_translation` 而不是裸 `task_service.create`：
    它带"每一路已有活动任务就复用"的幂等判断（否则会重复建两条同路的任务）。
    """
    from backend.services.translation import translation_service

    try:
        task = translation_service.start_translation(paper_id, mode="resume")
        log.info("解析完成 → 自动开始两路页级任务：%s（任务 %s%s）", paper_id,
                 "、".join(task.get("task_ids") or []),
                 "，复用已有任务" if task.get("reused") else "")
    except Exception as exc:  # noqa: BLE001
        log.warning("解析完成后的自动翻译没能启动：%s", exc)


def _sync_to_mysql(paper_id: str, result) -> None:
    """把解析结果回写 MySQL（block 定位 / formula / paper 统计）。"""
    from backend.repositories.mysql.models import Block as BlockRow
    from backend.repositories.mysql.models import Formula as FormulaRow

    with session_scope() as sess:
        sess.query(BlockRow).filter(BlockRow.paper_id == paper_id).delete()
        sess.query(FormulaRow).filter(FormulaRow.paper_id == paper_id).delete()

        for b in result.blocks:
            section = (b.section_id or b.meta.get("section_title") or "")[:64] or None
            sess.add(BlockRow(
                id=b.id, paper_id=paper_id, page_no=b.page_no, type=b.type,
                col=b.col, order_global=b.order_global,
                x0=b.rect.x0, y0=b.rect.y0, x1=b.rect.x1, y1=b.rect.y1,
                md_anchor=(b.md_anchor or "")[:128] or None,
                content_hash=b.content_hash,
                has_translation=False,
                section_id=section,
                char_count=b.char_count,
                # MySQL 只允许 ≤200 字预览（规划 §8 三条禁止之一）
                preview=(b.text_clean or b.text)[:200],
                meta_json=json_dumps(b.meta) if b.meta else None,
            ))

        for f in result.formulas:
            sess.add(FormulaRow(
                id=f.block_id, paper_id=paper_id, page_number=f.page_number,
                bbox_json=json_dumps(f.rect.to_list()),
                raw_text=(f.raw_text or "")[:60000],
                image_path=(f.image_path or "")[:256] or None,
                vector_data_json=json_dumps(f.vector_data.to_dict()),
                latex_candidate=json_dumps(f.latex_candidates),
                latex_repaired=f.latex_repaired, latex_final=f.latex_final,
                confidence=f.confidence, grade=f.grade,
                source=(f.source or "")[:64],
                status=(f.status or "candidate")[:16],
                validation_json=json_dumps(f.validation) if f.validation else None,
                review_note=f.review_note, is_inline=f.is_inline,
            ))

        row = sess.get(Paper, paper_id)
        if row is not None:
            row.status = "parsed"
            row.title = result.title or row.title
            row.authors = result.authors or row.authors
            # 会议/年份/摘要：**只填空字段**（解析重跑不该覆盖用户手改过的元数据；
            # 想要强制重填走 `POST /papers/{id}/metadata/auto`）。
            venue = str(result.stats.get("venue") or "").strip()
            if venue and not (row.venue or "").strip():
                row.venue = venue[:128]
            year = result.stats.get("year")
            if year and not row.year:
                row.year = int(year)
            abstract = (result.abstract or "").strip()
            if abstract and not (row.abstract or "").strip():
                row.abstract = abstract[:2000]
            row.page_count = len(result.pages) or row.page_count
            # 翻译是**页级**（唯一粒度）：分母必须是**页数**，不是块数 ——
            # 旧块级时代这里写的是 len(result.blocks)，卡片就会显示「0 / 480」这种假进度（用户报过）。
            #
            # ⚠️ 这里**不能**把 translate_done 清零：解析只重建 block/formula（见上面两条 delete），
            # **不会删掉已译好的页**（`md/pages.json` 与页图都还在）。曾经无条件写 0，于是对一篇
            # 已译完的论文点一次「解析」，论文库卡片立刻退回「逐页翻译 0 / 15」+「继续翻译」按钮，
            # 而阅读器里 15 页译文一条不少（用户报过："点击解析后翻译进度清零，实际翻译完成"）。
            # 正确做法：进度**以磁盘为权威**重新数一遍（与 `translation_stats` 同一套判据），
            # 论文行状态也一并对齐，别让"已译完"显示成"已解析"。
            stats = paper_service.translation_stats(paper_id)
            total = stats["total"] or len(result.pages) or row.translate_total
            log.info("解析后按磁盘重算进度 %s：done=%s total=%s（本次解析出 %s 页）",
                     paper_id, stats["done"], total, len(result.pages))
            row.translate_total = total
            row.translate_done = stats["done"]
            if total and stats["done"] >= total:
                row.status = "translated"
            elif stats["done"] > 0:
                row.status = "translating"
            row.parse_summary_json = json_dumps({
                "counts": result.counts(),
                "grades": result.grade_distribution(),
                "duration_ms": result.duration_ms,
                "warnings": result.warnings[:10],
                "quality": result.stats.get("quality"),
                "parse_stats": result.stats.get("stage_ms"),
            })


def _set_status(paper_id: str, status: str) -> None:
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is not None:
            row.status = status


# --------------------------------------------------------------------------
# TRANSLATE_MD / TRANSCRIBE_EN / TRANSLATE_ZH：页级任务（实现在 translation_service）
# --------------------------------------------------------------------------


def handle_translate_md(ctx: TaskContext) -> dict:
    """融合模式（同一次调用产出原文 + 译文）：对照臂 / 回滚用。"""
    result = translation_service.run_translate_task(ctx)
    _chain_index(ctx.paper_id)
    return result


def handle_transcribe_en(ctx: TaskContext) -> dict:
    """A 路：英文转录（只产出 `original_md`）。"""
    result = translation_service.run_path_task(ctx, "a")
    # A 路也可能是**最后**跑完的那一路（两路并发），所以两边都要试一次
    _chain_index(ctx.paper_id)
    return result


def handle_translate_zh(ctx: TaskContext) -> dict:
    """B 路：中文翻译（只产出 `translation_md` + `carry_md`）。"""
    result = translation_service.run_path_task(ctx, "b")
    _chain_index(ctx.paper_id)
    return result


def _pages_fully_translated(paper_id: str) -> bool:
    """两路（英文转录 + 中文翻译）都翻完了吗。

    `transcription_stats` / `translation_stats` 各自在"本路产物缺页"时退回旧合并产物
    （`pages.json`），所以融合模式的老论文也能判成完成。页数为 0（还没解析）时返回 False。
    """
    a = paper_service.transcription_stats(paper_id)
    b = paper_service.translation_stats(paper_id)
    total = max(int(a.get("total") or 0), int(b.get("total") or 0))
    if total <= 0:
        return False
    return int(a.get("done") or 0) >= total and int(b.get("done") or 0) >= total


def _chain_index(paper_id: str | None) -> None:
    """**翻译任务完成后自动纳入知识库**（用户口径，2026-09-27）。

    以前这一步是论文库卡片上的「建立索引」按钮（手动）。现在：
    两路都翻完 → 自动开 INDEX 任务 → INDEX 完成后自动接着生成两路解读
    （`_chain_understand`，即用户说的"自动继续生成后面的解读"）。

    幂等：`task_service.get_or_create` 对有活动 INDEX 任务的论文直接复用；
    没翻完（或只有部分页重译完）时不动 —— 半成品进知识库只会污染检索。
    """
    if not paper_id:
        return
    try:
        if not _pages_fully_translated(paper_id):
            return
        from backend.services.task.task_service import task_service

        task, created = task_service.get_or_create(
            "INDEX", "knowledge", paper_id,
            params={"rebuild": True, "then_understand": True,
                    "auto": "translation_done"},
            total_items=0,
            message="翻译完成，自动纳入知识库（chunk + 向量）",
        )
        log.info("翻译完成 → 自动建索引：%s（任务 %s%s）", paper_id, task.get("id"),
                 "" if created else "，复用进行中的任务")
    except Exception as exc:  # noqa: BLE001 —— 自动串联失败不能影响翻译任务本身的结论
        log.warning("翻译完成后的自动建索引没能启动：%s", exc)


def _chain_understand(paper_id: str | None) -> None:
    """索引完成后自动生成两路解读（论文精读 + 阅读地图/相关概念）。"""
    if not paper_id:
        return
    try:
        from backend.services.understanding import understanding_service as us

        res = us.start_understanding(paper_id, paths=["a", "b"])
        log.info("索引完成 → 自动生成解读：%s（任务 %s）", paper_id,
                 "、".join(res.get("task_ids") or []))
    except Exception as exc:  # noqa: BLE001
        log.warning("索引完成后的自动解读没能启动：%s", exc)


# --------------------------------------------------------------------------
# EMBED / INDEX：产线 C（规划 §21.6）
# --------------------------------------------------------------------------


def handle_index(ctx: TaskContext) -> dict:
    paper_id = ctx.paper_id
    if not paper_id:
        raise ValueError(f"{ctx.task_type} 任务缺少 paper_id")

    def progress(done: int, total: int) -> None:
        ctx.progress(done, total, message=f"向量化 {done}/{total}")

    data = knowledge_service.index_paper(
        paper_id, rebuild=bool(ctx.params.get("rebuild")), progress=progress)
    _set_paper_indexed(paper_id)
    ctx.progress(data["chunks"], data["chunks"], force=True, message="索引完成")
    # 索引完成 → 自动接着生成解读（只有自动串联的那条链会带这个参数；
    # 设置页里手动建索引仍留给用户自己按「生成解读」）
    if ctx.params.get("then_understand"):
        _chain_understand(paper_id)
    return {"message": (f"已索引 {data['chunks']} 个 chunk"
                        + (f"，向量 {data['embedded']} 条"
                           if data["embedded"] else "（向量入库失败，见 vector_error）")),
            **data}


def _set_paper_indexed(paper_id: str) -> None:
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is not None and row.status in ("parsed", "translated"):
            row.status = "indexed"


def handle_embed(ctx: TaskContext) -> dict:
    return handle_index(ctx)


# --------------------------------------------------------------------------
# RECONCILE：对账（规划 §21.12 步骤 5）
# --------------------------------------------------------------------------


def handle_reconcile(ctx: TaskContext) -> dict:
    report = paper_service.reconcile(ctx.paper_id)
    ctx.progress(len(report["papers"]), len(report["papers"]), force=True,
                 message=("对账完成：一致" if report["ok"]
                          else f"发现 {len(report['issues'])} 个不一致"))
    return {"message": "对账完成（只报告，不自动删）",
            "issues": report["issues"], "ok": report["ok"]}


# --------------------------------------------------------------------------
# 第二阶段：论文理解（两路各做不同的事，见 `services/understanding/`）
# --------------------------------------------------------------------------


def handle_understand_a(ctx: TaskContext) -> dict:
    from backend.services.understanding import understanding_service as us

    return us.run_understand_task(ctx, "a")


def handle_understand_b(ctx: TaskContext) -> dict:
    from backend.services.understanding import understanding_service as us

    return us.run_understand_task(ctx, "b")


# --------------------------------------------------------------------------
# 未实现的任务类型（下一轮）：给出明确原因，不留静默失败
# --------------------------------------------------------------------------


def _not_implemented(name: str, stage: str):
    def handler(ctx: TaskContext) -> dict:
        msg = (f"{name} 属于{stage}，本轮未实现（规划阶段 {stage}）。"
               f"请在后续里程碑中接入。")
        log.warning(msg)
        return {"message": msg, "implemented": False}
    return handler


def register_all() -> None:
    task_runner.register("IMPORT", handle_import)
    task_runner.register("PARSE", handle_parse)
    task_runner.register("TRANSLATE_MD", handle_translate_md)
    # 两路并发：各一个任务类型，幂等/取消/续跑/任务面板都分得清是哪一路
    task_runner.register("TRANSCRIBE_EN", handle_transcribe_en)
    task_runner.register("TRANSLATE_ZH", handle_translate_zh)
    # 第二阶段：理解（同样两路各一个任务）
    task_runner.register("UNDERSTAND_A", handle_understand_a)
    task_runner.register("UNDERSTAND_B", handle_understand_b)
    task_runner.register("EMBED", handle_embed)
    task_runner.register("INDEX", handle_index)
    task_runner.register("RECONCILE", handle_reconcile)
    # 产线 B / C 的后续阶段（占位，明确返回未实现）
    task_runner.register("TRANSLATE_PDF", _not_implemented("TRANSLATE_PDF", "18-20 阶段：版面保留 PDF"))
    task_runner.register("ANALYZE", _not_implemented("ANALYZE", "P1：论文结构分析"))
    task_runner.register("AI_READING", _not_implemented("AI_READING", "P1：AI 辅助阅读"))
    log.info("已注册 %d 个任务处理器", len(task_runner.handlers))
