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


def handle_parse(ctx: TaskContext) -> dict:
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
    summary = result.counts()
    return {
        "message": (f"解析完成：{summary['blocks']} 块（公式 {summary['formulas']}，"
                    f"插图 {summary['figures']}，表格 {summary['tables']}）"),
        "counts": summary,
        "grades": result.grade_distribution(),
        "duration_ms": result.duration_ms,
        "stats": {k: v for k, v in result.stats.items()
                  if k in ("stage_ms", "markdown", "rebind")},
    }


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
            row.page_count = len(result.pages) or row.page_count
            row.translate_total = len(result.blocks)
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
# TRANSLATE_MD：产线 A（实现在 translation_service）
# --------------------------------------------------------------------------


def handle_translate_md(ctx: TaskContext) -> dict:
    return translation_service.run_translate_task(ctx)


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
    task_runner.register("EMBED", handle_embed)
    task_runner.register("INDEX", handle_index)
    task_runner.register("RECONCILE", handle_reconcile)
    # 产线 B / C 的后续阶段（占位，明确返回未实现）
    task_runner.register("TRANSLATE_PDF", _not_implemented("TRANSLATE_PDF", "18-20 阶段：版面保留 PDF"))
    task_runner.register("ANALYZE", _not_implemented("ANALYZE", "P1：论文结构分析"))
    task_runner.register("AI_READING", _not_implemented("AI_READING", "P1：AI 辅助阅读"))
    log.info("已注册 %d 个任务处理器", len(task_runner.handlers))
