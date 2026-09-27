"""缺图自检：**PDF 文字层里的图注** vs **系统真正产出的插图区域**。

为什么需要这个脚本（实测事故，2026-09-27）：`3360-mmdfnd` 那篇的 Figure 4 **整张图没有产出** ——
它的图注行被解析阶段的阅读顺序/行聚类**拼进了正文段落块**里：

    块 3360mmdfndmu-p08-b21（y 499.5–600.0，820 字）：
    '…concatenating features from different **Figure 4: T-SNE visualizations display the prediction
     results** views. (2) w/o ITA: The CLIP encoder…'

而插图检测的前提是"图注自成一个块、且以 `Figure N` 开头"，于是这张图在解析阶段就没了；
下游（裁图 / 阅读器 / 知识库）再怎么修都补不回来。这类问题**没有报错、没有日志**，
除非拿 PDF 文字层去对账，否则只能靠人看出来"这篇好像少了一张图"。

用法：
    python scripts/figure_caption_audit.py            # 库里所有有 source.pdf 的论文
    python scripts/figure_caption_audit.py --paper <id>
退出码：发现缺图 → 1（可接进 CI / 自检流水线）。

口径（刻意保守，避免把正文里的引用当成图注）：
- **图注** = 文字层某一行**以** `Figure/Fig./图 + 编号` 开头（`Figure 3: …`、`Fig. 2. …`）；
  `… as shown in Figure 3` 这种行内引用不算。
- **产出** = `figure_regions.all_regions()` 里 `kind == "figure"` 的条目（编号取自图注文本）。
- 只在"图注的图号在产出里完全没有"时判缺图；同一图号多条 region（分块图/跨页续图）不算。

**顶部被收窄**（第二种病，2026-09-27）：区域收紧后的上边界比解析层给的图体 raw bbox
低出一大截（> :data:`TOP_TRIM_MAX`）—— 裁出来就是"图的上半截没了"（实测 9984 p23 图 9：
raw 78.9 被收到 185.9，只剩坐标轴标签 + 图例）。这条也判失败。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from backend.repositories.mysql.session import session_scope  # noqa: E402
from backend.repositories.mysql.models import Paper  # noqa: E402
from backend.services.paper import figure_regions, paper_service  # noqa: E402

#: 行首形态的图注（`Figure 3: …` / `Figure 3. …` / `Fig. 2 …` / `图 2 …`）
CAPTION_RE = re.compile(r"^\s*\**\s*(?:Figure|Fig\.?|图)\s*\.?\s*(\d{1,3})\b", re.I)

#: "真图注"形态：编号后紧跟标点，或整行很短（正文引用句 `Figure 2 shows that …` 又长又没标点）
_REAL_CAPTION = re.compile(r"^(\s*\**\s*(?:Figure|Fig\.?|图)\s*\.?\s*\d{1,3})(.*)$", re.I)

#: 区域上边界比 raw bbox 低多少就算"上半截被裁掉"（pt）。`_snap_edges` 贴边最多挪 16pt，
#: 所以 24pt 是"正常贴边"与"被另一个块的边界顶下来"之间的分界。
TOP_TRIM_MAX = 24.0


def _is_real_caption(line: str) -> bool:
    """这条行首 **像不像真图注**（页内对账用）。

    ⚠️ 必须比解析层的 `_looks_like_caption_line` 严：那里放宽"短行也算"是为了不漏掉
    分段的界标（放宽只影响墨迹的上界，安全）；而**对账**用宽口径会把正文引用句算成图注，
    凭空报"这页的图没产出"——实测 p26/p27/p32 的 `Figure 18 shows that …`、
    `Figure 22 shows that …`、`Figure 28 shows that` 都是正文句子。所以这里只认**带标点**的。
    """
    m = _REAL_CAPTION.match(line.strip())
    if not m:
        return False
    return m.group(2).strip()[:1] in (":", "：", ".", "．", "—", "–", "-", "|")


def pdf_captions(pdf: Path) -> dict[str, list[tuple[int, str]]]:
    """{图号: [(页, 整行)]} —— 只看行首形态。"""
    out: dict[str, list[tuple[int, str]]] = {}
    doc = fitz.open(pdf)
    try:
        for page_no, page in enumerate(doc, start=1):
            for raw in page.get_text("text").split("\n"):
                m = CAPTION_RE.match(raw)
                if m:
                    out.setdefault(m.group(1), []).append((page_no, raw.strip()[:90]))
    finally:
        doc.close()
    return out


def pdf_real_captions_by_page(pdf: Path) -> dict[int, set[str]]:
    """{页: {图号}} —— 只算"真图注"形态的行（带标点或很短），用于页内对账。

    页内对账能抓住"整篇的图号都在、但其中一页的图被上面那张吞掉了"这类问题
    （整篇按图号对账时它不报错）。
    """
    out: dict[int, set[str]] = {}
    doc = fitz.open(pdf)
    try:
        for page_no, page in enumerate(doc, start=1):
            for raw in page.get_text("text").split("\n"):
                m = CAPTION_RE.match(raw)
                if m and _is_real_caption(raw):
                    out.setdefault(page_no, set()).add(m.group(1))
    finally:
        doc.close()
    return out


def audit(paper_id: str) -> tuple[list[str], dict]:
    paths = paper_service.paper_paths(paper_id)
    pdf = Path(paths["root"]) / "source.pdf"
    if not pdf.is_file():
        return [], {"skipped": "没有 source.pdf"}
    regions = [r for page in figure_regions.all_regions(paper_id).values() for r in page]
    figs = [r for r in regions if r["kind"] == "figure"]
    produced = {str(r["number"]) for r in figs if r["number"]}
    captions = pdf_captions(pdf)
    missing = sorted((n for n in captions if n not in produced), key=int)
    # 顶部被收窄：收紧后的上边界比解析层的图体 raw bbox 低出一大截
    trimmed = []
    for r in figs:
        raw = r.get("raw_bbox") or r["bbox"]
        drop = float(raw[1]) - float(r["bbox"][1])
        if drop > TOP_TRIM_MAX:
            trimmed.append((r["number"] or "?", int(r["page"]), round(drop, 1),
                            [round(v, 1) for v in r["bbox"]],
                            [round(v, 1) for v in raw]))
    # 反向：区域上边界比图体**高出**一大截 → 多半把上一张图/正文吞进来了（"多截了"）
    spilled = [(r["number"] or "?", int(r["page"]),
                round(float(r["bbox"][1]) - float((r.get("raw_bbox") or r["bbox"])[1]), 1))
               for r in figs
               if float(r["bbox"][1]) - float((r.get("raw_bbox") or r["bbox"])[1]) > TOP_TRIM_MAX]
    # 页内对账：某页有真图注、但该页产出的插图里没有这个图号 → 这张图多半被同页另一张吞掉了
    by_page: dict[int, set[str]] = {}
    for r in figs:
        if r["number"]:
            by_page.setdefault(int(r["page"]), set()).add(str(r["number"]))
    orphan_pages = []
    for page, numbers in sorted(pdf_real_captions_by_page(pdf).items()):
        lack = sorted(numbers - by_page.get(page, set()), key=int)
        if lack:
            orphan_pages.append((page, lack))
    return missing, {
        "captions": len(captions), "regions": len(figs),
        "missing_detail": [(n, captions[n][0][0], captions[n][0][1]) for n in missing],
        "trimmed": trimmed, "spilled": spilled, "orphan_pages": orphan_pages,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", default="", help="只查这一篇（默认查全部）")
    args = ap.parse_args()

    if args.paper:
        papers = [args.paper]
    else:
        with session_scope() as sess:
            papers = [p.id for p in sess.query(Paper).order_by(Paper.created_at.asc()).all()]

    bad = 0
    for pid in papers:
        try:
            missing, info = audit(pid)
        except Exception as exc:  # noqa: BLE001 —— 单篇失败不该中断整轮对账
            print(f"[SKIP] {pid}：{type(exc).__name__}: {str(exc)[:120]}")
            continue
        if info.get("skipped"):
            print(f"[SKIP] {pid}：{info['skipped']}")
            continue
        trimmed = info.get("trimmed") or []
        spilled = info.get("spilled") or []
        orphans = info.get("orphan_pages") or []
        tag = "FAIL" if (missing or trimmed or spilled or orphans) else " OK "
        print(f"[{tag}] {pid}：PDF 图注 {info['captions']} 个 / 产出插图 {info['regions']} 张"
              + (f"，**缺 {len(missing)} 张**：{missing}" if missing else "")
              + (f"，**顶部被收窄 {len(trimmed)} 张**" if trimmed else "")
              + (f"，**顶部多出 {len(spilled)} 张**" if spilled else "")
              + (f"，**{len(orphans)} 页图号对不上**" if orphans else ""))
        for n, page, line in info["missing_detail"]:
            print(f"        缺 Figure {n}（p{page}）：{line}")
        for n, page, drop, bbox, raw in trimmed:
            print(f"        Figure {n}（p{page}）上边界比图体低 {drop}pt："
                  f"区域 y0={bbox[1]} / 图体 y0={raw[1]}")
        for n, page, extra in spilled:
            print(f"        Figure {n}（p{page}）上边界比图体高 {extra}pt（可能吞了上一张图/正文）")
        for page, lack in orphans:
            print(f"        p{page} 有图注 {lack}，但该页没有产出这些图号（多半被同页另一张吞了）")
        bad += 1 if (missing or trimmed or spilled or orphans) else 0
    print(f"\n共 {len(papers)} 篇，对账不通过 {bad} 篇。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
