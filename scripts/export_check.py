"""导出下载对账（走 HTTP，不依赖浏览器）——「下载 Markdown」「下载打包 zip」的验收脚本。

用法::

    python cli/pr.py serve --port 8710        # 另开终端
    python scripts/export_check.py                        # 默认拿一篇有译文的论文跑
    python scripts/export_check.py turboquant-online-vector-iclr-26

查四件事（都是用户 2026-09-28 明确要求的口径）：

1. 单文件下载的三个版本（原文 / 译文 / 中英对照）里**没有页标识**
   （``<!-- page N -->`` 与 ``## 第 N 页``）；
2. 单文件里的图片是**绝对地址**（下载到本地点开就能看图）；
3. zip 里有原 PDF、三个版本的 Markdown、以及**被 Markdown 引用到的**插图；
4. zip 里的图片是**相对路径**，解压后每个引用都落到真实文件（离线可看）。

没跑过页级翻译的论文：三个 md 允许为空，此时 zip 里**不写**空 md（只给 PDF + README）。
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

IMG = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
MODES = ("original", "translation", "bilingual")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paper", nargs="?", default="attention-is-all-you-need")
    ap.add_argument("--base", default="http://127.0.0.1:8710")
    args = ap.parse_args()
    base = f"{args.base.rstrip('/')}/api/v1"
    pid = args.paper

    bad = 0

    def check(cond: bool, label: str, extra: str = "") -> None:
        nonlocal bad
        if not cond:
            bad += 1
        print(f"[{'OK  ' if cond else 'FAIL'}] {label} {extra}")

    # ---- ① 三个版本的单文件下载 ----
    texts: dict[str, str] = {}
    for mode in MODES:
        url = f"{base}/papers/{pid}/markdown/export?mode={mode}"
        with urllib.request.urlopen(url, timeout=180) as r:
            texts[mode] = r.read().decode("utf-8")
        t = texts[mode]
        imgs = IMG.findall(t)
        check("<!-- page" not in t, f"{mode}：无 `<!-- page N -->`")
        check(not re.search(r"第\s*\d+\s*页", t), f"{mode}：无「第 N 页」")
        if t.strip():
            print(f"        {mode}: {len(t)} 字符，{len(imgs)} 张图；head={t[:56]!r}")
            if imgs:
                check(imgs[0].startswith(f"{args.base.rstrip('/')}/api/v1/papers/"),
                      f"{mode}：图片是绝对地址", imgs[0][:88])
        else:
            print(f"        {mode}: 空（这篇还没有页级产物）")

    nonempty = [m for m, t in texts.items() if t.strip()]

    # ---- ② zip 打包 ----
    with urllib.request.urlopen(f"{base}/papers/{pid}/export/zip", timeout=1800) as r:
        blob = r.read()
    check(len(blob) > 1000, "zip：下载到内容", f"{len(blob) / 1048576:.1f} MB")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    check(f"{pid}/source.pdf" in names, "zip：含原 PDF")
    check(f"{pid}/README.txt" in names, "zip：含 README.txt")
    for mode in MODES:
        if texts[mode].strip():
            check(f"{pid}/{mode}.md" in names, f"zip：含 {mode}.md")
        else:
            check(f"{pid}/{mode}.md" not in names, f"zip：不塞空 {mode}.md")

    inner = {m: zf.read(f"{pid}/{m}.md").decode("utf-8") for m in nonempty}
    refs = {p for t in inner.values() for p in IMG.findall(t)}
    figs = [n for n in names if "/figures/" in n]
    check(all(r.startswith("figures/") for r in refs),
          "zip：Markdown 里图片是**相对**路径", f"{len(refs)} 个引用")
    check(not [r for r in refs if f"{pid}/{r}" not in names], "zip：每个被引用的图片都在包里")
    check(len(figs) >= len(refs), "zip：figures/ 覆盖所有引用",
          f"{len(figs)} 个文件 / {len(refs)} 个引用")
    for mode, t in inner.items():
        check("<!-- page" not in t and not re.search(r"第\s*\d+\s*页", t),
              f"zip {mode}：无页标识")

    # ---- ③ 解压后离线可看：每个相对引用都落到真实文件 ----
    with tempfile.TemporaryDirectory(prefix="pm-export-") as tmp:
        zf.extractall(tmp)
        for ref in sorted(refs):
            f = Path(tmp) / pid / ref
            check(f.is_file() and f.stat().st_size > 0, f"解压后图片存在：{ref}",
                  f"{f.stat().st_size} B" if f.is_file() else "")

    print(f"\n不通过 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
