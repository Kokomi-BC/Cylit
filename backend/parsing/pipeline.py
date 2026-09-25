"""解析流水线（规划 §21.2 流程 2：PARSE 的完整实现）。

```
PARSE
 1 render_pages(150dpi) → pages/*.png
 2 extract_text/vector/image → 中间 JSON
 3 reading_order（双栏）→ paragraph → formula/figure/table 块
 4 formula: 候选 + 矢量层审计 → grade（LLM 修复通过注入的钩子，可选）
 5 figure: 裁剪 + 体检
 6 写 IR（构建期产物）+ 生成 Markdown 骨架（占位译文）
 7 （MySQL 回写由 service 层负责，本模块不依赖业务库）
```

本模块**不访问 MySQL / Service**，保证 CLI 可独立运行（规划 §6.1）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from backend.core.errors import ParseError
from backend.core.logging import get_logger
from backend.core.utils import Timer, json_dumps, new_id, normalize_text, write_json
from backend.parsing.extract.image_layer import extract_images
from backend.parsing.extract.render import (
    ink_bbox, ink_fraction, render_crop_png, render_page_png, render_pages,
)
from backend.parsing.extract.text_layer import extract_page_blocks, page_has_text_layer
from backend.parsing.extract.vector_layer import extract_drawings
from backend.parsing.formula.audit import audit_formula_vectors
from backend.parsing.formula.latex_recover import recover_candidates
from backend.parsing.formula.noise import is_noise_formula
from backend.parsing.formula.validate import grade_formula, validate_formula
from backend.parsing.ir.ir_writer import IRWriter
from backend.parsing.metadata import extract_paper_meta, extract_metadata, meta_from_text
from backend.parsing.ir.rebind import (
    assign_ids_in_order, id_prefix, make_block_id, rebind_block_ids,
)
from backend.parsing.md.generator import (
    extract_translations, render_markdown, upsert_translation, write_markdown,
    write_markdown_bundle,
)
from backend.parsing.models import (
    Block, FigureBlock, FormulaBlock, PageInfo, ParseResult, Rect,
    TableBlock,
)
from backend.parsing.ocr.vision_reader import (
    PageRead, blocks_from_page_markdown, looks_scanned, title_from_markdown,
)
from backend.parsing.qa.consistency import run_consistency_checks
from backend.parsing.qa.figure_audit import audit_figure_crop
from backend.parsing.qa.report import build_quality_report
from backend.parsing.structure.figure_cut import (
    caption_number, detect_figures, detect_tables,
)
from backend.parsing.structure.ink_layout import PageInk, locate_figure
from backend.parsing.structure.formula_block import detect_formula_candidates
from backend.parsing.structure.paragraph import (
    LineProfile, build_blocks_from_groups, build_line_profile,
    detect_boilerplate, group_lines_into_paragraphs, is_body_line,
    is_in_margin_band,
)
from backend.parsing.structure.reading_order import (
    detect_layout, order_page_blocks, order_page_lines, self_check_order,
)

log = get_logger(__name__)

# 公式修复钩子签名：evidence dict → {repaired_latex, reason, evidence_used, uncertainties}
RepairFn = Callable[[dict], dict]
# 扫描件读页钩子签名：(页号, 页图 PNG 字节) → 该页 Markdown（或带版面块的 PageRead）
VisionReadFn = Callable[[int, bytes], "str | PageRead"]

# --------------------------------------------------------------------------
# 扫描页插图：模型提案 → 像素定边界 的判据
#
# 这些阈值只服务于"要不要把这张裁图放进 Markdown"这一个决定：
# 模型框偏差几个百分点没关系（后面会贴到墨迹上），但**框得离谱就一张都不出**。
# --------------------------------------------------------------------------
SCAN_MIN_AREA_RATIO = 0.01      # 小于 1% 页面的框不是插图（印章/logo/水印）
SCAN_MAX_AREA_RATIO = 0.95      # 大于 95% 的框是"整页"，说明模型没定位到图体
SCAN_MIN_HEIGHT = 24.0          # pt：低于此高度的"图"没有意义（多半是裁到一行字）
SCAN_MIN_WIDTH = 24.0
SCAN_MIN_PROPOSAL_IOU = 0.20    # 墨迹细化后与模型提案的 IoU 下限（差太远 = 提案不可信）
SCAN_MIN_INK = 0.01             # 框内非白像素占比下限（低于它 = 框落在空白处）
SCAN_INK_PAD = 1.5              # 墨迹外扩的呼吸位（pt）


class _ScanCand:
    """给 :func:`_make_orphan_block` 用的最小候选对象（它只读 page_no / rect）。"""

    __slots__ = ("page_no", "rect", "lines", "raw_text")

    def __init__(self, page_no: int, rect: Rect) -> None:
        self.page_no = page_no
        self.rect = rect
        self.lines: list = []
        self.raw_text = ""


def _point_box(box, page_rect: Rect) -> Rect | None:
    """归一化框（0~1）→ 页面 pt 矩形；越界裁剪、上下/左右写反自动纠正，过小返回 ``None``。"""
    if not box:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    out = Rect(page_rect.x0 + x0 * page_rect.width,
               page_rect.y0 + y0 * page_rect.height,
               page_rect.x0 + x1 * page_rect.width,
               page_rect.y0 + y1 * page_rect.height)
    out = Rect(max(out.x0, page_rect.x0), max(out.y0, page_rect.y0),
               min(out.x1, page_rect.x1), min(out.y1, page_rect.y1))
    if out.width <= 1 or out.height <= 1:
        return None
    return out


def _box_iou(a: Rect, b: Rect) -> float:
    ix = min(a.x1, b.x1) - max(a.x0, b.x0)
    iy = min(a.y1, b.y1) - max(a.y0, b.y0)
    if ix <= 0 or iy <= 0:
        return 0.0
    inter = ix * iy
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def _scan_crop_dpi(crop_dpi: int, src_dpi: float) -> int:
    """扫描件裁图 dpi 上限：源只有 200dpi 时按 600dpi 渲染只是 3 倍上采样。

    源 dpi 未知（没有位图层信息）时退回调用方给的 ``crop_dpi``。
    """
    if src_dpi and src_dpi > 0:
        return int(max(72.0, min(float(crop_dpi), src_dpi * 1.2)))
    return crop_dpi


def _find_caption_block_by_number(blocks: list[Block], page_no: int,
                                  number: str) -> Block | None:
    """按**图号**找该页的图注块。

    扫描页不能用 bbox 重叠找宿主（块的 rect 是等分虚拟横带，必然"重叠"），
    只能用图号——这也正是阅读器挂图用的口径（图号 ↔ 图注行）。
    """
    if not number:
        return None
    for b in blocks:
        if b.page_no != page_no or b.type != "caption":
            continue
        if caption_number(b.text_clean or b.text or "") == number:
            return b
    return None


def _scan_caption_text(blocks: list[Block], page_no: int, number: str) -> str:
    """该图号的**完整图注文本**：同一图号的所有图注块按序接起来（空白压成单空格）。

    为什么要接：视觉模型把一条长图注写成两个段落（或我们的切块器在空行处切断了）时，
    只取第一块会让阅读器/侧栏/Markdown 的图注显示半句。按图号取还能避免把
    相邻那张图的图注吃进来（p8 的 Figure 2 与 Figure 3 就紧挨着）。

    **必须压掉换行**：这段文字会进 ``![alt](url)`` 的 alt 位置，带换行会把 Markdown
    图片行截断成两行 —— 图就显示不出来了。
    """
    if not number:
        return ""
    parts = [(b.order_global, (b.text_clean or b.text or "").strip())
             for b in blocks
             if b.page_no == page_no and b.type == "caption"
             and caption_number(b.text_clean or b.text or "") == number]
    joined = " ".join(text for _o, text in sorted(parts) if text)
    return re.sub(r"\s+", " ", joined).strip()


def _usable_caption_box(box: Rect, page_rect: Rect) -> bool:
    """模型给的"整段图注"框可不可信。

    图注一定在正文栏内、且是一小段文字：宽度不能太窄，高度不能超过小半页
    （超了说明模型把下面的正文一起框进来了，拿它当边界会把图切没）。
    """
    if box.width < 0.2 * page_rect.width:
        return False
    return box.height <= 0.25 * page_rect.height


class PaperBuilder:
    """单篇论文的解析构建器。"""

    def __init__(
        self,
        pdf_path: str | Path,
        paper_id: str,
        paper_dir: str | Path,
        *,
        dpi: int = 150,
        crop_dpi: int = 600,
        max_pages: int = 0,
        repair_fn: RepairFn | None = None,
        use_llm_repair: bool = False,
        vision_fn: VisionReadFn | None = None,
        vision_dpi: int = 200,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.paper_id = paper_id
        self.dir = Path(paper_dir)
        self.dpi = dpi
        self.crop_dpi = crop_dpi
        self.max_pages = max_pages
        self.repair_fn = repair_fn
        self.use_llm_repair = use_llm_repair and repair_fn is not None
        # 扫描件兜底：没有文本层时用视觉模型读页（由服务层注入，parsing 不依赖 services）
        self.vision_fn = vision_fn
        self.vision_dpi = vision_dpi
        self.progress = progress

        self.pages_dir = self.dir / "pages"
        self.figures_dir = self.dir / "figures"
        self.crops_dir = self.dir / "formula_crops"
        self.md_dir = self.dir / "md"
        self.reports_dir = self.dir / "reports"
        self.ir_path = self.dir / "ir.sqlite"
        # 块 ID 带论文前缀，保证跨论文在共享业务库（MySQL block 主键）中唯一
        self.id_prefix = id_prefix(paper_id)

    # ------------------------------------------------------------------
    def _emit(self, stage: str, done: int, total: int) -> None:
        if self.progress:
            try:
                self.progress(stage, done, total)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def _read_scanned_pages(self, doc, page_numbers, rendered: dict,
                            result: ParseResult) -> dict[int, PageRead]:
        """没有文本层的页 → 视觉模型整页识别，返回 ``{页号: PageRead}``。

        设计要点：
        - **只识别不翻译**：这一步产出"原文事实源"，翻译走后面的既有链路；
        - 读页用**更高 dpi**（默认 200）另渲一张图：150dpi 的阅读器页图
          对小字号上下标偏糊，OCR 会丢符号（1 次额外渲染，成本可忽略）；
        - 同一份模型输出里**顺带取插图粗定位**（``PageRead.figures``）：影印页上除它
          没有任何可用的版面坐标，插图裁剪只能靠这份"图号 → 大致位置"起步；
        - 没有注入 ``vision_fn`` 时只告警不报错：离线环境（mock）也要能跑完，
          页面其余部分照常解析。
        """
        if not self.vision_fn:
            scanned = [p for p in page_numbers
                       if looks_scanned(doc[p - 1])]
            if scanned:
                result.warnings.append(
                    f"检测到 {len(scanned)} 页扫描页（无文本层），"
                    f"但未配置视觉模型，这些页的正文将为空：{scanned[:5]}")
            return {}

        out: dict[int, PageRead] = {}
        for i, pno in enumerate(page_numbers, 1):
            if not looks_scanned(doc[pno - 1]):
                continue
            self._emit("vision_read", i, len(page_numbers))
            try:
                image = render_page_png(doc, pno, dpi=self.vision_dpi)
                raw = self.vision_fn(pno, image)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"p{pno}: 扫描页识别失败（{str(exc)[:120]}）")
                log.warning("第 %d 页扫描识别失败：%s", pno, exc)
                continue
            read = raw if isinstance(raw, PageRead) else PageRead.parse(raw or "")
            if not read.markdown.strip():
                result.warnings.append(f"p{pno}: 扫描页识别结果为空")
                continue
            if read.layout_mark and not read.figures:
                # 提示词漂移或本页确实没图：记一条，便于从报告里发现"模型不再给框"
                log.info("p%d: 版面块没有解析出可用插图框", pno)
            out[pno] = read
            _ = rendered
        if out:
            log.info("扫描件兜底：%d 页由视觉模型读出（%s），其中 %d 页带插图粗定位",
                     len(out), sorted(out),
                     sum(1 for r in out.values() if r.figures))
            result.stats["scanned_pages"] = sorted(out)
            result.stats["scan_figure_boxes"] = sum(len(r.figures) for r in out.values())
            # 第 1 页是扫描页时，文本层是空的（元数据只能拿文件名当标题），
            # 用模型读出来的内容覆盖：标题取第一个大标题，摘要/会议/年份同样从这段文字里抽。
            if 1 in out:
                vision_title = title_from_markdown(out[1].markdown).strip()
                if vision_title:
                    result.title = vision_title
                vmeta = meta_from_text(out[1].markdown, title=vision_title)
                if not result.abstract and vmeta.abstract:
                    result.abstract = vmeta.abstract
                if not result.stats.get("venue") and vmeta.venue:
                    result.stats["venue"] = vmeta.venue
                if not result.stats.get("year") and vmeta.year:
                    result.stats["year"] = vmeta.year
                if vmeta.sources:
                    result.stats["meta_sources"] = {
                        **(result.stats.get("meta_sources") or {}),
                        **{k: f"vision-{v}" for k, v in vmeta.sources.items()},
                    }
        return out

    # ------------------------------------------------------------------
    def _group_page(
        self,
        ordered_lines,
        prof: LineProfile,
        layout,
        boilerplate,
        page_no: int,
    ) -> list[tuple[list, int]]:
        """把阅读顺序的行序列切成段落组：``[(行组, 栏号)]``。

        - 剔除页眉页脚（跨页重复文本，或落在页边带内的短行）
        - 剔除图内文字 / 页码 / 公式编号等非正文行（``is_body_line``）
        - 栏切换处强制断开（左右栏段落不能合并）
        - 栏内交给 ``group_lines_into_paragraphs`` 做行距/字体/缩进判定
        """
        groups: list[tuple[list, int]] = []
        buffer: list = []
        buffer_col: int | None = None

        def flush() -> None:
            nonlocal buffer, buffer_col
            if buffer:
                for para in group_lines_into_paragraphs(buffer, prof):
                    groups.append((para, buffer_col if buffer_col is not None else 0))
                buffer = []
            buffer_col = None

        for ln, col in ordered_lines:
            text = ln.text.strip()
            if not text:
                continue
            header, footer = is_in_margin_band(ln, prof.page_height)
            if (header or footer) and (
                boilerplate.is_boilerplate(text) or len(text) <= 90
            ):
                flush()
                continue
            ok, _reason = is_body_line(ln, prof)
            if not ok:
                flush()
                continue
            if buffer_col is not None and col != buffer_col:
                flush()
            buffer_col = col
            buffer.append(ln)
        flush()
        merged = _coalesce_math_fragments(groups, prof)
        return _merge_formula_groups(merged, prof)

    # ------------------------------------------------------------------
    def _build_page_blocks(self, groups, prof, pno, page_w, page_h):
        kinds = _classify_groups(groups, prof)
        return build_blocks_from_groups(
            paper_id=self.paper_id, page_no=pno, groups=groups, kinds=kinds,
            body_size=prof.body_size, page_width=page_w, page_height=page_h,
        )


    # ------------------------------------------------------------------
    def build(self, store_ir: bool = True, write_md: bool = True) -> ParseResult:
        t_all = Timer()
        stage_ms: dict[str, float] = {}

        for d in (self.pages_dir, self.figures_dir, self.crops_dir, self.md_dir,
                  self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.crops_dir.mkdir(parents=True, exist_ok=True)

        if not self.pdf_path.is_file():
            raise ParseError(f"PDF 不存在：{self.pdf_path}")

        with t_all:
            doc = fitz.open(str(self.pdf_path))
            try:
                n_pages = doc.page_count
                page_numbers = list(range(1, n_pages + 1))
                if self.max_pages:
                    page_numbers = page_numbers[: self.max_pages]
                result = ParseResult(paper_id=self.paper_id, page_count=len(page_numbers))
                # 元数据（标题/作者/会议/年份/摘要）一次读全：论文库那几个字段长期是空的，
                # 用户只能手打（见 `backend/parsing/metadata.py` 顶部说明）。
                paper_meta = extract_paper_meta(doc, self.pdf_path)
                result.title, result.authors = paper_meta.title, paper_meta.authors
                result.abstract = paper_meta.abstract
                result.stats["venue"] = paper_meta.venue
                result.stats["year"] = paper_meta.year
                result.stats["meta_sources"] = paper_meta.sources

                # ------------- 1. 页面渲染 -------------
                with Timer() as stat:
                    self._emit("render", 0, len(page_numbers))
                    rendered = render_pages(
                        doc, self.pages_dir, dpi=self.dpi, page_numbers=page_numbers,
                        on_progress=lambda i, n, p: self._emit("render", i, n),
                    )
                stage_ms["render"] = stat.ms

                # ------------- 2. 提取三层 -------------
                with Timer() as stat:
                    raw_by_page = {}
                    self._emit("extract_text", 0, len(page_numbers))
                    for i, pno in enumerate(page_numbers, 1):
                        raw_by_page[pno] = extract_page_blocks(doc[pno - 1], pno)
                        self._emit("extract_text", i, len(page_numbers))

                    drawings_by_page = {}
                    self._emit("extract_vector", 0, len(page_numbers))
                    for i, pno in enumerate(page_numbers, 1):
                        drawings_by_page[pno] = extract_drawings(doc[pno - 1])
                        self._emit("extract_vector", i, len(page_numbers))

                    self._emit("extract_image", 0, len(page_numbers))
                    images_by_page = extract_images(doc, page_numbers)
                    self._emit("extract_image", len(page_numbers), len(page_numbers))
                stage_ms["extract"] = stat.ms

                write_json(self.reports_dir / "raw_text_blocks.json", {
                    str(p): [{"block_no": b.block_no, "bbox": b.rect.to_list(),
                              "text": b.text[:400], "sizes": b.sizes[:6]}
                             for b in blocks]
                    for p, blocks in raw_by_page.items()
                })

                # ------------- 2b. 扫描件兜底：整页交视觉模型读 -------------
                # 影印 PDF 没有文本层，抽不出 span，公式矢量层也不存在 ——
                # 内容只存在于像素里，正文只能由模型读出来（规划 §13.4）。
                with Timer() as stat:
                    vision_reads = self._read_scanned_pages(doc, page_numbers, rendered,
                                                            result)
                stage_ms["vision"] = stat.ms

                # ------------- 3. 结构层 -------------
                with Timer() as stat:
                    page_rects, page_infos, layouts, ordered_lines = {}, [], {}, {}
                    for pno in page_numbers:
                        page = doc[pno - 1]
                        pr = Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1)
                        page_rects[pno] = pr
                        lines = [ln for b in raw_by_page[pno] for ln in b.lines]
                        layout = detect_layout(lines, pno, pr.width, pr.height)
                        layouts[pno] = layout
                        ordered_lines[pno] = order_page_lines(raw_by_page[pno], layout)

                    page_heights = {p: page_rects[p].height for p in page_numbers}
                    boilerplate = detect_boilerplate(raw_by_page, page_heights)

                    all_blocks: list[Block] = []
                    order_global = 0
                    prev_groups: list[tuple[list, int]] = []
                    prev_prof: LineProfile | None = None
                    for pno in page_numbers:
                        # ---- 扫描页：块直接来自视觉模型的整页 Markdown ----
                        if pno in vision_reads:
                            blocks = blocks_from_page_markdown(
                                vision_reads[pno].markdown, page_no=pno,
                                paper_id=self.paper_id,
                                page_width=page_rects[pno].width,
                                page_height=page_rects[pno].height,
                            )
                            for b in blocks:
                                order_global += 1
                                b.order_global = order_global
                            all_blocks.extend(blocks)
                            page_infos.append(PageInfo(
                                page_no=pno, width=page_rects[pno].width,
                                height=page_rects[pno].height, rotation=page.rotation,
                                columns=1, gutter_x=None,
                                image_path=str(rendered[pno]) if rendered.get(pno) else None,
                                text_chars=sum(len(b.text_clean) for b in blocks),
                                block_count=len(blocks), has_text_layer=False,
                            ))
                            # 跨页续接不能跨越"文本层页 ↔ 扫描页"的边界
                            prev_groups, prev_prof = [], None
                            continue

                        page = doc[pno - 1]
                        prof = build_line_profile(
                            [ln for ab in raw_by_page[pno] for ln in ab.lines],
                            page_rects[pno].width, page_rects[pno].height)
                        layout = layouts[pno]
                        groups = self._group_page(
                            ordered_lines[pno], prof, layout, boilerplate, pno)
                        # 跨页段落续接（页边界处句子被切断，且首词常被拆开）
                        if prev_groups and prev_prof is not None:
                            prev_groups = _merge_across_pages(prev_groups, prev_prof,
                                                              groups, prof)
                        blocks = self._build_page_blocks(
                            groups, prof, pno, page_rects[pno].width,
                            page_rects[pno].height)
                        for b in blocks:
                            order_global += 1
                            b.order_global = order_global
                        all_blocks.extend(blocks)

                        result.warnings.extend(
                            f"p{pno}: {x}" for x in self_check_order(
                                order_page_blocks(raw_by_page[pno], layout), layout))
                        page_infos.append(PageInfo(
                            page_no=pno, width=page_rects[pno].width,
                            height=page_rects[pno].height, rotation=page.rotation,
                            columns=layout.columns, gutter_x=layout.gutter_x,
                            image_path=str(rendered[pno]) if rendered.get(pno) else None,
                            text_chars=sum(len(b.text_clean) for b in blocks),
                            block_count=len(blocks),
                            has_text_layer=page_has_text_layer(doc[pno - 1]),
                        ))
                        prev_groups, prev_prof = groups, prof

                    _annotate_sections(all_blocks)
                    result.blocks = all_blocks
                    result.pages = page_infos
                stage_ms["structure"] = stat.ms

                # ------------- 3b. block_id 稳定重绑定 -------------
                with Timer() as stat:
                    writer = IRWriter(self.ir_path, self.paper_id)
                    old_fps = writer.read_existing_fingerprints()
                    assign_ids_in_order(all_blocks, prefix=self.id_prefix)
                    if old_fps:
                        report = rebind_block_ids(all_blocks, old_fps)
                        result.stats["rebind"] = report.to_dict()
                    else:
                        result.stats["rebind"] = {"matched": 0,
                                                  "new_ids": len(all_blocks)}
                stage_ms["rebind"] = stat.ms

                # ------------- 4. 公式 -------------
                # 扫描页没有矢量层：公式已由视觉模型以 $$…$$ 写在正文里，
                # 这里跳过它，否则会把整页扫描图当成一个巨型"公式候选"。
                with Timer() as stat:
                    result.formulas = self._process_formulas(
                        doc, raw_by_page, drawings_by_page, page_rects, result,
                        skip_pages=set(vision_reads))
                stage_ms["formula"] = stat.ms

                # ------------- 5. 插图 / 表格 -------------
                with Timer() as stat:
                    figures, tables = self._process_figures_tables(
                        doc, raw_by_page, images_by_page, drawings_by_page,
                        page_rects, result, skip_pages=set(vision_reads),
                        page_reads=vision_reads)
                    result.figures, result.tables = figures, tables
                stage_ms["figure"] = stat.ms

                # ------------- 5b. 阅读顺序归一 -------------
                # 公式/插图阶段可能补入孤儿块，这里按 (页, y, x) 重排并重编号，
                # 保证 order_global 单调且与阅读顺序一致（阶段 03 验收项）
                with Timer() as stat:
                    _renumber_reading_order(result.blocks)
                stage_ms["renumber"] = stat.ms

                # ------------- 6. 质保 -------------
                with Timer() as stat:
                    findings = run_consistency_checks(
                        result.blocks, rules=[],
                        formula_latex={f.block_id: (f.latex_final or f.latex_candidate)
                                       for f in result.formulas},
                    )
                    # 复核清单已下线：一致性问题只落进统计（供质量报告/实验用），不再进人力队列
                    result.stats["consistency_findings"] = [c.to_dict() for c in findings]
                stage_ms["qa"] = stat.ms

                # ------------- 7. 写 IR + Markdown -------------
                with Timer() as stat:
                    summary: dict = {}
                    if store_ir:
                        summary = writer.write(result, source_pdf=self.pdf_path)
                    if write_md:
                        summary["markdown"] = self._write_markdown(
                            result, writer if store_ir else None)
                stage_ms["persist"] = stat.ms

                result.stats.update(stage_ms=stage_ms, **summary)
                result.duration_ms = round(t_all.elapsed * 1000, 1)
                result.stats["quality"] = build_quality_report(
                    result,
                    consistency_findings=result.stats.get("consistency_findings"),
                    llm_repaired_count=sum(1 for f in result.formulas if f.latex_repaired),
                    llm_structure_changed=sum(
                        1 for f in result.formulas
                        if f.validation.get("llm_changed_structure")),
                )
                write_json(self.reports_dir / "parse_report.json", {
                    "paper_id": self.paper_id, "counts": result.counts(),
                    "grades": result.grade_distribution(),
                    "stats": result.stats, "warnings": result.warnings,
                })
            finally:
                doc.close()
        return result

    # ------------------------------------------------------------------
    def _process_formulas(
        self,
        doc: "fitz.Document",
        raw_by_page: dict[int, list],
        drawings_by_page: dict[int, list],
        page_rects: dict[int, Rect],
        result: ParseResult,
        skip_pages: set[int] | None = None,
    ) -> list[FormulaBlock]:
        """公式：候选 → （可选 LLM 修复）→ 三重校验 → 分级 → 裁图。

        ``skip_pages`` 里的页整体跳过（扫描页）：那里没有矢量层与文本层，
        公式是由视觉模型以 ``$$…$$`` 写在正文里的，硬跑候选检测只会
        把整页扫描图当成一个巨型公式。
        """
        formulas: list[FormulaBlock] = []
        skip = skip_pages or set()
        # 建立「原始块 → 结构化 block」的映射：按 (page, block_no, split) 用 bbox 近似匹配
        block_index: dict[tuple[int, int], list[Block]] = {}
        for b in result.blocks:
            block_index.setdefault((b.page_no, 0), []).append(b)

        candidates = []
        for pno, raws in raw_by_page.items():
            if pno in skip:
                continue
            all_lines = [ln for ab in raws for ln in ab.lines]
            prof = build_line_profile(all_lines, page_rects[pno].width,
                                      page_rects[pno].height)
            cands = detect_formula_candidates(
                raws, {pno: drawings_by_page.get(pno, [])}, body_size=prof.body_size)
            candidates.extend(cands)

        for cand in candidates:
            page_drawings = drawings_by_page.get(cand.page_no, [])
            local_drawings = [d for d in page_drawings
                              if cand.rect.expand(6, 6).intersects(d.rect)]
            spans = [s for ln in cand.lines for s in ln.spans]
            audit = audit_formula_vectors(
                local_drawings, spans, cand.rect,
                font_height=max((ln.size for ln in cand.lines), default=10.0),
            )
            latex_cands = recover_candidates(cand.raw_text, audit, spans)
            latex_list = [c.latex for c in latex_cands] or [""]

            # 纯脚注符号（`∗†`）不是公式：连候选与原文都没有一点数学内容时直接跳过。
            # ⚠️ 必须在 `_find_host_block` / `_make_orphan_block` **之前**跳过 ——
            # 否则会为它补一个孤儿块（块进了 IR，公式却不在），也会裁图、进公式列表、进知识库。
            if is_noise_formula(cand.raw_text) and all(is_noise_formula(x) for x in latex_list):
                log.info("跳过非数学公式候选：p%s %r（%s）",
                         cand.page_no, cand.raw_text, cand.reason or "候选")
                continue

            # 找到对应的结构化 block（bbox 重合度最高者）
            host = _find_host_block(result.blocks, cand.page_no, cand.rect)
            if host is None:
                # 公式区域落在非正文行上（图内标注/表内公式等）：补一个孤儿块，
                # 保证 formula.block_id 的外键一致，并在 Markdown/阅读器中可见
                host = _make_orphan_block(result, cand)
            block_id = host.id

            fb = FormulaBlock(
                block_id=block_id, paper_id=self.paper_id, page_number=cand.page_no,
                rect=cand.rect, raw_text=cand.raw_text,
                vector_data=audit, is_inline=cand.is_inline,
                latex_candidates=latex_list,
                source="text+vector" + ("+geometry" if audit.has_structure else ""),
            )

            # LLM 修复（可选，必须基于证据包；规划 §14.4）
            llm_changed = False
            llm_uncertain = False
            if self.use_llm_repair and self.repair_fn is not None:
                chosen = latex_cands[0].latex if latex_cands else ""
                evidence = self._build_repair_evidence(
                    doc, cand, audit, chosen, result)
                try:
                    out = self.repair_fn(evidence) or {}
                    repaired = (out.get("repaired_latex") or "").strip()
                    if repaired:
                        fb.latex_repaired = repaired
                        llm_changed = _structure_changed(chosen, repaired)
                        fb.review_note = (out.get("reason") or "")[:400]
                    llm_uncertain = bool(out.get("uncertainties"))
                    fb.validation["llm_evidence_used"] = out.get("evidence_used", [])
                    fb.validation["llm_uncertainties"] = out.get("uncertainties", [])
                except Exception as exc:  # noqa: BLE001
                    log.warning("公式 %s LLM 修复失败：%s", block_id, exc)
                    result.warnings.append(f"公式 {block_id} LLM 修复失败：{exc}")

            final = fb.latex_repaired or fb.latex_candidates[0]
            rules: list[dict] = []
            validation = validate_formula(final, cand.raw_text, audit, rules)
            fb.validation.update(validation.to_dict())
            fb.validation["llm_changed_structure"] = llm_changed
            fb.latex_final = final if validation.all_passed or fb.latex_repaired else None

            grade, conf, reasons = grade_formula(
                audit=audit, validation=validation,
                candidate_kind=latex_cands[0].kind if latex_cands else "literal",
                llm_changed_structure=llm_changed,
                llm_reported_uncertainty=llm_uncertain,
                is_bitmap=False,
                has_ambiguous_encoding=any(
                    "PUA" in n for c in latex_cands for n in c.notes),
            )
            fb.grade = grade
            fb.confidence = conf
            fb.status = {"A": "validated", "B": "validated", "C": "needs_review"}[grade]
            fb.review_note = fb.review_note or ("；".join(reasons) if reasons else None)

            # 高倍裁图（A/B/C 都留，C 级是兜底展示的唯一来源）
            crop_name = f"{block_id}@{max(2, self.crop_dpi // self.dpi)}x.png"
            crop_path = self.crops_dir / crop_name
            try:
                data = render_crop_png(doc, cand.page_no, cand.rect, dpi=self.crop_dpi)
                crop_path.write_bytes(data)
                fb.image_path = str(crop_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("公式裁图失败 %s：%s", block_id, exc)
                result.warnings.append(f"公式 {block_id} 裁图失败：{exc}")

            if host is not None:
                host.type = "formula" if not cand.is_inline else host.type
                host.is_inline = cand.is_inline
                host.meta["formula_grade"] = grade
                host.meta["latex"] = fb.latex_final or ""
                host.meta["crop"] = crop_name
            formulas.append(fb)
        return self._dedupe_formulas(formulas)


# --------------------------------------------------------------------------
# 公式去重（多个候选可能落在同一个宿主块上 → 合并为一个公式块）
# --------------------------------------------------------------------------


    # ------------------------------------------------------------------
    # 公式去重（多个候选可能落在同一个宿主块上 → 合并为一个公式块）
    # ------------------------------------------------------------------
    @staticmethod
    def _dedupe_formulas(formulas: list[FormulaBlock]) -> list[FormulaBlock]:
        """同一 ``block_id`` 只保留一条（合并 bbox 与候选，保留最低分级）。

        ``formula`` 表以 ``block_id`` 为主键（规划 §10 ④ id = block_id），
        因此必须在落库前合并，否则会出现主键冲突。
        """
        by_id: dict[str, FormulaBlock] = {}
        for f in formulas:
            prev = by_id.get(f.block_id)
            if prev is None:
                by_id[f.block_id] = f
                continue
            prev.rect = prev.rect.union(f.rect)
            for cand in f.latex_candidates:
                if cand and cand not in prev.latex_candidates:
                    prev.latex_candidates.append(cand)
            # 分级取更保守的一侧（C < B < A）
            order = {"A": 2, "B": 1, "C": 0}
            if order.get(f.grade, 0) < order.get(prev.grade, 2):
                prev.grade = f.grade
                prev.confidence = min(prev.confidence, f.confidence)
                prev.status = f.status
                prev.review_note = prev.review_note or f.review_note
            prev.raw_text = (prev.raw_text + " " + f.raw_text).strip()[:4000]
            prev.validation = prev.validation or f.validation
            prev.is_inline = prev.is_inline and f.is_inline
        return list(by_id.values())

    def _build_repair_evidence(self, doc, cand, audit, chosen, result: ParseResult) -> dict:
        """构造 LLM 修复的输入契约（规划 §14.4：防编造的"证据包"）。"""
        before = _context_text(result.blocks, cand.page_no, cand.rect, before=True)
        after = _context_text(result.blocks, cand.page_no, cand.rect, before=False)
        return {
            "task": "repair_formula",
            "evidence": {
                "crop_image_base64_path": None,
                "crop_image": None,
                "raw_text": cand.raw_text,
                "vector_audit": audit.to_dict(),
                "font_glyphs": [
                    {"char": s.text[i:i + 1], "font": s.font, "size": s.size}
                    for ln in cand.lines for s in ln.spans
                    for i in range(min(len(s.text), 4))
                ][:80],
                "context_before": before,
                "context_after": after,
                "constraints": [
                    "只能依据 evidence 中的信息，不得引入未出现的符号或数值",
                ],
                "chosen_candidate": chosen,
            },
            "output_schema": {
                "repaired_latex": "string",
                "reason": "string（必须引用上述证据项）",
                "evidence_used": ["vector_audit.radical_bars[0]"],
                "uncertainties": ["string"],
            },
        }

    # ------------------------------------------------------------------
    def _process_figures_tables(
        self, doc, raw_by_page, images_by_page, drawings_by_page, page_rects,
        result: ParseResult, skip_pages: set[int] | None = None,
        page_reads: dict[int, PageRead] | None = None,
    ) -> tuple[list[FigureBlock], list[TableBlock]]:
        """插图/表格检测，两条路：

        - **文本层页**：几何法（图注定位 + 位图/矢量并集，见 ``figure_cut``）；
        - **扫描页**（``page_reads`` 里有）：视觉模型给的插图粗框 + 像素墨迹定边界
          （见 :meth:`_scan_figures`）。几何法在这些页上必须**完全不跑** ——
          整页位图会被判成一张巨型插图，而板块坐标又是等分虚拟横带。

        扫描页的表格仍不产出（``skip_pages`` 分支）：没有矢量网格，硬造出来的
        "表格"就是图注那一行。
        """
        figures: list[FigureBlock] = []
        tables: list[TableBlock] = []
        skip = skip_pages or set()
        reads = page_reads or {}

        for pno, raws in raw_by_page.items():
            read = reads.get(pno)
            if read is not None:                    # ---- 扫描页：模型粗框 + 墨迹 ----
                figures.extend(self._scan_figures(
                    doc, pno, read, page_rects[pno],
                    images_by_page.get(pno, []), result))
                continue
            if pno in skip:
                continue
            cands = detect_figures(raws, images_by_page, drawings_by_page, page_rects)
            for cand in cands:
                host = _find_host_block(result.blocks, pno, cand.rect)
                caption_block = _find_caption_block(result.blocks, pno, cand.caption)
                if host is None:
                    host = _make_orphan_block(result, cand, "figure", cand.caption)
                block_id = host.id
                fname = f"{block_id}.png"
                fpath = self.figures_dir / fname
                fb = FigureBlock(
                    block_id=block_id, paper_id=self.paper_id, page_number=pno,
                    rect=cand.rect, image_path=str(fpath),
                    caption=cand.caption,
                    caption_block_id=caption_block.id if caption_block else None,
                    subfigure_count=cand.subfigure_count,
                    effective_dpi=max((i.effective_dpi for i in cand.image_infos),
                                      default=self.crop_dpi),
                )
                try:
                    data = render_crop_png(doc, pno, cand.rect, dpi=self.crop_dpi)
                    fpath.write_bytes(data)
                    audit = audit_figure_crop(
                        fpath, rect=cand.rect, effective_dpi=fb.effective_dpi,
                        subfigure_count=cand.subfigure_count, caption=cand.caption)
                    fb.audit = audit.to_dict()
                    fb.edge_ink = audit.edge_ink
                    fb.aspect_ratio = audit.aspect_ratio
                    fb.status = audit.status
                except Exception as exc:  # noqa: BLE001
                    log.warning("插图裁剪失败 %s：%s", block_id, exc)
                    fb.status = "error"
                    fb.audit = {"issues": [f"裁剪失败：{exc}"]}
                    result.warnings.append(f"插图 {block_id} 裁剪失败：{exc}")

                if caption_block is not None:
                    caption_block.meta["figure_id"] = block_id
                if host is not None:
                    host.type = "figure"
                    host.meta["figure_id"] = block_id
                    host.meta["image_rel"] = f"../figures/{fname}"
                figures.append(fb)

            for tcand in detect_tables(raws, drawings_by_page, page_rects):
                host = _find_host_block(result.blocks, pno, tcand.rect)
                caption_block = _find_caption_block(result.blocks, pno, tcand.caption)
                if host is None:
                    host = _make_orphan_block(result, tcand, "table", tcand.caption)
                block_id = host.id
                tb = TableBlock(
                    block_id=block_id, paper_id=self.paper_id, page_number=pno,
                    rect=tcand.rect, caption=tcand.caption,
                    caption_block_id=caption_block.id if caption_block else None,
                    row_count=tcand.row_count, col_count=tcand.col_count,
                    has_borders=tcand.has_borders, grid=tcand.grid or [],
                    status="grid" if tcand.has_borders else "image_only",
                )
                # 表格一律保留裁图（L4 重排属 P2）
                tp = self.figures_dir / f"{block_id}-table.png"
                try:
                    tp.write_bytes(render_crop_png(doc, pno, tcand.rect, dpi=self.crop_dpi))
                    tb.image_path = str(tp)
                except Exception as exc:  # noqa: BLE001
                    log.warning("表格裁图失败 %s：%s", block_id, exc)
                if host is not None:
                    host.type = "table"
                    host.meta["table_id"] = block_id
                    host.meta["image_rel"] = f"../figures/{tp.name}"
                tables.append(tb)
        return figures, tables

    # ------------------------------------------------------------------
    def _scan_figures(self, doc, pno: int, read: PageRead, page_rect: Rect,
                      page_images, result: ParseResult) -> list[FigureBlock]:
        """扫描页的插图裁剪：**模型只给"图号 → 大致在哪"，边界由页图墨迹决定**。

        为什么不能直接用模型坐标出图：视觉模型的 grounding 精度只到"几个百分点"，
        直接裁出来会带进正文、切掉图体。为什么不能走几何法：扫描页没有文本层坐标
        （块的 rect 是等分虚拟横带）、没有矢量层、页图是一整张位图。

        所以这里的分工是：
        1. 模型框 → 页面坐标（归一化 → pt），仅作**提案**（认领哪一块墨迹是这张图）；
        2. **像素版面**（``ink_layout``）定出这张图的真实窗口：墨迹带分组 + 排除文字组；
        3. 图注行（模型给的整段图注框落在哪条**短行带**上）从上沿切掉；
        4. 校验（尺寸、墨迹覆盖率）不过就**不出图** —— 宁可少一张图，也不往
           Markdown 里插一张裁错的图。
        """
        out: list[FigureBlock] = []
        page_area = max(1.0, page_rect.width * page_rect.height)
        src_dpi = max((i.effective_dpi for i in page_images if i.effective_dpi),
                      default=0.0)
        probe: PageInk | None = None            # 同页多张图共用一份墨迹投影
        for item in read.figures:
            number = str(item.get("number") or "").strip()
            tag = f"图{number}" if number else "插图"
            box = _point_box(item.get("bbox"), page_rect)
            if box is None:
                result.warnings.append(f"p{pno}: {tag}的模型框不可用，这张图跳过")
                continue
            ratio = box.area / page_area
            if not (SCAN_MIN_AREA_RATIO <= ratio <= SCAN_MAX_AREA_RATIO):
                result.warnings.append(
                    f"p{pno}: {tag}的模型框面积占比 {ratio:.0%} 不合理，这张图跳过")
                continue

            proposal = box
            # 裁剪窗口由**页图像素**决定：模型框只用来"认领"哪一块墨迹区域是这张图。
            # 实测模型框会同时出现"多圈进上一段正文"和"只盖住一半图"两种偏差，
            # 直接当窗口用会裁出"半张图 + 一截正文"。
            cap_box_raw = _point_box(item.get("caption_bbox"), page_rect)
            if cap_box_raw is not None and not _usable_caption_box(cap_box_raw, page_rect):
                cap_box_raw = None
            if probe is None:
                probe = PageInk(doc, pno, page_rect)
            try:
                refined = locate_figure(doc, pno, page_rect, proposal, cap_box_raw,
                                        ink=probe, pad=SCAN_INK_PAD)
            except Exception as exc:  # noqa: BLE001
                log.warning("p%d %s 像素版面分析失败：%s", pno, tag, exc)
                refined = None
            if refined is None:
                result.warnings.append(
                    f"p{pno}: {tag}在页图上找不到对应的墨迹区域（模型框不可用），这张图跳过")
                continue
            if refined.height < SCAN_MIN_HEIGHT or refined.width < SCAN_MIN_WIDTH:
                result.warnings.append(f"p{pno}: {tag}的墨迹区域过小，这张图跳过")
                continue
            if _box_iou(refined, proposal) < SCAN_MIN_PROPOSAL_IOU:
                log.info("p%d %s：像素窗口 %s 与模型框 %s 相差较大（IoU %.2f），以像素为准",
                         pno, tag, [round(v, 1) for v in refined.to_list()],
                         [round(v, 1) for v in proposal.to_list()],
                         _box_iou(refined, proposal))
            # 「有墨迹窗口」不等于「这里有内容」：整幅全白时墨迹量测会原样返回入参，
            # 所以必须单独量一次覆盖率，否则模型把框画在空白处也会被当成一张图。
            frac = ink_fraction(doc, pno, refined)
            if 0.0 <= frac < SCAN_MIN_INK:
                result.warnings.append(
                    f"p{pno}: {tag}的框内几乎没有内容（墨迹 {frac:.1%}），这张图跳过")
                continue

            caption_block = _find_caption_block_by_number(result.blocks, pno, number)
            # 完整图注文本：优先用**转录出来的图注块**（那是事实源），按图号把该图的
            # 图注块全部接起来（模型把一条长图注拆成两段时也只显示半句）；转录里没有
            # 才退回模型在版面块里给的 caption。
            caption_text = _scan_caption_text(result.blocks, pno, number)
            if not caption_text:
                caption_text = str(item.get("caption") or "").strip()
            if not caption_text:
                caption_text = f"Figure {number}" if number else ""
            cap_box = cap_box_raw

            # 插图**独立成块**（外键 + Markdown 里能出图）：文字留空，图注仍归图注块，
            # 否则 full.md 里同一句图注会出现两次。
            #
            # 块的 rect 取**图注块所在的虚拟横带**（再往前挪 0.5pt）：扫描页的阅读顺序
            # 全靠这条横带排（``_renumber_reading_order`` 按 (页, y0, x0) 排），若用像素
            # 算出来的真实 y，插图会被排到正文中间、离自己的图注很远（实测：图落在第二段
            # 与第三段之间，图注在最后）。真实边界存在 ``FigureBlock.rect`` 里，
            # 而区域/裁图只读 figure 行，不读块的 rect。
            band = caption_block.rect if caption_block is not None else refined
            block_rect = Rect(band.x0, band.y0 - 0.5, band.x1, band.y1)
            host = _make_orphan_block(
                result, _ScanCand(pno, block_rect), "figure", "")
            fname = f"{host.id}.png"
            fpath = self.figures_dir / fname
            fb = FigureBlock(
                block_id=host.id, paper_id=self.paper_id, page_number=pno,
                rect=refined, image_path=str(fpath), caption=caption_text,
                caption_block_id=caption_block.id if caption_block else None,
                subfigure_count=1, effective_dpi=src_dpi,
            )
            try:
                data = render_crop_png(doc, pno, refined,
                                       dpi=_scan_crop_dpi(self.crop_dpi, src_dpi),
                                       padding=SCAN_INK_PAD, ink_trim=True)
                fpath.write_bytes(data)
                audit = audit_figure_crop(fpath, rect=refined, effective_dpi=src_dpi,
                                          subfigure_count=1, caption=caption_text)
                fb.audit = {**audit.to_dict(), "source": "scan",
                            "proposal": [round(v, 1) for v in proposal.to_list()],
                            "caption_box": ([round(v, 1) for v in cap_box.to_list()]
                                            if cap_box is not None else None),
                            "src_dpi": round(src_dpi, 1)}
                fb.edge_ink = audit.edge_ink
                fb.aspect_ratio = audit.aspect_ratio
                fb.status = audit.status
            except Exception as exc:  # noqa: BLE001
                log.warning("扫描页插图裁剪失败 %s：%s", host.id, exc)
                fb.status = "error"
                fb.audit = {"source": "scan", "issues": [f"裁剪失败：{exc}"]}
                result.warnings.append(f"p{pno}: {tag}裁剪失败：{exc}")
                continue

            host.meta["image_rel"] = f"../figures/{fname}"
            host.meta["figure_id"] = host.id
            if caption_block is not None:
                caption_block.meta["figure_id"] = host.id
                if cap_box is not None:
                    # 图注的**真实位置**：紧缩/卡片要用它（块自身的 rect 是虚拟横带）
                    caption_block.meta["page_bbox"] = [round(v, 2) for v in cap_box.to_list()]
            out.append(fb)
            log.info("扫描页插图：p%d %s bbox=%s（模型提案 %s）dpi=%d",
                     pno, tag or host.id, [round(v, 1) for v in refined.to_list()],
                     [round(v, 1) for v in proposal.to_list()],
                     _scan_crop_dpi(self.crop_dpi, src_dpi))
        return out

    # ------------------------------------------------------------------
    def _write_markdown(self, result: ParseResult, writer: IRWriter | None) -> dict:
        """写 Markdown 产物（full.md + blocks.json + 逐页 md）。

        实现位于 ``backend.parsing.md.generator.write_markdown_bundle``，
        CLI（``pr.py parse``）走同一条路径，保证两种入口产物一致。
        """
        return write_markdown_bundle(result, self.md_dir, paper_id=self.paper_id,
                                     assets_prefix="..")


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _find_host_block(blocks: list[Block], page_no: int, rect: Rect) -> Block | None:
    best: tuple[float, Block] | None = None
    for b in blocks:
        if b.page_no != page_no:
            continue
        r = b.rect
        if r.area <= 0:
            continue
        inter = rect.overlap_ratio(r) if rect.area else 0.0
        backward = r.overlap_ratio(rect) if r.area else 0.0
        score = max(inter, backward)
        if score > 0.45 and (best is None or score > best[0]):
            best = (score, b)
    return best[1] if best else None


def _find_caption_block(blocks: list[Block], page_no: int, caption: str) -> Block | None:
    key = normalize_text(caption)[:40]
    for b in blocks:
        if b.page_no == page_no and b.type == "caption" \
                and normalize_text(b.text_clean or b.text).startswith(key[:20]):
            return b
    return None


def _next_block_id(result: ParseResult, page_no: int) -> str:
    """为孤儿块分配不与任何已有 ID 冲突的编号（``{prefix}-p07-x03``）。

    注意：**不能重新调用 assign_ids_in_order**，否则会覆盖重绑定得到的稳定 ID。
    """
    existing = {b.id for b in result.blocks}
    prefix = id_prefix(result.paper_id)
    n = 1
    while f"{prefix}-p{page_no:02d}-x{n:02d}" in existing:
        n += 1
    return f"{prefix}-p{page_no:02d}-x{n:02d}"


def _merge_across_pages(
    prev_groups: list[tuple[list, int]],
    prev_prof: LineProfile,
    groups: list[tuple[list, int]],
    prof: LineProfile,
) -> list[tuple[list, int]]:
    """处理跨页段落续接（规划 §3.2 L2 的页内退化版）。

    判定（**保守**，宁可漏接也不误并）：
    - 上一页末段在页面底部且**未**以句末标点结尾
    - 本页首段在栏顶部、同栏、且**只有 1 行**（多行更可能是新段落）
    - 本页首行以小写字母/数学符号开头，或发生了跨页拆词
    """
    if not prev_groups or not groups:
        return prev_groups

    last_seg, last_col = prev_groups[-1]
    first_seg, first_col = groups[0]

    def is_bottom(seg: list) -> bool:
        return max((ln.rect.y1 for ln in seg), default=0) >= prev_prof.page_height * 0.82

    def is_top(seg: list) -> bool:
        return min((ln.rect.y0 for ln in seg), default=1e9) <= prof.page_height * 0.18

    if last_col != first_col or not (is_bottom(last_seg) and is_top(first_seg)):
        return prev_groups
    if len(first_seg) > 1:
        return prev_groups

    prev_text = "".join(ln.text for ln in last_seg).strip()
    next_text = "".join(ln.text for ln in first_seg).strip()
    if len(prev_text) < 40 or not next_text:
        return prev_groups
    from backend.core.utils import ends_sentence

    if ends_sentence(prev_text):
        return prev_groups

    head = next_text.lstrip()
    if not head:
        return prev_groups
    split_word = (prev_text[-1].isalpha() and head[0].islower())
    if head[0].isupper() and not split_word:
        return prev_groups
    if not (head[0].islower() or head[0].isdigit() or split_word
            or head[0] in "⟨⟨([|−-≈≤≥∑∏∫√∂∇"):
        return prev_groups

    if split_word:
        patched = _patch_split_word(last_seg, first_seg)
        if patched is not None:
            first_seg = patched

    groups[0] = (first_seg, first_col)
    return prev_groups[:-1]


def _patch_split_word(last_seg: list, first_seg: list) -> list | None:
    """修复跨页拆词：把上一页行尾片段并入下一页首行的首个 span。

    只修改**副本**：原始 span 必须保留文本层证据（规划 §9.2 ``source_text``）。
    """
    import copy

    if not last_seg or not first_seg:
        return None
    tail_line = last_seg[-1]
    head_line = first_seg[0]
    if not tail_line.spans or not head_line.spans:
        return None
    tail_text = tail_line.spans[-1].text
    m = _TRAILING_WORD.search(tail_text)
    if not m:
        return None
    fragment = m.group(0)
    if len(fragment) > 14:
        return None
    head_span = head_line.spans[0]
    head_word = _LEADING_WORD.match(head_span.text)
    if not head_word:
        return None
    rebuilt = fragment + head_word.group(0)

    new_head_line = copy.copy(head_line)
    new_head_spans = list(head_line.spans)
    new_head_first = copy.copy(head_span)
    new_head_first.text = rebuilt + head_span.text[head_word.end():]
    new_head_spans[0] = new_head_first
    new_head_line.spans = new_head_spans

    out = list(first_seg)
    out[0] = new_head_line
    return out


_TRAILING_WORD = re.compile(r"[A-Za-z]{1,14}$")
_LEADING_WORD = re.compile(r"[a-z]{1,14}")


# --------------------------------------------------------------------------
# 公式行的合并与类型标注（在段落组装之后、写库之前）
# --------------------------------------------------------------------------

# 用来"吸附"公式碎片的正文字符
_PROSE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _coalesce_math_fragments(groups: list[tuple[list, int]], prof: LineProfile
                             ) -> list[tuple[list, int]]:
    """把被 PyMuPDF 切碎的展示公式碎片聚合成一个块。

    PyMuPDF 会把一个公式拆成若干极小文本块（``h(x−Q⁻¹(Q(x)))²_i`` 之类，
    甚至单个 ``2``、``i``、``(1)``）。判据：块/行较短、含数学字符、
    纵向紧贴且水平方向有交集 → 合并为同一个块。
    """
    from backend.parsing.models import rects_span

    max_lead = max(prof.body_size * 1.5, prof.median_lead * 1.5)
    out: list[tuple[list, int]] = []
    for seg, col in groups:
        if not seg:
            continue
        if out and _fragments_joinable(out[-1][0], seg, col, out[-1][1], prof, max_lead):
            out[-1] = (list(out[-1][0]) + list(seg), out[-1][1])
        else:
            out.append((seg, col))
    return out


def _fragments_joinable(a_seg: list, b_seg: list, b_col: int, a_col: int,
                        prof: LineProfile, max_lead: float) -> bool:
    from backend.parsing.models import rects_span

    a_text = " ".join(ln.text.strip() for ln in a_seg).strip()
    b_text = " ".join(ln.text.strip() for ln in b_seg).strip()
    if not a_text or not b_text:
        return False
    # 两侧都必须是"碎片"：短、且至少一侧含数学字符
    if len(a_text) > 60 or len(b_text) > 60:
        return False
    a_math = _seg_has_math(a_seg)
    b_math = _seg_has_math(b_seg)
    if not (a_math or b_math):
        return False
    if b_col != a_col:
        return False
    a = rects_span([ln.rect for ln in a_seg])
    b = rects_span([ln.rect for ln in b_seg])
    lead = b.y0 - a.y1
    if not (-prof.body_size * 0.9 <= lead <= max_lead):
        return False
    ix = min(a.x1, b.x1) - max(a.x0, b.x0)
    if ix <= 0:
        return False
    overlap = ix / max(1.0, min(a.width, b.width))
    if overlap < 0.12:
        return False
    return True


def _seg_has_math(seg: list) -> bool:
    from backend.parsing.models import is_greek, is_math_operator

    for ln in seg:
        for s in ln.spans:
            if s.is_math_font:
                return True
            for c in s.text:
                if is_greek(c) or is_math_operator(c):
                    return True
    return False


def _line_math_density(line) -> float:
    from backend.parsing.models import is_greek, is_math_operator

    chars = [c for c in line.text if not c.isspace()]
    if not chars:
        return 0.0
    sym = sum(1 for c in chars if is_greek(c) or is_math_operator(c))
    return sym / len(chars)


def _is_formula_group(seg: list, prof: LineProfile) -> bool:
    """判断一个行组是否是"公式行"：短、符号密度高、不以句末标点结尾。"""
    text = " ".join(ln.text.strip() for ln in seg).strip()
    if not text or len(text) > 110:
        return False
    dens = sum(_line_math_density(ln) for ln in seg) / max(1, len(seg))
    if dens < 0.15:
        return False
    if text.endswith(".") and dens < 0.25:
        return False
    return True


def _merge_formula_groups(groups: list[tuple[list, int]], prof: LineProfile
                          ) -> list[tuple[list, int]]:
    """合并相邻且纵向紧贴的公式行组（修复跨行/跨块被切碎的多行公式）。

    典型场景：``h⟨y,x⟩−⟨y,Q−1 (Q(x))⟩`` + ``= ⟨y,x⟩.`` 被拆成两块。
    """
    if not groups:
        return groups
    max_join_lead = max(prof.body_size * 1.6, prof.median_lead * 1.6)
    out: list[tuple[list, int]] = [groups[0]]
    for seg, col in groups[1:]:
        prev_seg, prev_col = out[-1]
        if col != prev_col or not seg or not prev_seg:
            out.append((seg, col))
            continue
        if not (_is_formula_group(prev_seg, prof) and _is_formula_group(seg, prof)):
            out.append((seg, col))
            continue
        prev_y1 = max(ln.rect.y1 for ln in prev_seg)
        lead = min(ln.rect.y0 for ln in seg) - prev_y1
        if not (-prof.body_size * 0.6 <= lead <= max_join_lead):
            out.append((seg, col))
            continue
        # 水平方向需有重叠（同一公式的续行）
        from backend.parsing.models import rects_span

        a = rects_span([ln.rect for ln in prev_seg])
        b = rects_span([ln.rect for ln in seg])
        if a.width <= 0 or b.width <= 0:
            out.append((seg, col))
            continue
        ix = min(a.x1, b.x1) - max(a.x0, b.x0)
        if ix <= 0:
            out.append((seg, col))
            continue
        out[-1] = (list(prev_seg) + list(seg), col)
    return out


def _classify_groups(groups: list[tuple[list, int]], prof: LineProfile) -> list[str]:
    """为每组标注类型，供 ``build_blocks_from_groups`` 使用。"""
    from backend.parsing.structure.paragraph import classify_text

    kinds: list[str] = []
    for seg, _col in groups:
        text = " ".join(ln.text.strip() for ln in seg).strip()
        if _is_formula_group(seg, prof):
            kinds.append("formula")
            continue
        sizes = [ln.size for ln in seg if ln.size > 0]
        font_size = (max(set([round(s, 1) for s in sizes]), key=sizes.count)
                     if sizes else 0.0)
        is_bold = any(s.is_bold for ln in seg for s in ln.spans)
        dens = sum(_line_math_density(ln) for ln in seg) / max(1, len(seg))
        from backend.parsing.models import rects_span

        rect = rects_span([ln.rect for ln in seg])
        kinds.append(classify_text(
            text, font_size=font_size, body_size=prof.body_size, is_bold=is_bold,
            line_count=len(seg), char_count=len(text),
            page_width=prof.page_width, rect_width=rect.width,
            line_math_ratio=dens,
        ))
    return kinds


def _make_orphan_block(result: ParseResult, cand, block_type: str = "formula",
                       text: str | None = None) -> Block:
    """为落在非正文行上的公式/插图/表格补一个块（保持外键与阅读器可见）。"""
    page_blocks = [b for b in result.blocks if b.page_no == cand.page_no]
    max_order = max((b.order_global for b in page_blocks), default=0)
    raw = text if text is not None else getattr(cand, "raw_text", "")
    b = Block(
        id=_next_block_id(result, cand.page_no), paper_id=result.paper_id,
        page_no=cand.page_no, order_in_page=0, order_global=max_order + 1,
        type=block_type, col=0, rect=cand.rect, text=raw, text_clean=raw,
        source_text=raw, source_kind="text",
        font_size=max((ln.size for ln in getattr(cand, "lines", [])), default=0.0),
        line_count=len(getattr(cand, "lines", []) or []),
    )
    b.finalize()
    b.meta["orphan"] = True
    result.blocks.append(b)
    return b


def _context_text(blocks: list[Block], page_no: int, rect: Rect, *, before: bool,
                  max_chars: int = 400) -> str:
    """取公式前后的上下文（契约里的 context_before/after）。"""
    ordered = [b for b in blocks if b.page_no == page_no and b.type in ("para", "heading")]
    ordered.sort(key=lambda b: b.order_global)
    idx = None
    for i, b in enumerate(ordered):
        if b.rect.y1 > rect.y0:
            idx = i
            break
    if idx is None:
        idx = len(ordered)
    if before:
        chunk = ordered[max(0, idx - 2):idx]
    else:
        chunk = ordered[idx:idx + 2]
    text = " ".join((b.text_clean or b.text) for b in chunk)
    return text[-max_chars:] if before else text[:max_chars]


_HEADING_APPENDIX = ("appendix", "附录")
_HEADING_REFS = ("references", "bibliography", "参考文献")


def _renumber_reading_order(blocks: list[Block]) -> None:
    """按 (页, y, x) 重排并重编 ``order_global``，保证单调且与阅读顺序一致。"""
    blocks.sort(key=lambda b: (b.page_no, round(b.rect.y0, 1), round(b.rect.x0, 1)))
    for i, b in enumerate(blocks, 1):
        b.order_global = i


def _annotate_sections(blocks: list[Block]) -> None:
    """标注 ''in_appendix'' / ''in_ref_section'' / ''section_title''（供分类与一致性检查）。"""
    in_appendix = False
    in_refs = False
    current_section = ""
    for b in sorted(blocks, key=lambda x: x.order_global):
        text = normalize_text(b.text_clean or b.text)
        low = text.lower()
        if b.type == "heading":
            if any(h in low for h in _HEADING_APPENDIX):
                in_appendix = True
            if any(h in low for h in _HEADING_REFS):
                in_refs = True
            current_section = text[:120]
            b.meta["section_title"] = current_section
            b.meta["font_size"] = round(float(b.font_size or 0.0), 1)
        b.meta["in_appendix"] = in_appendix
        b.meta["in_ref_section"] = in_refs
        if in_refs and b.type == "para":
            b.type = "ref"


# 元数据抽取（标题/作者/会议/年份/摘要）已搬到 `backend/parsing/metadata.py`：
# 本模块仍从那里**再导出** `extract_metadata`，旧的 `from ...pipeline import extract_metadata` 不受影响。


def _structure_changed(a: str, b: str) -> bool:
    """判断 LLM 修复是否改变了结构（\\frac / \\sqrt / 上下标数量变化）。"""
    def sig(s: str) -> tuple[int, int, int]:
        return (s.count("\\frac") + s.count("\\dfrac"),
                s.count("\\sqrt"), s.count("^") + s.count("_"))
    return sig(a) != sig(b)


def new_paper_id_from_path(pdf_path: str | Path, title: str = "") -> str:
    from backend.core.utils import slugify

    base = slugify(title or Path(pdf_path).stem, fallback="paper")
    return base or f"paper-{new_id('x')[-6:]}"
