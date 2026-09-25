"""公式 LaTeX 的**来源选择**：解析产物 vs 页级视觉转写。

背景（用户报的）：知识库的公式条目会出现 `公式 …：f(x) =`、`C(f, b) :=min` 这种残缺内容。
根因是 dense 多行展示公式在 **PDF 文本层**里是碎的 —— 同一个公式被切成好几块
（`$$ C(f, b) :=min $$`，积分项 `|x-c|·f(x)dx.` 另成一块；p20 更是一条公式拆成 5 块），
而 `_formula_chunk_text` 只取 `latex_final`（= 候选[0]），于是库里只剩一小段。

但**页级视觉转写里公式是完整的**：``md/pages.json`` 的 ``original_md``（阅读器 Markdown 视图
渲染的就是它，由视觉模型按页图转写）对同一页给的是
``$$D_{\\text{mse}}(Q) := \\mathbb{E}[\\|x - Q^{-1}(Q(x))\\|_2^2] \\geq \\frac{1}{4^b}.$$``。
**所以不需要新的视觉调用，换源就行** —— 本模块负责"源"与"对齐"两件事。

难点是对齐：页级 md 按整页阅读顺序给 ``$$`` 块（数量少、常把多行公式合成一个 ``aligned``），
解析侧是很多碎片（数量多、切法不同）。所以：

- **同页 + 按阅读顺序单调对齐**（解析侧顺序 ↔ 页级顺序），单调约束挡掉绝大部分误配；
- **允许 N:1**（页级一个 ``aligned`` 大块 ↔ 解析侧几个碎片）；
- **阈值不够就退回解析版**（`source="parsed"` + `needs_review`）——
  沿用项目里"宁可漏配不错配"的既有口径（zh 对齐同理）：**贴错公式比贴不全更糟**。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

# 页级 md 里的公式：`$$…$$`（行间）与 `$…$`（**行内**）都要收 ——
# 实测页级转写经常把展示公式写成行内（p5 的 `$\mathcal{C}(f_X, b) := \min …$` 就是完整公式），
# 只收 `$$` 会漏掉一大半（p5/p16/p20 最初全军覆没就是这个原因）。
_DOLLAR_BLOCK = re.compile(r"\$\$([\s\S]*?)\$\$")
_BRACKET_BLOCK = re.compile(r"\\\[([\s\S]*?)\\\]")
_INLINE = re.compile(r"(?<!\$)\$(?!\$)([^$\n]{6,})\$(?!\$)")

# 打分门槛（按真实语料标定，见 tests/test_kb_formula_source.py）：
# 解析碎片太短就没法安全匹配（`b\cdot d`、`f(x) =` 这种 3~5 个符号，
# 撞上任何一条长公式都能"覆盖"，所以直接不配）；
# `MIN_CANON_CHARS` 同时是 `is_fragment` 的门槛 —— 短碎片连公式条目都不单独出。
MIN_CANON_CHARS = 10
MIN_MATCHED_CHARS = 8
HIGH_SCORE = 0.72
MID_SCORE = 0.6
MARGIN = 0.12
# 候选池往前看几个（页级块与解析碎片的切法不同，窗口太小会配不上）
LOOKAHEAD = 40
DEFAULT_THRESHOLD = HIGH_SCORE
# 保留旧名，便于外部引用
MIN_SHARED_GRAMS = 6

# LaTeX 命令 → 规范符号（只为"同一条公式的两种写法能对上"）
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


def _canon(latex: str | None) -> str:
    """把一个 LaTeX 片段压成**规范符号串**：命令归一、去掉排版噪音、只留数学符号。

    为什么不用字符 3-gram（第一版）：同一条公式的两份转写差在命令名上
    （`\\text{mse}` / `\\mathbb{E}` / `\\left\\|`），3-gram 几乎不重合 —— 实测
    p20-b04 与页级正确公式的 3-gram 重合只有 1~2 个，直接判 0 分。
    规范符号串把命令折叠成语义符号后，两份转写在符号层面高度一致。
    """
    t = (latex or "").lower()
    t = re.sub(r"\\(?:begin|end)\{[a-z*]+\}", "", t)
    t = re.sub(r"\\([a-z]+)", lambda m: _CMD.get(m.group(1), m.group(1)), t)
    t = re.sub(r"[\s{}]+", "", t)
    return re.sub(r"[^0-9a-z=<>+\-*/()\[\]|,.~^_']", "", t)


def _cover(a: str, b: str) -> int:
    """``a`` 有多少字符能在 ``b`` 里按顺序匹配上（字符数，不是比例）。"""
    if not a or not b:
        return 0
    m = SequenceMatcher(None, a, b, autojunk=False)
    return sum(bl.size for bl in m.get_matching_blocks())


def vision_math_blocks(original_md: str | None) -> list[str]:
    """一页的页级公式（按出现顺序）：`$$…$$`、`\\[…\\]`、以及**行内** `$…$`。"""
    text = original_md or ""
    found: list[tuple[int, str]] = []
    for rx in (_DOLLAR_BLOCK, _BRACKET_BLOCK, _INLINE):
        for m in rx.finditer(text):
            found.append((m.start(), m.group(1).strip()))
    found.sort(key=lambda x: x[0])
    # 同一段被外层 `$$` 与内层匹配同时收进来时去重（保留先出现的）
    out: list[str] = []
    for _, t in found:
        if t and t not in out:
            out.append(t)
    return out


def score_pair(parsed: str, vision: str) -> dict:
    """解析版与页级版的相似度（0~1）+ 分量，便于门槛判断与调参。"""
    a, b = _canon(parsed), _canon(vision)
    if len(a) < MIN_CANON_CHARS or not b:
        return {"score": 0.0, "cover": 0.0, "matched": 0}
    matched = _cover(a, b)
    if matched < MIN_MATCHED_CHARS:
        return {"score": 0.0, "cover": 0.0, "matched": matched}
    cover = matched / len(a)
    return {"score": round(cover, 4), "cover": round(cover, 4), "matched": matched}


def align_page(parsed: list[dict], vision: list[str], *,
               threshold: float = DEFAULT_THRESHOLD,
               lookahead: int = LOOKAHEAD, margin: float = MARGIN) -> list[dict]:
    """同页对齐：``parsed[i]["text"]`` → ``vision[k]``（或 None）。

    ``parsed`` 必须**已按阅读顺序**排好。判定规则（宁可不配，也不配错）：
    1. 分数 ≥ ``HIGH_SCORE`` 直接采纳；
    2. 否则要 ≥ ``MID_SCORE`` **且**明显领先第二名（差距 ≥ ``margin``）**且**不倒退回
       已用过的序号（单调）才采纳；
    3. 其余一律 None → 调用方保留解析版并标 ``needs_review``。

    返回与 ``parsed`` 等长的列表，每项 ``{"index": k|None, "score": float,
    "best_score": float, "second_score": float}``。
    """
    out: list[dict] = []
    j = 0
    for item in parsed:
        text = item.get("text") or ""
        scores: list[tuple[float, int]] = []
        hi = min(len(vision), j + 1 + max(0, lookahead))
        for k in range(min(j, len(vision) - 1) if vision else 0, hi):
            scores.append((score_pair(text, vision[k])["score"], k))
        scores.sort(key=lambda x: (-x[0], x[1]))     # 分高优先；**平分取阅读顺序靠前**的
        best, second = (scores[0] if scores else (0.0, None)), (scores[1] if len(scores) > 1
                                                               else (0.0, None))
        best_score, best_k = best
        take = best_k is not None and best_score >= MID_SCORE
        if take and best_score < HIGH_SCORE and (best_score - second[0]) < margin:
            take = False        # 中等分还咬得很紧 → 不配（宁可不配也不错配）
        if best_k is not None and take:
            out.append({"index": best_k, "score": best_score,
                        "best_score": best_score, "second_score": second[0]})
            j = best_k                      # 不回退：下一个碎片可以继续用同一块（N:1）
        else:
            out.append({"index": None, "score": 0.0,
                        "best_score": best_score, "second_score": second[0]})
    return out


def is_fragment(latex: str | None) -> bool:
    """短到不构成"一条公式"的碎片（`\\times 10`、`b\\cdot d`、`j=1`、`\\frac{1}{1}`）。

    这类东西在解析产物里大多是**图内坐标标签**或行内小片段，不该在知识库里单独占一条
    "公式"（用户搜"量化公式"时搜出一堆 `\\times 10` 就是这种）。
    规则：短，且没配上页级完整公式 —— 配上页级的说明它确实是一条真公式。
    """
    return len(_canon(latex)) < MIN_CANON_CHARS


def page_vision_math(paper_id: str) -> dict[int, list[str]]:
    """``{页号: [页级公式…]}``（读 ``md/pages.json``；没有页级产物就返回空）。

    只读、不写；页级翻译没做过时是空字典 —— 换源逻辑自然退化成"全用解析版"。
    """
    from backend.services.paper import reader_service

    try:
        store = reader_service.load_page_store(paper_id)
        pages = store.all_pages()
    except Exception:  # noqa: BLE001 —— 页级产物缺失/损坏不该让建索引失败
        return {}
    out: dict[int, list[str]] = {}
    for page_no, payload in (pages or {}).items():
        try:
            blocks = vision_math_blocks((payload or {}).get("original_md"))
        except Exception:  # noqa: BLE001
            continue
        if blocks:
            out[int(page_no)] = blocks
    return out


# "后面必须还有东西"的命令：出现在**结尾**说明式子被截断了
_NEEDS_OPERAND = ("min", "max", "inf", "sup", "sum", "prod", "int", "frac", "dfrac",
                  "sqrt", "cdot", "times", "geq", "leq", "ge", "le", "langle",
                  "to", "sim", "approx", "pm", "=", "+", "-", "*", "/", "(", ",")
_EMPTY_GROUP = re.compile(r"\\(?:sqrt|frac)\s*\{\s*\}|\\frac\{[^{}]*\}\{\s*\}")


def looks_incomplete(latex: str | None, candidates: list[str] | None = None) -> bool:
    """解析版是否"看起来残缺"（没配上页级源时用来标 ``needs_review``）。

    判据都可解释、且**只影响"待修复"标记**（不排除、不降权）：
    多候选（说明同一公式被切碎）、花括号或 `\\langle…\\rangle` 不配平、空的分式/根号、
    结尾是"后面还得有东西"的命令或运算符、规范符号串太短（短到无法判断完整性）。
    """
    t = (latex or "").strip()
    if not t:
        return True
    if len(candidates or []) > 1:
        return True
    if t.count("{") != t.count("}"):
        return True
    if t.count("\\langle") != t.count("\\rangle"):
        return True
    if _EMPTY_GROUP.search(t):
        return True
    tail = t.rstrip(" .,;:")
    if any(tail.endswith(c) or tail.endswith("\\" + c) for c in _NEEDS_OPERAND):
        return True
    return len(_canon(t)) < MIN_CANON_CHARS


def parsed_latex(f: dict) -> str:
    """解析产物里这条公式的 LaTeX（优先级：修复版 → 最终版 → 首个候选）。"""
    return (f.get("latex_repaired") or f.get("latex_final")
            or (f.get("latex_candidate") or [""])[0] or "").strip()


def assign_formulas(rows: list[dict], formulas: dict[str, dict],
                    paper_id: str) -> dict[str, dict]:
    """给每个公式块定"用哪份 LaTeX"，并标出**同一条完整公式的碎片分组**。

    返回 ``{block_id: {...}}``，每项：

    ==================  ==========================================================
    ``latex``           该用的 LaTeX（页级完整版，或解析版兜底）
    ``source``          ``"page_vision"`` / ``"parsed"``
    ``score``           对齐分数（``source == "page_vision"`` 时有意义）
    ``needs_review``    解析版残缺且没配上页级源 → 知识库标「待修复」
    ``group_first``     同一页级公式的碎片里，只有第一个为 True（只出一条原子 chunk）
    ``block_ids``       同一组的全部解析块 id（写进 metadata，便于回溯）
    ==================  ==========================================================

    ``rows`` 必须按阅读顺序（``order_global``）——对齐的单调性依赖它。
    """
    vision_by_page = page_vision_math(paper_id)
    by_page: dict[int, list[dict]] = {}
    for b in rows:
        if b["id"] in formulas:
            by_page.setdefault(int(b["page"]), []).append(b)

    out: dict[str, dict] = {}
    for page, items in by_page.items():
        vision = vision_by_page.get(page) or []
        parsed = [{"id": b["id"], "text": parsed_latex(formulas[b["id"]])} for b in items]
        aligned = align_page(parsed, vision) if vision else \
            [{"index": None, "score": 0.0, "best_score": 0.0, "second_score": 0.0}
             for _ in items]
        # 先分组：同一页级块被多个碎片命中 → 一条完整公式
        groups: dict[int, list[str]] = {}
        for b, r in zip(items, aligned):
            if r["index"] is not None:
                groups.setdefault(int(r["index"]), []).append(b["id"])
        for b, r in zip(items, aligned):
            f = formulas[b["id"]]
            parsed_latex_ = parsed_latex(f)
            cands = f.get("latex_candidate") or []
            if r["index"] is not None:
                k = int(r["index"])
                out[b["id"]] = {
                    "latex": vision[k],
                    "source": "page_vision",
                    "score": r["score"],
                    "needs_review": False,
                    "group_first": groups[k][0] == b["id"],
                    "block_ids": list(groups[k]),
                }
            else:
                out[b["id"]] = {
                    "latex": parsed_latex_,
                    "source": "parsed",
                    "score": 0.0,
                    "needs_review": looks_incomplete(parsed_latex_, list(cands)),
                    "group_first": True,
                    "block_ids": [b["id"]],
                }
    return out


def page_images_dir(paper_id: str) -> Path:
    """页图目录（给将来的审计/可视化用；本模块不写盘）。"""
    from backend.services.paper import paper_service

    return Path(paper_service.paper_paths(paper_id)["pages"])

