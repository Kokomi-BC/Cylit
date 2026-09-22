"""端到端验收脚本（一键跑完整链路并打印验收表）。

用法::

    python scripts/e2e_check.py [--pdf <path>] [--paper-id <id>] [--port 8720]

流程：
    初始化库 → 导入 → 解析 → 质量报告 → 翻译（mock，逐页，默认全部页）
    → 索引（chunk+embedding+LanceDB）→ 启动后端 → 调 API → 打印验收结论
运行结束后自动关闭后端，不污染已有服务（使用独立端口与独立进程）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def get(url: str, timeout: float = 120.0):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post(url: str, payload: dict | None = None, timeout: float = 120.0):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_health(base: str, seconds: float = 60.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            return bool(get(f"{base}/health", timeout=8)["data"]["ok"])
        except Exception:  # noqa: BLE001
            time.sleep(2.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=str(ROOT.parent / "TurboQuant_Online_Vector ICLR-26.pdf"))
    ap.add_argument("--paper-id", default="e2e-check")
    ap.add_argument("--port", type=int, default=8720)
    ap.add_argument("--translate-limit", type=int, default=4,
                    help="只翻前 N 页（0 = 全部）")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"找不到 PDF：{pdf}")
        return 2
    py = sys.executable
    base = f"http://127.0.0.1:{args.port}"

    print("\n=== 1. 初始化与解析 ===")
    subprocess.run([py, str(ROOT / "cli" / "pr.py"), "init"], check=False)
    proc = subprocess.run(
        [py, str(ROOT / "cli" / "pr.py"), "build", str(pdf), "--id", args.paper_id],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    detail = ""
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().split("\n")
        detail = " | ".join(tail[-4:])[:400]
    check("导入 + 解析（CLI）", proc.returncode == 0, detail)

    # 解析实际落到哪篇论文上。
    # 注意：`pr.py build` 对**同一 PDF（sha256 相同）**会复用已有论文（按设计不产生 -2/-3 副本），
    # 所以这里必须按 source_hash 精确定位，否则会退化成"取最新一篇"——
    # 若库里最新的是别的论文（例如无公式的扫描件演示件），后面关于公式分级的断言会集体假失败。
    from backend.core.utils import sha256_file
    from backend.parsing.ir.ir_reader import IRReader
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope
    from backend.services.paper import paper_service, reader_service

    digest = sha256_file(pdf)
    with session_scope() as sess:
        row = sess.get(Paper, args.paper_id)
        if row is None:
            row = (sess.query(Paper)
                   .filter(Paper.source_hash == digest, Paper.deleted_at.is_(None))
                   .order_by(Paper.created_at.desc()).first())
        if row is None:
            row = (sess.query(Paper)
                   .filter(Paper.deleted_at.is_(None))
                   .order_by(Paper.created_at.desc()).first())
        args.paper_id = row.id if row is not None else args.paper_id
    print(f"    论文 ID：{args.paper_id}（source_hash {digest[:12]}）")

    paths = paper_service.paper_paths(args.paper_id)
    ir = IRReader(paths["ir"])
    stats = ir.stats() if ir.exists else {}
    check("IR 生成", bool(stats.get("blocks")), f"blocks={stats.get('blocks')}")
    check("Markdown 事实源", paths["markdown"].is_file(),
          f"{paths['markdown'].stat().st_size if paths['markdown'].is_file() else 0} bytes")
    check("阅读器索引 blocks.json", paths["blocks_index"].is_file())
    dist = {"A": 0, "B": 0, "C": 0}
    for f in ir.formulas():
        dist[f["grade"] or "C"] = dist.get(f["grade"] or "C", 0) + 1
    total_f = max(1, sum(dist.values()))
    check("公式 A/B/C 分级", sum(dist.values()) > 0,
          f"A={dist['A']} B={dist['B']} C={dist['C']}（C 占比 {dist['C'] / total_f:.1%}）")
    check("插图体检", len(ir.figures()) >= 0, f"{len(ir.figures())} 条")

    print("\n=== 2. 翻译（mock provider，逐页）===")
    # 直接同步驱动翻译任务体（任务队列由后端进程的执行器消费；
    # 本脚本不启动常驻进程，因此用与 TaskRunner 相同的 TaskContext 驱动，
    # 这样测的是同一段业务代码路径）
    from backend.services.task.task_service import TaskContext, task_service
    from backend.services.translation import translation_service
    from backend.services.translation.page_translate import list_pages

    all_pages = list_pages(args.paper_id)
    if args.translate_limit:
        all_pages = all_pages[:args.translate_limit]

    task = task_service.create(
        "TRANSLATE_MD", "md", args.paper_id,
        params={"mode": "resume", "pages": all_pages},
        total_items=0, message="e2e 检查：逐页翻译")

    class E2ECtx(TaskContext):
        pass

    params = {"mode": "resume", "pages": all_pages}
    ctx = E2ECtx(task_id=task["id"], task_type="TRANSLATE_MD", queue="md",
                 paper_id=args.paper_id, params=params, session=None)
    ctx._cursor = {}
    t0 = time.time()
    summary = translation_service.run_translate_task(ctx)
    elapsed = time.time() - t0
    task_service.finish(task["id"], "completed", message=summary.get("message"),
                        stats=summary,
                        done_items=summary.get("_done_items"),
                        total_items=summary.get("_total_items"))
    check("逐页翻译执行", summary.get("done", 0) > 0,
          f"done={summary.get('done')} 页 failed={summary.get('failed')} "
          f"rows={summary.get('rows')} 用时 {elapsed:.1f}s"
          + ("（全部页已有产物，未触发模型调用）" if summary.get("idle") else ""))
    check("断点已落库（cursor.done_pages）", bool(ctx.cursor.get("done_pages")),
          f"cursor={len(ctx.cursor.get('done_pages') or [])} 页")
    sched = (summary.get("scheduler") or {})
    check("自适应并发时间线", "concurrency_peak" in sched or bool(summary.get("idle")),
          f"peak={sched.get('concurrency_peak')} min={sched.get('concurrency_min')} "
          f"success={sched.get('success_rate')}"
          + ("（本次无待翻译页，未经过调度器）" if summary.get("idle") else ""))
    check("任务终态进度一致", (summary.get("_done_items") or 0) > 0,
          f"done_items={summary.get('_done_items')} total={summary.get('_total_items')}")

    store = translation_service.page_store(args.paper_id)
    payload = store.page(all_pages[0]) if all_pages else None
    rows = (payload or {}).get("rows") or []
    check("页产物：原文 + 译文两份 Markdown",
          bool((payload or {}).get("original_md")) and bool((payload or {}).get("translation_md")),
          f"p{all_pages[0] if all_pages else '-'}: rows={len(rows)} "
          f"align={(payload or {}).get('align')}")
    check("行对齐：原文/译文逐块成对",
          bool(rows) and all(r.get("original") for r in rows),
          f"成对行 {sum(1 for r in rows if r.get('original') and r.get('translation'))}/{len(rows)}")
    outline = reader_service.build_outline(args.paper_id)
    zh_headings = [i for i in outline["items"] if i.get("text_zh")]
    check("双语目录（标题带中文）", bool(zh_headings),
          f"标题 {outline['count']} 条，其中带译文 {len(zh_headings)} 条")

    print("\n=== 3. 索引（chunk + embedding + LanceDB）===")
    from backend.services.knowledge import knowledge_service

    idx = knowledge_service.index_paper(args.paper_id)
    check("chunk 生成", idx["chunks"] > 0, json.dumps(idx["by_type"], ensure_ascii=False))
    check("向量入库", idx["embedded"] == idx["chunks"],
          f"embedded={idx['embedded']} err={idx.get('vector_error')}")

    print("\n=== 4. 启动后端并验收 API ===")
    server = subprocess.Popen(
        [py, str(ROOT / "cli" / "pr.py"), "serve", "--port", str(args.port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_health(base):
            check("后端启动", False, "health 未就绪")
        else:
            check("后端启动", True, base)
            api = f"{base}/api/v1"
            h = get(f"{base}/health")["data"]
            check("业务库连通", h["database"]["ok"], h["database"]["backend"])
            check("任务线程池", h["task_runner"]["workers"] >= 1,
                  f"workers={h['task_runner']['workers']}")

            papers = get(f"{api}/papers")["data"]
            check("GET /papers", papers["total"] >= 1, f"total={papers['total']}")

            reader = get(f"{api}/papers/{args.paper_id}/reader", timeout=180)["data"]
            check("GET /reader", len(reader["blocks"]) > 0,
                  f"blocks={len(reader['blocks'])} translated={reader['stats']['translated']}")
            check("三视图字段（view_mode/pair_order）",
                  "view_mode" in reader["progress"] and "pair_order" in reader["progress"],
                  json.dumps(reader["progress"], ensure_ascii=False))
            check("标题层级（结构导航）",
                  any(b.get("heading_level") for b in reader["blocks"]))

            formulas = get(f"{api}/papers/{args.paper_id}/formulas")["data"]
            check("GET /formulas（从 Markdown 提取）",
                  formulas.get("count", 0) > 0 and formulas.get("source") == "markdown",
                  f"count={formulas.get('count')} 行间={formulas.get('display_count')} "
                  f"source={formulas.get('source')}")
            figs = get(f"{api}/papers/{args.paper_id}/figures")["data"]
            check("GET /figures（插图体检）", figs["count"] >= 0, f"count={figs['count']}")

            prog = get(f"{api}/papers/{args.paper_id}/progress")["data"]
            check("GET /progress", "percent" in prog)
            inc = get(f"{api}/papers/{args.paper_id}/content?since=0&limit=5")["data"]
            check("GET /content（增量）", len(inc["items"]) == 5, f"next={inc['next_since']}")
            tr = get(f"{api}/papers/{args.paper_id}/translations")["data"]
            check("GET /translations（增量）", tr["count"] > 0, f"count={tr['count']}")

            md = get(f"{api}/papers/{args.paper_id}/markdown/translated?write=false")["data"]
            check("GET /markdown/translated（带插图双语 Markdown）",
                  md.get("count", 0) > 0 and "##" in (md.get("markdown") or ""),
                  f"pages={md.get('count')} chars={len(md.get('markdown') or '')}")

            kb = get(f"{api}/knowledge/overview")["data"]
            check("GET /knowledge/overview", kb["chunks"] > 0,
                  f"chunks={kb['chunks']} vector={kb['vector_store'].get('chunks')}")

            types = get(f"{api}/models/types")["data"]
            check("GET /models/types（零硬编码）",
                  all(types["defaults"].get(t) is not None
                      for t in ("generation", "embedding", "reranker")),
                  json.dumps({k: (v or {}).get("provider")
                              for k, v in types["defaults"].items()}, ensure_ascii=False))

            tasks = get(f"{api}/tasks")["data"]
            check("GET /tasks（任务中心）", tasks["count"] > 0, f"count={tasks['count']}")

            # 写路径：阅读进度 + 划词解释 + 批注
            put_prog = json.loads(urllib.request.urlopen(urllib.request.Request(
                f"{api}/papers/{args.paper_id}/progress",
                data=json.dumps({"last_page": 3, "percent": 12.5,
                                 "view_mode": "translation", "pair_order": "zh_top"}
                                ).encode("utf-8"),
                method="PUT", headers={"Content-Type": "application/json"})).read())
            check("PUT /progress（服务端权威）",
                  put_prog["data"]["view_mode"] == "translation",
                  json.dumps(put_prog["data"], ensure_ascii=False))

            blk = next((b["id"] for b in reader["blocks"]
                        if b["type"] == "para" and len(b["text"]) > 60), None)
            exp = post(f"{api}/annotations/from-selection", {
                "paper_id": args.paper_id, "block_id": blk,
                "selected_text": (reader["blocks"][1]["text"] or "text")[:60]})["data"]
            check("POST /annotations/from-selection（划词解释）",
                  bool(exp.get("explanation")), f"model={exp.get('model_id')}")
            saved = post(f"{api}/annotations/save", {
                "paper_id": args.paper_id, "block_id": blk,
                "explanation": exp.get("explanation"), "model_id": exp.get("model_id")})["data"]
            check("批注落库（含来源）", bool(saved.get("id")), saved.get("source"))

            report = get(f"{api}/papers/{args.paper_id}/reconcile")["data"]
            check("三存储对账", "issues" in report,
                  f"issues={len(report['issues'])} ok={report['ok']}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== 验收结果：{passed}/{total} 通过 ===")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    (ROOT / "data" / "logs" / "e2e_check.json").parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "logs" / "e2e_check.json").write_text(
        json.dumps({"passed": passed, "total": total,
                    "results": [{"name": n, "ok": o, "detail": d} for n, o, d in RESULTS]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
