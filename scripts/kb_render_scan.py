"""渲染扫描：把**线上索引里的每一条行**喂给真实渲染器（`useMarkdown.ts` 的 bundle），
统计 KaTeX 报错、漏出表格竖线、未渲染的裸 LaTeX、`<table>` 数量。

为什么必须用真实 bundle 而不是 python 侧猜：渲染是前端 marked + katex 的行为，
"漏 LaTeX / 表格散架"这类问题就是这么量出来的（见 dev-log §17.61/17.62/17.63）。
KaTeX 内部也会渲染出 `|` 字形，所以判"泄漏竖线"要先按**配平的 `<span>`** 把 katex 片段剥掉。

用法::

    python scripts/kb_render_scan.py --arm arm3
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NODE = "node"


def export_rows(tag: str) -> Path:
    from backend.repositories.vector.lancedb_store import vector_store

    # LanceDB 没有"取全部"的便捷接口，这里按论文逐条扫（行数很少）
    from backend.repositories.mysql.models import Chunk
    from backend.repositories.mysql.session import session_scope

    out = []
    with session_scope() as sess:
        ids = [i for (i,) in sess.query(Chunk.id).all()]
    for i in range(0, len(ids), 200):
        for row in vector_store.get_rows(ids[i:i + 200]):
            out.append({
                "chunk_id": row.get("chunk_id"), "paper_id": row.get("paper_id"),
                "chunk_type": row.get("chunk_type"), "lang": row.get("lang"),
                "section": row.get("section"),
                "section_kind": (row.get("metadata") or {}).get("section_kind"),
                "content": row.get("content") or "",
            })
    path = Path(f"data/logs/snapshot/live-rows-{tag}.json")
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"导出线上行 {len(out)} 条 → {path}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="arm3")
    ap.add_argument("--rows", default="")
    args = ap.parse_args()

    rows_path = Path(args.rows) if args.rows else export_rows(args.arm)
    bundle = ROOT / "frontend" / "_useMarkdown.mjs"
    subprocess.run([str(ROOT / "frontend" / "node_modules" / ".bin" / "esbuild.cmd"),
                    "src/composables/useMarkdown.ts", "--bundle", "--format=esm",
                    "--alias:@=./src", f"--outfile={bundle}"],
                   cwd=ROOT / "frontend", check=True, capture_output=True)
    print(f"渲染器 bundle → {bundle}")

    script = ROOT / "frontend" / "_render_scan.mjs"
    script.write_text(
        "import { readFileSync } from 'node:fs';\n"
        "import { mdToHtml, latexToHtml } from './_useMarkdown.mjs';\n"
        "const rows = JSON.parse(readFileSync(process.argv[2], 'utf8'));\n"
        "const out = { rows: rows.length, katex_error: [], raw_dollar: [], "
        "raw_tex: [], leaked_pipe: [], tables: 0, formulas: 0, html_len: 0 };\n"
        "const TEX = /\\\\[a-zA-Z]{2,}/;\n"
        "function stripKatex(html) {\n"
        "  let out = '', i = 0;\n"
        "  while (i < html.length) {\n"
        "    const at = html.indexOf('<span class=\"katex', i);\n"
        "    if (at < 0) { out += html.slice(i); break; }\n"
        "    out += html.slice(i, at);\n"
        "    let depth = 0, j = at;\n"
        "    while (j < html.length) {\n"
        "      if (html.startsWith('<span', j)) { depth += 1; j += 5; }\n"
        "      else if (html.startsWith('</span>', j)) { depth -= 1; j += 7; if (!depth) break; }\n"
        "      else j += 1;\n"
        "    }\n"
        "    i = j;\n"
        "  }\n"
        "  return out;\n"
        "}\n"
        "for (const r of rows) {\n"
        "  const html = mdToHtml(r.content, r.paper_id);\n"
        "  out.html_len += html.length;\n"
        "  const errs = (html.match(/katex-error/g) || []).length;\n"
        "  if (errs) out.katex_error.push({ id: r.chunk_id, errs });\n"
        "  else out.formulas += (html.match(/class=\"katex\"/g) || []).length;\n"
        "  // 正文里剩下的 `$`（渲染器已经把成对公式吃掉；剩下的就是没渲染的）\n"
        "  const textOnly = html.replace(/<span class=\"katex[\\s\\S]*?<\\/span><\\/span>/g, '')\n"
        "                        .replace(/<[^>]+>/g, ' ');\n"
        "  const dollars = (textOnly.match(/\\$/g) || []).length;\n"
        "  if (dollars >= 2) out.raw_dollar.push({ id: r.chunk_id, n: dollars,\n"
        "      head: textOnly.replace(/\\s+/g, ' ').trim().slice(0, 90) });\n"
        "  if (TEX.test(textOnly) && r.chunk_type === 'text')\n"
        "      out.raw_tex.push({ id: r.chunk_id,\n"
        "      head: (textOnly.match(/\\\\[a-zA-Z]{2,}[^ ]*/) || [''])[0] });\n"
        "  const outside = stripKatex(html).replace(/<table[\\s\\S]*?<\\/table>/g, ' ');\n"
        "  if (/\\|/.test(outside.replace(/<[^>]+>/g, ' ')))\n"
        "      out.leaked_pipe.push({ id: r.chunk_id });\n"
        "  out.tables += (html.match(/<table/g) || []).length;\n"
        "}\n"
        "console.log(JSON.stringify(out));\n", encoding="utf-8")
    proc = subprocess.run([NODE, str(script), str(rows_path.resolve())], cwd=ROOT / "frontend",
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        print(proc.stderr[-1500:])
        return 1
    res = json.loads(proc.stdout)
    print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                      for k, v in res.items()}, ensure_ascii=False, indent=1))
    for key in ("katex_error", "raw_dollar", "raw_tex", "leaked_pipe"):
        for item in res[key][:6]:
            print(f"  {key}: {item}")
    (ROOT / f"data/logs/snapshot/render-scan-{args.arm}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
