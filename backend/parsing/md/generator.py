"""Markdown 单一事实源生成（规划 §21.2 步骤 6、§14.5 C 级公式写法）。

结构约定（翻译服务据此写回译文，**不做文本相似度匹配**，只用 block_id 配对）：

```markdown
# 论文标题

> 元信息 ...

<!-- blk:p01-b01 type:para page:1 bbox:... -->
正文段落原文……

<!-- tr:p01-b01 rev:1 -->
中文译文……

<!-- blk:p19-b12 type:formula grade:C ... -->
![公式 p19-b12（C 级，只有裁图）](../formula_crops/p19-b12@4x.png)
```

- 每个 block 一段，**锚点唯一**（``<a id="blk-p01-b01"></a>``），供 reader.json 定位。
- A/B 级公式写行内/块级 LaTeX；C 级公式**不产出 LaTeX**，用高倍裁图兜底（规划 §14.5）。
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.core.utils import normalize_text, write_json, write_text_atomic
from backend.parsing.models import Block, FormulaBlock, ParseResult

BLOCK_MARKER = "<!-- blk:{id} type:{type} page:{page} bbox:{bbox} -->"
TR_MARKER = "<!-- tr:{id} rev:{rev} sha:{sha} -->"
TR_END = "<!-- /tr:{id} -->"

_BLOCK_RE = re.compile(r"<!-- blk:(?P<id>[\w\-]+) type:(?P<type>\w+)")
_TR_RE = re.compile(r"<!-- tr:(?P<id>[\w\-]+) rev:(?P<rev>\d+)(?: sha:(?P<sha>\w+))? -->")

def _anchor(block_id: str) -> str:
    return f'<a id="blk-{block_id}"></a>'


def _bbox_str(b: Block) -> str:
    return ",".join(f"{v:.1f}" for v in b.rect.to_list())


# --------------------------------------------------------------------------
# 公式在 Markdown 中的写法
# --------------------------------------------------------------------------


def formula_markdown(f: FormulaBlock, crop_rel: str | None = None) -> str:
    """按分级输出公式（规划 §14.5）。

    **两种分支都必须输出同一格式的块标记** ``<!-- blk:{id} type:formula ... -->``，
    否则阅读器/对账会认为该块在 Markdown 中缺失。
    """
    grade = (f.grade or "C").upper()
    bbox = ",".join(f"{v:.1f}" for v in f.rect.to_list())
    head = (f"<!-- blk:{f.block_id} type:formula grade:{grade} "
            f"page:{f.page_number} bbox:{bbox}")
    if grade in ("A", "B"):
        latex = f.latex_final or f.latex_repaired or f.latex_candidate
        if not latex:
            grade = "C"  # 没有可用的 LaTeX，退回裁图兜底
        else:
            body = f"$$ {latex} $$" if not f.is_inline else f"$ {latex} $"
            comment = (f"{head} confidence:{f.confidence:.2f} "
                       f"source:{f.source or ''} -->")
            return f"{comment}\n{body}"

    # C 级：裁图兜底 + 候选与证据写入注释（不可被 RAG 检索，需在论文库暴露占比）
    cand = "; ".join(f.latex_candidates[:3])
    reason = (f.review_note or "; ".join((f.validation or {}).get("issues") or [])
              or "结构歧义")
    comment = (
        f"{head} reason:{_slug(reason)} "
        f"candidate:\"{cand}\" repair:\"{f.latex_repaired or ''}\" "
        f"evidence:vector_audit crop:{crop_rel or f.image_path or ''} -->"
    )
    alt = f"公式 {f.block_id}（C 级，只有裁图）"
    src = crop_rel or f.image_path or ""
    return f"{comment}\n![{alt}]({src})"


def _slug(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text or "")
    return s.strip("_")[:max_len] or "unspecified"


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------


def render_markdown(
    result: ParseResult,
    *,
    paper_id: str,
    assets_prefix: str = "..",
    formulas_by_block: dict[str, FormulaBlock] | None = None,
    translations: dict[str, dict] | None = None,
    include_translations: bool = True,
) -> str:
    """渲染单篇 Markdown。

    ``translations``: ``{block_id: {"text": str, "revision": int, "sha": str}}``
    —— 生成时若已有译文（重跑场景）一并写入，避免丢失。
    """
    fmap = formulas_by_block or {f.block_id: f for f in result.formulas}
    translations = translations or {}

    lines: list[str] = []
    title = result.title or paper_id
    lines.append(f"# {title}")
    lines.append("")
    meta_bits = [f"`paper_id: {paper_id}`", f"`pages: {len(result.pages)}`",
                 f"`blocks: {len(result.blocks)}`"]
    if result.authors:
        meta_bits.insert(1, result.authors)
    lines.append("> " + " · ".join(meta_bits))
    lines.append("")
    counts = result.counts()
    dist = result.grade_distribution()
    lines.append(
        f"> 解析统计：段落 {counts.get('blocks_para', 0)} · 标题 {counts.get('blocks_heading', 0)}"
        f" · 图注 {counts.get('blocks_caption', 0)} · 公式 {len(result.formulas)}"
        f"（A {dist.get('A', 0)} / B {dist.get('B', 0)} / C {dist.get('C', 0)}）"
        f" · 插图 {len(result.figures)} · 表格 {len(result.tables)}"
    )
    if result.warnings:
        lines.append("")
        lines.append("> ⚠️ 解析告警：" + "；".join(result.warnings[:5]))
    lines.append("")
    lines.append("---")
    lines.append("")

    current_page = 0
    for b in result.blocks:
        if b.page_no != current_page:
            current_page = b.page_no
            lines.append("")
            lines.append(f"<!-- ===== page {current_page} ===== -->")
            lines.append("")

        f = fmap.get(b.id)
        if f is not None:
            lines.append(_anchor(b.id))
            crop_rel = None
            if f.image_path:
                crop_rel = f"{assets_prefix}/formula_crops/{Path(f.image_path).name}"
            lines.append(formula_markdown(f, crop_rel))
            lines.append("")
        elif b.type == "figure":
            lines.append(_anchor(b.id) + f"<!-- blk:{b.id} type:figure page:{b.page_no} -->")
            lines.append(_figure_markdown(b))
            lines.append("")
        else:
            lines.append(f"<!-- blk:{b.id} type:{b.type} page:{b.page_no} "
                         f"bbox:{_bbox_str(b)} -->")
            lines.append(_anchor(b.id))
            lines.append(_block_body_markdown(b))
            lines.append("")

        if include_translations:
            tr = translations.get(b.id)
            if tr and tr.get("text"):
                lines.append(f"<!-- tr:{b.id} rev:{tr.get('revision', 1)} "
                             f"sha:{tr.get('sha', '')} -->")
                lines.append(tr["text"].strip())
                lines.append(f"<!-- /tr:{b.id} -->")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _block_body_markdown(b: Block) -> str:
    return block_body_text(b, md=True)


def block_body_text(block: Block, *, md: bool = False) -> str:
    """块的正文文本（阅读器与 Markdown 共用，保证两侧一致）。

    ``md=True`` 时按 Markdown 语义加标题标记与图注加粗；``md=False``（阅读器）
    返回纯文本，标题级别通过块元信息（``meta['heading_level']``）传递。
    """
    text = (block.text_clean or block.text or "").strip()
    if not text:
        return ""
    if block.type == "heading":
        level = heading_level_or_none(text, block.font_size)
        if md:
            # 伪代码行等"假标题"不在 Markdown 里加 `#`：否则它们会进目录
            return f"{'#' * level} {text}" if level else text
        return text
    if block.type == "caption" and md:
        return f"**{text}**"
    return text


_HEADING_NUM_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z](?:\.\d{1,2}){1,3})")
# 章节号后面紧跟字母（PDF 文本层常把 "0.1 PROBLEM" 抽成 "0.1PROBLEM"）
_HEADING_NUM_TIGHT_RE = re.compile(
    r"^(\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z](?:\.\d{1,2}){1,3})(?=[A-Za-z\u4e00-\u9fff])")
# 附录标题被抽成 "AEXTENDED PRIOR WORK"（首字母与后词之间的空格丢失）。
# 只对 **A–H 单字母 + ≥6 个大写字母的整词** 生效，且排除 ABSTRACT 这类本来就是一个词的标题。
_APPENDIX_GLUE_RE = re.compile(r"^([A-H])(?=[A-Z]{6,}\b)")
_COMMON_SECTION_WORDS = {
    "abstract", "introduction", "references", "conclusion", "conclusions",
    "acknowledgements", "acknowledgments", "appendix", "relatedwork",
}
# 算法伪代码行：`3: Generate ...`、`6: r ← x − DEQUANT(idx)`、`QUANT(x)` 之类。
# 它们常因字号/编号被误判成 heading，一旦进 Markdown 就会污染目录。
_PSEUDOCODE_RE = re.compile(
    r"^\d{1,2}\s*[::.]\s"          # `3: ` / `12. ` 行号式开头（假章节号）
    r"|[←→↖↗]|\bleftarrow\b"       # 赋值箭头
    r"|^\s*(?:Procedure|Algorithm|Require|Ensure|Input|Output)\b"
    r"|\bend\s+(?:for|while|if)\b",
    re.IGNORECASE,
)
_LETTER_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")


def is_pseudocode_heading(text: str) -> bool:
    """该文本是否更像算法伪代码而不是标题（用于把误判的 heading 排除出目录）。"""
    return bool(_PSEUDOCODE_RE.search((text or "").strip()))


def is_probably_not_heading(text: str) -> bool:
    """表格数值行等：几乎不含字母的"标题"不是标题（如 `1645.2945.16`、`68.3859.5446.28`）。"""
    return len(_LETTER_RE.findall(text or "")) < 2


def normalize_heading_text(text: str) -> str:
    """标题显示规范化（**只用于阅读器索引与目录，不改 Markdown 事实源**）。

    处理三种 PDF 文本层抽出来的粘连：
    1. 只取首行（标题后面常粘上紧随的图片/正文行）；
    2. ``0.1PROBLEM`` → ``0.1 PROBLEM``、``B.1SHANNON`` → ``B.1 SHANNON``；
    3. 附录 ``AEXTENDED PRIOR WORK`` → ``A EXTENDED PRIOR WORK``（保守：A–H + 整词 ≥6 大写）。
    """
    t = re.sub(r"^#+\s*", "", (text or "").strip())
    t = t.split("\n", 1)[0].strip()
    t = _HEADING_NUM_TIGHT_RE.sub(lambda m: f"{m.group(1)} ", t).strip()
    if t.lower().replace(" ", "") not in _COMMON_SECTION_WORDS:
        t = _APPENDIX_GLUE_RE.sub(lambda m: f"{m.group(1)} ", t).strip()
    return t


def heading_level_or_none(text: str, font_size: float = 0.0) -> int | None:
    """标题层级；``None`` = 判定为**不是**标题（伪代码行/数值行），不进 Markdown 目录。"""
    t = normalize_heading_text(text)
    if not t or is_pseudocode_heading(t) or is_probably_not_heading(t):
        return None
    num = _HEADING_NUM_RE.match(t)
    if num:
        return min(2 + num.group(1).count("."), 6)
    if t.isupper() and len(t) <= 90:
        return 2
    # 无编号、非全大写：字号明显偏小则按正文处理
    if font_size and 0 < font_size < 9.5:
        return None
    _ = font_size
    return 2


def heading_level(text: str, font_size: float = 0.0) -> int:
    """标题层级（兼容旧调用）：不是标题时按 2 级处理。"""
    return heading_level_or_none(text, font_size) or 2


def _figure_markdown(b: Block) -> str:
    text = (b.text_clean or b.text or "").strip()
    img = b.meta.get("image_rel") if b.meta else None
    if img:
        return f"![{text[:60]}]({img})\n\n*{text}*" if text else f"![figure]({img})"
    return f"*{text}*" if text else ""


def write_markdown(path: str | Path, content: str) -> Path:
    return write_text_atomic(path, content)


def write_markdown_bundle(
    result: ParseResult,
    md_dir: str | Path,
    *,
    paper_id: str,
    assets_prefix: str = "..",
) -> dict:
    """一次性写全 Markdown 产物：``full.md`` + ``blocks.json`` + 逐页 Markdown。

    这是**唯一**的 Markdown 落盘入口（pipeline 与 CLI 共用），保证：
    - 旧的译文在重跑后按 block_id 保留（规划 §9.2 稳定重绑定的价值所在）；
    - ``blocks.json`` 与 Markdown 永远同源（阅读器据此拿到纯文本）。
    """
    md_dir = Path(md_dir)
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / "full.md"

    prev_translations: dict[str, dict] = {}
    if md_path.is_file():
        try:
            prev_translations = extract_translations(md_path.read_text(encoding="utf-8"))
        except OSError:
            prev_translations = {}

    valid_ids = {b.id for b in result.blocks}
    kept = {k: v for k, v in prev_translations.items() if k in valid_ids}

    content = render_markdown(result, paper_id=paper_id,
                              assets_prefix=assets_prefix,
                              translations=kept)
    write_text_atomic(md_path, content)

    parsed = parse_markdown(content)
    formula_ids = {f.block_id for f in result.formulas}
    blocks_index = []
    for b in sorted(result.blocks, key=lambda x: (x.page_no, x.order_global)):
        node = parsed.get(b.id, {})
        raw = node.get("text", "")
        font_size = b.meta.get("font_size")
        if b.type == "heading":
            # 阅读器索引存**裸文本** + Markdown 层级（规划 §5 决策 6：标记只由生成器加）。
            # 层级同时是"目录"的唯一依据：伪代码假标题在这里被记为 0（不进目录）。
            heading_text = normalize_heading_text(raw or (b.text_clean or b.text or ""))
            level = heading_level_or_none(heading_text, font_size or 0.0) or 0
            text_out = heading_text
        else:
            level = 0
            text_out = raw
        blocks_index.append({
            "id": b.id, "page": b.page_no, "type": b.type, "col": b.col,
            "order": b.order_global, "rect": b.rect.to_list(),
            "anchor": b.md_anchor, "text": text_out,
            "heading_level": level,
            "section": b.meta.get("section_title") or b.section_id,
            "font_size": font_size,
            "has_formula": b.id in formula_ids,
        })
    write_json(md_dir / "blocks.json", {
        "paper_id": paper_id, "title": result.title,
        "page_count": len(result.pages), "blocks": blocks_index,
        "counts": result.counts(),
    })

    by_page: dict[int, list[Block]] = {}
    for b in result.blocks:
        by_page.setdefault(b.page_no, []).append(b)
    for pno, blocks in by_page.items():
        page_result = ParseResult(
            paper_id=paper_id, title=f"{result.title} — p{pno}",
            pages=[p for p in result.pages if p.page_no == pno],
            blocks=blocks,
            formulas=[f for f in result.formulas if f.page_number == pno],
        )
        ids = {b.id for b in blocks}
        write_text_atomic(
            md_dir / f"page-{pno:03d}.md",
            render_markdown(page_result, paper_id=paper_id,
                            include_translations=True,
                            translations={k: v for k, v in kept.items() if k in ids}),
        )
    return {"md_path": str(md_path), "md_chars": len(content),
            "md_pages": len(by_page),
            "blocks_json": str(md_dir / "blocks.json"),
            "translations_kept": len(kept)}


# --------------------------------------------------------------------------
# 译文回写（翻译服务的接口：只替换对应 block 的译文段，不动其他内容）
# --------------------------------------------------------------------------


def upsert_translation(md_text: str, block_id: str, translation: str,
                       revision: int = 1, sha: str = "") -> str:
    """把某块的译文写入 Markdown（幂等：已存在则替换）。

    返回新的 Markdown 文本。**不使用文本相似度匹配**，只按 block_id 定位（规划 §3.1 A2）。
    """
    lines = md_text.split("\n")
    header = f"<!-- tr:{block_id} rev:{revision} sha:{sha} -->"
    footer = f"<!-- /tr:{block_id} -->"
    body = [ln for ln in (translation or "").split("\n")]

    # 已有译文段 → 替换
    for i, ln in enumerate(lines):
        if ln.startswith(f"<!-- tr:{block_id} "):
            j = i + 1
            while j < len(lines) and not lines[j].startswith(f"<!-- /tr:{block_id}"):
                j += 1
            if j >= len(lines):
                j = len(lines)
                new = lines[:i] + [header] + body + [footer]
            else:
                new = lines[:i] + [header] + body + [footer] + lines[j + 1:]
            return "\n".join(new)

    # 未找到 → 插到对应 block 之后（block 标记之后的第一处空行前）
    insert_at = None
    for i, ln in enumerate(lines):
        if ln.startswith(f"<!-- blk:{block_id} "):
            j = i + 1
            # 跳过锚点行与正文，直到遇到空行
            while j < len(lines) and lines[j].strip() != "":
                j += 1
            insert_at = j
            break
    if insert_at is None:
        # 没有该块（异常情况）：追加到文末并留记号，便于对账
        return md_text.rstrip() + f"\n\n<!-- orphan tr:{block_id} -->\n" \
            + "\n".join([header] + body + [footer]) + "\n"

    new = lines[:insert_at] + [""] + [header] + body + [footer] + lines[insert_at:]
    return "\n".join(new)


def extract_translations(md_text: str) -> dict[str, dict]:
    """从已有 Markdown 里解析出译文（重跑时保留，避免全部作废）。"""
    out: dict[str, dict] = {}
    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        m = _TR_RE.match(lines[i].strip())
        if m:
            bid = m.group("id")
            rev = int(m.group("rev"))
            sha = m.group("sha") or ""
            j = i + 1
            body: list[str] = []
            while j < len(lines) and not lines[j].startswith(f"<!-- /tr:{bid}"):
                body.append(lines[j])
                j += 1
            out[bid] = {"text": "\n".join(body).strip(), "revision": rev, "sha": sha}
            i = j + 1
        else:
            i += 1
    return out


def extract_block_ids(md_text: str) -> list[str]:
    return [m.group("id") for m in _BLOCK_RE.finditer(md_text)]


def parse_markdown(md_text: str) -> dict[str, dict]:
    """把 Markdown 解析为 ``{block_id: {text, translation, kind, revision}}``。

    Markdown 是**内容单一事实源**（规划 §8）：阅读器与翻译服务都从这里取正文，
    因此解析逻辑集中在本模块，避免多处实现不一致。
    """
    out: dict[str, dict] = {}
    current: dict | None = None
    for raw in (md_text or "").split("\n"):
        line = raw.rstrip()
        if line.startswith("<!-- blk:"):
            head = line[9:]
            bid = head.split(" ", 1)[0]
            current = {"text": [], "translation": [], "kind": "", "revision": 0,
                       "_in_tr": False}
            out[bid] = current
            for part in head.split(" "):
                if part.startswith("type:"):
                    current["kind"] = part[5:]
            continue
        if line.startswith("<!-- tr:"):
            bid = line[len("<!-- tr:"):].split(" ", 1)[0]
            node = out.setdefault(bid, {"text": [], "translation": [], "kind": "",
                                        "revision": 0, "_in_tr": False})
            current = node
            current.setdefault("text", [])
            current.setdefault("translation", [])
            current["_in_tr"] = True
            for part in line.split(" "):
                if part.startswith("rev:"):
                    try:
                        node["revision"] = int(part[4:])
                    except ValueError:
                        node["revision"] = 0
            continue
        if line.startswith("<!-- /tr:"):
            if current is not None:
                current["_in_tr"] = False
            current = None
            continue
        if line.startswith("<!--") or line.startswith("<a id="):
            continue
        if current is not None and line.strip():
            bucket = "translation" if current.get("_in_tr") else "text"
            current.setdefault(bucket, []).append(line)

    return {
        bid: {
            "text": "\n".join(node.get("text") or []).strip(),
            "translation": "\n".join(node.get("translation") or []).strip()
            if node.get("translation") else "",
            "kind": node.get("kind") or "",
            "revision": node.get("revision") or 0,
        }
        for bid, node in out.items()
    }
