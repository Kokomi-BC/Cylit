"""Agent 问答链路自检（**默认不调用模型，也不留任何痕迹**）。

设计原则（2026-10-03 重写，用户口径："大幅简化检查代码，避免在历史记录留下内容以及避免
额外的模型花费 —— 默认检查不调用模型"）：

1. **默认零成本**：默认只跑"读接口 / 形状 / 守卫"这一类不需要调模型的部分；
   凡是要打模型的（LLM 或重排模型）都收在 `--model` 后面，跑之前先打印一条提示。
2. **不留痕**：`--model` 造的会话在结束前**自己删掉**（`batch-delete`）；上传的测试图片
   也从 `data/attachments` 里删掉。跑完 check 之后，应用里的"对话历史"应该和跑之前一样。
3. **短**：一个函数一节，断言写在调用点旁边，不做花哨的抽象。

用法：

    python scripts/agent_check.py                 # 免费（推荐每次提交前跑）
    python scripts/agent_check.py --model         # 真问答一轮（1~2 次模型调用）
    python scripts/agent_check.py --model --deep  # 再加压缩 / 就地改 / 开分支（多几次调用）
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "http://127.0.0.1:8710"
DEFAULT_AGENT_BASE = "http://127.0.0.1:8711"
DEFAULT_PAPER = "attention-is-all-you-need"

#: 16x16 真 PNG（红底 + 蓝方块）：必须真图 —— 坏图会被模型端拒掉，量到的就只是探针的 bug
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAALElEQVR4nGO8IyfHQApgIkk1"
    "Az00sCBzLOT2YVV04pETHZ3ENBI1MA6DtAQA8VYFUbO5ASoAAAAASUVORK5CYII=")
#: 魔数像 PNG、内容却是断的：验证"坏图在上传口就被挡掉"
BROKEN_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
    "q842iQAAAABJRU5ErkJggg==")

results: list[dict] = []


def record(kind: str, path: str, ok: bool, detail: str = "") -> None:
    results.append({"kind": kind, "path": path, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {path:<44} {detail}")


def skip(path: str, detail: str) -> None:
    print(f"[SKIP] {path:<44} {detail}")


# --------------------------------------------------------------------------
# HTTP 小工具（只用标准库，脚本不依赖项目依赖）
# --------------------------------------------------------------------------


def get_json(url: str, headers: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 30.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def put_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 30.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def delete_json(url: str, headers: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, method="DELETE", headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def status_of(url: str) -> int:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return -1


def post_files(url: str, files: list[tuple[str, str, bytes, str]]):
    """multipart 上传（`urllib` 没有内置的，手拼一遍就够）。"""
    boundary = "----agentcheck" + uuid.uuid4().hex
    parts: list[bytes] = []
    for field, name, data, mime in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                     f'filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode("utf-8"))
        parts.append(data)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(url, data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def internal() -> dict:
    path = ROOT / "data" / "secrets" / "agent.token"
    return {"X-Internal-Token": path.read_text(encoding="utf-8").strip()} if path.is_file() else {}


def ask(base: str, paper: str, question: str, *, extra: dict | None = None,
        timeout: float = 300.0) -> dict:
    """POST /agent/ask，把整条 SSE 收成结构化结果（只在 `--model` 里用）。"""
    payload = json.dumps({"paper_id": paper, "question": question,
                          **(extra or {})}).encode("utf-8")
    req = urllib.request.Request(f"{base}/api/v1/agent/ask", data=payload,
                                 headers={"Content-Type": "application/json"})
    events: list[dict] = []
    deltas: list[str] = []
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        etype = None
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event: "):
                etype = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                events.append({"type": etype, "data": data})
                if etype == "answer_delta":
                    deltas.append(data.get("text") or "")
    terminal = [e for e in events if e["type"] in ("done", "error", "cancelled")]
    return {"events": events, "deltas": "".join(deltas), "elapsed": round(time.perf_counter() - t0, 1),
            "done": next((e["data"] for e in terminal if e["type"] == "done"), None),
            "error": next((e["data"] for e in terminal if e["type"] == "error"), None)}


# --------------------------------------------------------------------------
# ① 服务 / 技能 / 模型配置（免费）
# --------------------------------------------------------------------------


def check_service(base: str) -> bool:
    print("\n① 服务 / 技能 / 模型配置")
    try:
        status = get_json(f"{base}/api/v1/agent/status")["data"]
    except Exception as exc:  # noqa: BLE001
        record("service", "主后端 /agent/status 可达", False, f"{type(exc).__name__}: {exc}")
        return False
    record("service", "主后端 /agent/status 可达", True, f"endpoint={status.get('endpoint')}")
    record("service", "问答服务在跑", bool(status.get("running")))
    model = status.get("model") or {}
    record("service", "问答模型已配置（密钥已解析，不回明文）",
           bool(model.get("model_name")) and "api_key" not in model,
           f"{model.get('provider')}/{model.get('model_name')} "
           f"回落={model.get('fallback_to_generation')}")
    try:
        # ⚠️ 走主后端的接口都是信封 `{data, error, trace_id}`：读数据要下钻一层
        skills = get_json(f"{base}/api/v1/agent/skills")["data"].get("skills") or []
        record("service", "skills 已加载（框架加载器，不是目录扫描）",
               len(skills) >= 6 and all((s.get("chars") or 0) > 0 for s in skills),
               f"{len(skills)} 个：{[s.get('name') for s in skills][:4]}…")
        # 写能力的说明书（`text-fix`）必须在，否则模型不知道"改产物"这件事怎么做
        names = {s.get("name") for s in skills}
        record("service", "改了产物的 skill 已挂上（text-fix）", "text-fix" in names,
               f"{sorted(names)}")
        # 高亮/笔记那套动作的说明书（第六步）同样必须在
        record("service", "高亮 / 笔记的 skill 已挂上（highlight-note）", "highlight-note" in names,
               f"{sorted(names)}")
    except Exception as exc:  # noqa: BLE001
        record("service", "skills 已加载（框架加载器，不是目录扫描）", False, f"{exc}")
    record("service", "改动预算已生效（设置页口径）",
           int((status.get("limits") or {}).get("edit_budget") or 0) > 0,
           f"edit_budget={((status.get('limits') or {}).get('edit_budget'))}")
    return bool(status.get("running"))


# --------------------------------------------------------------------------
# ② 只读投影（免费：都是主后端的读接口，不打模型）
# --------------------------------------------------------------------------


def check_projections(base: str, paper: str) -> None:
    print("\n② 只读投影（档案 / 正文 / 公式 / 插图 / 书架 / 笔记）")
    api, head = f"{base}/api/v1/agent", internal()
    try:
        archive = get_json(f"{api}/papers/{paper}/archive", head)["data"]
        record("projection", "档案：标题 / 大纲 / 页数",
               bool(archive.get("title")) and bool(archive.get("outline"))
               and int(archive.get("page_count") or 0) > 0,
               f"《{(archive.get('title') or '')[:24]}》{archive.get('page_count')} 页，"
               f"大纲 {len(archive.get('outline') or [])} 条")
    except Exception as exc:  # noqa: BLE001
        record("projection", "档案：标题 / 大纲 / 页数", False, f"{exc}")

    try:
        pages = get_json(f"{api}/papers/{paper}/pages?pages=1", head)["data"]
        blocks = [b for p in (pages.get("pages") or []) for b in (p.get("blocks") or [])]
        anchored = [b for b in blocks if b.get("row_id")]
        record("projection", "按页取正文：块 + 行锚点（引用跳转靠 row_id）",
               len(blocks) > 0 and len(anchored) >= len(blocks) * 0.8,
               f"{len(blocks)} 块，{len(anchored)} 块有 row_id")
    except Exception as exc:  # noqa: BLE001
        record("projection", "按页取正文：块 + 行锚点（引用跳转靠 row_id）", False, f"{exc}")

    for label, url, key in (
        ("公式清单", f"{api}/papers/{paper}/formulas", "items"),
        ("插图清单（受控 url）", f"{api}/papers/{paper}/figures", "items"),
        ("书架检索", f"{api}/library", "items"),
        ("笔记 / 高亮 / 批注", f"{api}/notes", "items"),
    ):
        try:
            data = get_json(url, head)["data"]
            record("projection", label, isinstance(data.get(key), list),
                   f"{len(data.get(key) or [])} 条")
        except Exception as exc:  # noqa: BLE001
            record("projection", label, False, f"{exc}")

    # 内容**优先取页级产物（转录/翻译）**（用户口径 2026-10-03）：转录过的表格是规整的
    # Markdown 表格，没转录的页才退回解析块。数据相关：没页级产物就 SKIP。
    label = "按页取正文：优先转录（解析侧只兜底）"
    try:
        pages = get_json(f"{api}/papers/{paper}/pages?pages=1", head)["data"]
        units = [b for p in (pages.get("pages") or []) for b in (p.get("blocks") or [])]
        sources = {u.get("source") for u in units}
        if "transcript" not in sources:
            skip(label, "这篇论文还没有页级产物（未转录/未翻译）")
        else:
            row = next((u for u in units if u.get("row_id")), None)
            got = {}
            if row:
                got = get_json(f"{api}/papers/{paper}/rows/1/{row['row_id']}",
                               head)["data"]
            record("projection", label, sources == {"transcript"} and bool(got.get("text")),
                   f"第 1 页 {len(units)} 段全部来自转录；按行展开取回 "
                   f"{len(str(got.get('text') or ''))} 字")
            # 转录行的 row_id 必须与阅读器的行号**同源**（两边都来自 page_translations 的 rows）——
            # 引用点下去才跳得准；一旦各算一套，模型给的锚点就会落到别的段落。
            with urllib.request.urlopen(f"{base}/api/v1/papers/{paper}/reader",
                                        timeout=60) as resp:
                reader = json.loads(resp.read().decode("utf-8"))["data"]
            reader_rows = {int(p.get("page") or 0): [str(r.get("id"))
                                                     for r in (p.get("rows") or [])]
                           for p in (reader.get("page_translations") or [])}
            first = reader_rows.get(1) or []
            record("projection", "转录行 row_id 与阅读器行号同源（引用才跳得准）",
                   bool(row) and str(row.get("row_id")) in first,
                   f"阅读器第 1 页 {len(first)} 行，含 {row.get('row_id') if row else '-'}")
    except Exception as exc:  # noqa: BLE001
        record("projection", label, False, f"{exc}")

    # 表格必须能被 agent 拿到（2026-10-03）：模型抱怨过"知道这篇有表 1、却够不到表"。
    # 现在有两条路：① 转录过的页直接给 Markdown 表格；② 没转录的页走表图。
    # 数据相关：这篇没有识别到表格就 SKIP（换一篇有表的论文再跑）。
    label = "表格可取：转录给 Markdown 表 / 没转录走表图"
    try:
        hit = None
        for n in range(1, 26):
            data = get_json(f"{api}/papers/{paper}/figures?page={n}", head)["data"]
            hit = next((it for it in (data.get("items") or [])
                        if it.get("kind") == "table"), None)
            if hit:
                table_page = n
                break
        if hit is None:
            skip(label, "这篇论文没有识别到表格")
        else:
            with urllib.request.urlopen(f"{base}{hit['url']}", timeout=120) as resp:
                raw = resp.read()
            unit = None
            page_data = get_json(f"{api}/papers/{paper}/pages?pages={table_page}",
                                 head)["data"]
            unit = next((b for p in (page_data.get("pages") or [])
                         for b in (p.get("blocks") or []) if b.get("type") == "table"), None)
            markdown_table = bool(unit) and "|" in str(unit.get("text") or "")
            record("projection", label,
                   hit.get("index") is not None and raw.startswith(b"\x89PNG\r\n\x1a\n")
                   and len(raw) > 4096 and markdown_table,
                   f"p{table_page} 表{hit.get('number')}：图 {len(raw) // 1024} KB PNG、"
                   f"正文={'Markdown 表格' if markdown_table else '解析块（单元格可能粘连）'}")
    except Exception as exc:  # noqa: BLE001
        record("projection", label, False, f"{exc}")


# --------------------------------------------------------------------------
# ③ 产物纠错（免费：只走 HTTP；自检刻意 `reindex=false`，不花嵌入的钱）
# --------------------------------------------------------------------------


def _unprefix(lines: str) -> str:
    """把 `view` 的行号前缀（``   12| ``）剥掉，还原成产物原文。"""
    out = []
    for line in (lines or "").split("\n"):
        head, sep, rest = line.partition("| ")
        out.append(rest if (sep and head.strip().isdigit()) else line)
    return "\n".join(out)


def check_page_edit(base: str, paper: str) -> None:
    print("\n③ 产物纠错（写工具：拒绝路径 + 真改真撤；不重建索引）")
    api, head = f"{base}/api/v1/agent", internal()
    try:
        with urllib.request.urlopen(f"{base}/api/v1/papers/{paper}/reader",
                                    timeout=60) as resp:
            reader = json.loads(resp.read().decode("utf-8"))["data"]
    except Exception as exc:  # noqa: BLE001
        record("edit", "产物纠错：找到可改的一页", False, f"{exc}")
        return
    pages = [int(p.get("page") or 0) for p in (reader.get("page_translations") or [])
             if (p.get("rows") or [])]
    if not pages:
        skip("产物纠错", "这篇论文还没有页级产物（未转录 / 未翻译）")
        return

    side, view, page = "original", None, 0
    for cand in pages:
        data = get_json(f"{api}/papers/{paper}/pages/{cand}/product?side={side}",
                        head)["data"]
        if data.get("ok") is False:
            continue
        # 太长的一页会被截断（`MAX_VIEW`），"整页唯一"就验不准 —— 换一页
        if 200 <= int(data.get("chars") or 0) <= 20000:
            view, page = data, cand
            break
    if view is None:
        skip("产物纠错", "没有长度合适的产物页（都为空或过长）")
        return

    raw = _unprefix(view.get("lines") or "")
    record("edit", "产物视图：带行号的原文（op=view）",
           bool(view.get("lines", "").startswith("   1| "))
           and int(view.get("chars") or 0) == len(raw) and len(raw) > 100,
           f"p{page} {view.get('chars')} 字 / {view.get('rows')} 行")

    def post(payload: dict) -> dict:
        return post_json(f"{api}/papers/{paper}/pages/{page}/product",
                         {"side": side, "reindex": False, **payload}, head)["data"]

    def fresh() -> str:
        """每一步都重新读一遍（写被拒时状态与预期不同，不能拿上一次的文本当真）。"""
        data = get_json(f"{api}/papers/{paper}/pages/{page}/product?side={side}",
                        head)["data"]
        return _unprefix(data.get("lines") or "")

    # ---- 拒绝①：find 在整页里根本不存在 → 必须回"最像的几行"
    probe = next((ln.strip()[:8] for ln in raw.split("\n") if len(ln.strip()) >= 12), "")
    miss = post({"find": probe + "☃", "replace": "x"})
    record("edit", "拒绝：find 不存在（并给出最像的几行）",
           miss.get("ok") is False and bool(miss.get("near_miss")),
           f"near_miss={len(miss.get('near_miss') or [])} 条，"
           f"最像：{(miss.get('near_miss') or [{}])[0].get('text', '')[:32]}")

    # ---- 拒绝②：命中多处 → 必须拒绝并报次数（要求带上前后文）
    dup = next((s for ln in raw.split("\n") if len(ln.strip()) >= 20
                for s in (ln.strip()[:6], ln.strip()[3:9], ln.strip()[5:11])
                if raw.count(s) > 1), None)
    if dup is None:
        skip("拒绝：find 命中多处", "这一页没有合适的重复片段")
    else:
        multi = post({"find": dup, "replace": "x"})
        record("edit", "拒绝：find 命中多处（要求带上前后文）",
               multi.get("ok") is False and int(multi.get("occurrences") or 0) == raw.count(dup),
               f"{dup!r} 命中 {multi.get('occurrences')} 次")

    # ---- 真改一处 + 真撤回：唯一命中、不在行尾（免得被 rstrip 吃掉）
    needle = next((ln.strip()[2:9] for ln in raw.split("\n")
                   if len(ln.strip()) >= 12 and raw.count(ln.strip()[2:9]) == 1), None)
    if needle is None:
        skip("真改一处 + 撤回", "这一页找不到唯一且合适的片段")
        return
    edit = post({"find": needle, "replace": needle + " ", "reason": "自检：往返验证",
                 "actor": "selfcheck"})
    log_file = ROOT / "data" / "papers" / paper / "md" / "edit_log.jsonl"
    journal = log_file.is_file() and f'"page": {page}' in log_file.read_text(
        encoding="utf-8", errors="ignore")
    want = raw.replace(needle, needle + " ", 1)
    record("edit", "真改一处（唯一命中 + 改动日志留痕）",
           edit.get("ok") is True and int(edit.get("line") or 0) >= 1
           and fresh() == want and journal,
           f"p{page} 第 {edit.get('line')} 行，{len(raw)} → {len(want)} 字，"
           f"日志={'有' if journal else '没有'}")

    back = post_json(f"{api}/papers/{paper}/pages/{page}/product/undo",
                     {"side": side, "reindex": False}, head)["data"]
    record("edit", "撤回：逐字回到改动前",
           back.get("ok") is True and fresh() == raw,
           f"撤回 {back.get('reverted', {}).get('find')!r}，正文与改动前逐字一致")

    # ---- 拒绝③：跨块编辑（把两段并成一段）→ 必须回滚，磁盘逐字节不变
    #
    # ⚠️ 候选必须**真的会改变块数**：先用 `split_blocks`（守卫用的同一个函数）现场验一遍
    # 再发请求。以前只挑"第一个空行"，而 split_blocks 认分界的规则比"有 \n\n"更细
    # （换行、公式围栏、续行都算）—— 挑到它不认的那一处，守卫**本来就不该**触发，
    # 自检却报"失败"（实测在 attention p1 / mmdfnd p1 上都撞到过）。
    # 本脚本平时只走 HTTP（不 import 后端）；这一处必须用**守卫自己的那个函数**
    # 来判断"这个空行算不算分界"，所以临时把仓库根加进 sys.path 再 import。
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from backend.services.translation.page_translate import split_blocks

    span = None
    base_blocks = len(split_blocks(raw))
    for idx in range(3, len(raw) - 9):
        if raw[idx:idx + 2] == "\n\n":
            cand = raw[idx - 3:idx + 5]
            if (raw.count(cand) == 1 and cand.strip()
                    and len(split_blocks(raw.replace(cand, " ", 1))) != base_blocks):
                span = cand
                break
    if span is None:
        skip("拒绝：跨块编辑会回滚", "这一页没有能真正改变块数的分界")
    else:
        rolled = post({"find": span, "replace": " "})
        if rolled.get("ok") is True:        # 守卫失效：先自己收拾干净，再报失败
            post_json(f"{api}/papers/{paper}/pages/{page}/product/undo",
                      {"side": side, "reindex": False}, head)
        record("edit", "拒绝：跨块编辑（段落块 / 行变了）→ 已回滚",
               rolled.get("ok") is False and "结构" in str(rolled.get("error") or "")
               and fresh() == raw,
               f"段落块 {rolled.get('blocks_before')} → {rolled.get('blocks_after')}、"
               f"行 {rolled.get('rows_before')} → {rolled.get('rows_after')}，"
               f"回滚后正文逐字一致")

    # 自检不留痕：日志被撤回清空后就不该留着这个文件
    if log_file.is_file() and not log_file.read_text(encoding="utf-8").strip():
        log_file.unlink()


# --------------------------------------------------------------------------
# ④ 图片附件（免费：只走 HTTP，不打模型）
# --------------------------------------------------------------------------


def check_attachments(base: str) -> list[str]:
    print("\n③ 图片附件（上传 / 取回 / 守卫）")
    api = f"{base}/api/v1/agent"
    created: list[str] = []
    try:
        up = post_files(f"{api}/attachments", [
            ("files", "check-red-blue.png", PNG, "image/png"),
            ("files", "check-copy.png", PNG, "image/png"),
        ])["data"]
        items = up.get("items") or []
        created = [i["attachment_id"] for i in items]
        record("attach", "图片可上传（一次多张）+ 回可访问地址",
               len(items) == 2 and bool(items[0].get("url")) and items[0].get("mime") == "image/png",
               f"count={up.get('count')}")
        with urllib.request.urlopen(f"{base}{items[0]['url']}", timeout=20) as resp:
            raw = resp.read()
        record("attach", "取回的就是原图（PNG 魔数 + 字节数一致）",
               raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) == int(items[0]["bytes"]),
               f"{len(raw)} 字节")
    except Exception as exc:  # noqa: BLE001
        record("attach", "图片可上传（一次多张）+ 回可访问地址", False, f"{exc}")

    for label, payload, want in (
        ("坏图（魔数像 PNG、内容断了）在上传口被挡", [("files", "broken.png", BROKEN_PNG, "image/png")], "图"),
        ("非图片被拒（只收 PNG/JPEG/WebP/GIF）", [("files", "note.txt", b"hello", "text/plain")], "格式"),
    ):
        try:
            post_files(f"{api}/attachments", payload)
            record("attach", label, False, "竟然过了")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            record("attach", label, exc.code == 400 and want in detail, f"HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            record("attach", label, False, f"{exc}")
    record("attach", "不存在的附件 / 穿越 id → 404",
           status_of(f"{api}/attachments/{'0' * 32}") == 404
           and status_of(f"{api}/attachments/..%2F..%2Fsecrets%2Fagent.token") == 404)
    return created


def cleanup_attachments(ids: list[str]) -> int:
    """删掉这次上传的测试图片（脚本与后端同机，直接按 id 找文件）。"""
    removed = 0
    for aid in ids:
        for path in (ROOT / "data" / "attachments").glob(f"*/{aid}.*"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# --------------------------------------------------------------------------
# ⑤ 会话 CRUD（免费：不新建任何会话）
# --------------------------------------------------------------------------


def check_metadata(base: str, paper: str, agent_base: str) -> None:
    """⑤ 论文元数据（2026-10-07）：新字段 / 参考文献 / 两个写口。

    全程**免费**：只读接口 + 离线抽取（不调模型）+ "只填空字段"那几个守卫。
    ⚠️ 「模型补全」按钮那一头（`POST /papers/{id}/metadata/agent` → agent 读全文）**故意不在这里跑**：
    它会真的排一个任务、真的调模型。这里只验"agent 服务那条路通不通"的免费部分。
    """
    print("\n⑤ 论文元数据（中英文摘要/关键词、参考文献、字段来源）")
    head = internal()
    api = f"{base}/api/v1/agent"

    detail = get_json(f"{base}/api/v1/papers/{paper}", head)["data"]
    want = ("abstract_zh", "keywords_en", "domain", "one_liner", "contributions",
            "prereq", "difficulty", "read_time_min", "paper_type", "doi", "code_url",
            "meta_sources", "references", "reference_count")
    lack = [k for k in want if k not in detail]
    record("meta", "详情带上了全部元数据字段", not lack,
           f"缺 {lack}" if lack else f"参考文献 {detail.get('reference_count')} 条")

    refs = detail.get("references") or []
    record("meta", "参考文献条数与列表一致（详情接口才算它）",
           int(detail.get("reference_count") or 0) == len(refs),
           f"count={detail.get('reference_count')} / len={len(refs)}")

    # 离线补全：不花钱、幂等（只填空字段）。跑完把论文原名与参考文献条数对回来。
    off = post_json(f"{base}/api/v1/papers/{paper}/metadata/auto",
                    {"model": False}, head)["data"]
    after = get_json(f"{base}/api/v1/papers/{paper}", head)["data"]
    record("meta", "离线补全：不调模型也能跑（幂等、只填空字段）",
           isinstance(off.get("applied"), dict)
           and after.get("title") == detail.get("title")
           and len(after.get("references") or []) == len(refs),
           f"applied={len(off.get('applied') or {})} 项，参考文献仍 {len(refs)} 条")

    # 写口①：模型给的脏值必须被丢掉（不然"太短的摘要"会写进库里）
    bad = post_json(f"{api}/papers/{paper}/metadata", {"abstract_zh": "太短"}, head)["data"]
    record("meta", "agent 写口：脏值被拒（ok=false + 原因）",
           bad.get("ok") is False and bool(bad.get("error")),
           f"error={str(bad.get('error'))[:40]}")

    # 写口②：已有值的字段一律 **不覆盖**（这是"按钮不会毁掉手改值"的落点）
    filled = next((k for k in ("domain", "abstract_zh", "venue", "one_liner")
                   if detail.get(k)), None)
    if not filled:
        skip("agent 写口：已有值不覆盖", "这篇论文这些字段都还空着（跳过）")
    else:
        same = post_json(f"{api}/papers/{paper}/metadata",
                         {filled: detail[filled]}, head)["data"]
        again = get_json(f"{base}/api/v1/papers/{paper}", head)["data"]
        record("meta", "agent 写口：已有值不覆盖（applied 为空）",
               same.get("ok") is True and not (same.get("applied") or {})
               and filled in (same.get("skipped") or {})
               and again.get(filled) == detail.get(filled),
               f"{filled} 已在库 → applied=0、skipped=1，值未变")

    # agent 服务那条路：给一个不存在的论文，必须**正常返回 ok=false**（而不是 5xx）——
    # 调用方（fill_all）靠这个回落，5xx 会让「模型补全」整个失败。
    if status_of(f"{agent_base.rstrip('/')}/health") != 200:
        skip("agent 元数据入口：论文不存在 → ok=false", "agent 服务没在跑")
    else:
        try:
            gone = post_json(f"{agent_base.rstrip('/')}/v1/meta-fill",
                             {"paper_id": "no-such-paper-for-selfcheck"}, head)
            ok = gone.get("ok") is False and bool(gone.get("error"))
        except urllib.error.HTTPError as exc:
            ok, gone = False, {"error": f"HTTP {exc.code}"}
        record("meta", "agent 元数据入口：论文不存在 → ok=false（不 5xx）",
               ok, f"error={str(gone.get('error'))[:60]}")


def check_annotations(base: str, paper: str) -> None:
    """⑦ 高亮 / 笔记（《修改计划》第六步）：建 → 查 → 定位 → 改 → 加笔记 → 删（连带笔记）。

    全程免费（只走 HTTP，不打模型）。**自己造的自己删**：结束前把这次建的高亮删掉 ——
    后端删高亮时会连带删它挂的笔记，所以一次调用就把痕迹清干净。
    """
    print("\n⑦ 高亮 / 笔记（agent 手脚：按文字建高亮 + 笔记必须挂高亮）")
    api = f"{base}/api/v1/agent"
    head = internal()

    # 找一行真文字来做锚点：页投影里每条都带 `row_id` 与文本（与阅读器正文行 id 同一套）。
    # ⚠️ 挑**不带 Markdown 记号**的行来当探针：要标的文字必须与"渲染后的文字"逐字一致，
    #    而 `# 标题` 里的 `#` 在渲染后是不存在的（模型照抄原文时也会踩这个坑，工具会回最像的原文）。
    pages = get_json(f"{api}/papers/{paper}/pages?pages=1&lang=both", head)["data"]

    def _plain(text: str) -> bool:
        return not re.search(r"[*_`#|>\[\]]", text)

    row = next((b for p in (pages.get("pages") or [])
                for b in (p.get("blocks") or [])
                if b.get("row_id")
                and len(str(b.get("text") or b.get("translation") or "").strip()) > 40
                and _plain(str(b.get("text") or b.get("translation") or ""))), None)
    if row is None:
        skip("高亮：按文字建一条", "这一页没有够长的纯文本行可以当锚点")
        return
    anchor = str(row.get("row_id"))
    full = str(row.get("text") or row.get("translation") or "").strip()
    probe = " ".join(full.split())[:24].strip()          # 取一小段当"要标的文字"

    made = post_json(f"{api}/papers/{paper}/highlights",
                     {"anchor": anchor, "text": probe, "color": "green"}, head)["data"]
    hl = made.get("highlight") or {}
    record("annotations", "按文字建高亮：偏移量由宿主算（模型不编数字）",
           made.get("ok") is True and hl.get("id") and hl.get("scope") == "row"
           and probe in str(hl.get("text") or ""),
           f"{anchor} → {str(hl.get('text'))[:20]!r} [{hl.get('start')}:{hl.get('end')}] p{hl.get('page')}")
    if not hl.get("id"):
        return

    bad = post_json(f"{api}/papers/{paper}/highlights",
                    {"anchor": anchor, "text": probe + "☃"}, head)["data"]
    record("annotations", "找不到那段文字时**回最像的原文**（不是硬猜）",
           bad.get("ok") is False and bool(bad.get("near_miss")),
           f"near_miss={len(bad.get('near_miss') or [])} 条")

    listed = get_json(f"{api}/papers/{paper}/highlights", head)["data"].get("items") or []
    loc = get_json(f"{api}/papers/{paper}/highlights?highlight_id={hl['id']}", head)["data"]
    record("annotations", "列表与定位：带页码 + 挂在哪一面 + 原文",
           any(h.get("id") == hl["id"] for h in listed)
           and loc.get("kind") == "highlight" and loc.get("page") == hl.get("page")
           and "正文行" in str(loc.get("hint") or ""),
           f"{len(listed)} 条；定位：p{loc.get('page')} · {str(loc.get('hint'))[:24]}")

    upd = put_json(f"{api}/highlights/{hl['id']}", {"color": "pink"}, head)["data"]
    record("annotations", "改高亮（颜色）→ 库里真的变了",
           upd.get("color") == "pink", f"color={upd.get('color')}")

    note = post_json(f"{api}/papers/{paper}/notes",
                     {"highlight_id": hl["id"], "content": "自检写的笔记"}, head)["data"]
    n = note.get("note") or {}
    note_loc = get_json(f"{api}/papers/{paper}/notes?note_id={n.get('id')}", head)["data"]
    record("annotations", "笔记必须挂在高亮上（锚点/原文从高亮继承）",
           note.get("ok") is True and n.get("highlight_id") == hl["id"]
           and n.get("block_id") == anchor,
           f"{n.get('id')} → 高亮 {hl['id']}，引用 {str(n.get('selected_text'))[:16]!r}")
    record("annotations", "从笔记反查它对应的高亮（定位到原文）",
           (note_loc.get("highlight") or {}).get("id") == hl["id"]
           and note_loc.get("kind") == "note" and note_loc.get("page") == hl.get("page"),
           f"kind={note_loc.get('kind')} p{note_loc.get('page')} "
           f"· {str(note_loc.get('hint'))[:26]}")

    try:
        post_json(f"{api}/papers/{paper}/notes", {"content": "没有高亮的笔记"}, head)
        orphan_ok, orphan_detail = False, "居然成功了（不该）"
    except urllib.error.HTTPError as exc:
        orphan_ok, orphan_detail = exc.code == 400, f"HTTP {exc.code}"
    record("annotations", "拒绝孤儿笔记（不给 highlight_id 就报错）", orphan_ok, orphan_detail)

    gone = delete_json(f"{api}/highlights/{hl['id']}", head)["data"]
    after = get_json(f"{api}/papers/{paper}/highlights", head)["data"].get("items") or []
    left_notes = get_json(f"{api}/papers/{paper}/notes", head)["data"].get("items") or []
    record("annotations", "删高亮连带删它挂的笔记，且不留痕",
           hl["id"] in (gone.get("deleted_ids") or [])
           and (gone.get("deleted_notes") or []) == [n.get("id")]
           and all(h.get("id") != hl["id"] for h in after)
           and all(x.get("id") != n.get("id") for x in left_notes),
           f"删高亮 {len(gone.get('deleted_ids') or [])} 条 + 笔记 "
           f"{len(gone.get('deleted_notes') or [])} 条；列表剩 {len(after)} / {len(left_notes)}")


def check_conversations(base: str) -> None:
    print("\n④ 会话接口（列表 / 分类 / 搜索 / 批量删除；不建新会话）")
    api = f"{base}/api/v1/agent"
    try:
        items = get_json(f"{api}/conversations?limit=5")["data"]["items"]
        record("conv", "列表可用（只回当前论文之外的也带 paper_id）",
               isinstance(items, list) and all("conversation_id" in c for c in items),
               f"{len(items)} 条（最多 5）")
    except Exception as exc:  # noqa: BLE001
        record("conv", "列表可用（只回当前论文之外的也带 paper_id）", False, f"{exc}")

    try:
        all_items = get_json(f"{api}/conversations?limit=200")["data"]["items"]
        sel = get_json(f"{api}/conversations?kind=selection&limit=200")["data"]["items"]
        nor = get_json(f"{api}/conversations?kind=normal&limit=200")["data"]["items"]
        record("conv", "分类 = 「有没有划词消息」的划分（两类之和 == 全部）",
               all(c.get("has_selection") for c in sel) and not any(c.get("has_selection") for c in nor)
               and len(sel) + len(nor) == len(all_items),
               f"全部 {len(all_items)} = 划词 {len(sel)} + 常规 {len(nor)}")
    except Exception as exc:  # noqa: BLE001
        record("conv", "分类 = 「有没有划词消息」的划分（两类之和 == 全部）", False, f"{exc}")

    try:
        hit = get_json(f"{api}/conversations?q={urllib.parse.quote('zzz-绝不可能出现的词-zzz')}"
                       )["data"]["items"]
        record("conv", "搜索（标题 + 正文）能命中且无副作用", hit == [], f"搜不到 → {len(hit)} 条")
    except Exception as exc:  # noqa: BLE001
        record("conv", "搜索（标题 + 正文）能命中且无副作用", False, f"{exc}")

    # 时间戳必须带 UTC 标记（用户 2026-10-03 报"对话显示时间不对"）：库里的 datetime 是
    # UTC naive，前端 `new Date("...T16:24:16")` 会按**本地时区**解析 → 东八区显示成 8 小时前。
    label = "会话时间带 UTC 标记（否则前端差一个时区）"
    try:
        rows = get_json(f"{api}/conversations?limit=5")["data"]["items"]
        stamps = [str(c.get("updated_at") or c.get("created_at") or "") for c in rows]
        if not stamps:
            skip(label, "没有任何会话可比对")
        else:
            record("conv", label, all(s.endswith("Z") for s in stamps), f"例：{stamps[0]}")
    except Exception as exc:  # noqa: BLE001
        record("conv", label, False, f"{exc}")

    try:
        record("conv", "取不存在的会话 → 404",
               status_of(f"{api}/conversations/{uuid.uuid4()}") == 404)
        empty = post_json(f"{api}/conversations/batch-delete", {"conversation_ids": []})["data"]
        ghost = post_json(f"{api}/conversations/batch-delete",
                          {"conversation_ids": [str(uuid.uuid4())]})["data"]
        record("conv", "批量删除：空数组 / 不存在的 id 都不报错、也不误删",
               empty.get("deleted") == [] and ghost.get("deleted") == [])
    except Exception as exc:  # noqa: BLE001
        record("conv", "批量删除：空数组 / 不存在的 id 都不报错、也不误删", False, f"{exc}")

    check_run_trace(base)


#: 造一条"思考 / 正文 / 工具夹杂"的轨迹用的假数据（形状与 `agent/trace.py` 一致）
RUN_STEPS = [
    {"kind": "stage", "round": 0, "text": "正在阅读并组织回答…"},
    {"kind": "thinking", "round": 1, "text": "用户问的是核心贡献。"},
    {"kind": "tool", "round": 1, "tool": "paper_search", "digest": "query=multi-head",
     "status": "done", "ms": 312},
    {"kind": "thinking", "round": 2, "text": "再看一眼书架。"},
    {"kind": "tool", "round": 2, "tool": "library_list", "digest": "query=attention",
     "status": "done", "ms": 180},
    {"kind": "text", "round": 3, "text": "第一段回答[1]。"},
    {"kind": "tool", "round": 3, "tool": "page_markdown_get", "digest": "pages=3",
     "status": "done", "ms": 1200},
    {"kind": "text", "round": 4, "text": "第二段回答。"},
]


def check_run_trace(base: str) -> None:
    """过程轨迹（多轮思考 + 工具调用）**落库往返**：造一条临时会话，验完删掉。

    为什么值一条检查：轨迹是"用户看得见的过程"，它一旦在落库/读取这条路上串了序，
    界面上就是"思考跑到工具后面"这种说不清的错——而这种错**只有读回来才知道**。
    """
    print("\n④b 过程轨迹落库往返（造一条临时会话，验完删掉）")
    label = "过程轨迹：顺序 / 形状 / 耗时都能原样读回"
    api, head = f"{base}/api/v1/agent", internal()
    cid = str(uuid.uuid4())          # ⚠️ 只能是 uuid：`conversation.id` 是 VARCHAR(36)
    try:
        post_json(f"{api}/internal/messages", {
            "conversation_id": cid, "role": "user", "paper_id": "selfcheck",
            "title": "自检：过程轨迹（会被删掉）", "content": "自检用，不需要回答。",
        }, head)
        post_json(f"{api}/internal/messages", {
            "conversation_id": cid, "role": "assistant", "content": "第一段回答[1]。第二段回答。",
            "citations": [{"ref": 1, "kind": "chunk", "page": 3, "label": "第 3 页",
                           "snippet": "……", "ids": []}],
            "run": {"steps": RUN_STEPS, "elapsed_ms": 1179000, "rounds": 4},
            "thinking": "用户问的是核心贡献。再看一眼书架。",
        }, head)
        conv = get_json(f"{api}/conversations/{cid}")["data"]
        run = next((m.get("run") or {} for m in conv["messages"]
                    if m.get("role") == "assistant"), {})
        got = [s.get("kind") for s in (run.get("steps") or [])]
        want = [s["kind"] for s in RUN_STEPS]
        tools = [s.get("tool") for s in (run.get("steps") or []) if s.get("kind") == "tool"]
        record("conv", label,
               got == want and run.get("elapsed_ms") == 1179000 and run.get("rounds") == 4
               and tools == ["paper_search", "library_list", "page_markdown_get"]
               and all(s.get("ms") for s in (run.get("steps") or []) if s.get("kind") == "tool"),
               f"{len(got)} 步，顺序 {','.join(got[:4])}…，用时 {run.get('elapsed_ms')}ms")
    except Exception as exc:  # noqa: BLE001
        record("conv", label, False, f"{exc}")
    finally:
        try:
            post_json(f"{api}/conversations/batch-delete", {"conversation_ids": [cid]})
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# ⑥ 真问答（只在 --model 下跑；跑完自己清理）
# --------------------------------------------------------------------------


def _plain(text: str) -> str:
    """抹掉 `done` 阶段的两处**已知改写**后比较（否则这条断言必然是假失败）：

    - 引用编号：流式期间是 registry 的原始号，`done` 里被压成 `1..M`；
    - 正文图片：`done` 之前会把非受控图片删掉。
    剩下还要求逐字一致 —— 它抓的是"流式看到的"和"`done` 给的"不是同一份（比如思维链混进正文）。
    """
    import re

    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")
    text = re.sub(r"\[\d{1,3}\]", "", text)
    return re.sub(r"\s+", "", text)


def check_model_chain(base: str, paper: str, *, deep: bool, image_id: str | None) -> list[str]:
    """返回本次造出来的 conversation_id（调用方负责删掉）。"""
    print("\n⑤ 真问答链路（会调用模型；跑完自删会话）")
    made: list[str] = []
    try:
        run = ask(base, paper, "这篇论文的核心贡献是什么？请给出依据。",
                  extra={"attachments": [image_id]} if image_id else None)
    except Exception as exc:  # noqa: BLE001
        record("model", "一轮问答可跑", False, f"{type(exc).__name__}: {exc}")
        return made
    done = run["done"] or {}
    kinds = [e["type"] for e in run["events"]]
    sources = [e["data"].get("content_source") for e in run["events"] if e["type"] == "session"]
    answer = done.get("answer") or ""
    record("model", "SSE 有完整骨架（stage → session → … → 单终态）",
           "stage" in kinds and "session" in kinds
           and len([k for k in kinds if k in ("done", "error", "cancelled")]) == 1,
           f"事件 {sorted(set(kinds))}，{run['elapsed']}s")
    record("model", "拿到带引用的回答", len(answer) > 60 and bool(done.get("citations")),
           f"{len(answer)} 字 / {len(done.get('citations') or [])} 条引用")
    record("model", "流式增量拼接 == done.answer（抹掉编号/图片两处已知改写后逐字一致）",
           _plain(run["deltas"]) == _plain(answer),
           f"{len(run['deltas'])} → {len(_plain(run['deltas']))} vs {len(answer)} → {len(_plain(answer))} 字")
    cits = done.get("citations") or []
    record("model", "引用带跳转锚点（row_id / block_id）",
           all((c.get("row_id") or c.get("block_id")) for c in cits) if cits else False,
           f"{(cits[0].get('row_id') if cits else '')!r}")
    record("model", "正文清净：不外泄内部 id、不嵌外链图、没有 `[ref=` 口癖",
           "[ref=" not in answer and "chunk_id" not in answer
           and not any(x in answer for x in ("http://", "https://", "data:image", "blob:")),
           "")
    if image_id:
        record("model", "图片走多模态（content_source=image）", sources == ["image"],
               f"content_source={sources}")
    conv = done.get("conversation_id")
    if not conv:
        record("model", "落库并回 conversation_id", False, "done 里没有 conversation_id")
        return made
    made.append(conv)
    record("model", "落库先于 done（persisted + 会话里能读到这条回答）",
           bool(done.get("persisted")),
           f"persisted={done.get('persisted')}")
    try:
        msgs = get_json(f"{base}/api/v1/agent/conversations/{conv}")["data"]["messages"]
        last = next((m for m in reversed(msgs) if m["role"] == "assistant"), None)
        record("model", "助手消息落库（正文 + 引用 + 工具轨迹）",
               bool(last) and (last.get("content") or "") == answer
               and bool(last.get("citations")) and bool(last.get("tool_trace")),
               f"{len(last.get('tool_trace') or []) if last else 0} 条轨迹")
        thin = next((e["data"] for e in run["events"] if e["type"] == "session"), {})
        if (thin.get("content_source") != "selection"):
            record("model", "思考通道：思维链不进正文（单独一条通道）",
                   ("thinking_delta" in kinds) == bool(last and last.get("thinking"))
                   and (bool(last and last.get("thinking_ms")) or "thinking_delta" not in kinds),
                   f"thinking={len((last or {}).get('thinking') or '')} 字 / "
                   f"{kinds.count('thinking_delta')} 个增量")
    except Exception as exc:  # noqa: BLE001
        record("model", "助手消息落库（正文 + 引用 + 工具轨迹）", False, f"{exc}")

    if not deep:
        return made

    # 深度档：压缩 / 就地重答 / 就地编辑 / 开分支（每次多一轮模型调用）
    try:
        comp = post_json(f"{base}/api/v1/agent/conversations/{conv}/compress", {},
                         timeout=600.0)["data"]
        summary = comp.get("summary") or ""
        record("model", "压缩：产出要点、不留 `[n]` 编号",
               len(summary) > 60 and not __import__("re").search(r"\[\d{1,3}\]", summary),
               f"{len(summary)} 字")
        after = get_json(f"{base}/api/v1/agent/conversations/{conv}")["data"]
        record("model", "压缩作为一轮留痕（summary）+ 之后上下文只剩摘要",
               (after.get("messages") or [])[-1]["role"] == "summary"
               and (after.get("usage") or {}).get("active") == 1,
               f"active={(after.get('usage') or {}).get('active')}")
    except Exception as exc:  # noqa: BLE001
        record("model", "压缩：产出要点、不留 `[n]` 编号", False, f"{exc}")

    try:
        msgs = get_json(f"{base}/api/v1/agent/conversations/{conv}")["data"]["messages"]
        ans = next(m for m in reversed(msgs) if m["role"] == "assistant")
        ask(base, paper, "", extra={"conversation_id": conv,
                                    "regenerate_from": ans["message_id"]})
        after = get_json(f"{base}/api/v1/agent/conversations/{conv}")["data"]
        answers = [m for m in (after.get("messages") or []) if m["role"] == "assistant"]
        record("model", "就地重答：只替换这条回答（不留兄弟）",
               len(answers) == 1 and answers[0]["message_id"] != ans["message_id"]
               and len(answers[0].get("siblings") or []) == 1,
               f"可见 {len(after.get('messages') or [])} 条")
        ask(base, paper, "", extra={"conversation_id": conv,
                                    "regenerate_from": answers[0]["message_id"], "branch": True})
        after = get_json(f"{base}/api/v1/agent/conversations/{conv}")["data"]
        answers = [m for m in (after.get("messages") or []) if m["role"] == "assistant"]
        record("model", "开分支：同父下挂兄弟（原回答保留）",
               len(answers) == 1 and len(answers[0].get("siblings") or []) == 2,
               f"兄弟 {len((answers[0].get('siblings') if answers else []) or [])} 个")
        users = [m for m in (after.get("messages") or []) if m["role"] == "user"]
        ask(base, paper, "换个问法（就地改）：核心思路是什么？",
            extra={"conversation_id": conv, "edit_of": users[0]["message_id"]})
        after = get_json(f"{base}/api/v1/agent/conversations/{conv}")["data"]
        users2 = [m for m in (after.get("messages") or []) if m["role"] == "user"]
        record("model", "就地编辑：改写这条提问本身（id 不变、后面重来）",
               len(users2) == 1 and users2[0]["message_id"] == users[0]["message_id"]
               and "换个问法" in (users2[0].get("content") or ""),
               f"可见 {len(after.get('messages') or [])} 条")
    except Exception as exc:  # noqa: BLE001
        record("model", "就地重答：只替换这条回答（不留兄弟）", False, f"{exc}")
    return made


def cleanup_conversations(base: str, ids: list[str]) -> int:
    if not ids:
        return 0
    try:
        res = post_json(f"{base}/api/v1/agent/conversations/batch-delete",
                        {"conversation_ids": ids})["data"]
        return len(res.get("deleted") or [])
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------
def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Agent 问答链路自检（默认不调用模型）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--agent-base", default=DEFAULT_AGENT_BASE,
                    help="agent 服务地址（元数据入口那几条免费检查用）")
    ap.add_argument("--paper", default=DEFAULT_PAPER)
    ap.add_argument("--model", action="store_true",
                    help="跑真问答链路（**会调用模型**；跑完自动删掉造出来的会话）")
    ap.add_argument("--deep", action="store_true",
                    help="在 --model 基础上再跑压缩 / 就地改 / 开分支（更多次模型调用）")
    ap.add_argument("--image", action="store_true",
                    help="在 --model 基础上把测试图片一起发过去（验证多模态）")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 78)
    print("Cylit Agent 自检" + ("（含真问答：--model）" if args.model else "（默认零模型调用）"))
    print("=" * 78)
    if not internal():
        print("找不到 data/secrets/agent.token —— 先启动一次主后端（它会生成令牌）")
        return 2

    running = check_service(base)
    if not running:
        print("\n问答服务没在跑：跳过需要它的检查（`pr.py agent` 启动后再跑）")
    check_projections(base, args.paper)
    check_page_edit(base, args.paper)
    check_metadata(base, args.paper, args.agent_base)
    check_annotations(base, args.paper)
    created_atts = check_attachments(base)
    check_conversations(base)

    made: list[str] = []
    if args.model:
        if not running:
            skip("真问答链路", "问答服务没在跑")
        else:
            made = check_model_chain(base, args.paper, deep=args.deep,
                                     image_id=created_atts[0] if (args.image and created_atts) else None)
    else:
        skip("真问答链路", "默认不跑（加 --model 才调模型）")

    # ---- 收尾：清掉这次留下的东西，历史记录里不该多出任何会话 ----
    gone = cleanup_conversations(base, made)
    files = cleanup_attachments(created_atts)
    if made or created_atts:
        print(f"\n已清理：删除会话 {gone}/{len(made)} 个、测试图片 {files} 个文件")
        left = [c for c in (get_json(f"{base}/api/v1/agent/conversations?limit=200")["data"]["items"]
                            if not made else []) if c["conversation_id"] in made]
        record("cleanup", "跑完不留痕（造出来的会话都没了）", not left, f"残留 {len(left)} 个")

    print("\n" + "=" * 78)
    total, failed = len(results), sum(1 for r in results if not r["ok"])
    print(f"共 {total} 项：通过 {total - failed}，失败 {failed}")
    report = ROOT / "data" / "logs" / "agent_check.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"base": base, "paper": args.paper, "model": args.model,
                                  "passed": total - failed, "total": total,
                                  "results": results}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"报告：{report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
