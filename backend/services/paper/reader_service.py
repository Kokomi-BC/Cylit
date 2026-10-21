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
from backend.core.utils import iso_utc, json_loads
from backend.parsing.formula.noise import is_noise_formula
from backend.repositories.mysql.models import (
    Annotation, Block, Formula, Highlight, Note, ReadingProgress, Task,
)
from backend.repositories.mysql.session import session_scope
from backend.services.paper import anchor_map, paper_service
from backend.services.translation.coords import pair_rows_by_src as coords_pair
from backend.services.translation.page_translate import (
    PATH_VERSION, PageStore, absorb_one_sided_rows, apply_semantic_breaks,
    mark_rows_continuity, normalize_breaks, page_continuity, pair_rows,
    pair_rows_by_blocks,
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
    _backfill_from_legacy(a_pages, b_pages, legacy_pages)
    out: list[dict] = []
    for page in sorted(set(a_pages) | set(b_pages)):
        merged = _merge_page(paper_id, page, a_pages.get(page) or {},
                             b_pages.get(page) or {},
                             legacy_pages.get(page) or {}, shape)
        if merged is not None:
            out.append(merged)
    _rejoin_carried_rows(out, b_pages)
    for page in out:
        align_page_breaks(page)
    return out, shape


#: 行尾两个以上空格 / 反斜杠 = Markdown **硬换行**（作者故意要换行）
_HARD_BREAK_RE = re.compile(r"(?:\s{2,}|\\)$")


def align_hard_breaks(original: str, translation: str) -> str:
    """原文行尾是**硬换行**（两个空格 / 反斜杠）时，译文照同样的分行来。

    症状（用户 2026-10-08 截图：算法 2 那一整块伪代码）：原文 12 行、每行以 `  \\n` 结尾 →
    Markdown 渲染成 12 行；B 路译文用的是普通 `\\n` —— 那是**软换行**，渲染成一个空格，
    于是伪代码被接成一整段（"1: 输入: 维度 d … 2: 按算法 1 实例化…"）。

    这是"译文与原文逐块对齐"契约的一部分，所以放在**读模型**里做（`page_products`）：
    产物不用重写、已翻译的论文立刻正常。
    **行数不一致就不动** —— 行数不同说明结构已经变了，硬对齐只会把别处弄乱。
    """
    if not original or not translation or "\n" not in original:
        return translation
    src, dst = original.split("\n"), translation.split("\n")
    if len(src) != len(dst):
        return translation
    out: list[str] = []
    for i, line in enumerate(dst):
        needs = i < len(src) - 1 and bool(_HARD_BREAK_RE.search(src[i]))
        if needs and not _HARD_BREAK_RE.search(line):
            out.append(line.rstrip() + "  ")
        else:
            out.append(line)
    return "\n".join(out)


def align_page_breaks(page: dict) -> None:
    """把一页的译文按原文的硬换行**对齐分行**（行文本与整页 Markdown 都要）。

    两个都改：正文（`rows[].translation`，对照视图渲染的就是它）与 Markdown 视图那一面
    （`translation_md`）。`original` / `original_md` 是标准，永远不动。
    """
    for row in page.get("rows") or []:
        row["translation"] = align_hard_breaks(str(row.get("original") or ""),
                                               str(row.get("translation") or ""))
    page["translation_md"] = align_hard_breaks(str(page.get("original_md") or ""),
                                               str(page.get("translation_md") or ""))


def _ws(text: str) -> str:
    """空白归一（比对 carry 片段用：产物里的换行/多空格不该影响"是不是同一段"的判断）。"""
    return " ".join(str(text or "").split())


def _rejoin_carried_rows(pages: list[dict], b_pages: dict) -> None:
    """把被页边界拆开的那一句**读时合回上一页**（只改读模型，不动产物）。

    背景：块模式 + A 路提前转录之后，B 会用下一页的开头把页尾半句收完整（`carry_md`），
    于是**下一页的第 1 块整块**已经被上一页译完了 —— 那一项是空串，直接显示就是
    "只有原文、译文空"的一行（用户口径"减少副作用"）。

    修法：把那一行的**原文**并到上一页末行的原文栏（译文本来就在上一页那一行里），
    再把这半行从本页去掉 —— 拆开的句子在读模型里重新合成一句。

    判据刻意很窄（任一条不满足就原样保留，宁可留个空栏也不乱搬文字）：
    1. 上一页的 B 产物申报了 `carry_md`；
    2. 本页第 1 行**译文为空、原文非空**；
    3. 本页第 1 行的原文确实**以那段 carry 开头**（空白归一后比对）；
    4. 本页不止这一行（否则整页会被清空）。
    """
    by_page = {int(p["page"]): p for p in pages}
    for page_no in sorted(by_page):
        cur = by_page[page_no]
        rows = cur.get("rows") or []
        prev = by_page.get(page_no - 1)
        if not prev or len(rows) < 2:
            continue
        carry = _ws((b_pages.get(page_no - 1) or {}).get("carry_md") or "")
        if not carry:
            continue
        first = rows[0]
        if (first.get("translation") or "").strip() or not (first.get("original") or "").strip():
            continue
        if not _ws(first["original"]).startswith(carry):
            continue
        prev_rows = prev.get("rows") or []
        if not prev_rows:
            continue
        tail = prev_rows[-1]
        tail["original"] = f"{tail.get('original') or ''} {first['original']}".strip()
        cur["rows"] = rows[1:]
        for i, row in enumerate(cur["rows"]):
            row["id"] = f"p{int(page_no):03d}-{i:02d}"
            row["index"] = i
        cur["one_sided"] = sum(
            1 for r in cur["rows"]
            if bool((r.get("original") or "").strip())
            != bool((r.get("translation") or "").strip()))


def merged_page(paper_id: str, page: int) -> dict | None:
    """**单页**的合并读模型（与 `page_products` 的同一页逐字段一致）。

    为什么要单独给一页：两路并发时 `page_translated` / `page_transcribed` 是**逐页**上线的，
    SSE 的 payload 必须带上那一页已经合并好的 `rows` —— 单路产物里没有行，直接把
    `payload["rows"]`（空）发给前端会把那一页的行**清空**。这里复用同一套合并逻辑，
    保证"实时收到的行"和"刷新后拿到的行"完全一致。
    """
    a_store, b_store = load_path_stores(paper_id)
    a_pages, b_pages = a_store.all_pages(), b_store.all_pages()
    has_a, has_b = bool(a_pages), bool(b_pages)
    legacy_pages = load_page_store(paper_id).all_pages()
    if not (has_a or has_b or legacy_pages):
        return None
    shape = "paths" if (has_a and has_b) else ("mixed" if (has_a or has_b) else "legacy")
    _backfill_from_legacy(a_pages, b_pages, legacy_pages)
    page = int(page)
    if page not in a_pages and page not in b_pages:
        return None
    merged = _merge_page(paper_id, page, a_pages.get(page) or {},
                         b_pages.get(page) or {}, legacy_pages.get(page) or {}, shape)
    if merged is not None and page > 1:
        # 单页也要与整篇口径一致：上一页可能在页尾把这页的第 1 块收完了（见 `_rejoin_carried_rows`）
        prev = _merge_page(paper_id, page - 1, a_pages.get(page - 1) or {},
                           b_pages.get(page - 1) or {}, legacy_pages.get(page - 1) or {},
                           shape)
        _rejoin_carried_rows([p for p in (prev, merged) if p], b_pages)
    return merged


def _backfill_from_legacy(a_pages: dict, b_pages: dict, legacy_pages: dict) -> None:
    """旧产物补齐缺失的一侧：legacy = 两侧都补；mixed = 只补没跑的那一路。

    用 `setdefault`：**任何情况下都不覆盖两路自己的产物**。
    """
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


def _merge_page(paper_id: str, page: int, a: dict, b: dict, stored: dict,
                shape: str) -> dict | None:
    """把**一页**的两侧产物合并成读模型；两侧都空时返回 None（不产生一页）。"""
    original_md = str(a.get("original_md") or "")
    translation_md = str(b.get("translation_md") or "")
    if not original_md and not translation_md:
        return None                        # 两侧都空的页不产生一页（与旧 done 判据一致）
    fresh_rows, quality = pair_rows(original_md, translation_md, page)
    # **块模式**（A 定块、B 照译）：B 的产物带着"与 A 的块清单一一对应"的逐块译文数组，
    # 这时配对是**生成时约定好的**（第 i 项 ↔ A 第 i 块），比事后按结构/坐标猜都硬 ——
    # 所以它排在最前。块数不等（模型自己并/拆了块）→ 退回 `pair_rows`。
    blocks_b = b.get("blocks") if isinstance(b, dict) else None
    if b.get("blocks_from_a") and isinstance(blocks_b, list) and blocks_b:
        block_rows, block_quality = pair_rows_by_blocks(
            original_md, blocks_b, page,
            break_flags=b.get("break_blocks") if isinstance(b.get("break_blocks"), list) else None)
        if block_rows:
            fresh_rows, quality = block_rows, block_quality
    # P4b 共享坐标：两侧都带合法的 src（页级契约）时，**按解析器块号配对**——
    # 这是唯一能表示"一侧并成一块、另一侧拆成两块"的做法（合成一行，不错位、不留空栏）。
    # 任一侧缺 src → 退回 `pair_rows`（结构 + 跨语言标记差分），行为与改造前一致。
    src_a = a.get("src") if isinstance(a, dict) else None
    src_b = b.get("src") if isinstance(b, dict) else None
    if src_a and src_b:
        coord_rows, coord_quality = coords_pair(original_md, src_a, translation_md, src_b, page)
        if coord_rows:
            fresh_rows, quality = coord_rows, coord_quality
    rows = [dict(r) for r in fresh_rows]
    used_stored = False
    if shape != "paths" and stored.get("rows"):
        keep_stored = not (
            fresh_rows
            and (str(stored.get("align") or "") == "structure"
                 or len(fresh_rows) == len(stored.get("rows") or [])))
        if keep_stored:
            rows = [dict(r) for r in stored["rows"]]
            used_stored = True
    # 单侧行并进上一行的缺失侧：把"一侧把相邻两段并成了一块"这种分歧归一，
    # 否则阅读器会出现"这一行英文栏是空的"（实测两篇各 1~2 行，且**两轮prompt都没消掉**）。
    # 放在 `mark_rows_continuity` / `apply_semantic_breaks` **之前**：两者都按最终行重算。
    absorbed = absorb_one_sided_rows(rows, page)
    rows = absorbed["rows"]
    if rows:
        flags = page_continuity(paper_id, page)
        mark_rows_continuity(rows, head=flags["head"], tail=flags["tail"])
    # 两路锚点合成**一次**校验：`apply_semantic_breaks` 逐条独立判定，英文锚点落在原文侧、
    # 中文锚点落在译文侧（`_locate_anchor` 先原文后译文），并集天然成立 —— 不需要"按路存标记"。
    # `normalize_breaks` 负责把 v1 的纯字符串数组（存量产物）与 v2 的对象数组统一成同一种形状。
    breaks_a = normalize_breaks(a.get("breaks"))
    breaks_b = normalize_breaks(b.get("breaks"))
    break_stats = apply_semantic_breaks(rows, breaks_a + breaks_b)
    # 块模式的中文边界（**位置即锚点**）：必须在 `apply_semantic_breaks` **之后**翻过来 ——
    # 那个函数一上来就把所有行的标记清空（见 `pair_rows_by_blocks` 里的 `block_break`）。
    # 行可能已经被 `absorb_one_sided_rows` 折过，但 `block_break` 只落在**保留下来的那行**上。
    flagged = 0
    for row in rows:
        if row.pop("block_break", None):
            row["semantic_break"] = True
            row["semantic_break_side"] = "translation"
            flagged += 1
    if flagged:
        break_stats["flagged_rows"] = int(break_stats.get("flagged_rows") or 0) + flagged
        break_stats["by_side"]["translation"] = (
            int(break_stats["by_side"].get("translation") or 0) + flagged)
    page_total = (int(a.get("page_total") or 0) or int(b.get("page_total") or 0)
                  or int(stored.get("page_total") or 0))
    return {
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
        # 旧形态沿用存下来的质量标记（与改造前逐字一致）；两路形态用现算的合并质量。
        # ⚠️ 必须看 `used_stored`，不能只看 `stored` 有没有值：两路形态下**旧 `pages.json`
        # 通常还在**（没删），于是 `stored.get("align")` 会把上一版的 "exact" 顶上来 ——
        # 表现为"合并质量明明 structure、接口却报 exact"（实测 p1 就是这样）。
        "align": (str(stored.get("align") or "") if used_stored else "") or quality,
        "model": (a.get("model") or b.get("model") or stored.get("model") or ""),
        "updated_at": (a.get("updated_at") or b.get("updated_at")
                       or stored.get("updated_at")),
        # ---- 两路专属诊断（前端不用；对齐门限报告与回归用）----
        "source": shape,
        "sides": {"a": bool(original_md.strip()), "b": bool(translation_md.strip())},
        "one_sided": sum(1 for r in rows
                         if bool((r.get("original") or "").strip())
                         != bool((r.get("translation") or "").strip())),
    }


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
# 下载 / 打包导出（三个版本的 Markdown、原 PDF、被引用的插图）
# --------------------------------------------------------------------------

#: 下载与打包用的三个 Markdown 版本（与 Markdown 视图左上角那三个分段一一对应）
MD_EXPORT_MODES: tuple[str, ...] = ("original", "translation", "bilingual")

#: Markdown 里指向打包内插图的引用（`![图注](figures/xxx.png)`）
_FIG_REF = re.compile(r"!\[[^\]]*\]\((figures/[^)\s]+)\)")


def assemble_versions(pages: list[dict]) -> dict[str, str]:
    """把**逐页产物**拼成三个版本的整篇 Markdown（纯函数，便于单测）。

    ⚠️ **不带页标识**（用户口径 2026-09-28）：下载/打包出去的 Markdown 里不要
    ``<!-- page N -->`` 与 ``## 第 N 页`` —— 那是给人"在页面里定位"用的，落到文件里只是噪声。
    页与页之间仍留一条 ``---``，读的时候还看得出分页。阅读器里那一版照旧按页分段显示
    （页号由前端画，不在这份文本里）。

    ``pages``：``bilingual_markdown()`` 的 ``pages``（图片行已经注入）。
    """
    def join(parts: list[str]) -> str:
        kept = [p.strip() for p in parts if (p or "").strip()]
        return ("\n\n---\n\n".join(kept).strip() + "\n") if kept else ""

    # 两侧都空的页整页丢掉（否则"空页"在中英对照里会留下一个只有 `### 原文/### 译文` 的空壳）
    usable = [p for p in pages
              if str(p.get("original_md") or "").strip()
              or str(p.get("translation_md") or "").strip()]

    return {
        "original": join([str(p.get("original_md") or "") for p in usable]),
        "translation": join([str(p.get("translation_md") or "") for p in usable]),
        "bilingual": join([
            f"### 原文\n\n{p.get('original_md') or ''}\n\n### 译文\n\n{p.get('translation_md') or ''}"
            for p in usable]),
    }


def markdown_versions(paper_id: str, *, img_prefix: str) -> dict[str, str]:
    """三个版本的整篇 Markdown；``img_prefix`` 决定图片引用怎么写：

    - 单文件下载 → 拼**绝对**地址（`http://host/api/v1/...`），点开就能看图；
    - zip 打包 → ``figures``（相对 zip 内的目录），解压后**离线**也能看图。
    """
    bundle = bilingual_markdown(paper_id, write=False, api_prefix=img_prefix)
    return assemble_versions(bundle.get("pages") or [])


def export_markdown(paper_id: str, mode: str, *, img_prefix: str = "/api/v1",
                    absolute_base: str = "") -> str:
    """某一个版本的整篇 Markdown（下载用）。``mode`` 由接口层校验。

    ⚠️ ``img_prefix`` 必须是**根相对**的接口前缀（``/api/v1``），不能直接把
    ``http://host/api/v1`` 传进来：``figure_regions.markdown_refs`` 是按"前缀是不是以 / 开头"
    决定拼"接口 URL"还是"落盘相对路径"的，传绝对地址会掉进**相对路径**那一支，拼出
    ``http://host/api/v1/region-p0003-….png`` 这种不存在的地址（实测踩过）。

    要绝对地址就传 ``absolute_base``（如 ``http://127.0.0.1:8710``）：下载下来的 .md
    在本地编辑器里打开时，根相对路径会被解析成 ``file:///api/v1/...`` → 图全 404。
    """
    versions = markdown_versions(paper_id, img_prefix=img_prefix)
    text = versions.get(mode) or versions["bilingual"]
    if absolute_base:
        text = text.replace(f"]({img_prefix}/", f"]({absolute_base}{img_prefix}/")
    return text


def export_bundle(paper_id: str) -> Path:
    """把「原 PDF + 三个版本的 Markdown + 被 Markdown 引用的插图」打成 zip，返回临时文件路径。

    包内结构（**解压即用**，Markdown 里的图片是相对路径，不依赖本服务在跑）::

        <paper_id>/source.pdf
        <paper_id>/original.md      仅原文（英文转录）
        <paper_id>/translation.md   仅译文（中文）
        <paper_id>/bilingual.md     中英对照
        <paper_id>/figures/*.png    上面三份 Markdown 引用到的插图（600dpi 按需裁图）
        <paper_id>/README.txt       这份清单

    调用方（接口层）负责在响应发完后删掉这个临时文件。
    """
    import os
    import tempfile
    import zipfile

    pdf = paper_service.resolve_pdf(paper_id)          # 没有 PDF 直接报错（不该静默给个空包）
    paper = paper_service.get_paper(paper_id)
    versions = markdown_versions(paper_id, img_prefix="figures")
    figures_dir: Path = paper_service.paper_paths(paper_id)["figures"]
    # 只打**被引用到的**图：`markdown_refs` 已保证它们落盘（`inject_images` 用的就是这些引用）
    refs = sorted({m for text in versions.values() for m in _FIG_REF.findall(text)})

    fd, tmp = tempfile.mkstemp(prefix=f"{paper_id}-export-", suffix=".zip")
    os.close(fd)
    target = Path(tmp)
    missing: list[str] = []
    # 有产物的版本才写文件：一篇还没翻译的论文（A/B 路都没跑）会出现空 md，
    # 与其往包里塞三个空文件，不如不写、让 README 说清楚（前端也会先提示一句）。
    modes = [m for m in MD_EXPORT_MODES if versions[m].strip()]
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(pdf, f"{paper_id}/source.pdf")
            for mode in modes:
                zf.writestr(f"{paper_id}/{mode}.md", versions[mode])
            for rel in refs:                            # rel 形如 figures/<文件名>.png
                src = figures_dir / Path(rel).name
                if src.is_file():
                    zf.write(src, f"{paper_id}/{rel}")
                else:
                    missing.append(rel)
            zf.writestr(f"{paper_id}/README.txt",
                        _bundle_readme(paper, modes, missing, bool(refs)))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    log.info("打包导出完成：%s（%d 张插图，%d bytes）",
             paper_id, len(refs) - len(missing), target.stat().st_size)
    return target


def _bundle_readme(paper: dict, modes: list[str], missing: list[str],
                   has_figures: bool) -> str:
    """zip 里的清单（纯说明文件，不是论文内容）。"""
    title = str(paper.get("title") or "").strip() or str(paper.get("id") or "")
    labels = {
        "original": "original.md     仅原文（英文转录）",
        "translation": "translation.md  仅译文（中文）",
        "bilingual": "bilingual.md    中英对照（逐页：原文在上、译文在下）",
    }
    lines = [title, f"paper_id: {paper.get('id') or ''}", "",
             "source.pdf      原 PDF"]
    lines += [labels[m] for m in modes]
    if missing_modes := [m for m in MD_EXPORT_MODES if m not in modes]:
        lines += ["",
                  "⚠️ 这篇论文还没有页级产物（A 路英文转录 / B 路中文翻译），"
                  "这些版本没生成：",
                  *[f"  - {m}.md" for m in missing_modes],
                  "（先在阅读器里跑「继续翻译」，再重新打包）"]
    lines += ["figures/        上面这些 Markdown 引用到的插图（600dpi 裁图）"] if has_figures else []
    lines += ["", "图片引用是相对路径，整个目录一起移动/解压即可离线查看。"]
    if missing:
        lines += ["", f"⚠️ 有 {len(missing)} 张插图没能在磁盘上找到，已跳过：",
                  *[f"  - {m}" for m in missing[:20]]]
    return "\n".join(lines) + "\n"


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
    # 锚点 → 页码：批注/笔记/高亮存的是行 id（当前产物）或历史的 block id，
    # 前端"定位"要能在行 id 失效时退化为按页跳转。
    row_pages: dict[str, int] = {}
    for page_payload in page_payloads:
        for row in page_payload.get("rows") or []:
            if row.get("id"):
                row_pages[row["id"]] = int(page_payload["page"])
    block_pages = {b["id"]: int(b.get("page") or 1)
                   for b in (index.get("blocks") or [])}
    # 「这条笔记挂在哪一面」的口径**只有一处**（`annotation_service.note_scope`）：
    # 阅读器 payload 与 `/notes`、`/highlights` 必须给同一个值，前端才配得上高亮 ↔ 笔记。
    from backend.services.annotation.annotation_service import note_scope as _note_scope

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

        #: 块锚点 → 正文行 markdown。给"原文是占位符"的笔记/高亮兜底用：
        #: PDF 里选中**公式 / 图片块**时，浏览器交出来的不是文字，而是一枚 U+FFFC（对象替换符），
        #: 侧栏那条"高亮原文"于是只剩一个看不懂的占位字形（用户 2026-11-26 报的"显示成 obj"）。
        #: 这里把那一行真正的 markdown（公式的 LaTeX / 图注）带出去 —— 前端只拿它**显示**，
        #: 不参与定位：`text` 与偏移仍是原来的那一份，划词高亮的绘制逻辑一点不动。
        row_md_by_id = {str(row.get("id")): str(row.get("original") or row.get("translation") or "")
                        for page in page_payloads for row in (page.get("rows") or []) if row.get("id")}

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
                             "created_at": iso_utc(a.created_at)}
 for a in annotations],
            "highlights": [{"id": h.id, "block_id": h.block_id,
                            "start": h.start_offset, "end": h.end_offset,
                            "color": h.color, "text": h.selected_text,
                            "side": h.side or "en", "scope": h.scope or "row",
                            "group_id": h.group_id,
                            # 原文是占位符时用它显示（见 row_md_by_id 的说明）
                            "quote_md": row_md_by_id.get(str(h.block_id), ""),
                            "page": _anchor_page(h.block_id, row_pages, block_pages)}
                           for h in highlights],
            "notes": [{"id": n.id, "block_id": n.block_id,
                       # ⚠️ **`highlight_id` 不能漏**：侧栏「笔记」页签靠它把笔记挂到那处高亮上
                       # （`utils/notesFeed` 按 `n.highlight_id === h.id` 归并）。漏了它，同一条内容
                       # 会被渲染成两条 —— 一条「待写」高亮 + 一条「笔记」
                       # （用户报过"产生了两条莫名的笔记"）。`scope` 同理：面/页码是那一行要显示的。
                       "highlight_id": n.highlight_id,
                       "content_md": n.content_md,
                       "selected_text": n.selected_text,
                       # 原文是占位符时用它显示（公式行 → LaTeX、图片行 → 图注）
                       "quote_md": row_md_by_id.get(str(n.block_id), ""),
                       "scope": _note_scope(n.block_id),
                       "page": _anchor_page(n.block_id, row_pages, block_pages),
                       "updated_at": iso_utc(n.updated_at)}
                      for n in notes],
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
    """锚点（行 id / 块 id / `row-<行 id>` / `blk-<块 id>` / `md:p<页号>` / `pdf:p<页号>`）
    → 页码；查不到返回 **None**（"查不到"与"第 1 页"是两个意思）。

    规则**只有一处**：`anchor_map.resolve_anchor`。这里曾经自己写了一套，只认 md/pdf，
    于是公式/表格类 chunk 的 `row-` 锚点、解析器的 `blk-` 锚点页码恒为 None
    （侧栏批注跳不到那一页）。签名与"查不到 = None"的语义保持不变。
    """
    return anchor_map.resolve_anchor(
        block_id, row_pages=row_pages, block_pages=block_pages)["page"]


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
    """公式列表：**译文优先，未翻译的页用原文（解析产物）兜底**。

    "优先且只取翻译"这一半：有页级产物（翻译/转录）的页**只**从它抽，不掺解析那一套 ——
    否则同一条公式会以两种 LaTeX 出现两次（模型给的 vs 解析器抽的，后者还常常更差）。

    "未翻译用原文兜底"这一半：没有页级产物的页，正文显示的就是**解析块**（公式以 `$$…$$`
    渲染），所以那几页的公式必须来自解析产物。以前整篇只从 Markdown 抽 → 一篇**还没翻译**
    的论文永远显示"这一篇还没有可提取的公式"，可正文里明明有（用户报的第一条就是这个）。
    兜底是**按页**的，不是整篇开关：半翻译的论文里，已译页走译文、未译页走原文，
    与用户在正文里看到的那一页完全一致。

    单字符变量（``$b$``）这类噪声照旧不进列表。
    """
    from backend.services.paper import paper_service

    products = page_translations(paper_id)
    covered = {int(p["page"]) for p in products}
    collected: list[tuple[int, int, dict]] = []      # (页, 收集序, item)
    seq = 0
    for payload in products:
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
                collected.append((page, seq, {
                    "id": f"f{page:03d}-{len(collected):04d}",
                    "page": page, "row_id": row.get("id"),
                    "latex": latex, "display": display,
                    "context": _snippet(text, latex),
                    "context_zh": _snippet(row.get("translation") or "", latex),
                    "model": payload.get("model"),
                    "source": "markdown",
                }))
                seq += 1

    # 未翻译的页：原文兜底（正文在那几页显示的也是解析块）
    fallback_pages: set[int] = set()
    for f in _parse_formulas(paper_id):
        page = int(f.page_number or 0)
        if page <= 0 or page in covered:
            continue
        item = _parse_formula_item(f)
        if item is None:
            continue
        fallback_pages.add(page)
        collected.append((page, seq, item))
        seq += 1

    collected.sort(key=lambda t: (t[0], t[1]))
    items = [it for _p, _s, it in collected]
    if not items:
        source = "markdown" if covered else "parse"
    elif fallback_pages:
        source = "parse" if not covered else "mixed"
    else:
        source = "markdown"
    return {"items": items, "count": len(items), "source": source,
            "display_count": sum(1 for i in items if i["display"]),
            "fallback_pages": sorted(fallback_pages)}


def _parse_formula_item(f: Formula) -> dict | None:
    """一条解析阶段的公式 → **与 markdown 源同形状**的列表项（没有可用 LaTeX 就返回 None）。"""
    latex = (f.latex_final or f.latex_repaired
             or next(iter(json_loads(f.latex_candidate, []) or []), "") or "").strip()
    if not latex or is_noise_formula(latex):
        return None
    crop_name = Path(f.image_path).name if f.image_path else None
    return {
        "id": f.id, "page": int(f.page_number or 0), "latex": latex,
        "display": not bool(f.is_inline),
        # 解析块的 id 就是正文里那一行的锚点：前端"定位"直接可用
        "row_id": f.id,
        "context": "", "context_zh": "",
        "grade": f.grade,
        "crop_url": (f"/papers/{f.paper_id}/formula_crops/{crop_name}"
                     if crop_name else None),
        "source": "parse",
    }


def _snippet(text: str, needle: str, width: int = 70) -> str:
    idx = (text or "").find(needle)
    if idx < 0:
        return (text or "")[:width * 2].replace("\n", " ").strip()
    start = max(0, idx - width)
    end = min(len(text), idx + len(needle) + width)
    return ("…" if start else "") + text[start:end].replace("\n", " ").strip() \
        + ("…" if end < len(text) else "")


def formulas_payload(paper_id: str, source: str = "markdown") -> dict:
    """公式列表：**译文优先，未翻译的页用原文兜底**（口径见 ``markdown_formulas``）。

    ``source=parse`` 才强制只回解析阶段的公式卡（排障用：想看"解析器当时抽成什么样"）。
    """
    if source == "parse":
        return parse_formulas_payload(paper_id)
    return markdown_formulas(paper_id)


def parse_formulas_payload(paper_id: str) -> dict:
    """只回解析阶段的公式（``?source=parse``；形状与 ``markdown_formulas`` 一致）。"""
    items = [it for f in _parse_formulas(paper_id)
             if (it := _parse_formula_item(f)) is not None]
    return {"items": items, "count": len(items), "source": "parse",
            "display_count": sum(1 for i in items if i["display"])}


def _parse_formulas(paper_id: str) -> list[Formula]:
    with session_scope() as sess:
        return (sess.query(Formula).filter(Formula.paper_id == paper_id)
                .order_by(Formula.page_number.asc()).all())


# --------------------------------------------------------------------------
# 表格列表（从页级产物里抽 Markdown 表；说明取中文译文那行图注）
# --------------------------------------------------------------------------

#: Markdown 表的**分隔行**（`|---|:--:|---|`）：有它才算一张表，光有竖线不算
#: （行内公式 `|x|`、矩阵、切分符都带竖线，不能拿竖线当判据）。
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$", re.M)
#: 表格说明的开头：`表 1：…` / `Table 2. …`（中英文都认，编号可有可无）
_TABLE_CAP = re.compile(
    r"^\s*(?:\*\*|__)?\s*(?:表|Table)\s*([0-9]{1,3}[a-zA-Z]?|[IVXivx]{1,5})\s*[:：.、]?", re.I)


def _as_table(text: str) -> str:
    """这一行的文本是不是一张 Markdown 表：是就返回去掉首尾空白的它，不是返回空串。"""
    s = (text or "").strip()
    if not s or "|" not in s or not _TABLE_SEP.search(s):
        return ""
    return s


def _table_shape(md: str) -> tuple[int, int]:
    """`(表体行数, 列数)`：行数不含表头与分隔行，列数按表头那行的单元格数。"""
    lines = [ln for ln in md.splitlines() if "|" in ln and not _TABLE_SEP.match(ln)]
    if not lines:
        return 0, 0
    return max(0, len(lines) - 1), max(1, len(lines[0].strip().strip("|").split("|")))


#: 图注开头：`图 1` / `Figure 2`（给 `type=caption` 兜底时用来**排除**图注）
_FIG_CAP = re.compile(r"^\s*(?:\*\*|__)?\s*(?:图|Figure|Fig\.?)\s*[0-9]", re.I)


def _table_captions(rows: list[dict]) -> dict[int, tuple[str, str, str]]:
    """把这一页的表和它们的说明配起来：`{表所在行下标: (中文说明, 原文说明, 表号)}`。

    配对是**贪心最近、每条说明只用一次**。为什么不是"±1 优先"硬取：论文里「表 N：…」
    **既可能排在表上方、也可能排在下方**（实测 attention 那篇 p9 就是表 3 在上、
    表 4 在下，两条紧挨），只看相邻那行会把两条说明取成同一条（四张表全叫「表 4」）。
    同距离时取上方那条：正文里表题在表上方的写法更常见。

    判据两层：先认文本以 `表 N` / `Table N` 开头的行；一条都没配上才退到 `type=caption`
    （并排除 `图 N` / `Figure N` 那种图注 —— 图注紧挨着表时会被误抢）。
    中文那半来自 `translation`（用户口径：表格说明要中文），没有译文才退回原文。
    """
    strong: list[tuple[int, str, str, str]] = []
    weak: list[tuple[int, str, str, str]] = []
    for i, row in enumerate(rows):
        zh = (row.get("translation") or "").strip()
        en = (row.get("original") or "").strip()
        m = _TABLE_CAP.match(zh) or _TABLE_CAP.match(en)
        if m:
            strong.append((i, zh or en, en, m.group(1)))
        elif row.get("type") == "caption" and not _FIG_CAP.match(zh or en):
            weak.append((i, zh or en, en, ""))
    tables = [i for i, row in enumerate(rows)
              if _as_table(row.get("translation") or "") or _as_table(row.get("original") or "")]
    used: set[int] = set()
    out: dict[int, tuple[str, str, str]] = {}
    for i in tables:
        pick = _nearest_caption(i, strong, used) or _nearest_caption(i, weak, used)
        if pick is None:
            continue
        used.add(pick[0])
        out[i] = (pick[1], pick[2], pick[3])
    return out


def _nearest_caption(i: int, cands: list[tuple[int, str, str, str]],
                     used: set[int]) -> tuple[int, str, str, str] | None:
    """离第 `i` 行最近、还没被占用的那条说明（最远跨两行；同距离取上方）。"""
    best: tuple[tuple[int, int], tuple[int, str, str, str]] | None = None
    for c in cands:
        if c[0] in used:
            continue
        dist = abs(c[0] - i)
        if dist > 2:
            continue
        key = (dist, 0 if c[0] < i else 1)
        if best is None or key < best[0]:
            best = (key, c)
    return best[1] if best else None


def markdown_tables(paper_id: str) -> dict:
    """表格列表：从**页级产物**的行里抽 Markdown 表（只读，不做复核）。

    为什么只认页级产物：那里表格才是真的 Markdown 表（`| … |` + 分隔行）。
    解析产物里的 table 块是**按坐标拼出来的纯文本**（实测一整张表的格子被连成一行，
    `KV SizeSingleQAMultiQA…`），拿它画表只会画出一团乱码 —— 所以**未翻译的页不出条目**，
    空状态由前端说明白（"要先翻译那几页"），不在这里编一条假的。

    译文优先：同一行的 `translation` 是中文表就取它（模型逐格翻译），否则退回 `original`。
    """
    products = page_translations(paper_id)
    items: list[dict] = []
    for payload in products:
        page = int(payload["page"])
        rows = payload.get("rows") or []
        captions = _table_captions(rows)
        for i, row in enumerate(rows):
            body = _as_table(row.get("translation") or "")
            source = "translation"
            if not body:
                body = _as_table(row.get("original") or "")
                source = "original"
            if not body:
                continue
            caption, caption_original, number = captions.get(i, ("", "", ""))
            row_count, col_count = _table_shape(body)
            items.append({
                "id": f"t{page:03d}-{len(items):04d}",
                "page": page,
                "row_id": row.get("id"),
                "number": number,
                "caption": caption,
                "caption_original": caption_original,
                "markdown": body,
                "rows": row_count,
                "cols": col_count,
                "source": source,
                "model": payload.get("model"),
            })
    items.sort(key=lambda it: (it["page"], it.get("row_id") or ""))
    return {"items": items, "count": len(items),
            "translated": sum(1 for it in items if it["source"] == "translation"),
            "pages": len(products)}


def tables_payload(paper_id: str) -> dict:
    """表格列表（`GET /papers/{id}/tables`；口径见 `markdown_tables`）。"""
    return markdown_tables(paper_id)


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
        # 图注补中文译文（用户口径："侧栏图片的图注取中文译文"）：同一条媒体既喂正文也喂侧栏，
        # 因此中文化在这一处做完，两处口径一致。
        for m in media:
            m["caption_zh"] = _caption_zh(m.get("caption") or "", m.get("number") or "",
                                          rows)
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
        "updated_at": iso_utc(row.updated_at),

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


#: 图注比对前要洗掉的**Markdown 噪声**：图注行在产物里通常是 `**Figure 3: …**`，
#: 而插图区域里存的是 `Figure 3: …`（没有星号）—— 不清掉永远对不上（实测踩到）。
_CAPTION_NOISE = re.compile(r"[*_`#>\s]+")


def _caption_key(text: str, length: int = 24) -> str:
    """图注比对键：去掉 Markdown 记号与空白后的前 N 个字符。"""
    return _CAPTION_NOISE.sub("", str(text or ""))[:length]


def _caption_zh(caption: str, number: str, rows: list[dict] | None = None) -> str:
    """图注行的**中文译文**（没有译文就回空串，前端回退显示英文）。

    两条匹配路径（与 row_id_of_caption 同一口径）：
    ① 图号对得上（type=caption + number）—— 翻译产物里图注行是结构化标出来的；
    ② 图号缺失（扫描件常见，连 number 都是空串）时按英文前缀对，且**先洗掉 Markdown**。
    """
    num = str(number or "").strip()
    rows = rows or []
    if num:
        for row in rows:
            zh = str(row.get("translation") or "").strip()
            if not zh or row.get("type") != "caption":
                continue
            if str(row.get("number") or "") == num:
                return zh
    want = _caption_key(caption)
    if len(want) < 12:
        return ""
    for row in rows:
        zh = str(row.get("translation") or "").strip()
        if zh and _caption_key(row.get("original"), len(want)) == want:
            return zh
    return ""


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


_CAPTION_NO = re.compile(
    r"^\*{0,2}\s*(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})\b", re.IGNORECASE)


def caption_row_ids(paper_id: str, page_no: int) -> dict[str, str]:
    """这一页「图号 → 图注行的行 id」。

    引用登记要用它：前端点 ``[n]`` 时靠行 id 才能"闪一下那一行"，而插图引用以前只带页码，
    点过去只滚到位、什么都不亮。行 id 只有翻译产物知道（页内序号 ``p010-03``）。
    这里**一页只装一次行**再逐图号比对 —— 逐图调 `row_id_of_caption` 会把整篇产物重装 N 遍。
    """
    rows = _rows_of_page(paper_id, int(page_no))
    out: dict[str, str] = {}
    for row in rows:
        rid = str(row.get("id") or "")
        if not rid:
            continue
        number = str(row.get("number") or "").strip()
        if not number:
            # 图号经常没被结构化抽出来（实测 TurboQuant 的 p010-00：type=caption 但 number 空），
            # 那就从图注正文首行取 —— 与 `row_id_of_caption` 的兜底正则同一口径。
            hit = _CAPTION_NO.match(str(row.get("original") or "").strip())
            number = hit.group(1) if hit else ""
        if number:
            out.setdefault(number, rid)
    return out


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
    ⚠️ 导入时把「图片是否包含图注」关掉（**默认**）的论文，``card=1`` 由后端退化成纯图
    （见 ``figure_regions.ensure_region_crop``）：两个 URL 仍指向**同一张**，这条口径不破。
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
            # `v=<区域哈希>`：裁图接口允许浏览器缓存 7 天，而 URL 只看"页+序号" ——
            # 区域一变不加哈希就会一直显示旧图（实测事故，见 figure_regions.media_of_page）。
            # 版本串里还编进**图注开关**（`_ver`）：关掉「图片是否包含图注」时 `card=1`
            # 返回的是纯图，URL 必须跟着变，否则浏览器还是拿那张带图注的旧卡片。
            ver = figure_regions.region_version(r, paper_id)
            items.append({
                "block_id": row_id or f"fig-p{page_no:03d}-{i:02d}",
                "row_id": row_id,
                "page": page_no, "caption": r["caption"], "bbox": r["bbox"],
                # 侧栏的图注走中文译文（与正文里的媒体条目同一套规则）
                "caption_zh": _caption_zh(r["caption"], r["number"],
                                          rows_by_page.get(int(page_no))),
                "number": r["number"],
                "part": int(r.get("part", 0)), "parts": int(r.get("parts", 1)),
                "part_label": r.get("part_label") or "",
                "status": "ok", "audit": {},
                "image_url": f"{base}?card=1" + (f"&v={ver}" if ver else ""),
                "card_url": f"{base}?card=1" + (f"&v={ver}" if ver else ""),
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
