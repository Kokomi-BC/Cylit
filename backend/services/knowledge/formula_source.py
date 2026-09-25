"""知识库公式的来源与筛选（**唯一来源**：``reader_service.markdown_formulas()``）。

数据流（用户口径，已固定）::

    pages.json（页级产物）
        ↓
    reader_service.markdown_formulas()      ← 唯一负责"发现公式"
        ↓
    canonical formula list（TurboQuant 349 / Attention 90）
        ↓
    knowledge-base filter（C 口径，见 `is_kb_formula`）
        ↓
    知识库公式条目（TurboQuant 95 / Attention 13）

**本模块不再识别公式**（不扫 original_md、不写第二套正则）：
"发现"只由 ``markdown_formulas()`` 做，这里只做三件过滤/挂载的事：

1. **续行合并**（同一行内的多行推导合成一条，仍属于过滤/规范化）；
2. **C 口径过滤**：行间公式 ∪ 编号公式 ∪ 有独立语义的定义/关系式
   —— 挡掉 `$d$`、`$b$`、`$Q(\\cdot)$` 这类低信息量行内数学，避免它们挤占 RAG top-k；
3. **挂锚点与解析侧信息**：能和解析侧公式块可靠对应的 → ``formula_block`` / ``exact``
   （带 ``grade`` / ``crop`` / ``parsed_latex`` / ``formula_block_id`` / ``text_chunk_id``）；
   否则 → ``page_row`` / ``row``（``row_id`` 直接给阅读器定位：row_id → block_rows → 页级兜底）；
   连 row 都没有才 ``page`` / ``page``。

不变式：**知识库公式 ⊆ 阅读器公式清单**（每条都能用 ``paper_id + page + row_id`` 回溯）。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

from backend.core.logging import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------
# C 口径（对 canonical list 的过滤，不是重新抽取）
# --------------------------------------------------------------------------
RELATION = re.compile(r":=|\\geq\b|\\leq\b|\\ge\b|\\le\b|\\approx|\\sim\b|\\to\b|"
                      r"\\in\b|\\propto|\\equiv|=")
# "有独立语义的定义/关系式"的门槛：至少两个 LaTeX 命令、含关系符、规范符号串够长
DEF_MIN_COMMANDS = 2
DEF_MIN_CANON = 12
# 续行：`= \sum…` / `\geq \frac{1}{4^b}.` 这类以关系符/运算符开头的片段属于上一条公式
CONTINUATION = re.compile(
    r"^\s*(?:=|\\geq|\\leq|\\ge\b|\\le\b|\\approx|\\sim|\\to|\\cdot|\\times|\\pm|\\mp|"
    r"\+|-|<|>|\\sum|\\prod|\\int|\\\\|&)")
MIN_CONTINUATION_CHARS = 6

# 解析侧对齐用的门槛（按真实语料标定）
MIN_CANON_CHARS = 10
MIN_MATCHED_CHARS = 8
HIGH_SCORE = 0.72
MID_SCORE = 0.6
MARGIN = 0.12
LOOKAHEAD = 40

_CMD = {
    "frac": "/", "dfrac": "/", "tfrac": "/", "sqrt": "sqrt", "sum": "sum",
    "prod": "prod", "int": "int", "min": "min", "max": "max", "inf": "inf",
    "sup": "sup", "geq": ">=", "ge": ">=", "leq": "<=", "le": "<=",
    "cdot": "*", "times": "*", "langle": "<", "rangle": ">", "to": "->",
    "sim": "~", "approx": "~", "pi": "pi", "infty": "inf", "partial": "d",
    "mathbb": "", "mathcal": "", "mathbf": "", "boldsymbol": "", "text": "",
    "mathrm": "", "operatorname": "", "tilde": "", "hat": "", "bar": "",
    "left": "", "right": "", "quad": "", "qquad": "", "displaystyle": "",
    "limits": "", "aligned": "", "begin": "", "end": "", "ell": "l",
    "top": "t", "prime": "'", "dots": "...", "ldots": "...", "cdots": "...",
}
_ENGLISH_WORD = re.compile(r"\b[a-z]{4,}\b")


def _canon(latex: str | None) -> str:
    """把 LaTeX 压成**规范符号串**（命令归一、去排版噪音），用于比较与"够不够长"。"""
    t = (latex or "").lower()
    t = re.sub(r"\\(?:begin|end)\{[a-z*]+\}", "", t)
    t = re.sub(r"\\([a-z]+)", lambda m: _CMD.get(m.group(1), m.group(1)), t)
    t = re.sub(r"[\s{}]+", "", t)
    return re.sub(r"[^0-9a-z=<>+\-*/()\[\]|,.~^_']", "", t)


def _canon_loose(latex: str | None) -> str:
    """``_canon`` 的宽松版（再去括号/逗号/点/竖线），只用于**包含**判定。"""
    return re.sub(r"[(),.|]", "", _canon(latex))


def _prose_words(text: str) -> int:
    """普通英文单词数（**先剥 LaTeX 命令名** —— `\\left`/`\\quad` 不是英文词）。"""
    s = re.sub(r"\\(?:text|mathrm|operatorname|textit)\{[^}]*\}", "", text or "")
    s = re.sub(r"\\[a-zA-Z]+", " ", s)
    return len(_ENGLISH_WORD.findall(s))


def formula_number(latex: str | None) -> str | None:
    """公式编号（``\\tag{1}`` → ``"1"``）；没有编号返回 None（编号要单独进 metadata）。"""
    m = re.search(r"\\tag\*?\s*\{\s*([^}]{1,12})\s*\}", latex or "")
    return m.group(1).strip() if m else None


def has_definition_shape(latex: str | None) -> bool:
    """是不是"有独立语义的定义/关系式"：含关系符 + ≥2 个命令 + 规范串够长。

    ⚠️ 这里**不再加"散文词"判据**：``markdown_formulas()`` 已经把落单 `$` 夹出来的散文挡掉了
    （它按配对扫描），再叠一层会让 `\text{model}` 多的公式（`lrate = …`）被误杀 ——
    实测会让 C 口径从 95 掉到 92，与用户口径不符。
    """
    t = latex or ""
    if len(_canon(t)) < DEF_MIN_CANON:
        return False
    if not RELATION.search(t):
        return False
    return len(re.findall(r"\\[a-zA-Z]+", t)) >= DEF_MIN_COMMANDS


def is_kb_formula(item: dict) -> bool:
    """**C 口径**：行间公式 ∪ 编号公式 ∪ 有独立语义的定义/关系式。

    - 行间（``display=True``）说明视觉转写把它独立成行 → 是展示公式；
    - 有 ``\\tag{n}`` 编号 → 是正式编号公式；
    - 否则要求"定义/关系式"形状（`q \\cdot k = \\sum_i q_i k_i`、`W_i^Q \\in \\mathbb{R}^{…}`、
      `z_i := …`、`p_\\ell := \\int…`、`Q : \\mathbb{S}^{d-1} \\to …`）。
    挡掉的是 `$d$`、`$b$`、`$Q(\\cdot)$` 这类低信息量行内数学。
    """
    latex = item.get("latex") or ""
    if not latex:
        return False
    if item.get("display") or formula_number(latex):
        return True
    return has_definition_shape(latex)


def _join_lines(parts: list[str]) -> str:
    """多行推导拼成一条可渲染公式（已含 `\\begin{…}` 的用 `\\quad` 平接，避免嵌套环境）。"""
    parts = [p.strip() for p in parts if p and p.strip()]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    if any("\\begin{" in p for p in parts):
        return " \\quad ".join(parts)
    return "\\begin{aligned} " + " \\\\ ".join(parts) + " \\end{aligned}"


def group_continuations(items: list[dict]) -> list[dict]:
    """把**同一行**里的多行推导合成一条（纯过滤/规范化，不涉及公式识别）。

    页级产物按行给出；一条多行推导有时被拆成同一行里的几个 `$$` 片段
    （`D_mse = Σ…` / `= Σ…` / `≥ 1/4^b.`）。续行（以关系符/运算符开头且够长）并回上一条，
    ``source_item_ids`` 保留原始条目 id —— 回溯不变式照旧成立。
    """
    out: list[dict] = []
    for item in items:
        latex = (item.get("latex") or "").strip()
        if not latex:
            continue
        if (out and out[-1]["row_id"] == item.get("row_id")
                and CONTINUATION.match(latex)
                and len(_canon(latex)) >= MIN_CONTINUATION_CHARS):
            prev = out[-1]
            prev["latex"] = _join_lines([prev["latex"], latex])
            prev["source_item_ids"] = list(prev.get("source_item_ids") or []) + [item.get("id")]
            continue
        out.append({**item, "latex": latex, "source_item_ids": [item.get("id")]})
    return out


# --------------------------------------------------------------------------
# 解析侧对齐（**只用于挂锚点/挂解析信息**，不参与"发现公式"）
# --------------------------------------------------------------------------
def _cover(a: str, b: str) -> int:
    if not a or not b:
        return 0
    return sum(bl.size for bl in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())


def score_pair(parsed: str, vision: str) -> dict:
    """解析版与页级版的相似度（0~1）；用于判断"解析块和这条公式是不是同一条"。"""
    a, b = _canon(parsed), _canon(vision)
    if len(a) < MIN_CANON_CHARS or not b:
        return {"score": 0.0, "cover": 0.0, "matched": 0}
    matched = _cover(a, b)
    if matched < MIN_MATCHED_CHARS:
        return {"score": 0.0, "cover": 0.0, "matched": matched}
    return {"score": round(matched / len(a), 4), "cover": round(matched / len(a), 4),
            "matched": matched}


def align_page(parsed: list[dict], vision: list[str], *,
               threshold: float = HIGH_SCORE, lookahead: int = LOOKAHEAD,
               margin: float = MARGIN) -> list[dict]:
    """同页单调对齐（解析碎片 → 公式清单条目）；平分取阅读顺序靠前的，宁可不配也不错配。"""
    out: list[dict] = []
    j = 0
    for item in parsed:
        scores: list[tuple[float, int]] = []
        hi = min(len(vision), j + 1 + max(0, lookahead))
        for k in range(min(j, len(vision) - 1) if vision else 0, hi):
            scores.append((score_pair(item.get("text") or "", vision[k])["score"], k))
        scores.sort(key=lambda x: (-x[0], x[1]))
        best = scores[0] if scores else (0.0, None)
        second = scores[1] if len(scores) > 1 else (0.0, None)
        best_score, best_k = best
        take = best_k is not None and best_score >= MID_SCORE
        if take and best_score < threshold and (best_score - second[0]) < margin:
            take = False
        if take:
            out.append({"index": best_k, "score": best_score, "best_score": best_score,
                        "second_score": second[0]})
            j = best_k
        else:
            out.append({"index": None, "score": 0.0, "best_score": best_score,
                        "second_score": second[0]})
    return out


def parsed_latex(f: dict) -> str:
    """解析产物里这条公式的 LaTeX（修复版 → 最终版 → 首个候选）。"""
    return (f.get("latex_repaired") or f.get("latex_final")
            or (f.get("latex_candidate") or [""])[0] or "").strip()


# --------------------------------------------------------------------------
# 对外主入口
# --------------------------------------------------------------------------
def canonical_items(paper_id: str) -> list[dict]:
    """canonical formula list（唯一来源，逐字来自阅读器那份清单）。"""
    from backend.services.paper import reader_service

    try:
        data = reader_service.markdown_formulas(paper_id) or {}
    except Exception as exc:  # noqa: BLE001 —— 页级产物缺失不该让建索引失败
        log.warning("读取 canonical 公式清单失败（%s）：%s", paper_id, exc)
        return []
    return list(data.get("items") or [])


def kb_formula_entries(paper_id: str, formulas: dict[str, dict],
                       rows: list[dict]) -> list[dict]:
    """C 过滤 + 挂锚点，返回知识库公式条目（每项含 metadata 需要的全部字段）。

    - ``formulas``：``{block_id: IR 公式行}``（只为挂解析信息，不参与发现公式）；
    - ``rows``：按阅读顺序的块行（``{"id","page","order"}``），用于同页对齐。
    """
    items = canonical_items(paper_id)
    # ① C 过滤（用户口径 §2/§9.3：对 canonical 清单过滤 → TurboQuant 92 / Attention 13）；
    #    顺带挡掉没有任何数学内容的条目（页脚 `* †` 那种）
    kept_raw = [it for it in items
                if is_kb_formula(it) and not is_noise_latex(it.get("latex"))]
    # ② 同行续行合并（过滤之后的**规范化**，不涉及公式识别）——
    #    合并会让条目数略少于"通过 C 的条数"，`source_item_ids` 保住回溯关系
    kept = group_continuations(kept_raw)
    if not kept:
        return []

    # 解析侧公式块按页分组（同页、按阅读顺序）
    by_page: dict[int, list[dict]] = {}
    for b in rows:
        if b["id"] in formulas:
            by_page.setdefault(int(b["page"]), []).append(b)
    for items_ in by_page.values():
        items_.sort(key=lambda x: int(x["order"]))

    # 页 → 该页 kept 的清单序号
    page_items: dict[int, list[int]] = {}
    for idx, it in enumerate(kept):
        page_items.setdefault(int(it.get("page") or 0), []).append(idx)

    out: list[dict] = []
    for page, idxs in page_items.items():
        cands = [kept[i] for i in idxs]
        blocks = by_page.get(page) or []
        parsed = [{"id": b["id"], "text": parsed_latex(formulas[b["id"]])} for b in blocks]
        # align_page 是"每个解析块 → 命中的清单条目"；这里要反向映射（条目 → 解析块）
        cand_block: dict[int, dict] = {}
        if blocks:
            for pos, a in enumerate(align_page(parsed, [c["latex"] for c in cands])):
                if a.get("index") is not None and pos < len(blocks):
                    cand_block.setdefault(int(a["index"]), blocks[pos])
        for pos, cand in enumerate(cands):
            block = cand_block.get(pos)
            entry = {
                "id": cand.get("id"), "latex": cand["latex"],
                "display": bool(cand.get("display")),
                "row_id": cand.get("row_id"), "page": int(cand.get("page") or 0),
                "context": cand.get("context"), "context_zh": cand.get("context_zh"),
                "formula_number": formula_number(cand["latex"]),
                "source_item_ids": list(cand.get("source_item_ids") or []),
                "latex_source": "page_vision",
            }
            if block is not None:
                f = formulas[block["id"]]
                entry.update({
                    "formula_block_id": block["id"], "block_id": block["id"],
                    "parsed_latex": parsed_latex(f),
                    "grade": f.get("grade"), "crop": f.get("image_path"),
                    "raw_text": f.get("raw_text"),
                    "align_score": score_pair(parsed_latex(f), cand["latex"])["score"],
                    "anchor_type": "formula_block", "anchor_precision": "exact",
                })
            elif cand.get("row_id"):
                entry.update({"block_id": cand.get("row_id"),
                              "anchor_type": "page_row", "anchor_precision": "row"})
            else:
                entry.update({"block_id": None,
                              "anchor_type": "page", "anchor_precision": "page"})
            out.append(entry)
    return out


def page_images_dir(paper_id: str) -> Path:
    """页图目录（审计/可视化用；本模块不写盘）。"""
    from backend.services.paper import paper_service

    return Path(paper_service.paper_paths(paper_id)["pages"])


def is_noise_latex(latex: str | None) -> bool:
    """没有任何数学内容的"公式"（页脚 `* †` 那种）—— 不进条目、也不进正文。

    canonical 清单里 ``markdown_formulas()`` 已经挡过一道（``is_noise_formula``），
    这里再兜一层，避免它经由别处流进来。
    """
    from backend.parsing.formula.noise import is_noise_formula

    return is_noise_formula(latex or "")

