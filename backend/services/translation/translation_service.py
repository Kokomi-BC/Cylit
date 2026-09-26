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

保留的工程能力：重试/退避/熔断（调度器）、max_tokens 升档（思考模式吃光预算时）、
断点续跑（``cursor.done_pages`` + ``pages.json``）、暂停/取消、SSE 实时上线。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from backend.core.config import get_settings
from backend.core.errors import ValidationError
from backend.core.events import (
    EVENT_PAGE_TRANSCRIBED, EVENT_PAGE_TRANSLATED, EVENT_PAPER_STATUS, bus,
)
from backend.core.logging import get_logger
from backend.core.utils import now
from backend.services.model.model_manager import model_manager
from backend.services.paper import paper_service, reader_service
from backend.services.task.scheduler import (
    AdaptiveScheduler, ScheduleItem, SchedulerConfig,
)
from backend.services.translation.brief import generate_brief
from backend.services.translation.page_translate import (
    CONTEXT_MAX_CHARS, PATH_VERSION, PageStore, build_outline_skeleton,
    build_page_payload, build_path_payload, build_stable_prefix, call_page,
    drop_declared_fragment, effective_max_tokens, history_context, list_pages,
    load_page_image, next_max_tokens, page_continuity, page_lookahead,
    page_source_blocks, page_source_text, prefix_hash, render_page_prompt, split_blocks,
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


#: 每一路对应的任务类型。`None` = 融合模式（同一次调用产出两侧，保留作**对照臂 / 回滚**：
#: 它同时也是存量论文 `md/pages.json` 的生产者，不能在有对照结论之前删掉）。
PATH_TASK_TYPES: dict[str | None, str] = {
    None: "TRANSLATE_MD", "a": "TRANSCRIBE_EN", "b": "TRANSLATE_ZH",
}
#: 对外用字符串表示融合模式（JSON 里没有 `None` 这个值）
FUSED = "fused"
#: 每一路的中文名（日志/进度文案用）
PATH_LABELS = {None: "逐页翻译（融合）", "a": "英文转录（A 路）", "b": "中文翻译（B 路）"}


def normalize_path(value) -> str | None:
    """把对外写法（``"fused"`` / ``None`` / ``"a"`` / ``"b"``）归一成内部表示。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", FUSED, "both", "none"):
        return None
    if text in ("a", "b"):
        return text
    raise ValidationError(f"未知的路：{value!r}（只能是 'a' / 'b' / 'fused'）")


def page_store(paper_id: str, path: str | None = None) -> PageStore:
    """按路取产物 store：``None`` = 融合模式的 `md/pages.json`，``"a"``/``"b"`` = 各自的路文件。"""
    paths = paper_service.paper_paths(paper_id)
    if path is None:
        return PageStore(paper_id, paths["page_products"])
    if path == "a":
        return PageStore(paper_id, paths["path_a"], version=PATH_VERSION)
    if path == "b":
        return PageStore(paper_id, paths["path_b"], version=PATH_VERSION)
    raise ValidationError(f"未知的路：{path!r}")


# --------------------------------------------------------------------------
# 任务体
# --------------------------------------------------------------------------


def run_path_task(ctx, path: str | None = None) -> dict:
    """页级任务体（**唯一实现**）：`path=None` 融合模式；`"a"` 英文转录；`"b"` 中文翻译。

    两路与融合的差别只有下面这几处（都在这一个函数里显式分叉，不复制一份代码）：

    | | 融合 (`None`) | A 路 (`"a"`) | B 路 (`"b"`) |
    |---|---|---|---|
    | 产物文件 | `md/pages.json` | `md/path_a.json` | `md/path_b.json` |
    | system prompt | `SYSTEM_PROMPT` | `TRANSCRIBE_SYSTEM` | `TRANSLATE_SYSTEM` |
    | 稳定前缀 | 简报 + 术语表 + 骨架 | **只有骨架**（不翻译，术语表无用） | 简报 + 术语表 + 骨架 |
    | 【下一页开头】 | 给 | **不给**（转录只写本页） | 给 |
    | `carry_md` 去重 | 用 | **不用**（A 路没有 carry） | 用 |
    | 行产物 | 存 `rows` | 不存行（读时合并） | 不存行（读时合并） |
    | SSE 事件 | `page_translated` | `page_transcribed` | `page_translated`（带 `path`） |
    | 进度字段 | `translate_done` | `transcribe_done` | `translate_done` |

    **两路并发的确切含义**：并发是**路与路之间**，路内页序**仍然串行**（串行才能保证第 N 页拿到
    第 1..N-1 页的完整前文，从而命中前缀缓存 —— 实测稳定态命中 80%+，见 `core/config.py` 翻译段）。
    两路各自维护自己的前文与缓存前缀，所以并发不影响任何一路的缓存命中。
    """
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

    # 任务面板的分母 = **本次任务负责的页数**（指定页时就是那些页），与论文卡片的
    # "全篇进度"分开。以前 `ctx.progress` 一律拿 `page_total` 当分母，把
    # `_target_count` 的修正又盖回去了 —— 于是"重跑第 1、2 页"的任务显示成 `x/22`。
    scope = {int(p) for p in targets}
    scope_total = len(targets)

    store = page_store(paper_id, path)
    store.set_page_total(page_total)

    done: set[int] = {int(p) for p in (ctx.cursor.get("done_pages") or [])}
    if not force:
        done |= set(store.done_pages())
    pending = [p for p in targets if p not in done]

    def scoped_done() -> int:
        """本次任务负责的页里已完成的页数（任务面板的分母用 `scope_total`）。"""
        return len(done & scope)

    label = PATH_LABELS[path]
    ctx.progress(scoped_done(), scope_total, force=True,
                 message=f"待{label} {len(pending)} 页 / 共 {scope_total} 页")
    log.info("%s %s：%d 页待处理（共 %d 页，模式 %s）",
             PATH_TASK_TYPES[path], paper_id, len(pending), scope_total, mode)

    if not pending:
        # 全部页已有产物：不调用模型，但仍要把断点与进度写成"已完成"，
        # 否则调用方（CLI / 任务面板 / 自检脚本）看到的是"什么也没发生"。
        store.flush()
        existing = store.stats()
        ctx.set_cursor(done_pages=sorted(done), total_all=page_total,
                       finished=0, failed=0)
        ctx.progress(scoped_done(), scope_total, force=True,
                     message=f"全部 {scope_total} 页已有产物")
        _finish_paper_status(paper_id)
        return {
            "message": f"没有需要{label}的页（全部已完成）",
            "path": path or FUSED, "task_type": PATH_TASK_TYPES[path],
            "granularity": "page", "pages": page_total, "done": scoped_done(),
            "failed": 0, "rows": existing["rows"], "idle": True,
            "align_structure_pages": existing["align"].get("structure", 0),
            "max_tokens_escalations": 0, "max_tokens_final": 0,
            "usage": {"prompt_tokens": existing["prompt_tokens"],
                      "completion_tokens": existing["completion_tokens"],
                      "reasoning_tokens": existing["reasoning_tokens"]},
            "_done_items": scoped_done(), "_total_items": scope_total,
        }

    # ---------------- provider / 稳定前缀 ----------------
    provider = _path_provider(path)
    model_id = _current_model_id(path)
    txt_provider = provider
    brief = None
    # A 路是**英文转录**：文档简报（中文领域/风格）与术语表（源→目标）对它没有意义，
    # 但【全文标题骨架】必须给 —— 那是"同一层级的 # 深度在全文一致"的唯一依据。
    if use_brief and path != "a":
        try:
            brief = generate_brief(paper_id, provider=txt_provider)
            if brief and brief.enabled:
                log.info("文档简报就绪：%s（术语 %d 个，来源 %s）",
                         brief.domain[:40], len(brief.glossary), brief.source)
        except Exception as exc:  # noqa: BLE001
            log.warning("文档简报生成失败，退化为无简报模式：%s", exc)
    # 稳定前缀：简报 + 术语表 + **全文标题骨架**。三者都对整篇不变，所以前缀缓存仍然命中；
    # 骨架让模型对"编号 → 层级"有统一依据（否则每页各自猜 # 深度，同一层级在全文不一致）。
    skeleton = build_outline_skeleton(paper_id, store)
    glossary = "" if path == "a" else glossary_text(load_glossary(paper_id))
    prefix = build_stable_prefix(brief, glossary, skeleton=skeleton)
    prefix_sha = prefix_hash(prefix)
    use_context = params.get("context", True) is not False
    context_chars = int(params.get("context_chars") or CONTEXT_MAX_CHARS)
    if skeleton:
        log.info("标题骨架就绪：%d 行；前文历史上限 %d 字", skeleton.count("\n"), context_chars)

    # 页级翻译**固定串行**（窗口=1）：并发设置已移除，理由见 `config.py` 翻译段注释。
    # 调度器仍保留「重试 / 退避 / 熔断」——它们与并发无关，仍然有用。
    scheduler = AdaptiveScheduler(SchedulerConfig(
        initial=1, maximum=1, minimum=1,
        backoff_base=s.translate_backoff_base,
        backoff_cap=s.translate_backoff_cap,
        breaker_threshold=s.translate_breaker_threshold,
        recover_successes=s.translate_recover_successes,
    ))

    counters = {"done": 0, "failed": 0, "rows": 0, "escalations": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                "align_structure": 0}
    cursor_lock = threading.Lock()
    t_start = time.time()
    max_tokens_current = effective_max_tokens(
        (model_manager.get_config(_model_id_for(path), "vision").get("parameters") or {})
        .get("max_tokens"))
    # SSE 事件名与进度字段按路区分：A 路发 `page_transcribed`、写 `transcribe_done`；
    # B 路/融合发 `page_translated`（带 `path`）、写 `translate_done`。
    event = EVENT_PAGE_TRANSCRIBED if path == "a" else EVENT_PAGE_TRANSLATED

    def emit(page_no: int, payload: dict, *, cached: bool = False) -> None:
        store.put(page_no, payload)
        counters["done"] += 1
        counters["rows"] += len(payload.get("rows") or [])
        if payload.get("align") == "structure":
            counters["align_structure"] += 1
        with cursor_lock:
            done.add(page_no)
            ctx.set_cursor(done_pages=sorted(done), total_all=page_total)
        # 卡片进度条读的就是论文行 —— 每完成一页就写回，否则跑到一半卡片还是旧数字。
        # ⚠️ 这里刻意用**全篇**口径（`len(done)` / `page_total`）：论文卡片回答的是
        # "这篇论文做完多少了"，而任务面板回答的是"这个任务做完多少了"（`scope_total`）。
        # 两者在"只重跑几页"时本来就是不同的数，混用会让其中一个说谎。
        if path == "a":
            paper_service.set_transcribe_progress(paper_id, done=len(done),
                                                 total=page_total)
        else:
            paper_service.set_translate_progress(paper_id, done=len(done),
                                                 total=page_total)
        # SSE 的 `rows` 必须是**已合并**的那一页：两路模式下单路产物里没有行（A 件只有
        # `original_md`、B 件只有 `translation_md`），直接把 `payload["rows"]`（空）发出去，
        # 前端 `applyPage` 会把那一页的行清空。这里现算一次合并（单页，很便宜），
        # 保证"实时收到的行"与"刷新后拿到的行"完全一致。
        if path is None:
            sse_rows = payload.get("rows") or []
            sse_align = payload.get("align")
        else:
            merged = reader_service.merged_page(paper_id, page_no) or {}
            sse_rows = merged.get("rows") or []
            sse_align = merged.get("align")
        bus.publish("papers", event, {
            "paper_id": paper_id, "page": page_no, "page_total": page_total,
            "path": path or FUSED, "task_type": PATH_TASK_TYPES[path],
            "rows": sse_rows,
            "align": sse_align, "model": payload.get("model"),
            "cached": cached, "task_id": ctx.task_id,
            "done": len(done), "total": page_total,
        })
        ctx.progress(scoped_done(), scope_total,
                     message=f"已{'转录' if path == 'a' else '翻译'} "
                             f"{scoped_done()}/{scope_total} 页")

    def process(page_no: int) -> None:
        nonlocal max_tokens_current
        source, section = page_source_text(paper_id, page_no)
        # P4b 共享坐标：本页解析器块清单 + 输出契约，**只进页面级尾部**（不动稳定前缀 → 缓存照旧）
        from backend.services.translation import coords as _coords

        blocks = page_source_blocks(paper_id, page_no)
        codes = _coords.codes_of(blocks)
        coord_tail = (_coords.lattice_text(blocks) + _coords.SRC_TAIL) if codes else ""
        # 完整前文（第 1..N-1 页）——追加式渲染，与上一页 prompt 共享最长前缀 → 命中前缀缓存。
        # 两路各自的 store 里只有自己那一侧的内容（A 件只有 original_md / B 件只有
        # translation_md），所以这里天然就是"本路的前文"。
        history = (history_context(store, paper_id, page_no, max_chars=context_chars)
                   if use_context else "")
        # 下一页开头（仅上下文）：页尾被版面折断成 `bot-` 时，让模型知道后面接什么词，
        # 从而把**译文**补成完整词。A 路是转录，只写本页页图内容 —— 给了反而容易把那半句补完。
        lookahead = (page_lookahead(paper_id, page_no)
                     if (use_context and path != "a") else "")
        # 页边界事实（本页开头是否承接上页、末尾是否延续到下页）：
        # 只用解析器原文判定（确定性、与并发顺序无关），直接告诉模型"该停就停、该接就接"，
        # 否则它会自作主张把半句补成一个完整句 → 同一句话被拆成两句（用户报过）。
        edges = page_continuity(paper_id, page_no)
        # 上一页在自己产物里申报了"它从本页开头借走的那段原文"（carry_md）→
        # 本页正文把这截删掉再送模型：重复从结构上不可能发生，行对齐也不会因"少译一句"而错位。
        # 不靠提示词让模型"自己跳过"（实测执行不稳），也不用启发式猜（会把题注误剪）。
        # `carry_md` 是**翻译侧**的机制（A 路契约里没有这个字段），所以只有 B 路/融合用它。
        body = source
        if path != "a":
            prev_payload = store.page(page_no - 1) if page_no > 1 else None
            carried = str((prev_payload or {}).get("carry_md") or "").strip()
            if carried:
                body = drop_declared_fragment(source, carried)
                if body != source:
                    log.info("第 %d 页：正文剪掉上一页已译完的头部 %d 字符",
                             page_no, len(source) - len(body))
        prompt = render_page_prompt(page=page_no, page_total=page_total,
                                    body=body, prefix=prefix, section=section,
                                    previous=history, lookahead=lookahead, edges=edges,
                                    path=path, src_tail=coord_tail)
        image = load_page_image(paper_id, page_no)
        try:
            result = call_page(provider, prompt, image,
                               max_tokens=max_tokens_current, path=path)
        except Exception as exc:  # noqa: BLE001
            escalated = _escalate_max_tokens(exc, max_tokens_current)
            if escalated is None:
                raise
            log.warning("第 %d 页正文为空（思考模式占用 token），max_tokens %d → %d 重试",
                        page_no, max_tokens_current, escalated)
            max_tokens_current = escalated
            counters["escalations"] += 1
            result = call_page(provider, prompt, image, max_tokens=escalated, path=path)

        counters["prompt_tokens"] += result.prompt_tokens
        counters["completion_tokens"] += result.completion_tokens
        counters["reasoning_tokens"] += result.reasoning_tokens
        counters["cache_hit_tokens"] += result.cache_hit_tokens
        counters["cache_miss_tokens"] += result.cache_miss_tokens
        if result.prompt_tokens:
            log.info("第 %d 页：prompt %d token（缓存命中 %d / 未命中 %d）",
                     page_no, result.prompt_tokens,
                     result.cache_hit_tokens, result.cache_miss_tokens)
        if path is None:
            # 融合模式：一次调用产出两侧 + 对齐好的行（对照臂 / 存量产物的生产路径）
            payload = build_page_payload(page=page_no, page_total=page_total,
                                         result=result, source_chars=len(source))
        else:
            # 两路模式：**只存自己那一侧**（行由读时合并产生，单路存行必然是一堆单侧行）
            payload = build_path_payload(path=path, page=page_no, page_total=page_total,
                                         result=result, source_chars=len(source))
        payload["prev_context_chars"] = len(history)
        # P4b：把 src 校验成"每块覆盖的块号"，不合法就**整页不带坐标**（读时退回结构配对）。
        # 校验放在这里而不是 `coords` 里，是因为"输出块数"要等拿到这一侧的 Markdown 才知道。
        if codes:
            side_md = payload.get("original_md") if path == "a" else payload.get("translation_md")
            if path is not None:
                blocks_out = split_blocks(side_md or "")
                covered, why = _coords.validate_src(result.src, codes, len(blocks_out))
                if covered:
                    payload["src"] = covered
                    counters["coords"] = counters.get("coords", 0) + 1
                else:
                    log.warning("第 %d 页坐标契约不可用（%s）：%s", page_no, path, why)
        payload["lookahead_chars"] = len(lookahead)
        payload["cache_hit_tokens"] = result.cache_hit_tokens
        payload["cache_miss_tokens"] = result.cache_miss_tokens
        payload["edges"] = {"head": bool(edges.get("head")),
                            "tail": bool(edges.get("tail"))}
        if not payload.get("model"):
            payload["model"] = model_id or ""
        emit(page_no, payload)

    def on_error(item, exc) -> None:
        counters["failed"] += 1
        log.warning("第 %s 页最终失败：%s", item.key, exc)

    def should_stop() -> bool:
        ctrl = ctx.check_control()
        if ctrl:
            log.info("%s 收到 %s 信号（已完成 %d 页）", label, ctrl, counters["done"])
            return True
        return False

    stats = scheduler.run(
        [ScheduleItem(key=f"p{page:03d}", payload=page) for page in pending],
        process, on_error=on_error, should_stop=should_stop,
    )

    store.flush()
    elapsed = round(time.time() - t_start, 2)
    verb = "转录" if path == "a" else "翻译"
    ctx.set_cursor(done_pages=sorted(done), total_all=page_total,
                   finished=counters["done"], failed=counters["failed"])
    ctx.progress(scoped_done(), scope_total, force=True,
                 message=(f"已{verb} {scoped_done()}/{scope_total} 页"
                          + (f"，失败 {counters['failed']}" if counters["failed"] else "")))
    _finish_paper_status(paper_id)
    reader_service.invalidate_cache(paper_id)

    summary = {
        "message": (f"{label}完成：成功 {counters['done']} 页，"
                    f"失败 {stats.failed} 页，用时 {elapsed}s"),
        "path": path or FUSED,
        "task_type": PATH_TASK_TYPES[path],
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
                  "reasoning_tokens": counters["reasoning_tokens"],
                  "cache_hit_tokens": counters["cache_hit_tokens"],
                  "cache_miss_tokens": counters["cache_miss_tokens"]},
        "elapsed_s": elapsed,
        "scheduler": stats.to_dict(include_attempts=False),
        "concurrency_timeline": stats.timeline,
        "_done_items": scoped_done(),
        "_total_items": scope_total,
    }
    return summary


def run_translate_task(ctx) -> dict:
    """融合模式的任务体（`TRANSLATE_MD`）：CLI / 自检脚本沿用的入口。

    两路拆分后它不再是默认流程，但**保留**：它既是回滚臂，也是"同一次调用 vs 两路并发"
    这条对照实验的另一半（存量两篇论文的 `md/pages.json` 就是它产出的）。
    """
    return run_path_task(ctx, None)


def _escalate_max_tokens(exc: Exception, current: int) -> int | None:
    """只在"思考模式把预算吃光导致正文为空"这类错误上升档 max_tokens。"""
    msg = str(exc)
    if "正文为空" not in msg and "思维链" not in msg and "未返回完整" not in msg:
        return None
    return next_max_tokens(current)


def _model_id_for(path: str | None) -> str | None:
    """每一路可以各配一个模型（空 = 用 vision 默认）。

    键名：``translate_model_a`` / ``translate_model_b``（裸键，`retrieval.setting` 会先试
    ``kb.`` 前缀；两路拆开的意义之一就是"两个模型可以不同"，所以留出这个口子）。
    """
    if path is None:
        return None
    try:
        from backend.services.knowledge import retrieval

        value = str(retrieval.setting(f"translate_model_{path}", "") or "").strip()
        return value or None
    except Exception:  # noqa: BLE001 —— 设置读不到就用默认模型
        return None


def _path_provider(path: str | None = None):
    """优先用 vision 模型（要读页图）；未配置时退回 generation（纯文本翻译）。

    `path` 对应 `translate_model_a` / `translate_model_b` 里配置的模型 id（可为空）。
    """
    model_id = _model_id_for(path)
    try:
        return model_manager.get_vision(model_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("未配置 vision 模型，退回 generation（不带页图）：%s", exc)
        return model_manager.get_generation(model_id)


def _translation_provider():
    """融合模式的 provider（CLI / 脚本沿用）。"""
    return _path_provider(None)


def _current_model_id(path: str | None = None) -> str | None:
    model_id = _model_id_for(path)
    for mtype in ("vision", "generation"):
        try:
            return model_manager.get_config(model_id, mtype).get("id")
        except Exception:  # noqa: BLE001
            continue
    return None


def _finish_paper_status(paper_id: str) -> None:
    data = paper_service.refresh_progress_counters(paper_id)
    bus.publish("papers", EVENT_PAPER_STATUS, {
        "paper_id": paper_id, "status": data.get("status"),
        "translate_done": data.get("translate_done"),
        "translate_total": data.get("translate_total"),
        "transcribe_done": data.get("transcribe_done"),
        "transcribe_total": data.get("transcribe_total"),
    })


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------


def start_translation(paper_id: str, *, paths: list[str] | None = None,
                      mode: str = "resume", force: bool = False,
                      brief: bool = True, pages: list[int] | None = None) -> dict:
    """触发页级任务（幂等：每一路已有**活着**的任务则复用）。

    `paths`：``["a", "b"]``（默认，两路并发）／``[FUSED]``（融合，对照臂与回滚）／
    ``["b"]``（只补中文翻译）。

    幂等按**路**判定：同一路上重复触发会复用活动任务（复用**连 `force` 也不例外** ——
    同一路同时跑两套会互相覆盖产物文件，只会更难收拾）；不同路互不影响，这正是并发的意义。
    返回 `{tasks: {a?,b?,fused?}, task_ids, paths, reused, created}`；`reused` 为真表示
    **请求的每一路**都复用了已有任务（前端据此提示"沿用已有任务"）。
    """
    paper_service.get_paper(paper_id)
    if not list_pages(paper_id):
        raise ValidationError("这篇论文还没有页面图，请先完成解析")

    wanted = [normalize_path(p) for p in (paths or ["a", "b"])]
    # 去重但保序；融合模式是"单独一条"，不和两路混着跑（它写的是同一个 pages.json）
    seen: list[str | None] = []
    for p in wanted:
        if p not in seen:
            seen.append(p)
    if None in seen and len(seen) > 1:
        raise ValidationError("融合模式（fused）不能和 a/b 两路同时触发")

    tasks: dict[str, dict] = {}
    created_count = 0
    for path in seen:
        key = path or FUSED
        existing = _active_path_task(paper_id, path)
        if existing is not None and not _task_is_live(existing):
            # 已请求取消 / 心跳早就停了的僵尸任务不能挡住新任务：
            # 否则前端点「继续翻译」会拿回一个永远不会执行的任务，表现为"点了没反应"。
            _drop_task(existing["id"], "已请求取消或租约过期，本次触发不再复用")
            existing = None
        if existing:
            tasks[key] = {**existing, "reused": True}
            continue
        store = page_store(paper_id, path)
        total = len(list_pages(paper_id))
        done = len(store.done_pages())
        task = _create_page_task(paper_id, path, {
            "mode": mode, "force": force, "brief": brief, "pages": pages,
        }, total_items=_target_count(pages, total, done, force))
        tasks[key] = {**task, "reused": False}
        created_count += 1

    return {
        "paper_id": paper_id,
        "paths": [p or FUSED for p in seen],
        # 顶层保留一个 `id`（前端与 `_chain_translate` 的历史用法）：优先给"翻译"那一路
        "id": (tasks.get("b") or tasks.get(FUSED) or next(iter(tasks.values()))).get("id"),
        "task_ids": [t["id"] for t in tasks.values()],
        "tasks": tasks,
        "reused": created_count == 0,
        "created": created_count,
    }


def _target_count(pages: list[int] | None, total: int, done: int,
                  force: bool) -> int:
    """任务进度的分母：**指定页时就是指定的页数**。

    以前忽略了 `pages`（只按"总页数 - 已完成"算），于是"重跑第 1、2 页"的任务
    在任务页显示成 `0/0`，看着像什么都没做。
    """
    if pages:
        return len({int(p) for p in pages})
    return total if force else max(0, total - done)


def _task_is_live(task: dict) -> bool:
    """进行中的任务是否还会被真正执行（取消请求与僵尸都算不活）。

    ★ 时间戳是 `utils.now()` 写的 **UTC**（naive），这里必须用同一个时钟。
      之前用 `datetime.now()`（本地时差 +8h）去减 → 刚建的任务看着像 8 小时前的"僵尸"，
      于是被误判为不活 → 旧任务被取消、又新建一个；而旧任务可能正在 worker 里跑 →
      **同一篇两套翻译并发**（用户报过"重复运行翻译任务"）。
    """
    if task.get("cancel_requested"):
        return False
    stamp = task.get("updated_at") or task.get("created_at")
    if not stamp:
        return True
    try:
        age = (now() - datetime.fromisoformat(str(stamp))).total_seconds()
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


def _active_path_task(paper_id: str, path: str | None) -> dict | None:
    from backend.services.task.task_service import task_service

    return task_service.find_active(PATH_TASK_TYPES[path], paper_id, "md")


def _active_translate_task(paper_id: str) -> dict | None:
    """融合模式的活动任务（CLI / 旧调用点沿用）。"""
    return _active_path_task(paper_id, None)


def _create_page_task(paper_id: str, path: str | None, params: dict,
                      total_items: int) -> dict:
    from backend.services.task.task_service import task_service

    return task_service.create(
        PATH_TASK_TYPES[path], "md", paper_id, params=params, total_items=total_items,
        message=f"排队中（{PATH_LABELS[path]}）",
    )


def _create_translate_task(paper_id: str, params: dict, total_items: int) -> dict:
    """融合模式建任务（CLI / 旧调用点沿用）。"""
    return _create_page_task(paper_id, None, params, total_items)


def translation_cache_info(paper_id: str, path: str | None = None) -> dict:
    store = page_store(paper_id, path)
    return {"paper_id": paper_id, "path": str(store.path),
            "road": path or FUSED, **store.stats()}


def clear_translation_cache(paper_id: str, path: str | None = None) -> dict:
    return {"cleared": page_store(paper_id, path).clear(),
            "road": path or FUSED}
