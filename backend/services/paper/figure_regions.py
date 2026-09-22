"""插图区域：**按图注/标题锚定**收紧解析阶段给出的插图 bbox。

为什么需要它
------------
解析阶段检测插图区域时会"合并"：TurboQuant p8 的 Figure 3 记录 bbox 从 y=95.8 一直到
y=535.0，把**上面的 Figure 2 和中间的小标题**都吃了进来，裁出来就是"同一张图出现两次、
图里夹着正文"，完全不像论文里的插图。

规则（论文自己的排法）
----------------------
- 图注在图的**下方**（Figure）→ 下边界 = 图注顶边；上边界取以下三者的最大值：
  图块自身 y0、同页上一张图的已用下边界、图注上方最近的**标题/图注**的底边。
  中间夹着的标题（如 ``2.2 NEEDLE-IN-A-HAYSTACK``）因此被排除在图外。
- 题注在表的**上方**（Table）→ 上边界 = 题注底边；下边界取表块 y1 与下方最近标题/图注的顶边。
- 收紧后若高度过小（< 24pt），退回原始 bbox（宁可多裁一点也不要裁空）。

裁图按需渲染（600dpi，只渲染 clip 区域）并落盘缓存；缓存文件名带 bbox 哈希，
重解析后 bbox 变了会重新裁。
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

from backend.core.errors import NotFoundError
from backend.core.logging import get_logger
from backend.parsing.models import Rect

log = get_logger(__name__)

NUMBER_RE = re.compile(r"(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})", re.IGNORECASE)
STRUCT_TYPES = ("caption", "heading")     # 结构分隔元素：图注与标题
SEP_PAD = 6.0        # 分隔元素（标题/图注）要往外让开一点：字形墨迹会超出文本框
CROP_DPI = 600
MIN_HEIGHT = 24.0
TOL = 1.0

_lock = threading.Lock()
# {paper_id: (ir_mtime, {page: [regions]})}
_cache: dict[str, tuple[float, dict[int, list[dict]]]] = {}


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bbox(row: dict) -> list[float]:
    return [_num(row.get("x0")), _num(row.get("y0")),
            _num(row.get("x1")), _num(row.get("y1"))]


def _number_of(text: str) -> str:
    m = NUMBER_RE.search(text or "")
    return m.group(1) if m else ""


def _tighten(bbox: list[float], caption: dict | None, structs: list[dict],
             page_bottom: float, *, below: bool) -> list[float]:
    """把插图 bbox 收到"图注 + 结构分隔"之间。"""
    x0, y0, x1, y1 = bbox
    if caption is None:
        return bbox
    cy0, cy1 = _num(caption.get("y0")), _num(caption.get("y1"))

    if below:      # 图注在图下方：下边界 = 图注顶边；上边界排除夹在中间的标题/图注
        boundary = cy0 if cy0 - y0 >= MIN_HEIGHT else y1
        seps = [_num(s.get("y1")) + SEP_PAD for s in structs
                if _num(s.get("y1")) <= cy0 - TOL]
        top = max([y0] + seps[-1:])
        if boundary - top < MIN_HEIGHT:
            return bbox
        return [x0, top, x1, boundary]

    # 题注在表上方：上边界 = 题注底边；下边界 = 下方最近的标题/图注顶边
    boundary = cy1 if y1 - cy1 >= MIN_HEIGHT else y0
    top = max(y0, boundary)
    seps = [_num(s.get("y0")) - SEP_PAD for s in structs
            if _num(s.get("y0")) >= cy1 + TOL]
    bottom = min([y1] + seps[:1])
    if bottom - top < MIN_HEIGHT:
        return bbox
    return [x0, top, x1, bottom]


def _compute_all(paper_id: str) -> dict[int, list[dict]]:
    """一次读库算完整篇（按页分组）。"""
    from backend.parsing.ir.ir_reader import IRReader
    from backend.services.paper import paper_service

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    if not ir.exists:
        return {}
    pages = {int(p["page_no"]): p for p in ir.pages()}
    blocks_by_page: dict[int, list[dict]] = {}
    for b in ir.blocks():
        blocks_by_page.setdefault(int(b["page_no"]), []).append(b)
    figures_by_page: dict[int, list[dict]] = {}
    for f in ir.figures():
        figures_by_page.setdefault(int(f["page_number"]), []).append({**f, "_kind": "figure"})
    tables_by_page: dict[int, list[dict]] = {}
    for t in ir.tables():
        tables_by_page.setdefault(int(t["page_number"]), []).append({**t, "_kind": "table"})

    out: dict[int, list[dict]] = {}
    for page_no, page in pages.items():
        blocks = blocks_by_page.get(page_no, [])
        if not blocks:
            continue
        by_id = {b["id"]: b for b in blocks}
        structs = sorted([b for b in blocks
                          if (b.get("type") or "") in STRUCT_TYPES],
                         key=lambda b: _num(b.get("y0")))
        page_bottom = _num(page.get("height")) or 842.0
        rows = sorted(figures_by_page.get(page_no, []) + tables_by_page.get(page_no, []),
                      key=lambda r: _num(r.get("y0")))
        regions: list[dict] = []
        prev_bottom: float | None = None
        for row in rows:
            caption = by_id.get(row.get("caption_block_id") or "")
            if caption is None and row.get("caption"):
                head = str(row["caption"])[:40]
                caption = next((b for b in blocks
                                if str(b.get("text_clean") or b.get("text") or "")
                                .strip().startswith(head)), None)
            bbox = _bbox(row)
            below = row["_kind"] == "figure"
            seps = list(structs)
            if below and prev_bottom is not None:
                seps = seps + [{"y0": prev_bottom, "y1": prev_bottom}]
            tight = _tighten(bbox, caption, sorted(seps, key=lambda b: _num(b.get("y0"))),
                             page_bottom, below=below)
            cap_text = str(row.get("caption") or (caption or {}).get("text_clean") or "").strip()
            regions.append({
                "page": page_no, "kind": row["_kind"],
                "number": _number_of(cap_text), "caption": cap_text,
                "bbox": [round(v, 2) for v in tight],
                "raw_bbox": [round(v, 2) for v in bbox],
                "hash": hashlib.sha256(
                    repr([round(v, 1) for v in tight]).encode()).hexdigest()[:8],
            })
            if below:
                prev_bottom = max(_num(tight[3]) - 6.0, 0.0)
        out[page_no] = regions
    return out


def all_regions(paper_id: str, *, force: bool = False) -> dict[int, list[dict]]:
    """全篇插图区域（按 IR 文件 mtime 缓存，避免每个请求反复读库）。"""
    from backend.services.paper import paper_service

    ir_path: Path = paper_service.paper_paths(paper_id)["ir"]
    if not ir_path.is_file():
        return {}
    mtime = ir_path.stat().st_mtime
    with _lock:
        cached = _cache.get(paper_id)
        if cached and not force and abs(cached[0] - mtime) < 1e-6:
            return cached[1]
    data = _compute_all(paper_id)
    with _lock:
        _cache[paper_id] = (mtime, data)
    return data


def page_regions(paper_id: str, page_no: int) -> list[dict]:
    """该页插图区域（按文档顺序；表格也返回，kind=table）。"""
    return all_regions(paper_id).get(int(page_no), [])


def invalidate_cache(paper_id: str | None = None) -> None:
    with _lock:
        if paper_id is None:
            _cache.clear()
        else:
            _cache.pop(paper_id, None)


def region_crop_path(paper_id: str, page_no: int, index: int) -> Path:
    """缓存文件路径：``figures/region-p0008-00-ab12cd34.png``（含 bbox 哈希）。"""
    from backend.services.paper import paper_service

    regions = page_regions(paper_id, page_no)
    if index < 0 or index >= len(regions):
        raise NotFoundError(f"插图区域不存在：p{page_no} #{index}")
    figures_dir: Path = paper_service.paper_paths(paper_id)["figures"]
    return figures_dir / (f"region-p{int(page_no):04d}-{index:02d}"
                          f"-{regions[index]['hash']}.png")


def ensure_region_crop(paper_id: str, page_no: int, index: int) -> Path:
    """按需渲染并缓存该区域的裁图（只渲染 clip 区域，600dpi）。"""
    from backend.core.utils import write_bytes_atomic
    from backend.parsing.extract.render import render_crop_png
    from backend.services.paper import paper_service

    target = region_crop_path(paper_id, page_no, index)
    if target.is_file() and target.stat().st_size > 0:
        return target
    region = page_regions(paper_id, page_no)[index]
    x0, y0, x1, y1 = region["bbox"]
    pdf = paper_service.resolve_pdf(paper_id)
    with _lock:
        if target.is_file() and target.stat().st_size > 0:
            return target
        import pymupdf as fitz

        with fitz.open(str(pdf)) as doc:
            data = render_crop_png(doc, int(page_no), Rect(x0, y0, x1, y1),
                                   dpi=CROP_DPI, padding=3.0)
        write_bytes_atomic(target, data)
    log.info("插图裁图已生成：p%d #%d（%s %s）%d bytes",
             page_no, index, region["kind"], region["number"], target.stat().st_size)
    return target


def media_of_page(paper_id: str, page_no: int, *, figures_only: bool = True) -> list[dict]:
    """阅读器/侧栏用的插图条目（url 指向按需裁图接口）。"""
    out: list[dict] = []
    for i, r in enumerate(page_regions(paper_id, page_no)):
        if figures_only and r["kind"] != "figure":
            continue
        out.append({
            "kind": r["kind"], "number": r["number"], "caption": r["caption"],
            "bbox": r["bbox"],
            "url": f"/api/v1/papers/{paper_id}/figures/region/{int(page_no)}/{i}",
            "status": "ok",
        })
    return out


# --------------------------------------------------------------------------
# Markdown 图片引用：让插图**出现在 Markdown 本体里**
# --------------------------------------------------------------------------

# 图注行：整行就是图注（可带 ** 加粗）
_MD_CAPTION = re.compile(
    r"^\s*(?:\*\*)?\s*(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})\b", re.IGNORECASE)


def markdown_refs(paper_id: str, page_no: int, *, prefix: str) -> dict[str, str]:
    """该页每个图号对应的 Markdown 图片行，并**确保裁图已落盘**。

    ``prefix`` 决定引用形式：
    - 以 ``/`` 开头（如 ``/api/v1``）→ ``/api/v1/papers/<id>/figures/region/<page>/<i>``，
      应用内渲染用（浏览器能取到）；
    - 其它（如 ``../figures``）→ 相对路径，落盘 Markdown 用（在 md/ 目录下打开可用）。
    """
    refs: dict[str, str] = {}
    for i, r in enumerate(page_regions(paper_id, page_no)):
        if r["kind"] != "figure" or not r["number"]:
            continue
        try:
            path = ensure_region_crop(paper_id, page_no, i)
        except Exception as exc:  # noqa: BLE001
            log.warning("裁图渲染失败，Markdown 里跳过该图：p%d #%d %s", page_no, i, exc)
            continue
        if prefix.startswith("/"):
            base = f"{prefix.rstrip('/')}/papers/{paper_id}/figures/region/{int(page_no)}/{i}"
        else:
            base = f"{prefix.rstrip('/')}/{path.name}"
        refs[r["number"]] = f"![{r['caption'][:80] or f'Figure {r[number]}'}]({base})"
    return refs


def inject_images(md: str, refs: dict[str, str]) -> str:
    """把图片行插到**对应图号的图注行上方**（论文排法：图在上、图注在下）。

    已经带 ``![`` 的行不再重复插入；匹配不到的图放到末尾，绝不丢图。
    """
    text = (md or "").strip()
    if not text or not refs:
        return text
    if "![" in text:                    # 模型自己给了图片引用就尊重它
        return text
    blocks = re.split(r"\n\s*\n", text)
    used: set[str] = set()
    out: list[str] = []
    for block in blocks:
        m = _MD_CAPTION.match(block.strip())
        if m and m.group(1) in refs and m.group(1) not in used:
            used.add(m.group(1))
            out.append(refs[m.group(1)])      # 图在上
        out.append(block)                     # 图注在下
    for number, ref in refs.items():
        if number not in used:
            out.append(ref)
    return "\n\n".join(out).strip()
