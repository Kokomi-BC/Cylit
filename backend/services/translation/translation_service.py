"""页级翻译任务（产线 A）——**只有一种粒度：一页一次调用**。

流水线（三步，替换掉上一版的「切块 → 逐块/按页翻译 → 切回块 + 回退补译」）：

```
① 页清单：以渲染出来的页图为准（每页一个调用单元）
② 逐页调用：输入 = 页图 + 解析器抽出的页内文本 + 稳定前缀（简报/术语表）
           输出 = {original_md, translation_md}（两份逐块对应的 Markdown）
③ 落盘 + 推送：行对齐产物写 md/pages.json（原子落盘，中断可按页续跑）
               → SSE page_translated → 前端只替换那一页
```

为什么这样可以删掉整条对齐/回退链路：**原文也由模型在同一次调用里产出**，
两份 Markdown 的块序号天然一一对应（``pair_rows`` 只在块数不等时才做一次
difflib 序列对齐），不需要锚点、不需要 DP、不需要"回退补译"。

保留的工程能力：自适应并发调度、max_tokens 升档（思考模式吃光预算时）、
断点续跑（``cursor.done_pages`` + ``pages.json``）、暂停/取消、SSE 实时上线。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from backend.core.config import get_settings
from backend.core.errors import ValidationError
from backend.core.events import EVENT_PAGE_TRANSLATED, EVENT_PAPER_STATUS, bus
from backend.core.logging import get_logger
from backend.repositories.mysql.models import Block
from backend.repositories.mysql.session import session_scope
from backend.services.model.model_manager import model_manager
from backend.services.paper import paper_service, reader_service
from backend.services.task.scheduler import (
    AdaptiveScheduler, ScheduleItem, SchedulerConfig,
)
from backend.services.translation.brief import generate_brief
from backend.services.translation.page_translate import (
    PAGES_FILENAME, PageStore, build_stable_prefix, call_page,
    build_page_payload, effective_max_tokens, list_pages, load_page_image,
    next_max_tokens, page_source_text, prefix_hash, render_page_prompt,
)

log = get_logger(__name__)


# --------------------------------------------------------------------------
# 术语表（两级：paper / global）
# --------------------------------------------------------------------------


def load_glossary(paper_id: str, limit: int = 60) -> list[dict]:
    from backend.parsing.ir.ir_reader import IRReader

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    rows = ir.terms()
    terms = [t for t in rows if t.get("scope") in ("paper", "global")]
    terms.sort(key=lambda t: (t.get("scope") != "paper", -(t.get("hit_count") or 0)))
    return terms[:limit]


def glossary_text(terms: list[dict]) -> str:
    if not terms:
        return ""
    return "\n".join(f"- {t['source_term']} → {t['target_term']}"
                     for t in terms if t.get("source_term"))


def save_terms(paper_id: str, terms: list[dict]) -> int:
    from backend.parsing.ir.ir_writer import IRWriter

    writer = IRWriter(paper_service.paper_paths(paper_id)["ir"], paper_id)
    payload = [{"paper_id": paper_id, "scope": t.get("scope", "paper"),
                "source_term": t["source_term"], "target_term": t["target_term"],
                "note": t.get("note")} for t in terms if t.get("source_term")]
    return writer.write_terms(payload)


def page_store(paper_id: str) -> PageStore:
    path: Path = paper_service.paper_paths(paper_id)["md"] / PAGES_FILENAME
    return PageStore(paper_id, path)


# --------------------------------------------------------------------------
# 任务体
# --------------------------------------------------------------------------


def run_translate_task(ctx) -> dict:
    """TRANSLATE_MD 任务体：逐页翻译（``page_translated`` 逐页上线）。"""
    s = get_settings()
    paper_id = ctx.paper_id
    if not paper_id:
        raise ValidationError("TRANSLATE_MD 任务缺少 paper_id")
    paper_service.get_paper(paper_id)

    params = ctx.params or {}
    mode = str(params.get("mode") or "resume")
    # retranslate / force：忽略已有产物，全部重来（仍按 cursor 支持取消后续跑）
    force = bool(params.get("force")) or mode == "retranslate"
    use_brief = params.get("brief", True) is not False
    only_pages = params.get("pages") or None

    paper_pages = list_pages(paper_id)
    if not paper_pages:
        raise ValidationError("这篇论文还没有页面图，请先完成解析")
    page_total = len(paper_pages)
    targets = [int(p) for p in paper_pages]
    if only_pages:
        wanted = {int(x) for x in only_pages}
        targets = [p for p in targets if p in wanted]

    store = page_store(paper_id)
    store.set_page_total(page_total)

    done: set[int] = {int(p) for p in (ctx.cursor.get("done_pages") or [])}
    if not force:
        done |= set(store.done_pages())
    pending = [p for p in targets if p not in done]

    ctx.progress(len(done), page_total, force=True,
                 message=f"待翻译 {len(pending)} 页 / 共 {page_total} 页")
    log.info("TRANSLATE_MD %s：%d 页待翻译（共 %d 页，模式 %s）",
             paper_id, len(pending), page_total, mode)

    if not pending:
        # 全部页已有产物：不调用模型，但仍要把断点与进度写成"已完成"，
        # 否则调用方（CLI / 任务面板 / 自检脚本）看到的是"什么也没发生"。
        store.flush()
        existing = store.stats()
        ctx.set_cursor(done_pages=sorted(done), total_all=page_total,
                       finished=0, failed=0)
        ctx.progress(len(done), page_total, force=True,
                     message=f"全部 {page_total} 页已有产物")
        _finish_paper_status(paper_id)
        return {
            "message": "没有需要翻译的页（全部已完成）",
            "granularity": "page", "pages": page_total, "done": len(done),
            "failed": 0, "rows": existing["rows"], "idle": True,
            "align_structure_pages": existing["align"].get("structure", 0),
            "max_tokens_escalations": 0, "max_tokens_final": 0,
            "usage": {"prompt_tokens": existing["prompt_tokens"],
                      "completion_tokens": existing["completion_tokens"],
                      "reasoning_tokens": existing["reasoning_tokens"]},
            "_done_items": len(done), "_total_items": page_total,
        }

    # ---------------- provider / 稳定前缀 ----------------
    provider = _translation_provider()
    model_id = _current_model_id()
    brief = None
    if use_brief:
        try:
            brief = generate_brief(paper_id, provider=provider)
            if brief and brief.enabled:
                log.info("文档简报就绪：%s（术语 %d 个，来源 %s）",
                         brief.domain[:40], len(brief.glossary), brief.source)
        except Exception as exc:  # noqa: BLE001
            log.warning("文档简报生成失败，退化为无简报模式：%s", exc)
    prefix = build_stable_prefix(brief, glossary_text(load_glossary(paper_id)))
    prefix_sha = prefix_hash(prefix)

    scheduler = AdaptiveScheduler(SchedulerConfig(
        initial=s.translate_concurrency,
        maximum=s.translate_max_concurrency,
        minimum=s.translate_min_concurrency,
        backoff_base=s.translate_backoff_base,
        backoff_cap=s.translate_backoff_cap,
        breaker_threshold=s.translate_breaker_threshold,
        recover_successes=s.translate_recover_successes,
    ))

    counters = {"done": 0, "failed": 0, "rows": 0, "escalations": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                "align_structure": 0}
    cursor_lock = threading.Lock()
    t_start = time.time()
    max_tokens_current = effective_max_tokens(
        (model_manager.get_config(None, "vision").get("parameters") or {})
        .get("max_tokens"))

    def emit(page_no: int, payload: dict, *, cached: bool = False) -> None:
        store.put(page_no, payload)
        counters["done"] += 1
        counters["rows"] += len(payload.get("rows") or [])
        if payload.get("align") == "structure":
            counters["align_structure"] += 1
        with cursor_lock:
            done.add(page_no)
            ctx.set_cursor(done_pages=sorted(done), total_all=page_total)
        _mark_blocks_translated(paper_id, page_no)
        bus.publish("papers", EVENT_PAGE_TRANSLATED, {
            "paper_id": paper_id, "page": page_no, "page_total": page_total,
            "rows": payload.get("rows") or [],
            "align": payload.get("align"), "model": payload.get("model"),
            "cached": cached, "task_id": ctx.task_id,
            "done": len(done), "total": page_total,
        })
        ctx.progress(len(done), page_total,
                     message=f"已翻译 {len(done)}/{page_total} 页")

    def process(page_no: int) -> None:
        nonlocal max_tokens_current
        source, section = page_source_text(paper_id, page_no)
        prompt = render_page_prompt(page=page_no, page_total=page_total,
                                    body=source, prefix=prefix, section=section)
        image = load_page_image(paper_id, page_no)
        try:
            result = call_page(provider, prompt, image, max_tokens=max_tokens_current)
        except Exception as exc:  # noqa: BLE001
            escalated = _escalate_max_tokens(exc, max_tokens_current)
            if escalated is None:
                raise
            log.warning("第 %d 页正文为空（思考模式占用 token），max_tokens %d → %d 重试",
                        page_no, max_tokens_current, escalated)
            max_tokens_current = escalated
            counters["escalations"] += 1
            result = call_page(provider, prompt, image, max_tokens=escalated)

        counters["prompt_tokens"] += result.prompt_tokens
        counters["completion_tokens"] += result.completion_tokens
        counters["reasoning_tokens"] += result.reasoning_tokens
        payload = build_page_payload(page=page_no, page_total=page_total,
                                     result=result, source_chars=len(source))
        if not payload.get("model"):
            payload["model"] = model_id or ""
        emit(page_no, payload)

    def on_error(item, exc) -> None:
        counters["failed"] += 1
        log.warning("第 %s 页最终失败：%s", item.key, exc)

    def should_stop() -> bool:
        ctrl = ctx.check_control()
        if ctrl:
            log.info("翻译任务收到 %s 信号（已完成 %d 页）", ctrl, counters["done"])
            return True
        return False

    stats = scheduler.run(
        [ScheduleItem(key=f"p{page:03d}", payload=page) for page in pending],
        process, on_error=on_error, should_stop=should_stop,
    )

    store.flush()
    elapsed = round(time.time() - t_start, 2)
    ctx.set_cursor(done_pages=sorted(done), total_all=page_total,
                   finished=counters["done"], failed=counters["failed"])
    ctx.progress(len(done), page_total, force=True,
                 message=(f"已翻译 {counters['done']}/{page_total} 页"
                          + (f"，失败 {counters['failed']}" if counters["failed"] else "")))
    _finish_paper_status(paper_id)
    reader_service.invalidate_cache(paper_id)

    summary = {
        "message": (f"翻译完成（逐页）：成功 {counters['done']} 页，"
                    f"失败 {stats.failed} 页，用时 {elapsed}s"),
        "granularity": "page",
        "pages": page_total,
        "done": counters["done"],
        "failed": stats.failed,
        "rows": counters["rows"],
        "align_structure_pages": counters["align_structure"],
        "max_tokens_escalations": counters["escalations"],
        "max_tokens_final": max_tokens_current,
        "brief": brief.to_dict() if brief else None,
        "usage": {"prompt_tokens": counters["prompt_tokens"],
                  "completion_tokens": counters["completion_tokens"],
                  "reasoning_tokens": counters["reasoning_tokens"]},
        "elapsed_s": elapsed,
        "scheduler": stats.to_dict(include_attempts=False),
        "concurrency_timeline": stats.timeline,
        "_done_items": len(done),
        "_total_items": page_total,
    }
    return summary


def _escalate_max_tokens(exc: Exception, current: int) -> int | None:
    """只在"思考模式把预算吃光导致正文为空"这类错误上升档 max_tokens。"""
    msg = str(exc)
    if "正文为空" not in msg and "思维链" not in msg and "未返回完整" not in msg:
        return None
    return next_max_tokens(current)


def _translation_provider():
    """优先用 vision 模型（要读页图）；未配置时退回 generation（纯文本翻译）。"""
    try:
        return model_manager.get_vision()
    except Exception as exc:  # noqa: BLE001
        log.warning("未配置 vision 模型，退回 generation（不带页图）：%s", exc)
        return model_manager.get_generation()


def _current_model_id() -> str | None:
    for mtype in ("vision", "generation"):
        try:
            return model_manager.get_config(None, mtype).get("id")
        except Exception:  # noqa: BLE001
            continue
    return None


def _mark_blocks_translated(paper_id: str, page_no: int) -> None:
    """把该页的块标记为"已有译文"（只影响状态显示与对账，不存译文）。"""
    try:
        with session_scope() as sess:
            (sess.query(Block)
             .filter(Block.paper_id == paper_id, Block.page_no == page_no)
             .update({"has_translation": True}, synchronize_session=False))
    except Exception as exc:  # noqa: BLE001
        log.warning("标记第 %d 页块状态失败：%s", page_no, exc)


def _finish_paper_status(paper_id: str) -> None:
    data = paper_service.refresh_progress_counters(paper_id)
    bus.publish("papers", EVENT_PAPER_STATUS, {
        "paper_id": paper_id, "status": data.get("status"),
        "translate_done": data.get("translate_done"),
        "translate_total": data.get("translate_total"),
    })


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------


def start_translation(paper_id: str, *, mode: str = "resume",
                      force: bool = False, brief: bool = True,
                      pages: list[int] | None = None) -> dict:
    """触发产线 A（幂等：已有**活着**的翻译任务则复用）。"""
    paper_service.get_paper(paper_id)
    if not list_pages(paper_id):
        raise ValidationError("这篇论文还没有页面图，请先完成解析")
    existing = None if force else _active_translate_task(paper_id)
    if existing is not None and not _task_is_live(existing):
        # 已请求取消 / 心跳早就停了的僵尸任务不能挡住新翻译：
        # 否则前端点「继续翻译」会拿回一个永远不会执行的任务，表现为"点了没反应"。
        _drop_task(existing["id"], "已请求取消或租约过期，本次触发不再复用")
        existing = None
    if existing:
        return existing
    store = page_store(paper_id)
    total = len(list_pages(paper_id))
    done = len(store.done_pages())
    return _create_translate_task(paper_id, {
        "mode": mode, "force": force, "brief": brief, "pages": pages,
    }, total_items=total if force else max(0, total - done))


def _task_is_live(task: dict) -> bool:
    """进行中的任务是否还会被真正执行（取消请求与僵尸都算不活）。"""
    if task.get("cancel_requested"):
        return False
    stamp = task.get("updated_at") or task.get("created_at")
    if not stamp:
        return True
    try:
        age = (datetime.now() - datetime.fromisoformat(str(stamp))).total_seconds()
    except (TypeError, ValueError):
        return True
    lease = int(get_settings().task_lease_seconds or 120)
    return age < max(lease * 3, 300)


def _drop_task(task_id: str, message: str) -> None:
    from backend.services.task.task_service import task_service

    try:
        task_service.finish(task_id, "cancelled", message=message)
    except Exception as exc:  # noqa: BLE001
        log.warning("放弃僵尸任务 %s 失败：%s", task_id, exc)


def _active_translate_task(paper_id: str) -> dict | None:
    from backend.services.task.task_service import task_service

    return task_service.find_active("TRANSLATE_MD", paper_id, "md")


def _create_translate_task(paper_id: str, params: dict, total_items: int) -> dict:
    from backend.services.task.task_service import task_service

    return task_service.create(
        "TRANSLATE_MD", "md", paper_id, params=params,
        total_items=total_items, message="排队中（产线 A：逐页翻译）",
    )


def translation_cache_info(paper_id: str) -> dict:
    store = page_store(paper_id)
    return {"paper_id": paper_id, "path": str(store.path), **store.stats()}


def clear_translation_cache(paper_id: str) -> dict:
    return {"cleared": page_store(paper_id).clear()}
