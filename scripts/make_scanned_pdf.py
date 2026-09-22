"""把任意 PDF 转成"扫描件"（只剩图像、没有文本层），用于验证扫描件兜底链路。

做法：逐页按给定 dpi 渲染成灰度位图，再把这些位图重新拼成一个新 PDF。
输出 PDF 里**一个字符都没有**，`page.get_text()` 返回空串——这正是影印论文的形态。

用法：
    python scripts/make_scanned_pdf.py --src 论文.pdf --out scanned.pdf --pages 1-3
    python scripts/make_scanned_pdf.py --src 论文.pdf --out scanned.pdf --dpi 150 --gray
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf as fitz  # noqa: E402


def parse_pages(spec: str, total: int) -> list[int]:
    """``"1-3,7"`` → ``[1,2,3,7]``（1-based）。空串 = 全部。"""
    if not spec.strip():
        return list(range(1, total + 1))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return [p for p in dict.fromkeys(out) if 1 <= p <= total]


def make_scanned(src: Path, out: Path, pages: list[int], dpi: int = 150,
                 gray: bool = False) -> dict:
    src_doc = fitz.open(str(src))
    dst = fitz.open()
    try:
        for pno in pages:
            page = src_doc[pno - 1]
            zoom = dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False,
                                  colorspace=fitz.csGRAY if gray else fitz.csRGB)
            png = pix.tobytes("png")
            new_page = dst.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=png)
        out.parent.mkdir(parents=True, exist_ok=True)
        dst.save(str(out), garbage=4, deflate=True)
    finally:
        src_doc.close()
        dst.close()

    # 自检：新 PDF 必须真的没有文本层
    check = fitz.open(str(out))
    try:
        chars = sum(len((p.get_text("text") or "").strip()) for p in check)
        images = sum(len(p.get_images(full=True)) for p in check)
    finally:
        check.close()
    return {"pages": len(pages), "text_chars": chars, "images": images,
            "size_kb": round(out.stat().st_size / 1024, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pages", default="", help='页范围，如 "1-3,7"；默认全部')
    ap.add_argument("--dpi", type=int, default=150, help="渲染 dpi（模拟扫描分辨率）")
    ap.add_argument("--gray", action="store_true", help="灰度（更像影印件，体积更小）")
    args = ap.parse_args()

    src, out = Path(args.src), Path(args.out)
    if not src.is_file():
        print(f"源 PDF 不存在：{src}")
        return 2
    probe = fitz.open(str(src))
    try:
        total = probe.page_count
    finally:
        probe.close()
    pages = parse_pages(args.pages, total)
    if not pages:
        print("没有可处理的页")
        return 2

    info = make_scanned(src, out, pages, dpi=args.dpi, gray=args.gray)
    print(f"已生成扫描件：{out}")
    print(f"  页数 {info['pages']} | 文本层字符 {info['text_chars']}（0 = 纯扫描件）"
          f" | 图像 {info['images']} | 体积 {info['size_kb']} KB")
    if info["text_chars"]:
        print("  警告：仍有文本层，请调低 --dpi 或检查源 PDF")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
