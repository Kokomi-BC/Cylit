#!/usr/bin/env python
"""命令行入口 ``pr.py``（规划 §23 cli/、§6.1：CLI 与服务端共用解析代码）。

用法::

    python cli/pr.py init                      # 建库建表 + 写入内置模型
    python cli/pr.py import <pdf> [--id X] [--parse]
    python cli/pr.py parse <paper_id> [--pages N] [--llm-repair]
    python cli/pr.py structure <paper_id>      # 打印阅读顺序与块结构（人工核对）
    python cli/pr.py qa <paper_id>             # 质保报告（A/B/C 占比、一致性命中、插图体检）
    python cli/pr.py build <pdf> [--id X]      # 导入 + 解析一步到位
    python cli/pr.py translate <paper_id> [--mode resume|retranslate] [--pages 3,4]
    python cli/pr.py index <paper_id>          # chunk + embedding + LanceDB
    python cli/pr.py reconcile [paper_id]      # 三存储对账（只报告）
    python cli/pr.py serve [--host] [--port] [--reload]
    python cli/pr.py bench <pdf> [--mode fixed|adaptive] [--concurrency 4]
    python cli/pr.py stats                     # 论文/任务总览
    python cli/pr.py orphans [--apply]         # 孤儿行清扫（paper_id 已不在库里的行；默认只报告）
    python cli/pr.py trash [--purge] [--days 30]   # 回收站：软删论文留下的目录
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from backend.core.config import get_settings  # noqa: E402
from backend.core.logging import get_logger, setup_logging  # noqa: E402
from backend.core.utils import json_dumps  # noqa: E402

log = get_logger("pr")


def _print(obj, title: str | None = None) -> None:
    if title:
        print(f"\n=== {title} ===")
    if isinstance(obj, str):
        print(obj)
    else:
        print(json_dumps(obj, indent=2))


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def cmd_init(args) -> int:
    from backend.repositories.mysql.session import init_db
    from backend.services.model import model_service

    info = init_db(drop=args.drop)
    _print(info, "数据库初始化")
    n = model_service.ensure_builtin_models()
    print(f"内置模型：新增 {n} 个")
    s = get_settings()
    print(f"数据目录：{s.data_dir}")
    return 0


# --------------------------------------------------------------------------
# import / parse / build
# --------------------------------------------------------------------------


def cmd_import(args) -> int:
    from backend.services.paper import paper_service

    pdf = Path(args.pdf)
    row = paper_service.import_pdf(
        pdf, pdf.name, title=args.title, tags=args.tags,
        paper_id=args.id, auto_parse=args.parse,
    )
    _print(row, "导入完成")
    if args.parse:
        print("已排队 PARSE 任务；可用 `pr.py parse` 同步执行查看结果")
    return 0


def _run_parse_sync(paper_id: str, *, max_pages: int = 0, llm_repair: bool = False,
                    show: bool = True) -> int:
    """同步执行解析（CLI 不走任务队列，便于调试）。"""
    from backend.core.config import get_settings
    from backend.parsing.pipeline import PaperBuilder
    from backend.services.paper import paper_service, parse_hooks

    paths = paper_service.paper_paths(paper_id)
    pdf = paper_service.resolve_pdf(paper_id)
    t0 = time.time()
    last = {"stage": "", "at": 0.0}

    def progress(stage: str, done: int, total: int) -> None:
        now = time.time()
        if stage != last["stage"] or now - last["at"] > 0.6 or done == total:
            last["stage"], last["at"] = stage, now
            print(f"  [{stage}] {done}/{total}", flush=True)

    # 与服务层共用同一份钩子：公式修复 + 扫描件读页（避免"CLI 少注一个钩子"）
    builder = PaperBuilder(pdf, paper_id, paths["root"], max_pages=max_pages,
                           use_llm_repair=llm_repair,
                           repair_fn=parse_hooks.build_repair_fn(),
                           vision_fn=parse_hooks.build_vision_fn({}),
                           vision_dpi=get_settings().parse_vision_dpi,
                           progress=progress if show else None)
    result = builder.build()

    from backend.services.task.handlers import _sync_to_mysql

    _sync_to_mysql(paper_id, result)
    from backend.services.paper import reader_service

    reader_service.invalidate_cache(paper_id)

    if show:
        _print({
            "paper_id": paper_id, "title": result.title,
            "counts": result.counts(),
            "grades": result.grade_distribution(),
            "duration_s": round(time.time() - t0, 2),
            "stage_ms": result.stats.get("stage_ms"),
            "scanned_pages": result.stats.get("scanned_pages"),
            "markdown": result.stats.get("markdown"),
            "quality": result.stats.get("quality", {}).get("checks"),
            "warnings": result.warnings[:6],
        }, "解析结果")
    return 0


def cmd_parse(args) -> int:
    return _run_parse_sync(args.paper_id, max_pages=args.pages,
                           llm_repair=args.llm_repair)


def cmd_build(args) -> int:
    from backend.services.paper import paper_service

    pdf = Path(args.pdf)
    row = paper_service.import_pdf(pdf, pdf.name, title=args.title,
                                   paper_id=args.id, auto_parse=False)
    print(f"已导入：{row['id']}（{row['page_count']} 页）")
    return _run_parse_sync(row["id"], max_pages=args.pages,
                           llm_repair=args.llm_repair)


# --------------------------------------------------------------------------
# structure / qa
# --------------------------------------------------------------------------


def cmd_structure(args) -> int:
    from backend.parsing.ir.ir_reader import IRReader
    from backend.services.paper import paper_service

    ir = IRReader(paper_service.paper_paths(args.paper_id)["ir"])
    if not ir.exists:
        print("IR 不存在：请先执行 parse")
        return 1
    pages = ir.pages()
    two_col = sum(1 for p in pages if p["columns"] > 1)
    print(f"=== 结构概览（{len(pages)} 页，双栏页 {two_col}）===")
    for p in pages[: args.pages or len(pages)]:
        print(f"  p{p['page_no']:>3} {p['width']:.0f}x{p['height']:.0f} "
              f"cols={p['columns']} gutter={p['gutter_x']} blocks={p['block_count']} "
              f"chars={p['text_chars']}")
    print("\n=== 抽检块（前 25）===")
    for b in ir.blocks()[:25]:
        print(f"  {b['id']} {b['type']:8} col={b['col']} order={b['order_global']:>4} "
              f"y={b['y0']:6.1f} L={b['line_count']:>2} :: "
              f"{(b['text_clean'] or '')[:70]}")
    return 0


def cmd_qa(args) -> int:
    from backend.parsing.ir.ir_reader import IRReader
    from backend.services.paper import paper_service

    ir = IRReader(paper_service.paper_paths(args.paper_id)["ir"])
    if not ir.exists:
        print("IR 不存在：请先执行 parse")
        return 1
    formulas = ir.formulas()
    dist = {"A": 0, "B": 0, "C": 0}
    for f in formulas:
        dist[f["grade"] or "C"] = dist.get(f["grade"] or "C", 0) + 1
    total = max(1, sum(dist.values()))
    print(f"=== 公式分级（{sum(dist.values())} 条）===")
    for k in ("A", "B", "C"):
        print(f"  {k}: {dist.get(k, 0):>4}  ({dist.get(k, 0) / total * 100:.1f}%)")
    print("\n=== 插图体检 ===")
    for f in ir.figures():
        print(f"  {f['block_id']} p{f['page_number']} dpi={f['effective_dpi']} "
              f"edge_ink={f['edge_ink']} status={f['status']} :: "
              f"{(f['caption'] or '')[:60]}")
    return 0


# --------------------------------------------------------------------------
# translate / index / reconcile
# --------------------------------------------------------------------------


def cmd_translate(args) -> int:
    from backend.services.paper import paper_service
    from backend.services.translation import translation_service
    from backend.services.translation.page_translate import list_pages

    paper_service.get_paper(args.paper_id)
    pages = list_pages(args.paper_id)
    store = translation_service.page_store(args.paper_id)
    done = len(store.done_pages())
    pending = len(pages) if args.mode == "retranslate" else max(0, len(pages) - done)

    class Ctx:
        def __init__(self) -> None:
            self.paper_id = args.paper_id
            self.params = {"mode": args.mode, "brief": not args.no_brief}
            if args.pages:
                self.params["pages"] = [int(p) for p in args.pages.split(",") if p.strip()]
            self.cursor: dict = {}
            self.task_id = "cli"
            self.done_items = 0
            self.total_items = 0
            self._t0 = time.time()

        def progress(self, done, total, message=None, force=False, **kw):
            print(f"  进度 {done}/{total} {message or ''}", flush=True)

        def set_cursor(self, **kw):
            self.cursor.update(kw)

        def check_control(self):
            return None

        def should_stop(self):
            return False

        def should_pause(self):
            return False

        def _flush(self, **kw):
            pass

    print(f"共 {len(pages)} 页，已有产物 {done} 页，本次待翻译 {pending} 页（模式 {args.mode}）")
    if not pending:
        print("没有需要翻译的页")
        return 0
    # 页级翻译固定串行（没有并发设置），这里不再覆盖任何并发参数。
    ctx = Ctx()
    result = translation_service.run_translate_task(ctx)
    _print(result, "翻译结果")
    return 0


def cmd_index(args) -> int:
    from backend.services.knowledge import knowledge_service

    result = knowledge_service.index_paper(args.paper_id, rebuild=args.rebuild)
    _print(result, "索引结果")
    return 0


def cmd_reconcile(args) -> int:
    from backend.services.paper import paper_service

    report = paper_service.reconcile(args.paper_id)
    _print(report, "三存储对账")
    return 0 if report.get("ok") else 2


def cmd_refresh_progress(args) -> int:
    """按磁盘（页图 + `md/pages.json`）重算论文行的 translate_done/translate_total/status。

    为什么要这个命令：这两个字段是**派生数据**，正常路径由解析 / 翻译任务自己回写；
    老版本"解析时无条件清零"曾经把它们写坏（卡片显示 0 / 15 而译文其实都在），
    这里给一个**一次性修复**入口（磁盘是权威，不会误删任何产物）。
    """
    from backend.services.paper import paper_service

    if args.paper_id:
        targets = [args.paper_id]
        before = {args.paper_id: paper_service.get_paper(args.paper_id)}
    else:
        listing = paper_service.list_papers(page_size=1000)["items"]
        before = {p["id"]: p for p in listing}
        targets = [p["id"] for p in listing]
    fixed = 0
    for pid in targets:
        row = paper_service.refresh_progress_counters(pid)
        old = before.get(pid) or {}
        changed = (old.get("translate_done"), old.get("translate_total"), old.get("status")) != (
            row["translate_done"], row["translate_total"], row["status"])
        fixed += 1 if changed else 0
        print(f"  {'*' if changed else ' '} {pid:<28} {row['status']:<12} "
              f"翻译 {row['translate_done']}/{row['translate_total']}"
              + (f"（原 {old.get('translate_done')}/{old.get('translate_total')} "
                 f"{old.get('status')}）" if changed else "")
              + f" :: {(row['title'] or '')[:44]}")
    print(f"共 {len(targets)} 篇，修正 {fixed} 篇")
    return 0


def cmd_metadata_auto(args) -> int:
    """补全论文元数据（会议 / 年份 / 摘要 / 标题）：先离线读 PDF，`--model` 再补模型那一路。

    为什么要这个命令：元数据以前只有导入/解析两个入口会自动写，历史论文行里
    「会议 / 期刊」「年份」「摘要」大面积为空；这个入口给它们一次补齐的机会
    （只填空字段，`--overwrite` 才覆盖已有值）。
    """
    from backend.services.paper import metadata_fill, paper_service

    if args.paper_id:
        targets = [args.paper_id]
    else:
        targets = [p["id"] for p in paper_service.list_papers(page_size=1000)["items"]]
    for pid in targets:
        report = metadata_fill.auto(pid, model=bool(args.model),
                                    overwrite=bool(args.overwrite))
        applied = report.get("applied") or {}
        paper = report.get("paper") or {}
        bits = "、".join(f"{k}={str(v)[:40]}" for k, v in applied.items()) or "（无可补全）"
        print(f"  {pid:<32} {bits}")
        print(f"      → {(paper.get('title') or '')[:70]}")
        meta_bits = [paper.get("authors") or "", paper.get("venue") or "",
                     str(paper.get("year") or ""), "、".join(paper.get("tags") or [])]
        print(f"        {' · '.join(b for b in meta_bits if b) or '（作者/会议/年份/标签仍为空）'}")
        for err in report.get("errors") or []:
            print(f"        ! {err}")
    print(f"共 {len(targets)} 篇")
    return 0


def cmd_stats(args) -> int:
    from backend.services.knowledge import knowledge_service
    from backend.services.paper import paper_service
    from backend.services.task.task_service import task_service

    papers = paper_service.list_papers(page_size=200)
    print(f"=== 论文 {papers['total']} 篇 ===")
    for p in papers["items"]:
        print(f"  {p['id']:<28} {p['status']:<12} 页 {p['page_count']:>3} "
              f"翻译 {p['translate_done']}/{p['translate_total']} :: "
              f"{(p['title'] or '')[:50]}")
    _print(task_service.stats(), "任务统计")
    _print(knowledge_service.overview(), "知识库概览")
    return 0


def cmd_orphans(args) -> int:
    """孤儿行清扫：`paper_id` 已经不在 `paper` 表里的行（**默认只看不动**）。

    什么时候用：删过论文之后想确认"有没有留下东西"，或者库里攒了历史残留。
    加 `--apply` 才真删；删的是**读不到的行**（列表都按 paper_id 过滤），不会碰到在库论文的数据。
    """
    from backend.services.paper import paper_service

    report = paper_service.sweep_orphans(apply=bool(args.apply))
    rows = report.get("tables") or {}
    total = sum(int(v.get("rows") or 0) for v in rows.values())
    print(f"=== 孤儿行（paper_id 不在 paper 表里）{'（已清理）' if args.apply else '（只报告，加 --apply 才删）'} ===")
    for key, info in rows.items():
        if not info.get("rows"):
            continue
        tail = f" → 删了 {info['deleted']} 行" if args.apply else ""
        print(f"  {key:<14} {info['rows']:>5} 行 / {info['papers']} 篇{tail}   e.g. "
              f"{', '.join(str(x) for x in (info.get('sample') or [])[:3])}")
    msg = report.get("orphan_messages") or {}
    conv = report.get("orphan_conversations") or {}
    if msg.get("rows"):
        print(f"  {'messages':<14} {msg['rows']:>5} 行"
              f"{' → 删了 ' + str(msg['deleted']) + ' 行' if args.apply else ''}")
    if conv.get("papers"):
        print(f"  {'conversations':<14}       {conv['papers']} 篇"
              f"{' → 清了 ' + str(conv['purged']) + ' 个会话' if args.apply else ''}   e.g. "
              f"{', '.join(str(x) for x in (conv.get('sample') or [])[:3])}")
    if report.get("vector_chunks"):
        print(f"  {'lancedb':<14} {'':>5}    → 删了 {report['vector_chunks']} 条向量")
    print(f"共 {total} 行；没有孤儿就是干净的。" if total else "共 0 行 —— 干净。")
    return 0


def cmd_trash(args) -> int:
    """回收站：列出软删论文留在 `data/trash/` 里的目录，`--purge` 才真删。

    为什么要有这个命令：`delete_paper(hard=False)` 只是把论文目录**移进** trash，
    全仓库没有别的地方会清它 —— 不删就一直占盘（PDF + 页产物 + 插图）。
    `--days N` 只清 N 天前的（默认 30）；`--days 0` 全清。
    """
    import shutil
    import time

    s = get_settings()
    root = s.trash_dir
    if not root.is_dir():
        print(f"回收站是空的（{root} 不存在）。")
        return 0
    items = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)
    if not items:
        print(f"回收站是空的（{root}）。")
        return 0
    now = time.time()
    days = int(args.days)
    doomed = [p for p in items
              if days <= 0 or (now - p.stat().st_mtime) >= days * 86400]
    for p in items:
        age = (now - p.stat().st_mtime) / 86400
        mark = "→ 清" if (p in doomed and args.purge) else ("可清" if p in doomed else "保留")
        print(f"  {p.name:<48} {age:6.1f} 天前  {mark}")
    print(f"共 {len(items)} 个目录，符合条件 {len(doomed)} 个（--days {days}）。")
    if not args.purge:
        print("加 --purge 才真删。")
        return 0
    freed = 0
    for p in doomed:
        for f in p.rglob("*"):
            if f.is_file():
                freed += f.stat().st_size
        shutil.rmtree(p, ignore_errors=True)
    print(f"已清 {len(doomed)} 个目录，释放约 {freed / 1048576:.1f} MB。")
    return 0


# --------------------------------------------------------------------------
# serve / bench
# --------------------------------------------------------------------------


def cmd_serve(args) -> int:
    import uvicorn

    s = get_settings()
    host = args.host or s.host
    port = args.port or s.port
    print(f"启动后端：http://{host}:{port}  （API 文档 /docs）")
    fe = Path(s.project_root) / "frontend" / "index.html"
    if fe.is_file():
        print(f"前端界面：http://{host}:{port}/app/   "
              f"（或直接打开 http://{host}:{port}/ 会跳转）")
    else:
        print("未找到 frontend/index.html —— 只有 API 可用")
    uvicorn.run("backend.main:app", host=host, port=port, reload=args.reload,
                log_level="info")
    return 0


def cmd_agent(args) -> int:
    """启动问答 Agent 服务（独立进程，默认 :8711）。

    为什么不跟 `serve` 合一：agent 崩溃不该拖垮解析/翻译调度器，改一个 skill 也不该
    重启正在跑的解析任务与 SSE 连接（规划 §4.1）。两个进程共用同一个 conda 环境。
    """
    import uvicorn

    from backend.agent.config import AgentConfigError, load
    from backend.core.config import get_settings

    s = get_settings()
    if not s.agent_enabled:
        print("问答功能已被设置关闭（agent_enabled=false），仍要启动请改设置。")
    try:
        cfg = load()
    except AgentConfigError as exc:
        print(f"启动失败：{exc}")
        return 2
    host = args.host or s.agent_host
    port = args.port or s.agent_port
    print(f"Agent 服务：http://{host}:{port}  （主后端 {cfg.main_base}）")
    print(f"skills 目录：{cfg.skills_dir}")
    print(f"自检：http://{host}:{port}/readyz")
    uvicorn.run("backend.agent.service:create_app", host=host, port=port,
                factory=True, reload=args.reload, log_level="info")
    return 0


def cmd_bench(args) -> int:
    """并发策略对比实验（规划 §26 实验一/二：固定并发 vs 自适应调度器）。

    这是**独立的压测工具**（用 mock provider，不碰真实产物）：调度器本身仍然通用，
    但页级翻译已经固定串行，所以并发上下限在这里**显式写死**，不再读 `translate_*` 设置。
    """
    from backend.providers.generation.mock import MockGeneration
    from backend.services.task.scheduler import (
        AdaptiveScheduler, ScheduleItem, SchedulerConfig,
    )

    scenarios = []
    if args.mode in ("fixed", "both"):
        scenarios.append(("固定并发无重试", None, args.concurrency))
    if args.mode in ("adaptive", "both"):
        scenarios.append(("自适应并发", SchedulerConfig(
            initial=args.concurrency, maximum=args.concurrency * 2,
            minimum=1, breaker_threshold=8,
        ), args.concurrency))

    results = []
    for name, cfg, conc in scenarios:
        if cfg is None:
            # 固定并发 + 无重试：直接用线程池，失败即失败
            from concurrent.futures import ThreadPoolExecutor

            provider = MockGeneration({"parameters": {
                "rate_limit_every": args.rate_limit_every,
                "fail_every": args.fail_every, "delay_ms": args.delay_ms,
            }})
            items = list(range(args.n))

            def call_one(_i):
                from backend.providers.base import GenerationResult

                class Payload:
                    def __init__(self, i):
                        self.i = i
                        self.prompt_tokens = 0
                        self.completion_tokens = 0

                res = provider.generate(f"item {_i}")
                p = Payload(_i)
                p.prompt_tokens = res.prompt_tokens
                p.completion_tokens = res.completion_tokens
                return p

            t0 = time.time()
            done = failed = 0
            with ThreadPoolExecutor(max_workers=conc) as pool:
                for r in pool.map(lambda i: _safe(call_one, i), items):
                    if r:
                        done += 1
                    else:
                        failed += 1
            results.append({"scenario": name, "concurrency": conc,
                            "total": args.n, "done": done, "failed": failed,
                            "success_rate": round(done / args.n, 4),
                            "duration_s": round(time.time() - t0, 2)})
            continue

        provider = MockGeneration({"parameters": {
            "rate_limit_every": args.rate_limit_every,
            "fail_every": args.fail_every, "delay_ms": args.delay_ms,
        }})
        sched = AdaptiveScheduler(cfg)
        items = [ScheduleItem(key=f"i{i}", payload=i) for i in range(args.n)]
        t0 = time.time()
        stats = sched.run(items, lambda i: provider.generate(f"item {i}"))
        results.append({"scenario": name, "concurrency": conc,
                        "total": stats.total, "done": stats.done,
                        "failed": stats.failed,
                        "success_rate": round(stats.success_rate, 4),
                        "retries": stats.retries,
                        "duration_s": round(time.time() - t0, 2),
                        "concurrency_peak": stats.concurrency_peak,
                        "concurrency_min": stats.concurrency_min,
                        "timeline": stats.timeline[:40]})
    _print(results, f"并发对比实验（{args.n} 项，429 每 {args.rate_limit_every} 次）")
    return 0


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pr.py", description="Cylit CLI")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="建库建表 + 内置模型")
    sp.add_argument("--drop", action="store_true", help="先删除所有表（危险）")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("import", help="导入 PDF")
    sp.add_argument("pdf")
    sp.add_argument("--id", default=None)
    sp.add_argument("--title", default=None)
    sp.add_argument("--tags", default=None)
    sp.add_argument("--parse", action="store_true", help="同时创建解析任务")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("parse", help="同步解析（CLI 调试用）")
    sp.add_argument("paper_id")
    sp.add_argument("--pages", type=int, default=0)
    sp.add_argument("--llm-repair", action="store_true")
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("build", help="导入 + 解析一步到位")
    sp.add_argument("pdf")
    sp.add_argument("--id", default=None)
    sp.add_argument("--title", default=None)
    sp.add_argument("--pages", type=int, default=0)
    sp.add_argument("--llm-repair", action="store_true")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("structure", help="打印结构（人工核对阅读顺序）")
    sp.add_argument("paper_id")
    sp.add_argument("--pages", type=int, default=6)
    sp.set_defaults(func=cmd_structure)

    sp = sub.add_parser("qa", help="质保报告")
    sp.add_argument("paper_id")
    sp.set_defaults(func=cmd_qa)

    sp = sub.add_parser("translate", help="逐页翻译（产线 A，固定串行）")
    sp.add_argument("paper_id")
    sp.add_argument("--mode", default="resume",
                    choices=["resume", "retranslate"])
    sp.add_argument("--pages", default="",
                    help="只翻指定页，逗号分隔（如 3,4,7）")
    sp.add_argument("--no-brief", action="store_true",
                    help="不生成文档简报（少一次调用，术语一致性下降）")
    sp.set_defaults(func=cmd_translate)

    sp = sub.add_parser("index", help="chunk + embedding + LanceDB")
    sp.add_argument("paper_id")
    sp.add_argument("--rebuild", action="store_true")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("reconcile", help="三存储对账")
    sp.add_argument("paper_id", nargs="?", default=None)
    sp.set_defaults(func=cmd_reconcile)

    sp = sub.add_parser("refresh-progress", help="按磁盘重算论文行的翻译进度（修历史脏数据）")
    sp.add_argument("paper_id", nargs="?", default=None)
    sp.set_defaults(func=cmd_refresh_progress)

    sp = sub.add_parser("metadata-auto", help="补全论文元数据（会议/年份/摘要/标题）")
    sp.add_argument("paper_id", nargs="?", default=None)
    sp.add_argument("--model", action="store_true", help="PDF 里读不出来的再交给模型")
    sp.add_argument("--overwrite", action="store_true", help="连已有值一起覆盖（默认只填空）")
    sp.set_defaults(func=cmd_metadata_auto)

    sp = sub.add_parser("stats", help="总览")
    sp.set_defaults(func=cmd_stats)
    sp = sub.add_parser("orphans", help="孤儿行清扫（paper_id 已不在库里的行；默认只报告）")
    sp.add_argument("--apply", action="store_true", help="真删（不加只看报告）")
    sp.set_defaults(func=cmd_orphans)
    sp = sub.add_parser("trash", help="回收站：列出软删论文留下的目录（--purge 才真删）")
    sp.add_argument("--purge", action="store_true", help="真删符合条件的目录")
    sp.add_argument("--days", type=int, default=30, help="只清 N 天前的（0 = 全清）")
    sp.set_defaults(func=cmd_trash)

    sp = sub.add_parser("serve", help="启动后端")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--reload", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("agent", help="启动问答 Agent 服务（独立进程 :8711）")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--reload", action="store_true")
    sp.set_defaults(func=cmd_agent)

    sp = sub.add_parser("bench", help="并发策略对比实验")
    sp.add_argument("pdf", nargs="?", default=None)
    sp.add_argument("--mode", default="both", choices=["fixed", "adaptive", "both"])
    sp.add_argument("--n", type=int, default=60)
    sp.add_argument("--concurrency", type=int, default=4)
    sp.add_argument("--rate-limit-every", type=int, default=7, dest="rate_limit_every")
    sp.add_argument("--fail-every", type=int, default=11, dest="fail_every")
    sp.add_argument("--delay-ms", type=int, default=120, dest="delay_ms")
    sp.set_defaults(func=cmd_bench)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.error("命令失败：%s: %s", type(exc).__name__, exc)
        if get_settings().debug:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
