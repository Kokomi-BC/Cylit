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
from backend.repositories.mysql.models import (
    Annotation, Block, Formula, Highlight, Note, Paper, ReadingProgress, Task,
    Translation,
)
from backend.repositories.mysql.session import session_scope
from backend.services.paper import paper_service
from backend.services.translation.page_translate import (
    PAGES_FILENAME, PageStore,
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
    """页级翻译产物（``md/pages.json``）；缺失时是空 store（不抛错）。"""
    path: Path = paper_service.paper_paths(paper_id)["md"] / PAGES_FILENAME
    return PageStore(paper_id, path)


def page_translations(paper_id: str) -> list[dict]:
    """阅读器用：逐页的原文/译文 Markdown + 对齐后的行。"""
    store = load_page_store(paper_id)
    out: list[dict] = []
    for page, payload in sorted(store.all_pages().items()):
        out.append({
            "page": page,
            "page_total": int(payload.get("page_total") or 0),
            "original_md": payload.get("original_md") or "",
            "translation_md": payload.get("translation_md") or "",
            "rows": payload.get("rows") or [],
            "align": payload.get("align"),
            "model": payload.get("model"),
            "updated_at": payload.get("updated_at"),
        })
    return out


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

    store = load_page_store(paper_id)
    page_items: list[dict] = []
    combined: list[str] = []
    for page, payload in sorted(store.all_pages().items()):
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
    # 锚点 → 页码：批注/笔记/高亮存的是行 id（当前产物）或历史的 block id，
    # 前端"定位"要能在行 id 失效时退化为按页跳转。
    row_pages: dict[str, int] = {}
    for page_payload in page_translations(paper_id):
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
        translations = (sess.query(Translation)
                        .filter(Translation.paper_id == paper_id).all())
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

        latest_tr: dict[str, Translation] = {}
        for t in sorted(translations, key=lambda x: (x.version, x.revision)):
            latest_tr[t.block_id] = t
        f_map = {f.id: f for f in formulas}

        out_blocks = []
        for b in blocks:
            if since is not None and b.order_global <= since:
                continue
            md_node = md_map.get(b.id, {})
            idx_node = indexed.get(b.id, {})
            tr = latest_tr.get(b.id)
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
                "translation": md_node.get("translation") or "",
                "translation_status": tr.status if tr else None,
                "translation_revision": tr.revision if tr else 0,
                "translation_confidence": tr.confidence if tr else None,
                "has_translation": bool(md_node.get("translation")),
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
            "pages": _pages_payload(paper_id, store=load_page_store(paper_id)),
            "page_translations": page_translations(paper_id),
            "progress": _progress_payload(progress),
            "annotations": [{"id": a.id, "block_id": a.block_id, "type": a.type,
                             "content_md": a.content_md, "source": a.source,
                             "confidence": a.confidence,
                             "page": _anchor_page(a.block_id, row_pages, block_pages),
                             "created_at": a.created_at.isoformat(timespec="seconds")
                             if a.created_at else None} for a in annotations],
            "highlights": [{"id": h.id, "block_id": h.block_id,
                            "start": h.start_offset, "end": h.end_offset,
                            "color": h.color, "text": h.selected_text,
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
            "stats": _stats(paper_id, blocks),
            "since": since,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "paper_dir": str(paper_dir),
        }
    return payload


def _anchor_page(block_id: str | None, row_pages: dict[str, int],
                 block_pages: dict[str, int]) -> int | None:
    """锚点（行 id 或块 id）→ 页码；都查不到返回 None（前端只提示、不乱跳）。"""
    if not block_id:
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


def _pages_payload(paper_id: str, store: PageStore | None = None) -> list[dict]:
    """每页的版面信息 + 翻译状态 + 该页插图（**按图注锚定的裁图**，图注驱动）。"""
    from backend.parsing.ir.ir_reader import IRReader
    from backend.services.paper import figure_regions

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    if not ir.exists:
        return []
    store = store if store is not None else load_page_store(paper_id)
    out: list[dict] = []
    for p in ir.pages():
        page_no = int(p["page_no"])
        payload = store.page(page_no)
        rows = (payload or {}).get("rows") or []
        media = [m for m in figure_regions.media_of_page(paper_id, page_no)
                 if m["kind"] == "figure"]
        out.append({
            "page": page_no, "width": p["width"], "height": p["height"],
            "columns": p["columns"], "gutter_x": p["gutter_x"],
            "blocks": p["block_count"],
            "has_text_layer": bool(p["has_text_layer"]),
            "translated": bool(rows),
            "row_count": len(rows),
            "align": (payload or {}).get("align"),
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

    # 嵌套：往上找第一个更浅的标题当父节点；**编号断链时视为顶级**。
    # 为什么必须有"断链"判断：论文里 "0.1 PROBLEM DEFINITION" 的父标题（0）没被识别出来，
    # 若不判断就会把它挂到上一个无关的二级标题（如 ABSTRACT）下面 —— 目录语义就错了。
    tree: list[dict] = []
    for i, item in enumerate(flat):
        parent = None
        for j in range(i - 1, -1, -1):
            cand = flat[j]
            if cand["level"] < item["level"]:
                item_num, cand_num = item["number"], cand["number"]
                if item_num and "." in item_num and not (cand_num and item_num.startswith(f"{cand_num}.")):
                    parent = None            # 编号前缀对不上 → 顶级
                else:
                    parent = cand
                break
        if parent is None:
            tree.append(item)
        else:
            parent["children"].append(item)
    return {"items": flat, "tree": tree, "count": len(flat)}


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
                "scroll_ratio": 0.0}
    # 兼容旧记录里的上下对照取值：映射到左右对照的等价语义
    pair_order = row.pair_order or "en_left"
    pair_order = {"en_top": "en_left", "zh_top": "zh_left"}.get(pair_order, pair_order)
    return {
        "last_block_id": row.last_block_id, "last_page": row.last_page,
        "percent": float(row.percent or 0), "view_mode": row.view_mode,
        "pair_order": pair_order, "scroll_ratio": float(row.scroll_ratio or 0),
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


def _stats(paper_id: str, blocks: list[Block]) -> dict:
    """阅读器统计：**以页为进度口径**（页级翻译后的唯一粒度）。"""
    by_type: dict[str, int] = {}
    for b in blocks:
        by_type[b.type] = by_type.get(b.type, 0) + 1
    store = load_page_store(paper_id)
    stats = store.stats()
    total_pages = stats["page_total"] or len({b.page_no for b in blocks}) or 0
    done_pages = stats["pages_done"]
    return {
        "blocks": len(blocks), "by_type": by_type,
        "pages": total_pages, "pages_done": done_pages,
        "rows": stats["rows"],
        "translated": done_pages,
        "translate_total": total_pages,
        "translate_percent": round(done_pages / total_pages * 100, 1) if total_pages else 0.0,
        "align": stats["align"],
        "model": stats["model"],
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


def row_id_of_caption(paper_id: str, page_no: int, number: str) -> str | None:
    """这一页里"图号 = number"的图注行 id（侧栏/目录定位用）。

    行 id 是页内序号（``p008-03``），只有翻译产物知道；侧栏的插图条目必须带上它，
    否则"定位"按钮会拿着一个不存在的 id 找不到锚点。
    """
    if not number:
        return None
    payload = load_page_store(paper_id).page(int(page_no))
    rows = (payload or {}).get("rows") or []
    for row in rows:
        if row.get("type") == "caption" and str(row.get("number") or "") == str(number):
            return row.get("id")
    pattern = re.compile(rf"^\*{{0,2}}\s*(?:Figure|Fig\.?|Table|图|表)\s*{re.escape(str(number))}\b",
                         re.IGNORECASE)
    for row in rows:
        if pattern.match((row.get("original") or "").strip()):
            return row.get("id")
    return None


def figures_payload(paper_id: str) -> dict:
    """插图列表（**按图注锚定的裁图**，与阅读器正文里的图一致）。

    ``block_id`` / ``row_id`` 给的是**图注行的行 id**（存在时），前端"定位"直接可用；
    另带 ``page``，即使行 id 失效（重译后行号变了）也能退化为按页定位。
    """
    from backend.services.paper import figure_regions

    items = []
    for page_no, regions in sorted(figure_regions.all_regions(paper_id).items()):
        for i, r in enumerate(regions):
            if r["kind"] != "figure":
                continue
            row_id = row_id_of_caption(paper_id, page_no, r["number"])
            items.append({
                "block_id": row_id or f"fig-p{page_no:03d}-{i:02d}",
                "row_id": row_id,
                "page": page_no, "caption": r["caption"], "bbox": r["bbox"],
                "number": r["number"],
                "status": "ok", "audit": {},
                "image_url": f"/api/v1/papers/{paper_id}/figures/region/{page_no}/{i}",
            })
    return {"items": items, "count": len(items)}


# --------------------------------------------------------------------------
# 阅读进度（服务端权威，规划 §10 ⑧）
# --------------------------------------------------------------------------


VALID_VIEW_MODES = ("original", "translation", "bilingual")
# 对照方向：`en_left/zh_left`（左右对照，当前前端默认）与旧的 `en_top/zh_top`（上下对照）都接受
VALID_PAIR_ORDERS = ("en_left", "zh_left", "en_top", "zh_top")


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
