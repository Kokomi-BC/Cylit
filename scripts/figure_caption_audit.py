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
    return missing, {
        "captions": len(captions), "regions": len(figs),
        "missing_detail": [(n, captions[n][0][0], captions[n][0][1]) for n in missing],
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
        tag = "FAIL" if missing else " OK "
        print(f"[{tag}] {pid}：PDF 图注 {info['captions']} 个 / 产出插图 {info['regions']} 张"
              + (f"，**缺 {len(missing)} 张**：{missing}" if missing else ""))
        for n, page, line in info["missing_detail"]:
            print(f"        缺 Figure {n}（p{page}）：{line}")
        bad += 1 if missing else 0
    print(f"\n共 {len(papers)} 篇，缺图 {bad} 篇。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
