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
# 续行合并的长度门槛：低于它的以运算符开头的块（`-\\infty` 这类）不算续行。
# 比 MIN_CANON_CHARS 松一点：`\\geq \\frac{1}{4^b}.`（规范串 9 字符）应当并进上一条推导。
MIN_CONTINUATION_CHARS = 6
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


def _prose_words(text: str) -> int:
    """数"普通英文单词"个数 —— **先剥掉 LaTeX 命令名**。

    踩过的坑：`\\left` / `\\right` / `\\quad` / `\\mathbb` / `\\frac` 都是 ≥4 个字母的
    "英文单词"，不剥掉的话任何带括号的公式都会被判成散文 —— 实测把 `\\tag{1}` 的
    TurboQuant Eq.1 / Attention Eq.1 全挡在补漏之外。
    """
    s = re.sub(r"\\(?:text|mathrm|operatorname|textit)\{[^}]*\}", "", text or "")
    s = re.sub(r"\\[a-zA-Z]+", " ", s)
    return len(_ENGLISH_WORD.findall(s))


def _looks_like_math(t: str) -> bool:
    """行内 `$…$` 里抓到的到底是不是数学。

    页级 md 里偶尔有**落单的 `$`**（转写噪声），`$…$` 就会把两段散文夹在中间 ——
    实测抓到过 `is the sequence length,`、`minimize the following expected distortion measures`。
    判据：剥掉 LaTeX 命令后普通英文单词 ≥3 个 → 不是数学；且必须有点数学符号。
    """
    if _prose_words(t) >= 3:
        return False
    return bool(re.search(r"[\\^_=<>+\-/*]|\d", t or ""))


def vision_math_blocks(original_md: str | None) -> list[str]:
    """一页的页级公式（按出现顺序）：`$$…$$`、`\\[…\\]`、以及**行内** `$…$`。

    行内候选要过一道 `_looks_like_math`（挡掉落单 `$` 夹出来的散文）。
    """
    text = original_md or ""
    found: list[tuple[int, str]] = []
    for rx in (_DOLLAR_BLOCK, _BRACKET_BLOCK):
        for m in rx.finditer(text):
            found.append((m.start(), m.group(1).strip()))
    for m in _INLINE.finditer(text):
        cand = m.group(1).strip()
        if _looks_like_math(cand):
            found.append((m.start(), cand))
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


# --------------------------------------------------------------------------
# 编号 / "是不是独立展示公式" / 解析器漏掉的公式
# --------------------------------------------------------------------------
_TAG = re.compile(r"\\tag\*?\s*\{\s*([^}]{1,12})\s*\}")
# 行内散文数学里常见的小词（`minimize the following expected distortion`）：
# 靠它把"被误当公式的整句英文"挡掉
_ENGLISH_WORD = re.compile(r"\b[a-z]{4,}\b")


def formula_number(latex: str | None) -> str | None:
    """从 LaTeX 里取出公式编号（`\\tag{1}` → ``"1"``）；没有编号返回 None。

    用户口径：编号要**单独进 metadata**，不能只埋在 LaTeX 字符串里。
    """
    m = _TAG.search(latex or "")
    return m.group(1).strip() if m else None


# 关系符：定义式 / 尺寸式（`p_\ell := \int…`、`Q: \mathbb{R}^d \to …`、`S \sim \mathcal{N}(0,1)`）
_RELATION = re.compile(r":=|\\geq\b|\\leq\b|\\ge\b|\\le\b|\\approx|\\sim\b|\\to\b|"
                       r"\\in\b|\\propto|\\equiv|=")
# 算法伪代码的赋值行（`\text{idx} \leftarrow \text{QUANT}(x)`）—— 不是论文公式
_ASSIGN_ARROW = re.compile(r"\\leftarrow|\\rightarrow|←|→")


def is_synthetic_candidate(latex: str | None, *, display: bool = False) -> bool:
    """该不该**补进**公式库（用户口径第 1 条：只补正式展示公式，不补行内小式子）。

    只有两条准入（其余一律不补）：
    ① 有编号 `\\tag{n}` —— 正式编号公式（TurboQuant Eq.1、Attention Eq.1/2/3 都是这类）；
    ② 视觉转写自己写成了**行间** `$$…$$`（说明它是独立成行的展示公式）。

    **不收**行内 `$…$`：实测放开之后立刻涌进 40+ 条（`\\text{idx}\\leftarrow…` 伪代码行、
    `W_i^Q \\in \\mathbb{R}^{…}` 尺寸说明、`D_{\\text{mse}} \\approx 0.36, …` 数值行）——
    正是用户要求"不要补进公式库"的那批。
    """
    if not is_display_formula(latex):
        return False
    return bool(formula_number(latex)) or bool(display)


def is_display_formula(latex: str | None, raw_members: list[str] | None = None) -> bool:
    """页级公式块（合并后）是不是**独立展示公式**（区别于行内散文数学）。

    判据（宽严有序）：
    1. 先挡两类垃圾：规范符号串太短（`\\times 10`）、去掉 `\\text{}` 后还有 ≥3 个普通英文单词
       （落单 `$` 夹出来的散文）；
    2. **原始成员**里有 `\\tag{n}` / `\\begin{aligned}` / `\\begin{equation}` → 是；
    3. 否则要求够长（≥ 40 字符）且至少两个 LaTeX 命令。

    ⚠️ 第 2 条必须看**原始成员**，不能看合并后的文本 —— 合并时我们自己会包一层
    `\\begin{aligned}`，用它当判据等于"什么都是展示公式"（踩过：散文被包成 aligned 混进补漏）。
    """
    t = (latex or "").strip()
    if not t or len(_canon(t)) < MIN_CANON_CHARS:
        return False
    # 展示公式放宽到 ≥5 个英文词才判散文：`lrate = …\min(step\_num…, warmup\_steps…)` 这种
    # 公式里的长标识符（step/warmup/steps/lrate）会被算成"英文词"，用 ≥3 会误杀 Attention Eq.3。
    if _prose_words(t) >= 5:
        return False
    raws = [r for r in (raw_members or [t]) if r]
    if any(_TAG.search(r) or re.search(r"\\begin\{(?:aligned|equation|align)", r)
           for r in raws):
        return True
    if len(t) < 40:
        return False
    return len(re.findall(r"\\[a-zA-Z]+", t)) >= 2


def _order_ruler(aligned: list[dict], page_blocks: list[dict]) -> list[tuple[int, int]]:
    """``[(页级公式序号, 解析块 order)]`` —— 用已对齐的公式块当"位置刻度"。"""
    by_id = {b["id"]: b for b in page_blocks}
    out = []
    for a in aligned:
        if a.get("index") is None:
            continue
        blk = by_id.get(a.get("id"))
        if blk is not None:
            out.append((int(a["index"]), int(blk["order"])))
    return sorted(out)


def _nearest_block(order: float, page_blocks: list[dict]) -> dict | None:
    return min(page_blocks, key=lambda b: abs(float(b["order"]) - order)) if page_blocks else None


def _anchor_for(vision_index: int, vision_total: int, page_blocks: list[dict],
                ruler: list[tuple[int, int]]) -> tuple[dict | None, str, str]:
    """给"解析器漏掉的页级公式"找一个**同页**锚点（绝不跨页）。

    返回 ``(block, anchor_type, anchor_precision)``：找得到就是
    ``nearest_parsed_block`` / ``approximate``；同页连块都没有就退 ``page`` / ``page``。
    """
    if not page_blocks:
        return None, "page", "page"
    order: float | None = None
    before = [r for r in ruler if r[0] < vision_index]
    after = [r for r in ruler if r[0] > vision_index]
    if before and after:
        (ki, oi), (kj, oj) = max(before), min(after)
        order = oi + (oj - oi) * (vision_index - ki) / max(1, kj - ki)
    elif before:
        order = float(max(before)[1])
    elif after:
        order = float(min(after)[1])
    if order is None:
        # 该页没有任何已对齐的公式可当刻度 → 按"页内块数 × 公式序号比例"插值
        idx = int((vision_index + 0.5) / max(1, vision_total) * len(page_blocks))
        blk = page_blocks[min(max(idx, 0), len(page_blocks) - 1)]
    else:
        blk = _nearest_block(order, page_blocks)
    return blk, "nearest_parsed_block", "approximate"


def synthetic_formulas(rows: list[dict], formulas: dict[str, dict], paper_id: str,
                       assignments: dict[str, dict] | None = None) -> list[dict]:
    """**解析器漏掉的编号展示公式**：页级 md 里有、但没有任何解析块配到它。

    用户口径（第 1/3/4 条）：
    - 只补"独立展示公式"（`\\tag{n}` / `aligned` / 够长且几乎无英文词），
      普通行内数学 span 一条都不补；
    - 锚点用**同页最近的解析块**（``nearest_parsed_block`` / ``approximate``），
      同页连块都没有才退 ``page`` / ``page``；**绝不跨页**；
    - metadata 带 ``synthetic=True`` / ``formula_source="page_md"`` / ``latex_source="page_vision"`` /
      ``latex_parsed=None``（没有解析对应内容就不伪造）/ ``formula_number`` /
      ``anchor_type`` / ``anchor_precision``。

    ``assignments`` 传 ``assign_formulas`` 的结果（复用同一次对齐，避免两处判定不一致）。
    """
    groups_by_page = vision_groups(paper_id)
    if not groups_by_page:
        return []
    blocks_by_page: dict[int, list[dict]] = {}
    for b in rows:
        blocks_by_page.setdefault(int(b["page"]), []).append(
            {"id": b["id"], "order": int(b["order"])})
    for items in blocks_by_page.values():
        items.sort(key=lambda x: x["order"])

    # 每页重跑一次对齐（确定性的），据此知道哪些展示公式**已经**被解析块用掉了、
    # 以及哪些解析块可以当"位置刻度"。
    assignments = assignments if assignments is not None else \
        assign_formulas(rows, formulas, paper_id)
    vision_raw = page_vision_math(paper_id)      # 未合并的原始块
    display_map = vision_display_map(paper_id)   # 行间公式集合（判定"独立展示公式"用）
    out: list[dict] = []
    for page in sorted(set(blocks_by_page) | set(groups_by_page)):
        items = blocks_by_page.get(page) or []
        groups = groups_by_page.get(page) or []
        if not groups:
            continue
        raws = vision_raw.get(page) or []
        vision = [tex for tex, _ in groups]
        parsed = [{"id": b["id"], "text": parsed_latex(formulas.get(b["id"], {}))}
                  for b in items if b["id"] in formulas]
        aligned = align_page(parsed, vision)
        used = {int(a["index"]) for a in aligned if a["index"] is not None}
        ruler = _order_ruler(aligned, items)
        for i, (tex, members) in enumerate(groups):
            first_raw = raws[members[0]] if members and 0 <= members[0] < len(raws) else tex
            if i in used or not is_synthetic_candidate(
                    tex, display=(first_raw in (display_map.get(page) or set()))):
                continue
            blk, anchor_type, precision = _anchor_for(i, len(vision), items, ruler)
            out.append({
                "paper_id": paper_id, "page": page,
                "vision_index": members[0] if members else i,
                "latex": tex, "formula_number": formula_number(tex),
                "block_id": (blk or {}).get("id"),
                "order": int((blk or {}).get("order") or 0),
                "anchor_type": anchor_type, "anchor_precision": precision,
            })
    # 同一篇里完全相同的 LaTeX 只留最早那条（其余记进 also_on_pages，便于回溯）
    seen: dict[str, dict] = {}
    deduped: list[dict] = []
    for item in out:
        key = _canon(item["latex"])
        if key in seen:
            seen[key].setdefault("also_on_pages", []).append(item["page"])
            continue
        seen[key] = item
        deduped.append(item)
    return deduped


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


# "续行"开头：页级转写常把**一条多行推导**拆成好几个 `$$` 块，续行以关系符/运算符开头
# （`= \sum_j …`、`\geq \frac{1}{4^b}.`）。单独取其中一块就是"看起来不完整"的那种条目。
_CONTINUATION = re.compile(
    r"^\s*(?:=|\\geq|\\leq|\\ge\b|\\le\b|\\approx|\\sim|\\to|\\cdot|\\times|\\pm|\\mp|"
    r"\+|-|<|>|\\sum|\\prod|\\int|\\\\|&)")


def group_display_blocks(blocks: list[str]) -> list[tuple[str, list[int]]]:
    r"""把同一条展示公式的多行（续行）合成一条。

    返回 ``[(latex, [原始块序号…]), …]`` —— 序号留着做锚点与合成条目编号。

    为什么要合：p20 那条推导在页级 md 里是 5 个 `$$` 块（`D_mse := …` / `D_prod = …` /
    `D_mse = \sum…` / `= \sum…` / `\geq \frac{1}{4^b}.`）。按块对齐时，解析侧的
    `-1 =E\langle e, x\rangle -\langle e, QQ(x))\rangle` 只配到中间一块，用户看到的
    就是"以 `=` 开头、没有左边"的残缺公式。
    """
    out: list[tuple[str, list[int]]] = []
    for i, raw in enumerate(blocks):
        tex = (raw or "").strip()
        if not tex:
            continue
        if out and _CONTINUATION.match(tex) and len(_canon(tex)) >= MIN_CONTINUATION_CHARS:
            # 续行还得"够长"才算续行：页级 md 里 `$-\infty$`、`$\times$` 这种行内小式子
            # 也以运算符开头，但它们不属于上面那条公式（实测把 `W^O ∈ …` 和 `-\infty` 粘一起了）
            prev_tex, prev_idx = out[-1]
            out[-1] = (_join_lines([prev_tex, tex]), prev_idx + [i])
        else:
            out.append((tex, [i]))
    return out


def _join_lines(parts: list[str]) -> str:
    """把多行拼成一条可渲染的公式。

    已经含 `\\begin{…}`（页级自己给了 `aligned`）的用 `\\quad` 平接，避免嵌套环境；
    其余用 `aligned` + `\\\\` 保留多行结构 —— 两种写法都过了 KaTeX
    （`throwOnError: true`，含 `\\tag{n}` 的情形）。
    """
    parts = [p.strip() for p in parts if p and p.strip()]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    if any("\\begin{" in p for p in parts):
        return " \\quad ".join(parts)
    return "\\begin{aligned} " + " \\\\ ".join(parts) + " \\end{aligned}"


def vision_display_blocks(original_md: str | None) -> list[str]:
    """只取**行间**公式（`$$…$$` / `\\[…\\]`），不含行内 `$…$`。

    用来判断"这是不是一条独立展示公式"：视觉转写把独立公式写成 `$$`、
    把行内小式子（含算法伪代码行 `$\\text{idx} \\leftarrow …$`）写成 `$…$` ——
    补漏只补前者（用户口径：普通行内数学不进公式库）。
    """
    text = original_md or ""
    found: list[tuple[int, str]] = []
    for rx in (_DOLLAR_BLOCK, _BRACKET_BLOCK):
        for m in rx.finditer(text):
            found.append((m.start(), m.group(1).strip()))
    found.sort(key=lambda x: x[0])
    out: list[str] = []
    for _, t in found:
        if t and t not in out:
            out.append(t)
    return out


def vision_display_map(paper_id: str) -> dict[int, set[str]]:
    """``{页号: {行间公式文本…}}``（判定补漏候选用）。"""
    from backend.services.paper import reader_service

    try:
        pages = reader_service.load_page_store(paper_id).all_pages()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, set[str]] = {}
    for page_no, payload in (pages or {}).items():
        blocks = vision_display_blocks((payload or {}).get("original_md"))
        if blocks:
            out[int(page_no)] = set(blocks)
    return out


def vision_groups(paper_id: str) -> dict[int, list[tuple[str, list[int]]]]:
    """``{页号: [(合并后的公式, [原始块序号…]), …]}``。"""
    return {page: group_display_blocks(blocks)
            for page, blocks in page_vision_math(paper_id).items()}


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
    # 对齐在**合并后的展示公式**上做：续行先并回同一条（见 group_display_blocks）
    groups_by_page = vision_groups(paper_id)
    by_page: dict[int, list[dict]] = {}
    for b in rows:
        if b["id"] in formulas:
            by_page.setdefault(int(b["page"]), []).append(b)

    out: dict[str, dict] = {}
    for page, items in by_page.items():
        groups = groups_by_page.get(page) or []
        vision = [tex for tex, _ in groups]
        parsed = [{"id": b["id"], "text": parsed_latex(formulas[b["id"]])} for b in items]
        aligned = align_page(parsed, vision) if vision else \
            [{"index": None, "score": 0.0, "best_score": 0.0, "second_score": 0.0}
             for _ in items]
        # 先分组：同一页级块被多个碎片命中 → 一条完整公式
        groups_used: dict[int, list[str]] = {}
        for b, r in zip(items, aligned):
            if r["index"] is not None:
                groups_used.setdefault(int(r["index"]), []).append(b["id"])
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
                    "group_first": groups_used[k][0] == b["id"],
                    "block_ids": list(groups_used[k]),
                    "formula_number": formula_number(vision[k]),
                }
            else:
                out[b["id"]] = {
                    "latex": parsed_latex_,
                    "source": "parsed",
                    "score": 0.0,
                    "needs_review": looks_incomplete(parsed_latex_, list(cands)),
                    "group_first": True,
                    "block_ids": [b["id"]],
                    "formula_number": None,
                }
    return out


def page_images_dir(paper_id: str) -> Path:
    """页图目录（给将来的审计/可视化用；本模块不写盘）。"""
    from backend.services.paper import paper_service

    return Path(paper_service.paper_paths(paper_id)["pages"])





