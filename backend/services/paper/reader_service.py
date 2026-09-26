"""阅读器数据装配（规划 §20.2、§22 /reader/:paperId）。

关键设计（规划 §8）：
- **正文只在 Markdown**：MySQL 的 ``block`` 只存定位与类型；
- reader.json 动态装配 = 逐页译文行 + 解析块 + 图注锚定的插图 + 批注/笔记 + 任务状态；
- 支持 ``since`` 增量（逐页流式上线时前端只取新块）。

文本从 Markdown 解析一次并缓存（按 mtime 失效），避免每次请求读大文件。
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from backend.core.errors import NotFoundError
from backend.core.logging import get_logger
from backend.core.utils import json_loads
from backend.parsing.formula.noise import is_noise_formula
from backend.repositories.mysql.models import (
    Annotation, Block, Formula, Highlight, Note, Paper, ReadingProgress, Task,
)
from backend.repositories.mysql.session import session_scope
from backend.services.paper import anchor_map, paper_service
from backend.services.translation.page_translate import (
    PATH_VERSION, PageStore, apply_semantic_breaks, mark_rows_continuity,
    page_continuity, pair_rows,
)

log = get_logger(__name__)

# Markdown 解析缓存：{paper_id: (mtime, parsed)}
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------
# Markdown 解析
# --------------------------------------------------------------------------


def parse_markdown(text: str) -> dict:
    """兼容包装：实现位于 ``backend.parsing.md.generator``（单一实现）。"""
    from backend.parsing.md.generator import parse_markdown as _parse

    return _parse(text)


def load_markdown_map(paper_id: str, force: bool = False) -> dict:
    paths = paper_service.paper_paths(paper_id)
    md: Path = paths["markdown"]
    if not md.is_file():
        return {}
    mtime = md.stat().st_mtime
    with _cache_lock:
        cached = _cache.get(paper_id)
        if cached and not force and abs(cached[0] - mtime) < 1e-6:
            return cached[1]
    parsed = parse_markdown(md.read_text(encoding="utf-8"))
    with _cache_lock:
        _cache[paper_id] = (mtime, parsed)
    return parsed


def load_block_index(paper_id: str, force: bool = False) -> dict:
    """读取 ``md/blocks.json``（阅读器索引，由解析阶段派生）。

    比每次解析整篇 Markdown 快得多；缺失时回落到 Markdown 解析。
    """
    paths = paper_service.paper_paths(paper_id)
    index_path: Path = paths["blocks_index"]
    if index_path.is_file():
        mtime = index_path.stat().st_mtime
        key = f"{paper_id}::index"
        with _cache_lock:
            cached = _cache.get(key)
            if cached and not force and abs(cached[0] - mtime) < 1e-6:
                return cached[1]
        from backend.core.utils import read_json

        data = read_json(index_path, {}) or {}
        with _cache_lock:
            _cache[key] = (mtime, data)
        return data
    from backend.parsing.md.generator import parse_markdown

    md_map = load_markdown_map(paper_id)
    return {"blocks": [{"id": k, "text": v.get("text", ""),
                        "section": None, "font_size": None}
                       for k, v in md_map.items()]}


def invalidate_cache(paper_id: str) -> None:
    with _cache_lock:
        _cache.pop(paper_id, None)
        _cache.pop(f"{paper_id}::index", None)


def load_page_store(paper_id: str, force: bool = False) -> PageStore:
    """旧格式的**合并**页产物（``md/pages.json``）；缺失时是空 store（不抛错）。

    只用于兼容读取与"这篇论文到底有没有旧产物"的判断。新的读取方一律走 `page_products`
    —— 它会同时看 A / B 两路产物并给出统一的逐页读模型。
    """
    path: Path = paper_service.paper_paths(paper_id)["page_products"]
    return PageStore(paper_id, path)


def load_path_stores(paper_id: str) -> tuple[PageStore, PageStore]:
    """``(A 路 store, B 路 store)``：A = 英文转录，B = 中文翻译。

    **两路各一个文件**是硬约束，不是风格选择：`PageStore.flush()` 是整文件原子重写，
    两个并发写者共用一个文件必然互相覆盖（后写者把人家的页抹掉）。
    """
    paths = paper_service.paper_paths(paper_id)
    return (PageStore(paper_id, paths["path_a"], version=PATH_VERSION),
            PageStore(paper_id, paths["path_b"], version=PATH_VERSION))


def page_products(paper_id: str) -> tuple[list[dict], str]:
    """逐页的**合并读模型**（阅读器 / 分块 / 双语导出共用的唯一入口）。

    返回 ``(页列表, 形态)``；形态取值：

    - ``paths``：两路产物都在（拆开之后的正常形态）；
    - ``mixed``：只跑完一路（过渡态）—— 缺的那一侧从旧合并产物补，页面仍然完整可读；
    - ``legacy``：只有旧合并产物（存量论文**零迁移**可读）；
    - ``none``：什么产物都没有（未翻译，前端退回解析块）。

    合并靠 `pair_rows`：它只按**结构**（类型 / 层级 / 编号 / 公式 LaTeX）对齐，**不比较
    中英正文** —— 这正是当年修掉"整体错位一行"的那次修复，也是两路拆开后唯一可用的对齐方式。

    ⚠️ 旧形态（legacy / mixed）保留原有的**行 id 保护判据**：重算出的行数与存下来的不一致、
    且存下来的 ``align`` 不是 ``structure`` 时，仍然用存下来的行。行 id 是位置编号
    （``p{页}-{序号}``），块数一变，已有的高亮 / 笔记就会指到别的文字上（用户报过）。

    读时重算还有两个理由（改造前就有，这次一并保留）：① 老产物里行尾的 Markdown 硬换行
    标记被剥过，重算能把 `split_blocks` 后来的修复带到老产物上；② `breaks` 存在文件里而
    行是现算的，标记必须跟着行一起重算。
    """
    a_store, b_store = load_path_stores(paper_id)
    a_pages, b_pages = a_store.all_pages(), b_store.all_pages()
    has_a, has_b = bool(a_pages), bool(b_pages)
    legacy_pages = load_page_store(paper_id).all_pages()
    if not (has_a or has_b or legacy_pages):
        return [], "none"
    shape = "paths" if (has_a and has_b) else ("mixed" if (has_a or has_b) else "legacy")

    # 旧产物补齐缺失的一侧：legacy = 两侧都补；mixed = 只补没跑的那一路。
    # 用 `setdefault`：**任何情况下都不覆盖两路自己的产物**。
    for page, payload in legacy_pages.items():
        a_pages.setdefault(page, {
            "original_md": payload.get("original_md") or "",
            # 旧格式的锚点全是**英文**的（同一次调用产出，只给原文锚点）→ 归 A 路；
            # B 路锚点因此为空 → 中文单元退化为 TOC + 长度兜底（重跑后才会细分）。
            "breaks": list(payload.get("breaks") or []),
            "model": payload.get("model") or "",
            "updated_at": payload.get("updated_at"),
        })
        b_pages.setdefault(page, {
            "translation_md": payload.get("translation_md") or "",
            "carry_md": payload.get("carry_md") or "",
            "breaks": [],
            "model": payload.get("model") or "",
            "updated_at": payload.get("updated_at"),
        })

    out: list[dict] = []
    for page in sorted(set(a_pages) | set(b_pages)):
        a = a_pages.get(page) or {}
        b = b_pages.get(page) or {}
        original_md = str(a.get("original_md") or "")
        translation_md = str(b.get("translation_md") or "")
        if not original_md and not translation_md:
            continue                       # 两侧都空的页不产生一页（与旧 done 判据一致）
        fresh_rows, quality = pair_rows(original_md, translation_md, page)
        stored = legacy_pages.get(page) or {}
        rows = [dict(r) for r in fresh_rows]
        if shape != "paths" and stored.get("rows"):
            keep_stored = not (
                fresh_rows
                and (str(stored.get("align") or "") == "structure"
                     or len(fresh_rows) == len(stored.get("rows") or [])))
            if keep_stored:
                rows = [dict(r) for r in stored["rows"]]
        if rows:
            flags = page_continuity(paper_id, page)
            mark_rows_continuity(rows, head=flags["head"], tail=flags["tail"])
        # 两路锚点合成**一次**校验：`apply_semantic_breaks` 逐条独立判定，英文锚点落在原文侧、
        # 中文锚点落在译文侧（`_locate_anchor` 先原文后译文），并集天然成立 —— 不需要"按路存标记"。
        breaks_a = [str(x) for x in (a.get("breaks") or [])]
        breaks_b = [str(x) for x in (b.get("breaks") or [])]
        break_stats = apply_semantic_breaks(rows, breaks_a + breaks_b)
        page_total = (int(a.get("page_total") or 0) or int(b.get("page_total") or 0)
                      or int(stored.get("page_total") or 0))
        out.append({
            "page": page,
            "page_total": page_total,
            "original_md": original_md,
            "translation_md": translation_md,
            "rows": rows,
            "breaks": breaks_a + breaks_b,
            # 按路分开的原始锚点：分块层要用**自己那一路**的锚点（A 划英文、B 划中文）
            "breaks_a": breaks_a,
            "breaks_b": breaks_b,
            "break_stats": break_stats,
            # 旧形态沿用存下来的质量标记（与改造前逐字一致）；两路形态用现算的合并质量
            "align": str(stored.get("align") or "") or quality,
            "model": (a.get("model") or b.get("model") or stored.get("model") or ""),
            "updated_at": (a.get("updated_at") or b.get("updated_at")
                           or stored.get("updated_at")),
            # ---- 两路专属诊断（前端不用；对齐门限报告与回归用）----
            "source": shape,
            "sides": {"a": bool(original_md.strip()), "b": bool(translation_md.strip())},
            "one_sided": sum(1 for r in rows
                             if bool((r.get("original") or "").strip())
                             != bool((r.get("translation") or "").strip())),
        })
    return out, shape


def page_translations(paper_id: str) -> list[dict]:
    """`page_products` 的页列表（兼容旧签名：只关心逐页读模型的调用方用它）。"""
    return page_products(paper_id)[0]


# --------------------------------------------------------------------------
# 双语 Markdown 导出（**图片写进 Markdown 本体**）
# --------------------------------------------------------------------------


def bilingual_markdown(paper_id: str, *, write: bool = True,
                       api_prefix: str = "/api/v1") -> dict:
    """逐页输出带插图的 Markdown（原文 / 译文 / 对照）+ 整篇合并版。

    为什么单独做：页级翻译产物里只有文字（模型不会自己写 ``![]()``），
    直接看 Markdown 是**看不到任何插图**的。这里按图号把插图行插到对应图注上方，
    并保证裁图已落盘 —— 于是 Markdown 渲染器（应用内视图 / Typora / VS Code）
    都能正常显示图片。

    两份引用形式：
    - 返回值里用 ``{api_prefix}/papers/<id>/figures/region/<page>/<i>``（浏览器可访问）；
    - 落盘的 ``md/translated.md`` 用 ``../figures/<file>``（相对 md/ 目录，编辑器直接可用）。
    """
    from backend.core.utils import write_text_atomic
    from backend.services.paper import figure_regions

    page_items: list[dict] = []
    combined: list[str] = []
    # 走**合并读模型**（而不是直接读旧格式文件）：两路拆分后 pages.json 不再写入，
    # 直接读它会让导出变成空文件。
    for payload in page_translations(paper_id):
        page = int(payload["page"])
        refs_web = figure_regions.markdown_refs(paper_id, page, prefix=api_prefix)
        refs_disk = figure_regions.markdown_refs(paper_id, page, prefix="../figures")
        original = figure_regions.inject_images(payload.get("original_md") or "", refs_web)
        translation = figure_regions.inject_images(payload.get("translation_md") or "", refs_web)
        page_items.append({
            "page": page, "page_total": int(payload.get("page_total") or 0),
            "original_md": original, "translation_md": translation,
            "align": payload.get("align"), "model": payload.get("model"),
        })
        original_disk = figure_regions.inject_images(
            payload.get("original_md") or "", refs_disk)
        translation_disk = figure_regions.inject_images(
            payload.get("translation_md") or "", refs_disk)
        combined.append(f"<!-- page {page} -->\n\n## 第 {page} 页\n\n"
                        f"### 原文\n\n{original_disk}\n\n### 译文\n\n{translation_disk}")

    text = "\n\n---\n\n".join(combined).strip() + "\n"
    path: Path | None = None
    if write:
        md_dir: Path = paper_service.paper_paths(paper_id)["md"]
        target = md_dir / "translated.md"
        write_text_atomic(target, text)
        path = target
    return {"paper_id": paper_id, "pages": page_items, "markdown": text,
            "path": str(path) if path else None, "count": len(page_items)}


# --------------------------------------------------------------------------
# reader.json 装配
# --------------------------------------------------------------------------


def build_reader(paper_id: str, *, user_id: int = 1,
                 include_annotations: bool = True,
                 since: int | None = None) -> dict:
    paper = paper_service.get_paper(paper_id)
    md_map = load_markdown_map(paper_id)
    index = load_block_index(paper_id)
    indexed = {b["id"]: b for b in (index.get("blocks") or [])}
    paper_dir = paper_service.paper_paths(paper_id)["root"]
    # 页级产物**只装一次**：行 id→页、正文行、锚点映射都要它，而它内部会重跑
    # `pair_rows`（不便宜），装载三次纯属浪费。
    page_payloads = page_translations(paper_id)
    # 逐页索引：`_pages_payload`（版面 + 翻译状态）与 `_stats`（进度口径）都复用同一批产物，
    # 不再各自去读一次文件。
    pages_by_no = {int(p["page"]): p for p in page_payloads}
    # 锚点 → 页码：批注/笔记/高亮存的是行 id（当前产物）或历史的 block id，
    # 前端"定位"要能在行 id 失效时退化为按页跳转。
    row_pages: dict[str, int] = {}
    for page_payload in page_payloads:
        for row in page_payload.get("rows") or []:
            if row.get("id"):
                row_pages[row["id"]] = int(page_payload["page"])
    block_pages = {b["id"]: int(b.get("page") or 1)
                   for b in (index.get("blocks") or [])}

    with session_scope() as sess:
        blocks = (sess.query(Block)
                  .filter(Block.paper_id == paper_id)
                  .order_by(Block.order_global.asc()).all())
        formulas = (sess.query(Formula)
                    .filter(Formula.paper_id == paper_id).all())
        progress = sess.get(ReadingProgress, (user_id, paper_id))
        annotations = []
        if include_annotations:
            annotations = (sess.query(Annotation)
                           .filter(Annotation.paper_id == paper_id,
                                   Annotation.status == "active")
                           .order_by(Annotation.created_at.asc()).all())
        highlights = (sess.query(Highlight)
                      .filter(Highlight.paper_id == paper_id,
                              Highlight.user_id == user_id).all())
        notes = (sess.query(Note)
                 .filter(Note.paper_id == paper_id, Note.user_id == user_id).all())
        tasks = (sess.query(Task)
                 .filter(Task.paper_id == paper_id)
                 .order_by(Task.created_at.desc()).limit(10).all())

        f_map = {f.id: f for f in formulas}

        out_blocks = []
        for b in blocks:
            if since is not None and b.order_global <= since:
                continue
            md_node = md_map.get(b.id, {})
            idx_node = indexed.get(b.id, {})
            f = f_map.get(b.id)
            meta = json_loads(b.meta_json, {}) or {}
            # 正文优先取阅读器索引（纯文本），其次 Markdown，最后 MySQL 预览
            text = idx_node.get("text") or md_node.get("text") or b.preview or ""
            if b.type == "heading":
                # 兼容旧索引（文本里带 `##` 标记）并规范化章节号后的空格
                from backend.parsing.md.generator import normalize_heading_text

                text = normalize_heading_text(text)
            item = {
                "id": b.id, "page": b.page_no, "type": b.type, "col": b.col,
                "order": b.order_global,
                "bbox": [b.x0, b.y0, b.x1, b.y1],
                "anchor": b.md_anchor or f"blk-{b.id}",
                "text": text,
                "heading_level": _heading_level(b, text, idx_node),
                # 块级译文：页级时代只有历史产物里才有（新论文一律为空字符串）。
                # 前端拿它做"未翻译页退回解析块"时的占位显示，所以字段保留。
                "translation": md_node.get("translation") or "",
                "content_hash": b.content_hash,
                "section": idx_node.get("section") or meta.get("section_title")
                or b.section_id,
            }
            if f is not None:
                item["formula"] = _formula_card(f)
            out_blocks.append(item)

        payload = {
            "paper": paper,
            "blocks": out_blocks,
            "pages": _pages_payload(paper_id, pages=page_payloads),
            "page_translations": page_payloads,
            # 块 id → 正文行 id：检索/RAG 给的是**块 id**（`attentionisa-p01-b06`），
            # 已翻译页的正文行 id 却是页级产物的位置编号（`p001-06`）—— 两套命名空间。
            # 没有这张表，"跳回原文"就是空跳（真实事故）。见 anchor_map 模块头。
            "block_rows": anchor_map.build_block_row_map(
                out_blocks, anchor_map.rows_by_page(page_payloads)),            "progress": _progress_payload(progress),
            "annotations": [{"id": a.id, "block_id": a.block_id, "type": a.type,
                             "content_md": a.content_md, "source": a.source,
                             "confidence": a.confidence,
                             "page": _anchor_page(a.block_id, row_pages, block_pages),
                             "created_at": a.created_at.isoformat(timespec="seconds")
                             if a.created_at else None} for a in annotations],
            "highlights": [{"id": h.id, "block_id": h.block_id,
                            "start": h.start_offset, "end": h.end_offset,
                            "color": h.color, "text": h.selected_text,
                            "side": h.side or "en", "scope": h.scope or "row",
                            "group_id": h.group_id,
                            "page": _anchor_page(h.block_id, row_pages, block_pages)}
                           for h in highlights],
            "notes": [{"id": n.id, "block_id": n.block_id, "content_md": n.content_md,
                       "selected_text": n.selected_text,
                       "page": _anchor_page(n.block_id, row_pages, block_pages),
                       "updated_at": n.updated_at.isoformat(timespec="seconds")
                       if n.updated_at else None} for n in notes],
            "tasks": [{"id": t.id, "type": t.type, "queue": t.queue,
                       "status": t.status, "progress": float(t.progress or 0),
                       "message": t.message,
                       "done_items": t.done_items, "total_items": t.total_items}
                      for t in tasks],
            "outline": build_outline(paper_id),
            "stats": _stats(paper_id, blocks, page_payloads),
            "since": since,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "paper_dir": str(paper_dir),
        }
    return payload


def _anchor_page(block_id: str | None, row_pages: dict[str, int],
                 block_pages: dict[str, int]) -> int | None:
    """锚点（行 id / 块 id / Markdown 视图的 `md:p<页号>`）→ 页码；查不到返回 None。"""
    if not block_id:
        return None
    if block_id.startswith("md:p"):
        try:
            return int(block_id[4:])
        except ValueError:
            return None
    return row_pages.get(block_id) or block_pages.get(block_id)


# --------------------------------------------------------------------------
# 公式：**直接从 Markdown（页级产物）里提取**，不做复核
# --------------------------------------------------------------------------

_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)


def _extract_math(text: str) -> list[tuple[int, int, str, bool]]:
    """从一段 Markdown 里抽出公式：``(start, end, latex, display)``。

    行内公式必须**成对扫描**：直接正则 ``\\$([^$]+)\\$`` 会把两段公式之间的正文
    也当成公式（例：``$d$-dimensional ... $B$`` 中间的说明文字），实测会多出成百条噪声。
    这里先占住 ``$$…$$``，再逐字符配对 ``$…$``，并且跳过已被占用的区间。
    """
    out: list[tuple[int, int, str, bool]] = []
    spans: list[tuple[int, int]] = []
    for m in _DISPLAY_MATH.finditer(text):
        latex = m.group(1).strip()
        spans.append((m.start(), m.end()))
        if latex:
            out.append((m.start(), m.end(), latex, True))

    def taken(i: int) -> bool:
        return any(a <= i < b for a, b in spans)

    i = 0
    while i < len(text):
        if text[i] != "$" or taken(i):
            i += 1
            continue
        j = i + 1
        while j < len(text) and text[j] != "\n" and (text[j] != "$" or taken(j)):
            j += 1
        if j < len(text) and text[j] == "$" and not taken(j):
            latex = text[i + 1:j].strip()
            if latex and not latex.startswith("$"):
                out.append((i, j + 1, latex, False))
            i = j + 1
            continue
        i += 1
    return sorted(out, key=lambda x: x[0])


def markdown_formulas(paper_id: str) -> dict:
    """把页级产物 Markdown 里的公式逐条抽出来（行间 + 行内），只读、不复核。

    为什么不再用解析阶段的公式卡：阅读器展示的公式本来就来自模型（看页图生成的 LaTeX），
    解析器的公式抽取只是解析质检的副产物。公式列表直接从**你读到的 Markdown** 里抽，
    因此与页面上的公式一字不差；单字符变量（``$b$``）这类噪声不进列表。
    """
    items: list[dict] = []
    for payload in page_translations(paper_id):
        page = int(payload["page"])
        for row in payload.get("rows") or []:
            text = row.get("original") or ""
            if "$" not in text:
                continue
            for _s, _e, latex, display in _extract_math(text):
                if not display and len(latex) < 4 and "\\" not in latex:
                    continue
                # 页脚脚注符号被模型写成了 `$$ * † $$` —— 没有任何数学内容，不进公式列表
                if is_noise_formula(latex):
                    continue
                items.append({
                    "id": f"f{page:03d}-{len(items):04d}",
                    "page": page, "row_id": row.get("id"),
                    "latex": latex, "display": display,
                    "context": _snippet(text, latex),
                    "context_zh": _snippet(row.get("translation") or "", latex),
                    "model": payload.get("model"),
                })
    return {"items": items, "count": len(items), "source": "markdown",
            "display_count": sum(1 for i in items if i["display"])}


def _snippet(text: str, needle: str, width: int = 70) -> str:
    idx = (text or "").find(needle)
    if idx < 0:
        return (text or "")[:width * 2].replace("\n", " ").strip()
    start = max(0, idx - width)
    end = min(len(text), idx + len(needle) + width)
    return ("…" if start else "") + text[start:end].replace("\n", " ").strip() \
        + ("…" if end < len(text) else "")


def formulas_payload(paper_id: str, source: str = "markdown") -> dict:
    """公式列表：默认来自 Markdown（页级产物）；``source=parse`` 才回解析阶段的公式卡。"""
    if source == "parse":
        rows = _parse_formulas(paper_id)
        return {"items": [_formula_card(f) for f in rows],
                "count": len(rows), "source": "parse"}
    return markdown_formulas(paper_id)


def _parse_formulas(paper_id: str) -> list[Formula]:
    with session_scope() as sess:
        return (sess.query(Formula).filter(Formula.paper_id == paper_id)
                .order_by(Formula.page_number.asc()).all())


def _pages_payload(paper_id: str, pages: list[dict] | None = None) -> list[dict]:
    """每页的版面信息 + 翻译状态 + 该页插图（**按图注锚定的裁图**，图注驱动）。

    ``pages`` 传**合并后的页产物**（`page_products` 的结果）就复用调用方已经装好的那一份；
    不传就自己取一次。以前这里接的是旧格式 store —— 两路拆分后那样会永远看不到"已翻译"。
    """
    from backend.parsing.ir.ir_reader import IRReader
    from backend.services.paper import figure_regions

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    if not ir.exists:
        return []
    by_page = ({int(p["page"]): p for p in (pages if pages is not None
                                            else page_translations(paper_id))})
    out: list[dict] = []
    for p in ir.pages():
        page_no = int(p["page_no"])
        payload = by_page.get(page_no) or {}
        rows = payload.get("rows") or []
        media = [m for m in figure_regions.media_of_page(paper_id, page_no)
                 if m["kind"] == "figure"]
        out.append({
            "page": page_no, "width": p["width"], "height": p["height"],
            "columns": p["columns"], "gutter_x": p["gutter_x"],
            "blocks": p["block_count"],
            "has_text_layer": bool(p["has_text_layer"]),
            # `translated` 的口径是 **B 路（中文译文）已产出**：A 路只有英文转录的页
            # 仍然算"未翻译"（前端据此显示"等待翻译"）。
            "translated": bool(str(payload.get("translation_md") or "").strip()),
            "row_count": len(rows),
            "align": payload.get("align"),
            "source": payload.get("source"),
            "media": media,
        })
    return out


def _heading_level(block: Block, text: str, idx_node: dict | None = None) -> int | None:
    """标题层级（阅读器与目录共用；与 Markdown 生成保持一致，规划 §18 结构导航）。

    取值优先级：**阅读器索引里的 heading_level**（由 Markdown 生成阶段判定，
    伪代码假标题记 0）→ 按字号/编号回退计算（旧索引没有该字段时用）。
    """
    if block.type != "heading":
        return None
    from backend.parsing.md.generator import heading_level_or_none

    node = idx_node or {}
    if node.get("heading_level") is not None and "heading_level" in node:
        level = int(node["heading_level"] or 0)
        return level or None
    font_size = float(node.get("font_size")
                      or (json_loads(block.meta_json, {}) or {}).get("font_size") or 0.0)
    return heading_level_or_none(text or block.text_clean or "", font_size)


# --------------------------------------------------------------------------
# 目录（Markdown 结构，规划 §18 结构导航）
# --------------------------------------------------------------------------


def build_outline(paper_id: str) -> dict:
    """**中英双语**目录：标题行直接来自页级翻译产物（``pages.json`` 的 heading 行）。

    每个标题行同时带原文与译文 —— 目录因此可以「英文在上、中文在下」并排显示。
    没有页级产物时退回解析阶段的标题索引（只有英文，``text_zh`` 为空）。

    返回 ``{items: [扁平], tree: [嵌套], count}``；每项含
    ``block_id / page / level / number / text / text_zh / children``。

    **层级与父子关系由章节编号决定**（``level`` 只作兜底）：产物里的 ``#`` 深度是模型给的、
    原生页是解析器按字号判定的，两者都不可信，直接拿来建树会把目录挂错、顺序也会乱 ——
    详见 `_apply_semantic_levels` 与 `_nest_outline`。
    """
    flat: list[dict] = []
    for page_payload in page_translations(paper_id):
        for row in page_payload.get("rows") or []:
            if row.get("type") != "heading":
                continue
            level = int(row.get("level") or 0)
            if level <= 0 or level > 4:
                continue
            en = _strip_heading(row.get("original") or "")
            zh = _strip_heading(row.get("translation") or "")
            if not en and not zh:
                continue
            flat.append({
                "block_id": row.get("id"), "page": page_payload["page"],
                "level": level,
                "number": row.get("number") or _section_number(en or zh),
                "text": en or zh, "text_zh": zh,
                "children": [],
            })
    if not flat:
        flat = _outline_from_index(paper_id)

    _adopt_appendix_letters(flat)
    has_numbers = _apply_semantic_levels(flat)
    return {"items": flat, "tree": _nest_outline(flat, has_numbers), "count": len(flat)}


def _adopt_appendix_letters(flat: list[dict]) -> None:
    """让「B MATHEMATICAL PRELIMINARIES」这类**没有编号的附录大标题**认领它的字母。

    为什么需要：附录子节 `B.1 SHANNON…` 的编号能抽出来，父标题 `B MATHEMATICAL PRELIMINARIES`
    却抽不出来（字母编号只在"带点号"时可靠），于是 `B.1` 会因为"找不到编号为 `B` 的父节点"
    被甩成顶级 —— 附录子节就不在附录下面了。

    做法是**数据驱动**的：先从 `X.n` 形式的编号里收集"哪些字母真的有子节"（如 {B, C}），
    再让**以该字母单独成词**开头的标题认领它（`^A …`、`^APPENDIX A …`）。
    这样 "A NOVEL FRAMEWORK"（没有 A.1 子节）不会被误判成附录 A，
    ABSTRACT / REFERENCES 也不受影响（首字母后面不是词边界）。
    """
    letters = {
        str(item.get("number") or "").split(".")[0].upper()
        for item in flat
        if "." in str(item.get("number") or "") and str(item.get("number") or "")[:1].isalpha()
    }
    if not letters:
        return
    for item in flat:
        if str(item.get("number") or "").strip():
            continue
        match = re.match(r"^(?:APPENDIX\s+)?([A-Za-z])\b", str(item.get("text") or "").strip())
        if match and match.group(1).upper() in letters:
            item["number"] = match.group(1).upper()


def _apply_semantic_levels(flat: list[dict]) -> bool:
    """把每项的 ``level`` 换成**由章节编号推出的语义层级**；返回这篇论文是否有编号体系。

    为什么不能直接信产物里的 ``level``：页级产物的 ``#`` 深度是**模型**写的（原生页由解析器
    按字号判定），两者都不稳定 —— 实测同一篇论文里 ``1 TURBOQUANT…`` 是 1 级、``2 EXPERIMENTS``
    却是 2 级、``1.2 INNER-PRODUCT…`` 又是 3 级、``B.1 SHANNON…`` 只有 2 级。层级不可信时，
    任何"按 level 建树"的算法都会把目录挂错，顺序也就跟着乱。
    """
    has_numbers = any(str(item.get("number") or "").strip() for item in flat)
    from backend.parsing.md.generator import section_depth

    for item in flat:
        number = str(item.get("number") or "").strip()
        # 层级 = 编号语义（1 → 1、1.2 → 2、B.1 → 2），与标题骨架 / 阅读区同一套（见 generator.section_depth）
        item["level"] = section_depth(number) if has_numbers else int(item.get("level") or 1)
    return has_numbers

def _nest_outline(flat: list[dict], has_numbers: bool) -> list[dict]:
    """把扁平标题建成树：**编号优先，`level` 兜底**（层级来源见 `_apply_semantic_levels`）。

    规则：
    - 有编号：父节点 = 前面最近的、编号是本文编号**严格前缀**的标题（`1` 之于 `1.2`、`B` 之于 `B.1`）；
      找不到父节点（如 `0.1` 而目录里根本没有 `0`）→ 按**顶级**处理。这条"宁可当顶级，
      也不要挂到上一个无关标题下面"是刻意保留的（否则 `0.1 PROBLEM DEFINITION` 会挂到
      `ABSTRACT` 底下，目录语义就错了）。
    - 无编号：在有编号体系的论文里一律按顶级（它们本来就是章节级）；
      整篇都没有编号（老论文 / 扫描件）时才退回经典的"往上找第一个更浅的标题当父节点"。
    """
    tree: list[dict] = []
    by_number: dict[str, dict] = {}      # 编号 → 最近一次出现的标题（供后代认亲）
    stack: list[dict] = []               # 只在"整篇无编号"时用：深度递增的祖先链
    for item in flat:
        number = str(item.get("number") or "").strip()
        parent: dict | None = None
        if number:
            parts = number.split(".")
            for cut in range(len(parts) - 1, 0, -1):     # 1.2 → 先试 "1"；B.1 → 先试 "B"
                parent = by_number.get(".".join(parts[:cut]))
                if parent is not None:
                    break
        elif not has_numbers:
            while stack and int(stack[-1].get("level") or 1) >= int(item.get("level") or 1):
                stack.pop()
            parent = stack[-1] if stack else None

        if parent is None:
            tree.append(item)
        else:
            parent["children"].append(item)

        if number:
            by_number[number] = item
        if not has_numbers:
            stack.append(item)
    return tree


_NUM_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z](?:\.\d{1,2}){1,3})\b")


def _section_number(text: str) -> str:
    m = _NUM_RE.match((text or "").strip())
    return m.group(1) if m else ""


def _strip_heading(text: str) -> str:
    return re.sub(r"^[\s#]+", "", (text or "").strip()).strip()


def _outline_from_index(paper_id: str) -> list[dict]:
    """退回解析阶段的标题索引（只有英文；``text_zh`` 为空）。"""
    index = load_block_index(paper_id)
    out: list[dict] = []
    for node in index.get("blocks") or []:
        if node.get("type") != "heading":
            continue
        level = int(node.get("heading_level") or 0)
        if level <= 0 or level > 4:
            continue
        text = str(node.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "block_id": node["id"], "page": node.get("page") or 1, "level": level,
            "number": _section_number(text), "text": text, "text_zh": "",
            "children": [],
        })
    return out


def _progress_payload(row: ReadingProgress | None) -> dict:
    if row is None:
        return {"last_block_id": None, "last_page": 1, "percent": 0.0,
                "view_mode": "bilingual", "pair_order": "en_left",
                "original_mode": "text", "scroll_ratio": 0.0,
                "page_side": "zh",
                # 界面状态：没有记录时给 None（前端据此回退到本地偏好/默认值）
                "aside_open": None, "text_size": None, "show_refs": None, "zoom": None}
    # 兼容旧记录里的上下对照取值：映射到左右对照的等价语义
    pair_order = row.pair_order or "en_left"
    pair_order = {"en_top": "en_left", "zh_top": "zh_left"}.get(pair_order, pair_order)
    return {
        "last_block_id": row.last_block_id, "last_page": row.last_page,
        "percent": float(row.percent or 0), "view_mode": row.view_mode,
        "pair_order": pair_order, "original_mode": row.original_mode or "text",
        "page_side": row.page_side or "zh",
        "scroll_ratio": float(row.scroll_ratio or 0),
        "aside_open": row.aside_open, "text_size": row.text_size,
        "show_refs": row.show_refs,
        "zoom": float(row.zoom) if row.zoom is not None else None,
        "updated_at": row.updated_at.isoformat(timespec="seconds")
        if row.updated_at else None,
    }


def _formula_card(f: Formula) -> dict:
    from backend.core.config import get_settings

    s = get_settings()
    crop_name = Path(f.image_path).name if f.image_path else None
    return {
        "id": f.id, "page": f.page_number,
        "bbox": json_loads(f.bbox_json, None),
        "raw_text": f.raw_text,
        "latex_candidate": json_loads(f.latex_candidate, []) or [],
        "latex_repaired": f.latex_repaired,
        "latex_final": f.latex_final,
        "grade": f.grade, "confidence": f.confidence,
        "source": f.source, "status": f.status,
        "vector_audit": json_loads(f.vector_data_json, {}) or {},
        "validation": json_loads(f.validation_json, {}) or {},
        "review_note": f.review_note,
        "is_inline": bool(f.is_inline),
        "crop_url": f"/papers/{f.paper_id}/formula_crops/{crop_name}"
        if crop_name else None,
        "crop_path": str(Path(s.paper_dir(f.paper_id)) / "formula_crops" / crop_name)
        if crop_name else None,
    }


def _stats(paper_id: str, blocks: list[Block],
           pages: list[dict] | None = None) -> dict:
    """阅读器统计：**以页为进度口径**（页级翻译后的唯一粒度）。

    ``pages_done`` 的口径是 **B 路（中文译文）已产出的页数** —— 两路拆开之后"翻译进度"
    就是中文那一侧的完成度；旧格式（同一次调用产出两侧）两种口径等价（旧产物里"有行"
    必然意味着"有译文"，缺任一侧的调用会直接报错重试）。
    """
    by_type: dict[str, int] = {}
    for b in blocks:
        by_type[b.type] = by_type.get(b.type, 0) + 1
    products = pages if pages is not None else page_translations(paper_id)
    done_pages = sum(1 for p in products if str(p.get("translation_md") or "").strip())
    rows = sum(len(p.get("rows") or []) for p in products)
    align: dict[str, int] = {}
    for p in products:
        key = str(p.get("align") or "?")
        align[key] = align.get(key, 0) + 1
    total_pages = (max((int(p.get("page_total") or 0) for p in products), default=0)
                   or len({b.page_no for b in blocks}) or 0)
    models = {str(p.get("model")) for p in products if p.get("model")}
    return {
        "blocks": len(blocks), "by_type": by_type,
        "pages": total_pages, "pages_done": done_pages,
        "rows": rows,
        "translated": done_pages,
        "translate_total": total_pages,
        "translate_percent": round(done_pages / total_pages * 100, 1) if total_pages else 0.0,
        "align": align,
        "model": sorted(models)[0] if len(models) == 1 else (", ".join(sorted(models))
                                                             if models else ""),
        "deleted_at": None,
    }


# --------------------------------------------------------------------------
# 增量接口
# --------------------------------------------------------------------------


def content_since(paper_id: str, since: int = 0, limit: int = 200) -> dict:
    """按 block 取正文（支持 since 增量，规划 §20.2）。"""
    md_map = load_markdown_map(paper_id)
    with session_scope() as sess:
        blocks = (sess.query(Block)
                  .filter(Block.paper_id == paper_id, Block.order_global > since)
                  .order_by(Block.order_global.asc()).limit(limit).all())
        items = [{"id": b.id, "page": b.page_no, "order": b.order_global,
                  "type": b.type, "text": md_map.get(b.id, {}).get("text", ""),
                  "bbox": [b.x0, b.y0, b.x1, b.y1]} for b in blocks]
    return {"items": items, "next_since": items[-1]["order"] if items else since,
            "has_more": len(items) >= limit}


def translations_since(paper_id: str, since_page: int = 0,
                       pages: list[int] | None = None) -> dict:
    """译文的增量接口（前端 LiveUpdater 轮询兜底用）：**以页为单位**返回。

    SSE 断线时前端按此接口补齐；页级粒度下"增量"就是"页号大于 since_page"。
    """
    items = []
    for payload in page_translations(paper_id):
        page_no = int(payload["page"])
        if since_page and page_no <= int(since_page):
            continue
        if pages and page_no not in {int(p) for p in pages}:
            continue
        items.append({
            "page": page_no, "page_total": payload.get("page_total"),
            "rows": payload.get("rows") or [], "align": payload.get("align"),
            "model": payload.get("model"), "updated_at": payload.get("updated_at"),
        })
    return {"items": items, "count": len(items), "since_page": since_page}


def row_id_of_caption(paper_id: str, page_no: int, number: str,
                      rows: list[dict] | None = None) -> str | None:
    """这一页里"图号 = number"的图注行 id（侧栏/目录定位用）。

    行 id 是页内序号（``p008-03``），只有翻译产物知道；侧栏的插图条目必须带上它，
    否则"定位"按钮会拿着一个不存在的 id 找不到锚点。

    ``rows`` 可传入调用方已经装好的行（`figures_payload` 逐图调用时用 —— 否则每张图都会
    把整篇的两路产物重装一遍，那是 O(图数 × 全书) 的浪费）。
    """
    if not number:
        return None
    if rows is None:
        rows = _rows_of_page(paper_id, page_no)
    for row in rows:
        if row.get("type") == "caption" and str(row.get("number") or "") == str(number):
            return row.get("id")
    pattern = re.compile(rf"^\*{{0,2}}\s*(?:Figure|Fig\.?|Table|图|表)\s*{re.escape(str(number))}\b",
                         re.IGNORECASE)
    for row in rows:
        if pattern.match((row.get("original") or "").strip()):
            return row.get("id")
    return None


def _rows_of_page(paper_id: str, page_no: int) -> list[dict]:
    for payload in page_translations(paper_id):
        if int(payload["page"]) == int(page_no):
            return payload.get("rows") or []
    return []


def figures_payload(paper_id: str) -> dict:
    """插图列表（**按图注锚定的裁图**，与阅读器正文里的图一致）。

    ``block_id`` / ``row_id`` 给的是**图注行的行 id**（存在时），前端"定位"直接可用；
    另带 ``page``，即使行 id 失效（重译后行号变了）也能退化为按页定位。

    ``image_url`` 与 ``card_url`` 都是**「图 + 图注」卡片**（图 + 图注合成一张）：侧栏是把图当
    "一张能看懂的图"来展示的，卡片自带图注更实用；正文里也用同一张（用户口径：
    **点开看大图和论文里的范围必须一致**，纯图那张"贴着边、少了图注"，是裁得不好的那张）。
    """
    from backend.services.paper import figure_regions

    # 页 → 行 装一次：`row_id_of_caption` 会被每张图调用一次，不预装就是
    # "每张图把整篇的两路产物重装一遍"（O(图数 × 全书)，纯浪费）。
    rows_by_page = {int(p["page"]): (p.get("rows") or [])
                    for p in page_translations(paper_id)}
    items = []
    for page_no, regions in sorted(figure_regions.all_regions(paper_id).items()):
        for i, r in enumerate(regions):
            if r["kind"] != "figure":
                continue
            row_id = row_id_of_caption(paper_id, page_no, r["number"],
                                       rows=rows_by_page.get(int(page_no)) or [])
            base = f"/api/v1/papers/{paper_id}/figures/region/{page_no}/{i}"
            items.append({
                "block_id": row_id or f"fig-p{page_no:03d}-{i:02d}",
                "row_id": row_id,
                "page": page_no, "caption": r["caption"], "bbox": r["bbox"],
                "number": r["number"],
                "part": int(r.get("part", 0)), "parts": int(r.get("parts", 1)),
                "part_label": r.get("part_label") or "",
                "status": "ok", "audit": {},
                "image_url": f"{base}?card=1",
                "card_url": f"{base}?card=1",
            })
    return {"items": items, "count": len(items)}


# --------------------------------------------------------------------------
# 阅读进度（服务端权威，规划 §10 ⑧）
# --------------------------------------------------------------------------


VALID_VIEW_MODES = ("original", "translation", "bilingual")
# 对照方向：`en_left/zh_left`（左右对照，当前前端默认）与旧的 `en_top/zh_top`（上下对照）都接受
VALID_PAIR_ORDERS = ("en_left", "zh_left", "en_top", "zh_top")
# 原文栏呈现：`text` = 转录的原文文本；`page` = 原 PDF 页图
VALID_ORIGINAL_MODES = ("text", "page")
# 正文字号（前端 TEXT_SIZES 的三个档）
VALID_TEXT_SIZES = ("sm", "md", "lg")
# 对照 + 原 PDF 时右侧文字列放哪一侧：`zh` 译文（默认）/ `en` 原文
VALID_PAGE_SIDES = ("en", "zh")
# 原 PDF 缩放的合法区间（与前端工具栏按钮的夹取一致）
ZOOM_MIN, ZOOM_MAX = 0.4, 3.0


def save_progress(paper_id: str, payload: dict, user_id: int = 1) -> dict:
    paper_service.get_paper(paper_id)
    with session_scope() as sess:
        row = sess.get(ReadingProgress, (user_id, paper_id))
        if row is None:
            row = ReadingProgress(user_id=user_id, paper_id=paper_id)
            sess.add(row)
        if payload.get("last_block_id") is not None:
            row.last_block_id = payload["last_block_id"]
        if payload.get("last_page") is not None:
            row.last_page = int(payload["last_page"])
        if payload.get("percent") is not None:
            row.percent = float(payload["percent"])
        if payload.get("view_mode") in VALID_VIEW_MODES:
            row.view_mode = payload["view_mode"]
        if payload.get("pair_order") in VALID_PAIR_ORDERS:
            row.pair_order = payload["pair_order"]
        if payload.get("original_mode") in VALID_ORIGINAL_MODES:
            row.original_mode = payload["original_mode"]
        if payload.get("page_side") in VALID_PAGE_SIDES:
            row.page_side = payload["page_side"]
        # 界面状态：类型不对就当作"没说"，不覆盖已有记录（一个手滑的请求不该改坏用户的选择）
        if isinstance(payload.get("aside_open"), bool):
            row.aside_open = payload["aside_open"]
        if payload.get("text_size") in VALID_TEXT_SIZES:
            row.text_size = payload["text_size"]
        if isinstance(payload.get("show_refs"), bool):
            row.show_refs = payload["show_refs"]
        if payload.get("zoom") is not None:
            try:
                row.zoom = min(ZOOM_MAX, max(ZOOM_MIN, float(payload["zoom"])))
            except (TypeError, ValueError):
                pass
        if payload.get("scroll_ratio") is not None:
            row.scroll_ratio = float(payload["scroll_ratio"])
        sess.flush()
        data = _progress_payload(row)
    paper_service.touch_read(paper_id)
    return data


def get_progress(paper_id: str, user_id: int = 1) -> dict:
    with session_scope() as sess:
        return _progress_payload(sess.get(ReadingProgress, (user_id, paper_id)))


def block_detail(paper_id: str, block_id: str) -> dict:
    md_map = load_markdown_map(paper_id)
    with session_scope() as sess:
        b = sess.get(Block, block_id)
        if b is None or b.paper_id != paper_id:
            raise NotFoundError(f"块不存在：{block_id}")
        f = sess.get(Formula, block_id)
        return {
            "id": b.id, "paper_id": b.paper_id, "page": b.page_no, "type": b.type,
            "col": b.col, "order": b.order_global,
            "bbox": [b.x0, b.y0, b.x1, b.y1],
            "text": md_map.get(b.id, {}).get("text", b.preview or ""),
            "translation": md_map.get(b.id, {}).get("translation", ""),
            "content_hash": b.content_hash, "section": b.section_id,
            "formula": _formula_card(f) if f is not None else None,
        }
