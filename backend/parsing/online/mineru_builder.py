"""在线解析构建器：MinerU 的 ``content_list.json`` → **与本地解析同构的 ParseResult**。

为什么产物必须同构：阅读器三视图、逐页翻译、知识库索引、论文理解读的都是 IR 里的
block/formula/figure/table 与 ``md/full.md``。只要这里产出同一份结构，"换引擎"就只影响
导入/重解析这一处，下游一行都不用改（这是选 MinerU 做在线解析的前提）。

与 :class:`backend.parsing.pipeline.PaperBuilder` 的**关键差别**（每条都是踩过才写的）：

1. **阅读顺序直接用 MinerU 给的顺序**，不做 ``_renumber_reading_order`` 的按 ``y`` 重排：
   MinerU 的版面模型已经解决了双栏/跨栏顺序，按 ``y`` 排反而会把左右栏交错穿插。
2. **不重跑公式候选/校验**：MinerU 的 ``equation`` 项给的就是 LaTeX，直接当 A 级采用
   （本地那条路是自己从文本层+矢量层恢复，才需要三重校验）。
3. **不重跑插图裁切**：MinerU 的 ``bbox`` 是版面模型给的准边界，区域层按"精确来源"
   直接采用（见 ``figure_regions._has_precise_bbox``），不再收紧/贴边/切分。
4. **表格是结构化的**：``table_body`` 是 HTML，正文文本取它的纯文本形态（可翻译/可检索），
   原始 HTML 留在 ``block.meta`` 与 IR 里。
5. **bbox 是 0~1000 归一化坐标、原点左上**（官方输出契约 + 官方 SDK 的
   ``bbox_attributes={"origin":"LEFTTOP","max":1000}``），要乘页面尺寸才得到 pt。
   仍保留一次自检：见 :meth:`_resolve_bbox_scale`。
"""

from __future__ import annotations

import html as html_lib
import re
import shutil
from pathlib import Path
from typing import Any, Callable

import pymupdf as fitz

from backend.core.errors import ParseError
from backend.core.logging import get_logger
from backend.core.utils import Timer, normalize_text, write_json
from backend.parsing.extract.render import render_pages
from backend.parsing.ir.ir_writer import IRWriter
from backend.parsing.ir.rebind import assign_ids_in_order, id_prefix, rebind_block_ids
from backend.parsing.md.generator import write_markdown_bundle
from backend.parsing.models import (
    Block, FigureBlock, FormulaBlock, PageInfo, ParseResult, Rect, TableBlock,
)
from backend.parsing.online.mineru_client import load_json, locate_bundle, sha256_file
from backend.parsing.online.page_ranges import PAGE_RANGES_HINT, parse_page_ranges
from backend.parsing.qa.report import build_quality_report

log = get_logger(__name__)

#: MinerU 的 item 类型分派表（官方 content_list 契约 + 3.x 起新增的 chart/list/code）。
HEADING_TYPES = ("title", "section_header")
IMAGE_TYPES = ("image", "chart", "picture", "drawing")
#: 页眉/页脚/页码/丢弃块：本地解析也会剔除，进正文只会污染翻译与检索
SKIP_TYPES = ("page_number", "header", "footer", "discarded")
#: 图注块（我们为阅读器/翻译补的）在版面上的估计行高
CAPTION_LINE_HEIGHT = 12.0

#: 面板合并的判据（pt）——**只合并"横向并排、顶底对齐"的面板**：
#: 顶/底边错开不超过 `PANEL_ALIGN_PT`、水平间隙不超过 `PANEL_GAP_PT`。
#:
#: 为什么需要合并：MinerU 把**多面板图**拆成多个 `chart`/`image` item（实测 DeepSeek-V3.2 p5：
#: Figure 2 的 (a)(b) 两个面板 + Figure 3 的 (a)(b) 两个面板 = 4 个 item），
#: 照单全收会让"插图"数量比论文里的图多（用户报"标注 6 张图，实际 4 张"）。
#: 为什么只认"横向并排"：竖排相邻的两块更可能是**两张不同的图**（上下堆叠），
#: 这个风险不值得为少见的两行面板布局去冒。顶/底对齐这一条则把"左右栏各一张图"
#: 挡在门外 —— 那两张的顶边几乎不可能同时对齐。
PANEL_ALIGN_PT = 15.0
PANEL_GAP_PT = 30.0


class MineruBuilder:
    """把一次 MinerU 结果包变成一篇论文的解析产物。

    与 ``PaperBuilder`` 保持同一套构造参数（``figure_caption`` / ``deep_figure_search`` /
    ``progress``），这样任务处理器里两条引擎的分支写法完全对称。
    """

    def __init__(
        self,
        pdf_path: str | Path,
        paper_id: str,
        paper_dir: str | Path,
        *,
        bundle: dict,
        dpi: int = 150,
        crop_dpi: int = 600,
        figure_caption: bool = False,
        deep_figure_search: bool = False,
        model_version: str = "pipeline",
        bbox_scale: float | None = None,
        mineru_params: dict | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.paper_id = paper_id
        self.dir = Path(paper_dir)
        self.bundle = bundle or {}
        self.dpi = int(dpi)
        self.crop_dpi = int(crop_dpi)
        self.figure_caption = bool(figure_caption)
        # 在线解析的"要不要出无图注的图"由 MinerU 决定，这里只把它记进产物留痕
        self.deep_figure_search = bool(deep_figure_search)
        self.model_version = model_version or "pipeline"
        #: 本次在线解析真正生效的参数（模型/语言/OCR/公式/表格/页码范围）。
        #: 记进 IR：事后核对"这篇是按什么参数解析的"只有这一处留痕。
        self.mineru_params = dict(mineru_params or {})
        self.bbox_scale = float(bbox_scale) if bbox_scale else None
        self.progress = progress

        self.pages_dir = self.dir / "pages"
        self.figures_dir = self.dir / "figures"
        self.crops_dir = self.dir / "formula_crops"
        self.md_dir = self.dir / "md"
        self.reports_dir = self.dir / "reports"
        self.ir_path = self.dir / "ir.sqlite"
        # 与其他论文的块 ID 前缀避让（两条引擎共用一个业务库主键空间）
        from backend.parsing.pipeline import _other_paper_prefixes

        self.id_prefix = id_prefix(paper_id, taken=_other_paper_prefixes(paper_id))

        self._order = 0
        self._items_seen: dict[str, int] = {}
        self._warnings: list[str] = []
        self._figure_no = 0
        #: 待落盘的插图资产（等块 ID 定稿后再写文件，见 `_finalize_assets`）
        self._pending_assets: list[dict] = []
        #: 页码范围生效时，`content_list` 里的 `page_idx` 是**切片内序号**，要按这张表映射回
        #: 原文档页号（见 `parsing.online.page_ranges` 模块头的真机实测表）。空列表 = 全篇解析。
        self._page_map: list[int] = []

    # ------------------------------------------------------------------
    def _emit(self, stage: str, done: int, total: int) -> None:
        if self.progress is None:
            return
        try:
            self.progress(stage, done, total)
        except Exception:  # noqa: BLE001 —— 进度上报失败不该影响解析
            pass

    # ------------------------------------------------------------------
    def build(self, store_ir: bool = True, write_md: bool = True) -> ParseResult:
        t_all = Timer()
        stage_ms: dict[str, float] = {}
        for d in (self.pages_dir, self.figures_dir, self.crops_dir, self.md_dir,
                  self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)

        if not self.pdf_path.is_file():
            raise ParseError(f"PDF 不存在：{self.pdf_path}")
        items = self._load_items()
        if not items:
            raise ParseError(
                "MinerU 的 content_list.json 是空的：这份结果包里没有可用的版面结构")

        with t_all:
            doc = fitz.open(str(self.pdf_path))
            try:
                n_pages = doc.page_count
                page_numbers = list(range(1, n_pages + 1))
                result = ParseResult(paper_id=self.paper_id, page_count=n_pages)
                result.id_prefix = self.id_prefix

                # 元数据照旧从 PDF 首页离线抽（在线解析也要有作者/年份/摘要）
                from backend.parsing.metadata import extract_paper_meta

                meta = extract_paper_meta(doc, self.pdf_path)
                result.title, result.authors = meta.title, meta.authors
                result.abstract = meta.abstract
                result.stats["venue"] = meta.venue
                result.stats["year"] = meta.year
                result.stats["meta_sources"] = meta.sources

                scale, scale_note = self._resolve_bbox_scale(
                    items, {p: (doc[p - 1].rect.width, doc[p - 1].rect.height)
                            for p in page_numbers})
                self.bbox_scale = scale
                # 页码映射：限定页码范围时 `page_idx` 从切片起点重新计数（真机实测），
                # 不映射就会把内容整体挂到错误的页上
                self._page_map = self._build_page_map(n_pages, result)
                result.stats["parse_options"] = {
                    "figure_caption": self.figure_caption,
                    "deep_figure_search": self.deep_figure_search,
                    "engine": "mineru",
                }

                # ---- 1. 页图（阅读器/图谱/后续裁图都靠它，MinerU 不给页图）----
                with Timer() as stat:
                    self._emit("render", 0, len(page_numbers))
                    rendered = render_pages(
                        doc, self.pages_dir, dpi=self.dpi, page_numbers=page_numbers,
                        on_progress=lambda i, n, p: self._emit("render", i, n))
                stage_ms["render"] = stat.ms

                # ---- 2. 结构（content_list → blocks/figures/formulas/tables）----
                with Timer() as stat:
                    page_rects = {p: Rect(0.0, 0.0, doc[p - 1].rect.width,
                                          doc[p - 1].rect.height)
                                  for p in page_numbers}
                    pages_info, blocks, figures, formulas, tables = self._build_structure(
                        doc, items, page_rects, rendered, result)
                    result.pages = pages_info
                    result.blocks = blocks
                    result.figures = figures
                    result.formulas = formulas
                    result.tables = tables
                stage_ms["structure"] = stat.ms

                # ---- 3. 块 ID：先按阅读顺序编号，再与旧 IR 对齐（译文/批注不丢）----
                with Timer() as stat:
                    writer = IRWriter(self.ir_path, self.paper_id)
                    old_fps = writer.read_existing_fingerprints()
                    # ⚠️ 编号前先记住"临时 ID → 块对象"：插图/公式/表格是先按临时 ID 建好的，
                    # 编号之后必须把它们的外键跟着改过去（`figure.block_id` 是 block 主键的
                    # 外键，不改就是 `FOREIGN KEY constraint failed`，IR 只写一半）。
                    # 本地那条路没这个问题：它先编号、再建插图/公式，那时 host.id 已经是终值。
                    by_tmp_id = {b.id: b for b in result.blocks}
                    assign_ids_in_order(result.blocks, prefix=self.id_prefix)
                    if old_fps:
                        report = rebind_block_ids(result.blocks, old_fps,
                                                  prefix=self.id_prefix)
                        result.stats["rebind"] = report.to_dict()
                    else:
                        result.stats["rebind"] = {"matched": 0,
                                                  "new_ids": len(result.blocks)}
                    self._relink(result, by_tmp_id)
                    # 资产（插图文件）同样要等 ID 定稿：文件名 = 最终的 block_id
                    self._finalize_assets(doc)
                stage_ms["rebind"] = stat.ms

                # ---- 4. 落 IR + Markdown ----
                #
                # ⚠️ **统计必须在写 IR 之前填好**：`IRWriter.write` 会把 `result.stats`
                # 序列化进 `paper.stats_json`，之后再改 `result.stats` 只影响内存与
                # `parse_report.json`，IR 里那份就永远缺"这篇是怎么解析的"（实测：
                # `stats.mineru` 全为 null，事后无法从 IR 核对坐标口径与结果包信息）。
                # 注意 `summary`（IR 路径 / Markdown 字符数）只能写完之后才知道，
                # 所以它照旧最后合并 —— 它属于"产物清单"，不属于"解析口径"。
                result.warnings = self._warnings
                result.stats["engine"] = "mineru"
                result.stats["mineru"] = {
                    "model_version": self.model_version,
                    "batch_id": self.bundle.get("batch_id"),
                    "content_list": str(self.bundle.get("content_list") or ""),
                    "full_md": str(self.bundle.get("full_md") or ""),
                    "raw_dir": str(self.bundle.get("raw_dir") or ""),
                    "zip_sha256": self.bundle.get("zip_sha256"),
                    "bbox_space": scale_note,
                    "params": dict(self.mineru_params),
                    "items": dict(sorted(self._items_seen.items())),
                    "figures_written": len(result.figures),
                }
                result.stats["stage_ms"] = stage_ms
                result.duration_ms = round(t_all.elapsed * 1000, 1)
                result.stats["quality"] = build_quality_report(
                    result, consistency_findings=[],
                    llm_repaired_count=0, llm_structure_changed=0)

                with Timer() as stat:
                    summary: dict = {}
                    if store_ir:
                        summary = writer.write(result, source_pdf=self.pdf_path)
                    if write_md:
                        summary["markdown"] = write_markdown_bundle(
                            result, self.md_dir, paper_id=self.paper_id,
                            assets_prefix="..")
                stage_ms["persist"] = stat.ms
                result.stats["stage_ms"] = stage_ms
                result.stats.update(summary)
                write_json(self.reports_dir / "parse_report.json", {
                    "paper_id": self.paper_id, "engine": "mineru",
                    "counts": result.counts(), "grades": result.grade_distribution(),
                    "stats": result.stats, "warnings": result.warnings,
                })
            finally:
                doc.close()
        return result

    # ------------------------------------------------------------------
    # 输入
    # ------------------------------------------------------------------
    def _load_items(self) -> list[dict]:
        path = self.bundle.get("content_list")
        if not path:
            idx = locate_bundle(self.bundle.get("raw_dir") or self.dir)
            path = idx.get("content_list")
        if not path or not Path(path).is_file():
            raise ParseError("MinerU 结果包里找不到 content_list.json")
        data = load_json(path)
        if isinstance(data, dict):
            # v2 形态可能是 {"pages":[[...],[...]]} 或包了一层；统一摊平成 item 列表，
            # 顺序仍是文档顺序（v2 按页分组，摊平后正好是按页顺序）。
            data = self._flatten(data)
        if not isinstance(data, list):
            raise ParseError("content_list.json 结构不可识别（既不是数组也没有可摊平的页）")
        return [it for it in data if isinstance(it, dict)]

    @staticmethod
    def _flatten(data: dict) -> list[dict]:
        out: list[dict] = []
        for key in ("content_list", "items", "result", "pages"):
            value = data.get(key)
            if isinstance(value, list):
                for row in value:
                    if isinstance(row, list):
                        out.extend(x for x in row if isinstance(x, dict))
                    elif isinstance(row, dict):
                        out.append(row)
                if out:
                    return out
        return out

    def _build_page_map(self, n_pages: int, result: ParseResult) -> list[int]:
        """页码范围 → "切片内序号 → 原文档页号"的映射表（没填页码范围时返回空表）。

        映射错了不会报错，只会让整篇内容出现在错误的页上（阅读器每页都有东西，看不出异常），
        所以这里把生效的映射同时记进 `stats.mineru.page_map`，事后能一眼核对。
        """
        ranges = str(self.mineru_params.get("page_ranges") or "")
        if not ranges:
            return []
        try:
            pages = parse_page_ranges(ranges, n_pages)
        except ValueError:
            self._warn(f"页码范围 {ranges!r} 无法解析（{PAGE_RANGES_HINT}）：按全篇页号处理")
            return []
        if not pages:
            self._warn(f"页码范围 {ranges!r} 在本文档（{n_pages} 页）里没有选中任何页")
            return []
        result.stats["mineru_page_map"] = {"page_ranges": ranges, "selected_pages": pages}
        log.info("页码范围 %s → 选中页 %s（content_list 的 page_idx 按此映射）", ranges, pages)
        return pages

    def _page_no(self, raw_index: int) -> int:
        """`content_list` 的 `page_idx`（0 起）→ 原文档页号（1 起）。

        全篇解析时就是 `idx + 1`；限定了页码范围时是 `选中页[idx]` —— 后者是真机实测出来的
        （`3-4` 返回 0,1；`2,4-6` 返回 0,1,2,3），不是猜的。
        """
        if not self._page_map:
            return raw_index + 1
        if 0 <= raw_index < len(self._page_map):
            return self._page_map[raw_index]
        self._warn(f"内容里出现第 {raw_index} 个切片页，但页码范围只选中了 "
                   f"{len(self._page_map)} 页：该条内容按原始页号处理")
        return raw_index + 1

    def _resolve_bbox_scale(self, items: list[dict],
                            sizes: dict[int, tuple[float, float]]) -> tuple[float, str]:
        """判定 ``bbox`` 是 0~1000 归一化还是页面 pt —— 判据只有两条，且都能自证。

        返回 ``(分母, 说明)``：``1000`` = 归一化（要乘页面尺寸）、``1`` = 已经是 pt。
        分母而不是"倍率"，是因为归一化那一支的换算是"坐标 ÷ 1000 × 页面尺寸"，
        写成倍率会在 pt 那一支退化成"坐标 × 页面尺寸"（1200pt 直接飞出去）。

        - 有任何一个坐标 > 1000.5 ⟹ **必然是 pt**（归一化坐标上限就是 1000）；
        - 有任何一个坐标 > 该 item 所在页的尺寸 ⟹ **必然是归一化**（pt 不可能落在页外）；
        - 两条都不成立时按**官方契约**取归一化 —— 这是文档写明的口径，也是官方 SDK
          的默认值，不能因为"某页内容恰好只占左上角"就把它当 pt（那种论文的坐标会整体
          缩到 1/1000，图会全部挤在左上角，是比"少一点误差"严重得多的错）。
        """
        if self.bbox_scale:
            return self.bbox_scale, f"指定 {self.bbox_scale:g}"
        max_coord = 0.0
        over_page = False
        for it in items:
            box = it.get("bbox")
            if not (isinstance(box, (list, tuple)) and len(box) >= 4):
                continue
            try:
                vals = [float(v) for v in box[:4]]
            except (TypeError, ValueError):
                continue
            max_coord = max(max_coord, *vals)
            page = int(it.get("page_idx") or 0) + 1
            w, h = sizes.get(page, (0.0, 0.0))
            if max(vals) > max(w, h) + 1.0:
                over_page = True
        if max_coord > 1000.5:
            note = f"pt（最大坐标 {max_coord:.0f} > 1000）"
            log.warning("MinerU bbox 看起来是 pt 坐标，按 pt 处理：%s", note)
            return 1.0, note
        if over_page:
            note = f"归一化 0~1000（最大坐标 {max_coord:.0f} 超过页宽/页高 ⟹ 只能是归一化）"
            return 1000.0, note
        note = f"归一化 0~1000（按官方契约；最大坐标 {max_coord:.0f}）"
        return 1000.0, note

    # ------------------------------------------------------------------
    # 结构
    # ------------------------------------------------------------------
    def _rect(self, item: dict, page_no: int, page_rect: Rect) -> Rect | None:
        box = item.get("bbox")
        if not (isinstance(box, (list, tuple)) and len(box) >= 4):
            return None
        try:
            x0, y0, x1, y1 = (float(v) for v in box[:4])
        except (TypeError, ValueError):
            return None
        # `bbox_scale` 是**分母**：1000 = 官方归一化坐标（乘页面尺寸才是 pt）；
        # 1 = 已经是 pt（不再乘，否则 1200pt 会被乘成 1200×612）。
        # 官方输出契约是 0~1000，见 `_resolve_bbox_scale` 的两条自证判据。
        denom = self.bbox_scale or 1000.0
        if denom > 1.0:
            x0, x1 = (x0 / denom * page_rect.width, x1 / denom * page_rect.width)
            y0, y1 = (y0 / denom * page_rect.height, y1 / denom * page_rect.height)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        out = Rect(max(x0, 0.0), max(y0, 0.0),
                   min(x1, page_rect.width), min(y1, page_rect.height))
        if out.width <= 0.5 or out.height <= 0.5:
            return None
        return out

    def _build_structure(
        self,
        doc: "fitz.Document",
        items: list[dict],
        page_rects: dict[int, Rect],
        rendered: dict[int, Path],
        result: ParseResult,
    ) -> tuple[list[PageInfo], list[Block], list[FigureBlock],
               list[FormulaBlock], list[TableBlock]]:
        """按 **content_list 的原始顺序** 生成块（顺序就是 MinerU 给的阅读顺序）。"""
        blocks: list[Block] = []
        figures: list[FigureBlock] = []
        formulas: list[FormulaBlock] = []
        tables: list[TableBlock] = []
        per_page_count: dict[int, int] = {}
        page_chars: dict[int, int] = {}
        total = max(1, len(items))

        # 先把"同一张图的多面板"归好组（详见 `_plan_figure_groups` 的说明）
        panel_group, groups = self._plan_figure_groups(items, page_rects)
        emitted: set[str] = set()

        for i, item in enumerate(items, 1):
            self._emit("structure", i, total)
            itype = str(item.get("type") or item.get("label") or "text").lower()
            self._items_seen[itype] = self._items_seen.get(itype, 0) + 1
            # ⚠️ 不能直接 `page_idx + 1`：限定了页码范围时它是**切片内序号**（见 `_page_no`）
            page_no = self._page_no(int(item.get("page_idx") or 0))
            if page_no < 1 or page_no > len(rendered):
                self._warn(f"item#{i}（{itype}）映射到第 {page_no} 页，超出 PDF 页数，已跳过")
                continue
            page_rect = page_rects[page_no]
            rect = self._rect(item, page_no, page_rect) or Rect(
                page_rect.x0 + 1, page_rect.y0 + 1, page_rect.x1 - 1,
                page_rect.y0 + 1 + CAPTION_LINE_HEIGHT)
            if itype in SKIP_TYPES:
                continue

            heading_text, level = self._heading_of(item, itype)
            if heading_text:
                b = self._add_block(blocks, page_no, rect, "heading", heading_text,
                                    font_size=10.5,
                                    meta={"mineru_text_level": level})
                per_page_count[page_no] = per_page_count.get(page_no, 0) + 1
                page_chars[page_no] = page_chars.get(page_no, 0) + b.char_count
                continue

            if itype in IMAGE_TYPES:
                key = panel_group.get(id(item))
                if key is not None:
                    # 多面板：**只在这一组的第一块上出图**，后面的面板跳过（它们已经并进来了）
                    if key in emitted:
                        continue
                    emitted.add(key)
                    fig, cap = self._figure_group(doc, groups[key], page_rects, blocks)
                else:
                    fig, cap = self._figure(item, page_no, rect, page_rect, blocks)
                if fig is not None:
                    figures.append(fig)
                    per_page_count[page_no] = per_page_count.get(page_no, 0) + 1
                    if cap is not None:
                        per_page_count[page_no] += 1
                continue

            if itype == "table":
                tb, cap = self._table(item, page_no, rect, page_rect, blocks)
                if tb is not None:
                    tables.append(tb)
                    per_page_count[page_no] = per_page_count.get(page_no, 0) + 1
                    if cap is not None:
                        per_page_count[page_no] += 1
                continue

            if itype == "equation":
                fb, host = self._formula(item, page_no, rect, blocks)
                if fb is not None:
                    formulas.append(fb)
                    per_page_count[page_no] = per_page_count.get(page_no, 0) + 1
                    page_chars[page_no] = page_chars.get(page_no, 0) + len(
                        fb.latex_final or "")
                continue

            text = self._text_of(item, itype)
            if not text.strip():
                continue
            b = self._add_block(blocks, page_no, rect, "para", text)
            per_page_count[page_no] = per_page_count.get(page_no, 0) + 1
            page_chars[page_no] = page_chars.get(page_no, 0) + b.char_count

        # 章节状态机与元数据标注复用本地解析那一份（口径必须一致，否则知识库判据两边不同）
        from backend.parsing.pipeline import _annotate_sections

        _annotate_sections(blocks)

        pages_info = [
            PageInfo(
                page_no=p, width=page_rects[p].width, height=page_rects[p].height,
                rotation=int(doc[p - 1].rotation),
                # 栏数照实记：MinerU 的 reading order 已经按栏给好，这里只做展示
                columns=1, gutter_x=None,
                image_path=str(rendered[p]) if rendered.get(p) else None,
                text_chars=page_chars.get(p, 0),
                block_count=per_page_count.get(p, 0),
                has_text_layer=True,
            )
            for p in sorted(rendered)
        ]
        # 每页 block_count 与真实块数对齐（页面载荷用它判断"这页有没有内容"）
        real: dict[int, int] = {}
        for b in blocks:
            real[b.page_no] = real.get(b.page_no, 0) + 1
        for p in pages_info:
            p.block_count = real.get(p.page_no, 0)
        return pages_info, blocks, figures, formulas, tables

    # ------------------------------------------------------------------
    # item → block
    # ------------------------------------------------------------------
    @staticmethod
    def _heading_of(item: dict, itype: str) -> tuple[str, int]:
        """是不是标题，以及 MinerU 给的层级（1 = 最高）。

        判定与官方契约一致：``title``/``section_header`` 类型，或 ``text`` 带正 ``text_level``。
        """
        if itype in HEADING_TYPES:
            text = MineruBuilder._text_of(item, itype).strip()
            try:
                level = int(item.get("level") or item.get("text_level") or 1)
            except (TypeError, ValueError):
                level = 1
            return text, max(level, 1)
        if itype == "text":
            try:
                level = int(item.get("text_level") or 0)
            except (TypeError, ValueError):
                level = 0
            if level > 0:
                return MineruBuilder._text_of(item, itype).strip(), level
        return "", 0

    @staticmethod
    def _text_of(item: dict, itype: str) -> str:
        """取 item 的正文文本（不同类型字段名不同，与官方 content_list 契约一一对应）。"""
        if itype == "list":
            rows = item.get("list_items")
            if isinstance(rows, list):
                return "\n".join(str(x) for x in rows if str(x).strip())
        if itype == "code":
            body = item.get("code_body") or item.get("text") or ""
            lang = str(item.get("sub_type") or "").strip()
            return f"```{lang}\n{body}\n```" if body else ""
        for key in ("text", "content", "body", "table_body"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    def _add_block(self, blocks: list[Block], page_no: int, rect: Rect,
                   btype: str, text: str, *, col: int = 0, font_size: float = 0.0,
                   meta: dict | None = None, is_inline: bool = False) -> Block:
        self._order += 1
        b = Block(
            id=f"tmp-{self._order}", paper_id=self.paper_id, page_no=page_no,
            order_in_page=0, order_global=self._order, type=btype, col=col,
            rect=rect, text=text, text_clean=normalize_text(text),
            source_text=text, source_kind="mineru",
            font_size=font_size, line_count=max(1, text.count("\n") + 1),
            is_inline=is_inline, meta=dict(meta or {}),
        )
        b.finalize()
        blocks.append(b)
        return b

    def _plan_figure_groups(self, items: list[dict],
                            page_rects: dict[int, Rect]
                            ) -> tuple[dict[int, str], dict[str, dict]]:
        """把**同一页里横向并排的面板**归到一张图下。

        真机实测（DeepSeek-V3.2 p5）：论文里是 Figure 2（(a) BrowseComp / (b) SWE 两个面板）
        与 Figure 3（(a) Prefilling / (b) Decoding 两个面板），而 MinerU 输出 **4 个 chart item**，
        并且把 "Figure 2 | RL training curve…" 这条**图级图注挂在下一行那张图上**（它的
        `chart_caption` 里混着图级图注与自己的面板标签）。照单全收 → 侧栏"插图"数量比论文里的
        图数多（用户报"标注 6 张图，实际 4 张"）。

        这里的做法（两条都很保守，宁可少合并）：

        1. **分组**：只合并"顶边与底边都对得齐（≤ `PANEL_ALIGN_PT`）+ 水平间隙够小
           （≤ `PANEL_GAP_PT`）"的横向并排面板；竖排相邻的**不合并**（那更可能是上下两张不同的图）。
        2. **图注**：把该页的"图级图注"（`caption_number()` 认得出 `Figure N` / `图 N` 的）
           按**阅读顺序**依次配给各组，各面板自己的标签（`(a) …`）追加在后面。
           为什么按顺序配而不是信 MinerU 的挂法：实测它会把图注挂到**下一行**的图上；
           同一页的图注顺序与图组顺序都是文档顺序，按顺序配比信它的错误挂法更可靠。
           图注条数与组数不一致时**不做重配**（退回各成员自己的图注），避免瞎配。

        返回 ``(id(item) → 组键, 组键 → 组信息)``；不成组（单面板）的 item 不出现在表里。
        """
        per_page: dict[int, list[dict]] = {}
        for it in items:
            itype = str(it.get("type") or it.get("label") or "").lower()
            if itype not in IMAGE_TYPES:
                continue
            pno = self._page_no(int(it.get("page_idx") or 0))
            if pno not in page_rects:
                continue
            per_page.setdefault(pno, []).append(it)

        owner: dict[int, str] = {}
        groups: dict[str, dict] = {}
        for pno, panels in per_page.items():
            if len(panels) < 2:
                continue
            rects = [self._rect(it, pno, page_rects[pno]) for it in panels]
            parent = list(range(len(panels)))

            def find(x: int, _parent=parent) -> int:
                while _parent[x] != x:
                    _parent[x] = _parent[_parent[x]]
                    x = _parent[x]
                return x

            for a in range(len(panels)):
                for b in range(a + 1, len(panels)):
                    if rects[a] is None or rects[b] is None:
                        continue
                    if not _panels_in_a_row(rects[a], rects[b]):
                        continue
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra

            buckets: dict[int, list[int]] = {}
            for idx in range(len(panels)):
                buckets.setdefault(find(idx), []).append(idx)
            merged = {k: v for k, v in buckets.items() if len(v) > 1}
            if not merged:
                continue

            # 阅读顺序：(顶边, 左边)
            order = sorted(merged, key=lambda k: (
                min(rects[i].y0 for i in merged[k]),
                min(rects[i].x0 for i in merged[k])))

            # 该页的"图级图注"（按文档顺序）与各面板自己的标签
            fig_caps: list[str] = []
            labels: dict[int, list[str]] = {}
            for idx, it in enumerate(panels):
                for cap in _captions_of(it):
                    if _is_figure_level_caption(cap):
                        if cap not in fig_caps:
                            fig_caps.append(cap)
                    else:
                        labels.setdefault(idx, []).append(cap)

            assign = fig_caps if len(fig_caps) == len(order) else []
            if fig_caps and not assign:
                self._warn(
                    f"p{pno}: 图级图注 {len(fig_caps)} 条与面板组 {len(order)} 组对不上，"
                    f"这一页不做图注重配（按 MinerU 原样保留）")

            for pos, key in enumerate(order):
                members = sorted(merged[key], key=lambda i: (rects[i].y0, rects[i].x0))
                rect = rects[members[0]]
                for i in members[1:]:
                    rect = rect.union(rects[i])
                parts: list[str] = []
                if assign:
                    parts.append(assign[pos])
                for i in members:
                    for txt in labels.get(i, []):
                        if txt not in parts:
                            parts.append(txt)
                if not parts:      # 没有可用图注：退回成员自己的原图注（含被重配走的那条）
                    parts = [c for i in members for c in _captions_of(panels[i])]
                gkey = f"p{pno}-g{pos}"
                groups[gkey] = {
                    "key": gkey, "page_no": pno, "rect": rect,
                    "members": members,
                    "panels": [panels[i] for i in members],
                    "caption": "\n".join(p for p in parts if p).strip(),
                }
                for i in members:
                    owner[id(panels[i])] = gkey
            self._items_seen["figure_panels_merged"] = (
                self._items_seen.get("figure_panels_merged", 0)
                + sum(len(v) - 1 for v in merged.values()))
        return owner, groups

    def _figure_group(self, doc: "fitz.Document", group: dict,
                      page_rects: dict[int, Rect],
                      blocks: list[Block]) -> tuple[FigureBlock | None, Block | None]:
        """把一组面板拼成**一张图**：bbox 取并集、图从 PDF 整块裁（不是拿其中一个面板的图）。"""
        page_no = int(group["page_no"])
        rect: Rect = group["rect"]
        caption = str(group.get("caption") or "")
        host = self._add_block(blocks, page_no, rect, "figure", caption,
                               meta={"mineru_panels": len(group["panels"])})
        fig = FigureBlock(
            block_id=host.id, paper_id=self.paper_id, page_number=page_no, rect=rect,
            caption_block_id=None, caption=caption,
            subfigure_count=len(group["panels"]),
            # `source: "online"` + `merged`：区域层按"精确来源"直接用这个并集 bbox；
            # `img_paths` 留档是哪些面板拼出来的，便于人工核对
            audit={"source": "online", "merged": True,
                   "panel_count": len(group["panels"]),
                   "img_paths": [p.get("img_path") for p in group["panels"]]},
            status="ok",
        )
        # 图从 PDF 整块裁：等块 ID 定稿后再落盘（见 `_finalize_assets`）
        self._pending_assets.append({"fig": fig, "host": host, "kind": "crop",
                                     "page_no": page_no, "rect": rect})

        caption_block = None
        if caption:
            caption_block = self._add_caption_block(
                blocks, page_no, rect, page_rects[page_no], caption, host.id)
            fig.caption_block_id = caption_block.id
        self._items_seen["figures_merged"] = self._items_seen.get("figures_merged", 0) + 1
        return fig, caption_block

    def _figure(self, item: dict, page_no: int, rect: Rect, page_rect: Rect,
                blocks: list[Block]) -> tuple[FigureBlock | None, Block | None]:
        """插图（单面板）：MinerU 已裁好的图 + 版面 bbox；图注单独补一个块。"""
        captions = item.get("image_caption") or item.get("chart_caption") or []
        if isinstance(captions, str):
            captions = [captions]
        caption = "\n".join(str(c).strip() for c in captions if str(c).strip())
        host = self._add_block(
            blocks, page_no, rect, "figure", caption, meta={"mineru_img_path": item.get("img_path")})

        self._figure_no += 1
        fig = FigureBlock(
            block_id=host.id, paper_id=self.paper_id, page_number=page_no, rect=rect,
            caption_block_id=None, caption=caption,
            # `source: "online"` 是给区域层看的信号：bbox 是版面模型给的准边界，
            # 不要再收紧/贴边/切分（见 figure_regions._has_precise_bbox）
            audit={"source": "online", "img_path": item.get("img_path"),
                   "mineru_type": item.get("type")},
            status="ok",
        )
        if item.get("img_path"):
            # 复制 MinerU 自己裁好的图（它比我们从 PDF 重裁更准）：同样等 ID 定稿后再落盘
            self._pending_assets.append({"fig": fig, "host": host, "kind": "copy",
                                         "ref": item.get("img_path")})
        else:
            self._warn(f"p{page_no}: MinerU 没给插图文件（img_path={item.get('img_path')!r}），"
                       f"阅读器仍会按 bbox 从 PDF 裁图")

        caption_block = None
        if caption:
            caption_block = self._add_caption_block(
                blocks, page_no, rect, page_rect, caption, host.id)
            fig.caption_block_id = caption_block.id
        return fig, caption_block

    def _finalize_assets(self, doc: "fitz.Document") -> None:
        """块 ID 定稿后再把插图落盘成 ``figures/<block_id>.<ext>``。

        为什么必须放到这一步：资产是在结构阶段"边解析边"要的，而块 ID 要到 ``assign_ids_in_order``
        / ``rebind_block_ids`` 之后才定稿。早先直接用当时的**临时 ID**（``tmp-1``）命名，
        于是 `meta["image_rel"]` 指向一个无语义、且**每次重解析都可能换主**的文件名
        （临时编号来自本次构建的计数），导出的 Markdown 与磁盘文件之间的对应关系会悄悄漂掉。
        """
        from backend.parsing.extract.render import render_crop_png

        for job in self._pending_assets:
            host: Block = job["host"]
            fig = job["fig"]
            dest: Path | None = None
            try:
                if job["kind"] == "copy":
                    dest = self._copy_asset(job["ref"], host.id)
                    if dest is None:
                        self._warn(f"p{fig.page_number}: 读不到 MinerU 的插图文件"
                                   f"（img_path={job['ref']!r}），阅读器仍会按 bbox 从 PDF 裁图")
                        continue
                else:
                    dest = self.figures_dir / f"{host.id}.png"
                    dest.write_bytes(render_crop_png(doc, job["page_no"], job["rect"],
                                                    dpi=self.crop_dpi))
            except Exception as exc:  # noqa: BLE001 —— 一张图的资产失败不该让它整张消失
                self._warn(f"p{fig.page_number}: 插图落盘失败（{exc}），"
                           f"阅读器仍会按 bbox 从 PDF 裁图")
                continue
            fig.image_path = str(dest)
            host.meta["image_rel"] = f"../figures/{dest.name}"

    def _copy_asset(self, img_path: Any, block_id: str) -> Path | None:
        """把 MinerU 结果包里的插图复制进 ``figures/``（保持在结果包内，拒绝路径穿越）。"""
        raw = self.bundle.get("raw_dir")
        ref = str(img_path or "").strip()
        if not raw or not ref:
            return None
        base = Path(self.bundle.get("content_list") or "").parent if self.bundle.get(
            "content_list") else Path(raw)
        for candidate in (base / ref, Path(raw) / ref):
            try:
                src = candidate.resolve()
                root = Path(raw).resolve()
                if root not in src.parents or not src.is_file():
                    continue
            except OSError:
                continue
            suffix = src.suffix.lower() or ".png"
            dest = self.figures_dir / f"{block_id}{suffix}"
            shutil.copy2(src, dest)
            return dest
        return None

    def _add_caption_block(self, blocks: list[Block], page_no: int, fig_rect: Rect,
                           page_rect: Rect, caption: str, figure_id: str) -> Block:
        """给图/表补一个图注块。

        MinerU 把图注**并进了图/表 item 的 caption 字段**，没有单独给它的 bbox，所以这里
        按"紧贴图体下方的一行"合成一个矩形。它有两个用途，都不依赖坐标精确：

        1. 阅读器/知识库要有一条**可翻译的图注文本**（图注是论文里信息密度很高的句子）；
        2. 区域层用 ``caption_block_id`` 找图注文本（拿图号做去重、给卡片配文）。

        坐标只影响阅读器里"跳到图注"的高亮位置，所以估算即可（在线解析的区域边界
        本来就不走图注，见 :meth:`_figure` 里 ``source="online"`` 那段）。
        """
        top = fig_rect.y1 + 1.0
        bottom = min(top + CAPTION_LINE_HEIGHT, page_rect.y1 - 0.5)
        if bottom - top < 2.0:                      # 图体已经贴到页底：放宽到图体内部
            top = max(page_rect.y0, fig_rect.y1 - CAPTION_LINE_HEIGHT - 1.0)
            bottom = min(page_rect.y1 - 0.5, top + CAPTION_LINE_HEIGHT)
        rect = Rect(fig_rect.x0, top, max(fig_rect.x1, fig_rect.x0 + 1.0), bottom)
        return self._add_block(blocks, page_no, rect, "caption", caption,
                               meta={"figure_id": figure_id, "mineru_caption": True})

    def _table(self, item: dict, page_no: int, rect: Rect, page_rect: Rect,
               blocks: list[Block]) -> tuple[TableBlock | None, Block | None]:
        """表格：``table_body`` 是 HTML，正文取纯文本、原 HTML 留在 meta。"""
        body_html = item.get("table_body") or item.get("text") or ""
        if isinstance(body_html, list):
            body_html = "".join(str(x) for x in body_html)
        body_html = str(body_html or "")
        text = html_table_to_text(body_html)
        if not text.strip() and not body_html.strip():
            self._warn(f"p{page_no}: MinerU 给了一个空表格，已跳过")
            return None, None

        caption = _join_caption(item.get("table_caption"))
        host = self._add_block(
            blocks, page_no, rect, "table", text,
            meta={"table_html": body_html[:200000],
                  "mineru_img_path": item.get("img_path")})

        caption_block = None
        if caption:
            caption_block = self._add_caption_block(
                blocks, page_no, rect, page_rect, caption, host.id)

        rows, cols = _table_shape(item, body_html)
        tb = TableBlock(
            block_id=host.id, paper_id=self.paper_id, page_number=page_no, rect=rect,
            caption_block_id=caption_block.id if caption_block else None,
            caption=caption,
            row_count=rows, col_count=cols, has_borders=bool(body_html.strip()),
            grid=[], status="grid" if body_html.strip() else "image_only",
        )
        if item.get("img_path"):
            # 表格裁图同样等 ID 定稿后再复制（见 `_finalize_assets`）
            self._pending_assets.append({"fig": tb, "host": host, "kind": "copy",
                                         "ref": item.get("img_path")})
        return tb, caption_block

    def _formula(self, item: dict, page_no: int, rect: Rect,
                 blocks: list[Block]) -> tuple[FormulaBlock | None, Block | None]:
        """公式：MinerU 直接给 LaTeX，按 A 级采用（不再跑本地那套候选/校验）。"""
        latex = str(item.get("text") or item.get("content") or "").strip()
        latex = strip_math_wrappers(latex)
        if not latex:
            self._warn(f"p{page_no}: MinerU 给了一个空公式，已跳过")
            return None, None
        fmt = str(item.get("text_format") or "").lower()
        is_inline = fmt in ("inline", "inline_equation")
        host = self._add_block(blocks, page_no, rect, "formula", latex,
                               is_inline=is_inline, meta={"latex": latex})
        fb = FormulaBlock(
            block_id=host.id, paper_id=self.paper_id, page_number=page_no, rect=rect,
            raw_text=latex, vector_data=_online_audit(),
            latex_candidates=[latex], latex_final=latex,
            confidence=0.95, grade="A", source="mineru", status="validated",
            validation={"online": True, "model_version": self.model_version},
            is_inline=is_inline,
        )
        return fb, host

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _relink(result: ParseResult, by_tmp_id: dict[str, Block]) -> None:
        """把插图/公式/表格对块的外键改到**最终** block_id 上（见调用点的说明）。

        ``by_tmp_id`` 是"编号前的 ID → 块对象"，块对象在编号时被就地改 ``id``，
        所以拿它查一次就能得到终值。
        """
        def final(old_id: str | None) -> str | None:
            if not old_id:
                return old_id
            host = by_tmp_id.get(old_id)
            return host.id if host is not None else old_id

        for fig in result.figures:
            fig.block_id = final(fig.block_id) or fig.block_id
            fig.caption_block_id = final(fig.caption_block_id)
        for f in result.formulas:
            f.block_id = final(f.block_id) or f.block_id
        for t in result.tables:
            t.block_id = final(t.block_id) or t.block_id
            t.caption_block_id = final(t.caption_block_id)
        # 块自己的元数据里也存了指向（阅读器/区域层用它把图挂到块上）
        for b in result.blocks:
            for key in ("figure_id", "table_id", "caption_block_id"):
                if b.meta.get(key):
                    b.meta[key] = final(str(b.meta[key]))

    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)
            log.info("在线解析告警：%s", message)


# --------------------------------------------------------------------------
# 纯函数辅助
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<\s*br\s*/?\s*>")
_BLOCK_END_RE = re.compile(r"(?i)</\s*(p|div|li|tr|h[1-6]|table|caption|td|th)\s*>")


def _captions_of(item: dict) -> list[str]:
    """取 item 的图注文本（`image_caption` / `chart_caption`，两种类型字段名不同）。"""
    raw = item.get("image_caption") or item.get("chart_caption") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(c).strip() for c in (raw or []) if str(c).strip()]


def _is_figure_level_caption(text: str) -> bool:
    """这条是**图级图注**（`Figure 3 | …` / `Fig. 2 …` / `图 1 …`）还是面板标签（`(a) …`）。

    判据直接用既有的 `caption_number()`（"图注里那个图号"的唯一实现），
    不在这里再写一套正则 —— 两套正则必然会在某篇论文上给出不同答案。
    """
    from backend.parsing.structure.figure_cut import caption_number

    return bool(caption_number(text or ""))


def _panels_in_a_row(a: Rect, b: Rect) -> bool:
    """两个矩形是不是"同一张图的左右两个面板"（见 `PANEL_ALIGN_PT` 的说明）。

    条件（全部满足才算）：
    1. 横向并排：一个在左、一个在右，水平间隙在 `[0, PANEL_GAP_PT]`；
    2. 顶边与底边都对得齐：错开不超过 `PANEL_ALIGN_PT`。

    第 2 条是**防误合并**的关键：左右两栏各一张**不同**的图，顶边几乎不可能同时对齐；
    而同一张图的面板是排版工具一次对齐出来的（实测误差 1pt 以内）。
    """
    left, right = (a, b) if a.x0 <= b.x0 else (b, a)
    gap = right.x0 - left.x1
    if gap < -1.0 or gap > PANEL_GAP_PT:
        return False
    return (abs(left.y0 - right.y0) <= PANEL_ALIGN_PT
            and abs(left.y1 - right.y1) <= PANEL_ALIGN_PT)


def html_table_to_text(body: str) -> str:
    """HTML 表格 → 纯文本（保留行/单元格分隔，供翻译与检索）。

    为什么要自己写：项目里没有 HTML 解析依赖，而 ``table_body`` 只是
    MinerU 生成的简单 ``<table><tr><td>`` 片段。这里把 ``</td>`` 变成 ``\t``、
    ``</tr>`` 变成换行，去掉其余标签并反转义实体 —— 对翻译模型来说这比一串标签好懂得多。
    """
    if not body:
        return ""
    text = _BR_RE.sub("\n", str(body))
    text = re.sub(r"(?i)</\s*(td|th)\s*>", "\t", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    lines = []
    for raw in text.split("\n"):
        cells = [c.strip() for c in raw.split("\t") if c.strip()]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines).strip()


def _join_caption(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(str(v).strip() for v in value if str(v).strip())
    return ""


def _table_shape(item: dict, body_html: str) -> tuple[int, int]:
    """行列数：优先用 MinerU 给的 ``num_rows``/``num_cols``，否则从 HTML 数。"""
    try:
        rows = int(item.get("num_rows") or 0)
    except (TypeError, ValueError):
        rows = 0
    try:
        cols = int(item.get("num_cols") or 0)
    except (TypeError, ValueError):
        cols = 0
    if rows <= 0:
        rows = len(re.findall(r"(?i)<\s*tr\b", body_html))
    if cols <= 0:
        first = re.search(r"(?is)<\s*tr\b.*?</\s*tr\s*>", body_html)
        cols = len(re.findall(r"(?i)<\s*(?:td|th)\b", first.group(0))) if first else 0
    return rows, cols


def strip_math_wrappers(latex: str) -> str:
    """去掉 ``$$…$$`` / ``$…$`` / ``\\[…\\]`` 外层 —— 渲染端自己会按 display/inline 决定。"""
    text = (latex or "").strip()
    for left, right in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
        if text.startswith(left) and text.endswith(right) and len(text) > len(left) + len(right):
            return text[len(left):-len(right)].strip()
    return text


def _online_audit():
    """给在线公式一个"证据"占位（``VectorAudit`` 的 kind 标明来源，便于统计区分）。"""
    from backend.parsing.models import VectorAudit

    return VectorAudit(kind="online", notes=["MinerU 在线解析直接给出 LaTeX"])


# --------------------------------------------------------------------------
# 兼容入口：任务处理器只需要"给我一篇的 ParseResult"
# --------------------------------------------------------------------------


def build_from_bundle(pdf_path: str | Path, paper_id: str, paper_dir: str | Path,
                      bundle: dict, **kwargs) -> ParseResult:
    """一次在线解析的完整入口（含 zip 指纹记录，便于排查"两次结果不一样"）。"""
    zip_path = bundle.get("zip_path")
    if zip_path and Path(zip_path).is_file():
        bundle.setdefault("zip_sha256", sha256_file(zip_path))
    builder = MineruBuilder(pdf_path, paper_id, paper_dir, bundle=bundle, **kwargs)
    return builder.build()


__all__ = [
    "MineruBuilder", "build_from_bundle", "html_table_to_text",
    "strip_math_wrappers",
]
