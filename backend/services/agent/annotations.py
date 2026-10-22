"""Agent 的高亮 / 笔记手脚（《修改计划》第六步）。

**复用** `annotation_service`（同一套表、同一套增删改查、同一套 ID 前缀）—— 这里只补两件
"只有 agent 才需要"的事：

1. **按文字定位偏移量**。模型只会说"把这段文字标成高亮"，它算不出 DOM 偏移量 —— 而
   `Highlight` 存的是"某一面渲染文字里的起止偏移"。所以这里把**文字 → 偏移**这一步做掉。
2. **面感知的定位信息**。正文行 / Markdown 视图 / 原 PDF 是三套锚点（见 `docs/API.md` §8），
   "这条笔记挂在哪、点它能跳到哪"要按面分别回答；PDF 那一面服务端**看不到** pdf.js 文字层，
   所以只能查不能建（建了也没有偏移量可用）。

⚠️ 坐标系的唯一事实源在**前端**（`utils/domMarks.renderedText`：Markdown 标记不算字、
公式整体算 1 个 `U+FFFC`、只有空白的文本节点跳过、段内软换行按中/英分别接回）。
这里的 `render_like_dom()` 是它的**近似**：偏移量按近似值算，`selected_text` 一起存下来 ——
前端画的时候若发现偏移量对不上，会**按文字重新找位置**（`markRanges` 的兜底），
那才是"两边不会错位"的真正保证。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable

from backend.core.errors import NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.services.annotation import annotation_service as svc
from backend.services.annotation.annotation_service import HIGHLIGHT_COLORS

log = get_logger(__name__)

#: 公式在前端坐标系里占的字符（`domMarks.ATOM`）
ATOM = "\ufffc"
#: 中文/全角：判断软换行接回时要不要补空格（与前端 `useMarkdown.CJK_OR_FULL` 同一类字符）
_CJK_OR_FULL = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff01-\uff60\uffe0-\uffe6]")
_BLOCK_START = re.compile(r"^\s{0,3}(?:#{1,6}\s|[-*+]\s|\d{1,9}[.)]\s|>|\||```|~~~|\$\$)")
_HARD_BREAK = re.compile(r"(?:\s{2,}|\\)$")
_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_LIST_MARK = re.compile(r"^\s{0,3}(?:[-*+]\s+|\d{1,9}[.)]\s+)")
_QUOTE_MARK = re.compile(r"^\s{0,3}>\s?")
_HEAD_MARK = re.compile(r"^\s{0,3}#{1,6}\s+")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)")


def _join_soft_breaks(md: str) -> str:
    """段内软换行接回（与前端 `useMarkdown.normalizeSoftBreaks` 同一套规则）。

    分块的行文本里几乎没有这种换行，但 Markdown 视图那一面（整页 markdown）会有 ——
    不接回的话"文字对得上、偏移量差几个字节"，前端就得靠兜底找位置。
    """
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        joinable = (nxt is not None and line.strip() and nxt.strip()
                    and not _BLOCK_START.match(line) and not _HARD_BREAK.search(line)
                    and not _BLOCK_START.match(nxt))
        if not joinable:
            out.append(line)
            i += 1
            continue
        left, right = line.rstrip(), nxt.lstrip()
        if (_CJK_OR_FULL.search(left[-1:]) or _CJK_OR_FULL.search(right[:1])):
            out.append(left + right)
        else:
            out.append(f"{left} {right}")
        i += 2
    return "\n".join(out)


def render_like_dom(md: str) -> str:
    """Markdown → 前端坐标系里的那串文字（**近似**，够用来算偏移量）。"""
    text = _COMMENT.sub("", md or "")
    text = _IMAGE.sub("", text)                  # `<img>` 不产生文字
    text = _LINK.sub(r"\1", text)
    text = _DISPLAY_MATH.sub(ATOM, text)         # 公式整体算 1 个字符
    text = _INLINE_MATH.sub(ATOM, text)
    text = _join_soft_breaks(text)
    text = _HTML_TAG.sub("", text)
    out: list[str] = []
    for raw in text.split("\n"):
        line = _QUOTE_MARK.sub("", raw)
        line = _HEAD_MARK.sub("", line)
        line = _LIST_MARK.sub("", line)
        out.append(_EMPHASIS.sub("", line))
    return "\n".join(out)


#: 探针里的 Markdown 记号：`**` / `__` / 反引号 / `*`。模型会**连同记号一起抄**
#: （图注在行里就是 `**图 1：…**`），而渲染出来的那一侧没有这些字 —— 不洗掉，逐字匹配
#: 永远差这两个字符。单下划线**不动**：它可能是 LaTeX 下标，公式那条路也不走这里。
_PROBE_MARKS = re.compile(r"\*\*|__|`|\*")


def _norm_probe_md(value: object) -> str:
    """把引文按**渲染口径**洗一遍（Markdown 记号不算字）。

    实测 2026-12-07：图注 `**图 1：TURBOQUANT$_{\\text{prod}}$ 与 …**` 被原样传进来，
    而 `render_like_dom` 只洗了**行**那一侧 → 逐字匹配与「忽略公式」两条路都失配，
    回执说「找不到这一段」；用户看到的就是「带 Markdown 格式就挂不上」。
    """
    text = _PROBE_MARKS.sub("", str(value or ""))
    text = _LINK.sub(r"\1", text)
    return _HTML_TAG.sub("", text)


#: 公式探针：`$…$` / `$$…$$` 包裹，或者一段带反斜杠命令的 LaTeX（模型常常只给片段）
_DOLLAR_WRAP = re.compile(r"^\s*\${1,2}(.*)\${1,2}\s*$", re.DOTALL)


def _norm_latex(value: str) -> str:
    """公式比对前的规范化：空白一律去掉（`\frac {a} {b}` 与 `\frac{a}{b}` 是同一个公式）。"""
    return re.sub(r"\s+", "", str(value or ""))


def looks_like_formula(text: str) -> bool:
    """这段探针是**公式**还是**夹着公式的句子**。

    实测 2026-12-07：英文图注 `**Figure 1: … TURBOQUANT$_{\\text{prod}}$ …**` 里带 `$…$`，
    整句被判成"公式" → 走整式匹配 → 覆盖不到一半 → 回执「这段文字找不到」，于是**英文图注
    标不上**（中文那侧因为带汉字被排除，反而没事）。判据改成看**摘掉公式以后剩什么**：
    还剩成片的自然语言就是句子，只剩余数几个词、或还带着反斜杠命令才是公式。
    """
    probe = str(text or "").strip()
    if not probe or _CJK_RE.search(probe):
        return False
    if "$" not in probe and "\\" not in probe:
        return False
    rest = _strip_probe_math(probe).strip()
    if not rest:
        return True                      # 整串就是一个（或几个）公式
    if "\\" in rest:
        return True                      # 还带着 LaTeX 命令 → 这就是公式
    words = [w for w in re.split(r"\s+", rest) if len(w) > 1]
    return len(words) < 4


def math_spans(md: str) -> list[tuple[int, int, str]]:
    """Markdown 里所有公式的 ``(start, end, latex)``，按出现顺序（行内公式不重复计入行间公式内部）。"""
    md = md or ""
    out: list[tuple[int, int, str]] = []
    for match in _DISPLAY_MATH.finditer(md):
        out.append((match.start(), match.end(), match.group(1)))
    for match in _INLINE_MATH.finditer(md):
        if any(start <= match.start() < end for start, end, _ in out):
            continue
        out.append((match.start(), match.end(), match.group(1)))
    out.sort()
    return out


def find_formula_span(md: str, probe: str) -> tuple[int, int, str] | None:
    """公式探针 → 渲染坐标系里的 ``(start, end)``（公式整体只占 **1 个** `ATOM`）。

    为什么需要它（agent 报过「公式没法加高亮」）：本模块的坐标系把每条公式压成一个
    `U+FFFC`（前端 `domMarks` 同一口径），于是拿 LaTeX 去 `find_span` **永远**匹配不上 ——
    整式、片段都不行。但前端 `wrapRange` 早就支持「选区里是 ATOM」：它会把整个公式元素包进
    `mark`（块级公式还专门画在 `.katex-html` 的外接框上）。所以缺的只是**把 LaTeX 换成那个
    ATOM 的位置**，这一步在这里做。

    匹配口径：先比「规范化后完全相同」，再退到「探针是它的片段 / 它是探针的片段」（片段至少
    8 个字符才认，免得一个字母 `x` 匹配到别的公式）。偏移量按**逐段渲染**累加，与
    `render_like_dom` 同一套规则（前一段的渲染长度 + 前面各公式各占 1）。
    """
    raw = str(probe or "").strip()
    want = _norm_latex(_DOLLAR_WRAP.sub(r"\1", raw) or raw)
    if not want:
        return None
    spans = math_spans(md)
    target: tuple[int, int] | None = None
    for start, end, latex in spans:
        if _norm_latex(latex) == want:
            target = (start, end)
            break
    if target is None:
        # 片段匹配：**取"覆盖最长"的那一条，不是第一个**。
        #
        # 反例（agent 报过）：探针 `D_{\text{mse}} \leq \frac{\sqrt{3\pi}}{2} \cdot \frac{1}{4^b}`
        # 里天然包含 `_{\text{mse}}`，而后者是行里更早出现的一枚行内公式 —— 取第一个就把高亮
        # 挂到了那个碎片上。按覆盖长度取最大，才会命中整条式子。
        #: 片段必须**解释得了探针的大部分**（至少一半，且不少于 8 字符）。只要求 8 个字符时，
        #: 一条 165 字符的整式会被 14 字符的 `_{\text{prod}}` 碎片"解释"掉 8 个字符 ——
        #: 覆盖率 8%，高亮却挂到了碎片上（实测 2026-12-07，第 6 页）。
        need = max(8, (len(want) + 1) // 2)
        best: tuple[int, tuple[int, int]] | None = None
        for start, end, latex in spans:
            hay = _norm_latex(latex)
            covered = min(len(want), len(hay))
            if covered < need:
                continue
            if want in hay or hay in want:
                if best is None or covered > best[0]:
                    best = (covered, (start, end))
        if best is not None:
            target = best[1]
    if target is None:
        return None
    offset = 0
    pos = 0
    for start, end, _latex in spans:
        offset += len(render_like_dom(md[pos:start]))
        if start == target[0]:
            # 第三个值是**命中的那条公式的原始 Markdown**（含 `$$`），不是模型给的探针 ——
            # 探针允许是片段，存进库里笔记卡就会显示半截式子（用户报的"散块"）。
            return offset, offset + 1, md[start:end]
        offset += 1                      # 这条公式自己占 1 个 ATOM
        pos = end
    return None


def find_span(text: str, probe: str, *, occurrence: int = 1) -> tuple[int, int] | None:
    """``probe`` 在 ``text`` 里的第 ``occurrence`` 次出现 → ``(start, end)``；找不到返回 None。"""
    probe = (probe or "").strip()
    if not probe:
        return None
    start = -1
    for _ in range(max(1, int(occurrence or 1))):
        start = text.find(probe, start + 1)
        if start < 0:
            return None
    return start, start + len(probe)


#: 「最像的原文」的候选切分：按句末标点 / 换行切，太长的再用逗号补切一刀。
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；!?;])|\n")
_CLAUSE_SPLIT = re.compile(r"(?<=[，,、])")


def _strip_probe_math(text: str) -> str:
    """探针里的公式段与占位符都去掉（用户粘贴的中文里那一处是空的）。"""
    out = _DISPLAY_MATH.sub("", str(text or ""))
    out = _INLINE_MATH.sub("", out)
    return out.replace(ATOM, "")


def find_span_ignoring_atoms(rendered: str, probe: str, *,
                            occurrence: int = 1) -> tuple[int, int] | None:
    """允许探针**漏掉公式**的匹配（返回渲染坐标系里的 ``(start, end)``）。

    为什么需要（实测 2026-12-07）：译文里的公式在前端是个 KaTeX 元素，用户从界面复制那句
    中文时，公式那一处到手就是个**空位**（或者干脆没有）；而高亮坐标系里它是 `ATOM`，
    于是"逐字一致"永远对不上 —— 用户手打必然失败，`near_miss` 里回显的也只是个方块
    （看不出那里原本是 `$x \\in \\mathbb{S}^{d-1}$`）。

    做法：把渲染文本里的 `ATOM` **全部摘掉**再匹配（记下每个字在渲染坐标里的位置），
    探针里的 `$…$` 与占位符同样摘掉。这样「我们的目标是在将 量化为每个坐标 比特」里
    那两个空位就能对上；命中后把下标映射回渲染坐标。
    """
    #: 空白也一起摘掉：占位符两侧的空格在"复制粘贴"里常常留一个、去掉一个，
    #: `将 $x$ 量化` 与 `将 量 化`（中间带空格）都该能对上。
    bare: list[str] = []
    rmap: list[int] = []
    for i, ch in enumerate(rendered or ""):
        if ch == ATOM or ch.isspace():
            continue
        bare.append(ch)
        rmap.append(i)
    needle = re.sub(r"\s+", "", _strip_probe_math(_norm_probe_md(probe)))
    if not bare or not needle:
        return None
    hit = find_span("".join(bare), needle, occurrence=occurrence)
    if hit is None:
        return None
    start, end = hit
    return rmap[start], rmap[end - 1] + 1

def near_misses(text: str, probe: str, *, limit: int = 3, width: int = 80) -> list[str]:
    """找不到时给模型几句"最像的原文"，让它照着改一版（与产物纠错工具同一套路）。

    为什么要按**相似度**挑，而不是"头 8 个字必须逐字相同"（实测 2026-12-07）：用户把
    「内积估计上是有偏的」写成「对估计内积是有偏的」，头几个字就不一样了 —— 旧实现退回
    "最长公共前缀"，公共前缀是 0，于是返回空数组、提示"这一段里没有相近的句子"，而那句
    原文就在同一块里、只差几个字。现在用 `difflib.SequenceMatcher` 打分，**错几个字照样
    能给出候选**。
    """
    probe = " ".join(str(probe or "").split())
    if not probe or not text:
        return []
    cands: list[str] = []
    for piece in _SENTENCE_SPLIT.split(str(text)):
        piece = piece.strip()
        if len(piece) >= 4:
            cands.append(piece)
        # 一句话几十个字时，再按逗号切成小句：候选越细，越容易对上"半句"
        if len(piece) >= 24:
            cands.extend(one.strip() for one in _CLAUSE_SPLIT.split(piece)
                         if len(one.strip()) >= 4)
    if not cands:
        cands = [str(text).strip()]
    #: 打分用**探针被解释了多少**（匹配字符 / 探针长度），不是整体相似度：
    #: 「对估计内积是有偏的」vs 原文「…在内积估计上是有偏的」= 8/10 = 0.8（要留）；
    #: 而一句无关的英文 vs 那句中文只有 0.18 —— 给出来只会把模型带偏（它会以为论文就是
    #: 这么写的，照着抄一遍还是错）。0.6 是"大部分字都在"的下限。
    scored: list[tuple[float, float, str]] = []
    for cand in cands:
        matcher = SequenceMatcher(None, probe, cand)
        matched = sum(block.size for block in matcher.get_matching_blocks())
        coverage = matched / max(1, len(probe))
        if coverage >= 0.6:
            scored.append((coverage, matcher.ratio(), cand))
    if not scored:
        return []
    scored.sort(key=lambda one: (-one[0], -one[1]))
    pieces: list[str] = []
    seen: set[str] = set()
    for _cov, _ratio, cand in scored:
        piece = cand[:width]
        # 同一条原文常被切成"整句"和"小句"两份 → 去重（实测 2026-12-07）
        if piece in seen:
            continue
        seen.add(piece)
        pieces.append(piece)
    # 嵌套的短句只留长的：短的那条不提供任何新信息，只会让模型以为有两个候选
    return [one for one in pieces
            if not any(one != other and one in other for other in pieces)][:limit]


#: 行锚点长什么样（`p004-06`）。认不出来的锚点要**当场报格式错**，不能当成"这一行没这段文字"。
_ROW_ANCHOR_RE = re.compile(r"^p\d{1,5}-\d+$")


def anchor_kind(anchor: str) -> str:
    """锚点属于哪一面：``row`` / ``markdown`` / ``pdf``（认不出来当 ``row``）。"""
    text = str(anchor or "").strip()
    if text.startswith("md:p"):
        return "markdown"
    if text.startswith("pdf:p"):
        return "pdf"
    return "row"


def anchor_page_no(anchor: str) -> int:
    """``md:p9`` / ``pdf:p3`` → 页号；**行锚点解不出来**（回 0，调用方只在 Markdown 分支用它）。"""
    text = str(anchor or "").strip()
    if not (text.startswith("md:p") or text.startswith("pdf:p")):
        return 0
    digits = "".join(ch for ch in text.split(":", 1)[1] if ch.isdigit())
    return int(digits) if digits else 0


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


#: 侧别词表 → 数据库口径（`HIGHLIGHT_SIDES = ("en", "zh")`）。
#:
#: ⚠️ 页级产物用的是**另一套**词（`original` / `translation`），调用方常常把两套混着传：
#: `highlight_manage` 早先就把 `zh` 翻成 `translation` 再送进来，而落库只认 `en`/`zh`，
#: 于是被 `create_highlight` 静默改成 `en` —— "要求标中文、高亮却画在英文面"（agent 报过）。
#: 这里把两套都收进来，任何一边传哪套都对。
SIDE_ALIASES = {
    "en": "en", "zh": "zh",
    "original": "en", "transcript": "en", "transcription": "en", "ocr": "en",
    "translation": "zh", "translated": "zh",
    "原文": "en", "英文": "en", "转录": "en",
    "译文": "zh", "中文": "zh", "翻译": "zh",
}


def normalize_side(value: object) -> str:
    """侧别 → ``"en"`` / ``"zh"``；不认识返回空串（交给调用方按文字自动判）。"""
    return SIDE_ALIASES.get(str(value or "").strip().lower(), "")


def side_of_text(text: str) -> str:
    """引用的文字是中文还是英文 → ``"zh"`` / ``"en"``。

    为什么要按文字定这一面（用户口径 2026-10-07）：高亮**只画在它那一面**上 ——
    界面按"译文"读时（`pside=zh`，读者常这么看）只渲染中文那一列，标在原文那一面的高亮
    **根本画不出来**（用户报过"未产生高亮"：锚点、文字都对，就是看不见）。
    所以 `create()` 在调用方没指定面时按文字判断：引中文 → 译文面。
    """
    return "zh" if _CJK_RE.search(str(text or "")) else "en"


def locate_in_markdown(md: str, text: str, *, occurrence: int = 1
                      ) -> tuple[tuple[int, int], bool, str] | None:
    """在这一面的 Markdown 里找落点：先按**文字**，找不到再当**公式探针**。

    返回 ``((start, end), 是否公式, 该存进库的文字)``。两者是两套口径：文字走 `find_span`
    （坐标系里的逐字匹配），公式走 `find_formula_span`（LaTeX → 那个占位符的位置）。
    公式那条的第三个值是**命中的完整公式**（不是探针片段），因为笔记卡显示的就是它。
    """
    #: 引文先按渲染口径洗一遍：模型常把 `**…**` 一起抄（图注就是这种），行那一侧洗过了、
    #: 探针没洗 → 两条路都会失配（实测 2026-12-07「带 markdown 格式就挂不上」）。
    probe = _norm_probe_md(text)
    span = find_span(render_like_dom(md), probe, occurrence=occurrence)
    if span is not None:
        return span, False, ""
    # 用户从界面复制那句话时，公式那一处是**空位**（前端把 KaTeX 元素复制成了空 / 干脆没有），
    # 逐字匹配必然失败 —— 允许"漏掉公式"再试一次（见 `find_span_ignoring_atoms`）。
    relaxed = find_span_ignoring_atoms(render_like_dom(md), probe, occurrence=occurrence)
    if relaxed is not None:
        return relaxed, False, ""
    hit = find_formula_span(md, text)
    if hit is not None:
        return (hit[0], hit[1]), True, hit[2]
    return None


def _probe_latex(text: str) -> str:
    """公式探针 → 规范化 LaTeX（剥掉 `$$` 包裹、再去掉所有空白）。"""
    raw = str(text or "").strip()
    return _norm_latex(_DOLLAR_WRAP.sub(r"\1", raw) or raw)


def locate_formula_in_rows(paper_id: str, page: int, text: str, *,
                           prefer_side: str = "zh"
                           ) -> tuple[str, tuple[int, int], str, str, str] | None:
    r"""这一页里**覆盖面最大**的那条公式 → ``(行 id, (start, end), LaTeX, 渲染文本, 命中面)``。

    为什么不能"第一个命中就返回"（实测 2026-12-07）：agent 给的是第 6 页整条
    ``Q_{prod}(x) = [Q_{mse}(x), ...]``，而它在正文里第一次出现是 ``p006-00`` 里的一枚行内
    碎片 ``$_{prod}$``（覆盖率 8%）；真正那一条在 ``p006-09``（``$$Q_{prod}(x) = ...$$``，
    覆盖率 100%）。按行序取第一个就把高亮挂到了碎片上 —— 用户看到的就是"标错地方"。

    所以：**按覆盖率排名**，并要求覆盖率至少达到探针长度的一半（至少 8 字符）；达不到就
    返回 None —— 宁可让调用方报"找不到"，也不拿碎片冒充整式。`find_formula_span` 对单面
    做同样的把关，两处口径一致。
    """
    want = _probe_latex(text)
    if not want or not page:
        return None
    need = max(8, (len(want) + 1) // 2)
    sides = (prefer_side, "en" if prefer_side == "zh" else "zh")
    for payload in _page_payloads(paper_id):
        if int(payload.get("page") or 0) != int(page):
            continue
        best: tuple[int, int, str, tuple[int, int], str, str, str] | None = None
        for row in payload.get("rows") or []:
            row_id = str(row.get("id") or "")
            for order, side in enumerate(sides):
                raw = str(row.get("translation") if side == "zh"
                          else row.get("original") or "")
                if not raw.strip():
                    continue
                hit = find_formula_span(raw, text)
                if hit is None:
                    continue
                covered = min(len(want), len(_probe_latex(hit[2])))
                if covered < need:
                    continue
                # 覆盖率相同 → 优先 prefer_side（`-order` 越小越靠前）
                if best is None or (covered, -order) > (best[0], best[1]):
                    best = (covered, -order, row_id, (hit[0], hit[1]), hit[2],
                            render_like_dom(raw), side)
        if best is None:
            return None
        return best[2], best[3], best[4], best[5], best[6]
    return None


def locate_in_rows(paper_id: str, page: int, text: str, *, side: str = "en",
                   occurrence: int = 1) -> tuple[str, tuple[int, int], str] | None:
    """在这一页的**正文行**里找这段文字 → ``(行 id, (start, end), 渲染后的行文本)``。

    为什么 Markdown 锚点要往这里退一步（用户口径 2026-10-07）：高亮画在**哪一面**决定了
    用户能不能看见它 —— 阅读器默认是"对照 / 译文"（正文行那一面），而 `md:pN` 的高亮只在
    Markdown 视图里出现。agent 从 `page_markdown_get` 拿到的锚点恰好是 `md:pN`，
    于是"帮我给这一段加笔记"标出来的高亮在用户眼前**根本不存在**（用户报"未产生高亮"）。
    能在行里找到就落在行上（默认那一面），找不到才留在 Markdown 面。
    """
    if not page:
        return None
    if looks_like_formula(text):
        # 公式要**整式优先**（跨行比覆盖率），不能按行序取第一个命中：碎片常常出现在整式之前。
        best = locate_formula_in_rows(paper_id, page, text, prefer_side=side)
        if best is None:
            return None
        row_id, span, _src, rendered, _hit_side = best
        return row_id, span, rendered
    for payload in _page_payloads(paper_id):
        if int(payload.get("page") or 0) != int(page):
            continue
        for row in payload.get("rows") or []:
            raw = str(row.get("translation") if side == "zh" else row.get("original") or "")
            if not raw.strip():
                continue
            rendered = render_like_dom(raw)
            hit = locate_in_markdown(raw, text, occurrence=occurrence)
            if hit is not None:
                return str(row.get("id") or ""), hit[0], rendered
        return None                      # 这一页的行里没有（别去别的页找：锚点本来就指定了页）
    return None


def _page_payloads(paper_id: str) -> list[dict]:
    from backend.services.paper import reader_service

    return list(reader_service.page_translations(paper_id) or [])


def anchor_text(paper_id: str, anchor: str, *, side: str = "en") -> str | None:
    """锚点那一面的**原始行文本 / 页 Markdown**（还没过 `render_like_dom`）。

    - 正文行：`p004-02` → 页产物里那一行的 ``original`` / ``translation``；
    - Markdown 视图：`md:p4` → 第 4 页的 ``original_md`` / ``translation_md``（md 视图渲染的就是它）；
    - 原 PDF：`pdf:p4` → **None**（文字层在前端的 pdf.js 里，服务端看不到）。
    """
    kind = anchor_kind(anchor)
    text = str(anchor or "").strip()
    payloads = _page_payloads(paper_id)
    if kind == "pdf":
        return None
    if kind == "markdown":
        try:
            page = int(text.split(":", 1)[1].lstrip("p"))
        except (IndexError, ValueError):
            return None
        for payload in payloads:
            if int(payload.get("page") or 0) == page:
                key = "translation_md" if side == "zh" else "original_md"
                return str(payload.get(key) or "")
        return None
    for payload in payloads:
        for row in payload.get("rows") or []:
            if str(row.get("id") or "") == text:
                key = "translation" if side == "zh" else "original"
                return str(row.get(key) or "")
    return None


#: 配色建议（与 `annotation_service.HIGHLIGHT_COLORS` 里"新界面会提供"的那七个一致）。
#: 用户口径（2026-12-06）：**多彩、尽量不一样** —— 一片同色等于没有颜色，标注就看不出层次。
PALETTE = ("blue", "violet", "green", "orange", "red", "indigo", "yellow")


def spare_color(used: Iterable[str]) -> str:
    """在调色板里挑一个**最少被用过**的颜色（全都没用过就是第一个）。

    为什么要有这个（用户口径 2026-12-06「多彩、尽量不一样」）：让模型每次都自己挑，实测结果
    是清一色（8 条高亮 5 条同色）—— 颜色就不再携带信息了。这里做**兜底分配**：模型显式给了
    颜色就尊重它（它可能按语义配色），没给才由宿主挑一个当前用得最少的。
    """
    counts = {name: 0 for name in PALETTE}
    for raw in used or []:
        name = str(raw or "").strip().lower()
        if name in counts:
            counts[name] += 1
    # sorted 稳定：同票时按 PALETTE 顺序取，结果可预期（不会每次挑到不同的颜色）
    return min(PALETTE, key=lambda name: (counts[name], PALETTE.index(name)))


def spare_color_for(paper_id: str) -> str:
    """这篇论文当前**用得最少**的颜色（读一次高亮表，失败就退回调色板第一个）。"""
    try:
        items = svc.list_highlights(paper_id).get("items") or []
    except Exception as exc:  # noqa: BLE001 —— 配色不该挡住"存高亮"
        log.warning("取现有高亮配色失败（用调色板第一个）：%s: %s", paper_id, exc)
        return PALETTE[0]
    return spare_color([item.get("color") for item in items if isinstance(item, dict)])

#: 常见 CSS 颜色词 → hex。用户口径 2026-12-06：模型说 `teal` 不能被**静默改成黄色**。
#: 调色板（`HIGHLIGHT_COLORS`）只有七个名字，其余颜色词在这里落成 hex —— 前端本来就能画 hex，
#: 于是"任意颜色词"都能如实存下来。
CSS_COLOR_NAMES = {
    # 蓝绿系（agent 最常用它，之前全被吞成 yellow）
    "teal": "#0d9488", "cyan": "#06b6d4", "aqua": "#06b6d4", "turquoise": "#14b8a6",
    "sky": "#0ea5e9", "emerald": "#10b981", "lime": "#84cc16", "mint": "#6ee7b7",
    # 红橙黄系
    "coral": "#f87171", "salmon": "#fb7185", "rose": "#e11d48", "amber": "#f59e0b",
    "gold": "#eab308", "brown": "#92400e", "maroon": "#7f1d1d", "crimson": "#dc2626",
    # 紫粉 / 中性 / 其它
    "magenta": "#d946ef", "fuchsia": "#d946ef", "pink": "#ec4899", "plum": "#a855f7",
    "slate": "#64748b", "gray": "#6b7280", "grey": "#6b7280", "zinc": "#71717a",
    "navy": "#1e3a8a", "olive": "#65a30d", "forest": "#166534",
}


def normalize_color(value: object) -> str:
    """颜色词 / 色码 → 落库值；**认不出就返回空串**（交给调色板分配）。

    为什么认不出要给空串而不是原样透传：`_norm_color` 对未知写法会**静默退回 yellow**
    （那是"取色器坏了"观感的来源）。这里先自己把好关 —— 认不出就当"没指定"，由宿主挑一个
    当前用得最少的颜色，用户至少能看到一个像样的颜色，而不是一片黄。
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in CSS_COLOR_NAMES:
        return CSS_COLOR_NAMES[raw]
    if raw in HIGHLIGHT_COLORS:
        return raw
    if re.fullmatch(r"#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", raw):
        return raw
    if re.fullmatch(r"(?:rgb|rgba|hsl|hsla)\([0-9a-f.,%\s/]+\)", raw):
        return raw
    return ""


def _miss_reason(paper_id: str, page: int, text: str) -> str:
    """找不到时讲清**为什么**：在别的页 / 在别的行 / 跨了两行。

    用户口径（2026-12-07）：「跨块/跨页失败时不说原因」——把第 5 页两段拼成一句、或把第 3 页
    的文字传到 `md:p4`，以前只回「这一段里没有相近的句子」，看不出是**跨块**这个限制。
    高亮只能落在单个块内，这条限制本身合理，但要说出来。
    """
    probe = " ".join(str(text or "").split())
    if len(probe) < 6:
        return ""
    head, tail = probe[:10], probe[-10:]
    pages = _page_payloads(paper_id)
    for payload in pages:
        pg = int(payload.get("page") or 0)
        for row in payload.get("rows") or []:
            for key in ("translation", "original"):
                body = " ".join(str(row.get(key) or "").split())
                if probe in body:
                    where = f"第 {pg} 页的正文行 {row.get('id') or '?'}"
                    if pg and int(page or 0) and pg != int(page):
                        return f"这句其实在{where} —— 跨页标不了，请改传到那一页的锚点。"
                    return f"这句在{where}，跟你给的锚点不是同一处。"
    for payload in pages:
        if int(payload.get("page") or 0) != int(page or 0):
            continue
        rows = payload.get("rows") or []
        for i in range(len(rows) - 1):
            one, two = rows[i], rows[i + 1]
            a = " ".join(str(one.get("translation") or one.get("original") or "").split())
            b = " ".join(str(two.get("translation") or two.get("original") or "").split())
            if head and tail and head in a and tail in b and probe not in a and probe not in b:
                return (f"这段文字跨了两行（{one.get('id')} 与 {two.get('id')}）：高亮只能落在"
                        "单个块内，请分成两次标注，或缩到其中一行里。")
    return ""

def _norm_for_rows(value: object) -> str:
    """行比对用的归一化：去掉公式与占位符、压掉空白。

    为什么要两边都归一（2026-12-07）：探针里的公式可能是 `$x$`、也可能是个空位（用户从
    界面复制），而行文本里是 LaTeX —— 只比原始字符串，含公式的句子（尤其英文侧）永远对不上，
    于是回执退回一句「多半是横跨了多段」，用户拿不到块号也拿不到拆分建议。
    """
    return re.sub(r"\s+", "", _strip_probe_math(_norm_probe_md(value)))

def _span_row_hint(paper_id: str, page: int, text: str) -> str:
    """这段文字**为什么挪不到单个正文行**：跨了两段 / 页内对不上。

    用于"落在 Markdown 面"的回执（用户口径 2026-12-07：建成功但看不见，且没说是跨块导致）。
    先看有没有单行装得下它；装不下就找"头在哪一行、尾在哪一行"。
    """
    #: **两边都先归一**：探针里公式那处可能是 `$x$`、也可能只是个空位（用户从界面复制），
    #: 而行文本里是 LaTeX —— 直接比字符串只会在英文侧 / 含公式的句子上失配，然后退回
    #: 「多半是横跨了多段」这种没营养的话（2026-12-07 用户实测：中文侧给出块号、英文侧只给这句）。
    probe = _norm_for_rows(text)
    if len(probe) < 6:
        return ""
    advice = "高亮只能落在单个块内，建议拆成两条分别标。"

    def _holds(body: str, part: str) -> bool:
        """`part` 是不是在这一行里（行本身很短时按行长的八成比，别拿长串去卡短行）。"""
        if not part or not body:
            return False
        size = min(len(part), max(6, int(len(body) * 0.8)))
        return size >= 6 and part[:size] in body

    for payload in _page_payloads(paper_id):
        if int(payload.get("page") or 0) != int(page or 0):
            continue
        rows = payload.get("rows") or []
        #: **两面都看**：行是双语并排的，探针可能是中文也可能是英文 —— 只看译文会漏掉英文
        #: 那一侧的跨块（2026-12-07 实测：英文句只回「多半是横跨了多段」，因为只比了中文行）。
        bodies = [[_norm_for_rows(r.get(key)) for key in ("translation", "original")]
                  for r in rows]
        flat = [body for pair in bodies for body in pair if body]
        if any(probe in body for body in flat):
            return ""                      # 单行装得下 → 不跨块（那就不该落在 Markdown 面）
        # ① 探针**包含**整整两行（"把两段拼成一句"就是这种）：直接指名道姓
        inside = [i for i, pair in enumerate(bodies)
                  if any(len(b) >= 8 and b in probe for b in pair)]
        if len(inside) >= 2:
            one, two = rows[inside[0]], rows[inside[-1]]
            return f"这段跨了两个段落（{one.get('id')} 与 {two.get('id')}）：{advice}"
        # ② 探针只覆盖每行的一部分：找"头在哪一行、尾在哪一行"
        head, tail = probe[:12], probe[-12:]

        def holds(row_index: int, part: str) -> bool:
            return any(_holds(body, part) for body in bodies[row_index])

        for i in range(len(rows)):
            if not holds(i, head):
                continue
            for j in range(len(rows)):
                if i == j or not holds(j, tail):
                    continue
                one, two = rows[i], rows[j]
                return f"这段跨了两个段落（{one.get('id')} 与 {two.get('id')}）：{advice}"
        # 头尾都对不上：**确定地说**它不在这一页的正文行里（不再写「多半是」这种猜测）
        return f"这一页的正文行里没有这一段（它只存在于 Markdown 面）：{advice}"
    return ""

def _side_word(side: str) -> str:
    """回执里的叫法：用户看到的是「原文面 / 译文面 / Markdown 面」。"""
    return {"zh": "译文面", "en": "原文面"}.get(str(side or ""), "Markdown 面")


def _brief(value: object, limit: int = 40) -> str:
    body = re.sub(r"\s+", " ", str(value or "")).strip()
    return body if len(body) <= limit else body[:limit] + "…"


def create(paper_id: str, *, anchor: str, text: str, color: str = "",
           side: str = "", occurrence: int = 1) -> dict:
    """按**文字**建一条高亮（agent 的入口）。返回创建结果或 ``{"ok": False, "error": ...}``。

    借用 `annotation_service.create_highlight` 落库（同一套表与校验），这里只负责
    "把文字翻成偏移量"这一件事。
    """
    anchor = str(anchor or "").strip()
    if not anchor:
        raise ValidationError("anchor 必填（正文行 id / `md:p<页号>` / `pdf:p<页号>`）")
    # 没指定颜色就挑**当前用得最少**的（用户口径：多彩、尽量不一样）
    color = normalize_color(color) or spare_color_for(paper_id)
    # 面没指定（或给了个不认识的值）→ 按**引用的文字**判：引中文就落在译文面
    # （界面按译文读时，原文那一面的高亮根本画不出来，见 `side_of_text`）。
    # 面没指定时：**公式优先落在译文面**（用户口径：平时看的是中文那一列，标在原文面
    # 根本画不出来）；正文句子按文字判（引中文 → 译文面）。
    is_formula_probe = looks_like_formula(text)
    #: 调用方**显式**要的面（空 = 没指定，宿主按文字判）。公式要不要尊重它见下。
    requested_side = normalize_side(side)
    side = requested_side or side_of_text(text)
    # 公式**没指定面时**默认落译文面：用户平时读的是中文那一列，标在英文面根本画不出来
    # （2026-12-06 实测：agent 每次都显式传 `side=en`，8 条公式高亮全落在英文列）。
    # 但**显式给了 `en` 就尊重**（2026-12-07 实测反例：传英文 LaTeX + `side=en`，却被静默
    # 改成译文面，落到另一条公式上）。落面若与要求不符，回执里带 `side_note` 说清。
    if is_formula_probe and not requested_side:
        side = "zh"
    # **中文引文只可能落在译文面**：调用方显式给 `en` 也不听（与上面公式那条同一个理由）。
    # 实测（2026-12-07）4 条中文高亮全被当 `en` 面 → `locate_in_rows` 在英文行里找不到中文
    # → 退成 `md:pN`，而 Markdown 面的高亮在阅读视图里**根本画不出来**（用户报过"没出现"）。
    if _CJK_RE.search(str(text or "")):
        side = "zh"
    kind = anchor_kind(anchor)
    # 锚点格式错要**当场说清格式**（实测 2026-12-07：传 `anchor="row"` 回的是
    # 「找不到这个锚点对应的文字：row（行号可能在重译后变了）」—— 像"内容找不到"，
    # 其实是参数写错了，模型会照着这句去改文字，白折腾一轮）。
    if kind == "row" and not _ROW_ANCHOR_RE.match(anchor):
        return {"ok": False,
                "error": f"anchor 格式不对：{anchor!r}。只有两种合法写法：`md:p<页号>`"
                         "（例如 `md:p4`，宿主会自动挪到那一页的正文行）或行 id（例如 "
                         "`p004-06`）。行 id 通常拿不到 —— 直接传 `md:pN`。"}
    if kind == "markdown" and anchor_page_no(anchor) <= 0:
        return {"ok": False,
                "error": f"anchor 缺少页号：{anchor!r}（应写成 `md:p4` 这种）"}
    if kind == "pdf":
        return {"ok": False, "error": "原 PDF 那面的文字层只存在于阅读器里，服务端算不出偏移量："
                                      "请让用户在 PDF 页上自己划词高亮（或者改标正文行 / Markdown 视图）。"}
    def _write(on: str, on_side: str, on_kind: str, span_: tuple[int, int],
               stored: str, rendered_: str) -> dict:
        """落库这一步只此一处：整式快路与常规路径共用，免得两边的字段慢慢走偏。"""
        created = svc.create_highlight({
            "paper_id": paper_id, "block_id": on,
            "selected_text": stored,   # 文字高亮存坐标系里那一段；公式存 LaTeX
            "start_offset": span_[0], "end_offset": span_[1],
            "color": color, "side": on_side, "scope": on_kind,
        })
        created["page"] = svc.anchor_page(on)
        log.info("agent 建高亮 %s %s[%d:%d] %r", paper_id, on, span_[0], span_[1],
                 (rendered_ or "")[span_[0]:span_[1]][:30])
        return _finish({"ok": True, "highlight": created, "page": created["page"],
                        "scope": on_kind})

    def _finish(result: dict) -> dict:
        """落面与要求不符就**说出来**，不静默改（用户口径 2026-12-07）。

        另外**每次成功都带**「落在哪一面、第几页、命中原文」（用户口径 2026-12-07）：
        以前只有失败或落面不符才有提示，模型只能自己推断「这条标在了译文面」，
        也没法准确告诉用户位置。
        """
        hl = result.get("highlight") or {}
        landed = str(hl.get("side") or "")
        page_no = int(result.get("page") or 0)
        quote = _brief(str(hl.get("text") or ""))
        result["landed"] = {"side": landed, "page": page_no, "anchor": str(hl.get("block_id") or ""),
                            "text": quote}
        result["summary"] = f"第 {page_no} 页 · {_side_word(landed)} · 「{quote}」"
        if requested_side and landed and landed != requested_side:
            why = ("那一面没有这段文字（或公式默认落译文面）。要标在英文面，就把引用文字换成那一面的原文。")
            result["side_note"] = f"你要的是 `{requested_side}` 面，这条却落到了 `{landed}` 面：" + why
        return result

    # 公式走**整式优先**的快路：就算调用方给的是某一行，只要这一页里有覆盖面更大的整式，
    # 就落到那条上（`locate_formula_in_rows`）。实测（2026-12-07）agent 给的是整条
    # `Q_prod(x) = \left[…]\right]`，却因为所在行只匹配到 `$_{\text{prod}}$` 碎片而挂错节点。
    page_no = svc.anchor_page(anchor)
    if is_formula_probe and page_no:
        best = locate_formula_in_rows(paper_id, page_no, text, prefer_side=side)
        if best is not None:
            row_id, f_span, f_src, f_rendered, f_side = best
            log.info("公式高亮整式对齐：%s → %s[%d:%d]", anchor, row_id, f_span[0], f_span[1])
            return _write(row_id, f_side, "row", f_span, f_src, f_rendered)
    if kind == "markdown":
        # Markdown 锚点的文字能在**正文行**里找到 → 落在行上（用户默认读的就是那一面，
        # 高亮才看得见；见 `locate_in_rows`）。找不到才留在 Markdown 面。
        # **两面都试**：调用方给 `en`、引文却是中文时，只试 en 会把中文逼回 Markdown 面。
        for want_side in dict.fromkeys((side, "zh" if side == "en" else "en")):
            hit = locate_in_rows(paper_id, anchor_page_no(anchor), text,
                                 side=want_side, occurrence=occurrence)
            if hit:
                log.info("高亮锚点从 Markdown 挪到正文行（%s 面）：%s → %s",
                         want_side, anchor, hit[0])
                anchor, kind, side = hit[0], "row", want_side
                break
    def _locate(on: str):
        """在这一面找落点：返回 (渲染文本, 命中)；这一面没有这段文字时命中为 None。"""
        text_md = anchor_text(paper_id, anchor, side=on)
        if text_md is None:
            return None, None
        return render_like_dom(text_md), locate_in_markdown(text_md, text, occurrence=occurrence)

    other = "zh" if side == "en" else "en"
    order = [side, other] if side != other else [side]
    if is_formula_probe:
        # 探针看上去就是公式 → **先试译文面**（哪怕调用方显式给了 en）
        order = ["zh"] + [one for one in order if one != "zh"]
    raw = rendered = None
    #: 有没有哪一面**能取到文字**（与"这段文字在不在里面"是两件事，报错要分开）
    any_text = None
    span = None
    is_formula = False
    formula_src = ""
    for candidate in order:
        cand_rendered, cand_hit = _locate(candidate)
        # 只认**第一面**的文字（= 首选面）：以前每面都覆盖一次，于是中文探针最后拿英文页
        # 去做 `near_misses`，候选一句中文都不会有（实测 2026-12-07：错几个字的中文回了空数组）。
        if cand_rendered is not None and any_text is None:
            any_text = cand_rendered
        if cand_hit is None:
            continue
        # 命中的是**公式**时，即便这一面是原文面也要再试译文面：侧别该跟着**匹配结果**走，
        # 不能跟着探针长相走（实测探针写成 `C(f_X, b) :=` 时 looks_like_formula=False，
        # 于是"公式命中了、侧别却留在 en"）。
        if cand_hit[1] and candidate != "zh":
            zh_rendered, zh_hit = _locate("zh")
            if zh_hit is not None:
                cand_rendered, cand_hit = zh_rendered, zh_hit
                candidate = "zh"
        if candidate != side:
            log.info("高亮落面：%s 由 %s 面改到 %s 面（%s）", anchor, side, candidate,
                     "公式固定译文面" if cand_hit[1] else "这一面才有这段文字")
        side, raw, rendered = candidate, anchor_text(paper_id, anchor, side=candidate), cand_rendered
        span, is_formula, formula_src = cand_hit
        break
    if raw is None:
        if any_text is None:
            # 锚点本身解析不出文字：行号可能在重译后变了（跟"引文抄错"是两种错）
            return {"ok": False,
                    "error": f"找不到这个锚点对应的文字：{anchor}（行号可能在重译后变了；"
                             f"不确定行 id 就传 `md:p<页号>`）"}
        # 锚点在、这一段文字不在里面 —— 落下去走"找不到这段文字"，并给近似候选
        rendered = any_text
    if span is None:
        # 候选从**原始 Markdown** 里挑：公式会以 `$…$` 露出来（渲染态里它只是个方块，
        # 用户看不出那里原本是什么，也就没法照着改）。
        near = near_misses(raw if raw is not None else (rendered or ""), text)
        why = _miss_reason(paper_id, svc.anchor_page(anchor) or anchor_page_no(anchor), text)
        # near_miss 为空时也要说人话：回一个空数组，模型只能猜"我到底差在哪"。
        # 已经查出"它在别的页 / 跨行"时不再补这句 —— 否则等于自己跟自己打脸（实测 2026-12-07）。
        tail = "" if (near or why) else "这一段里没有相近的句子，换一段更完整的原文再试。"
        message = " ".join(one for one in (
            f"这段文字在 {anchor} 里找不到（要逐字一致；`**` 这类记号会自动忽略，"
            "公式处留空也认）。", why, tail) if one)
        return {"ok": False, "error": message, "near_miss": near}
    start, end = span
    # 公式那条存 **LaTeX 原文**而不是占位符：前端画标记时"选区含 ATOM 就跳过文字校验"
    # （`markRanges`），所以不影响定位；而列表 / 笔记里显示一个隐形的 U+FFFC 毫无意义。
    matched = rendered[start:end]
    had_atom = ATOM in matched
    if is_formula:
        stored_text = formula_src or str(text or "").strip()
    else:
        # 文字高亮存坐标系里那一段，但**把公式占位符换成空**：`ATOM`（U+FFFC）在列表 / 笔记 /
        # 引用里显示成方块，用户看不懂；偏移量仍按渲染坐标给，前端画标记时"选区含 ATOM 就跳过
        # 文字校验"，所以不影响定位（实测 2026-12-07 用户报的"那个方块是什么"就是它）。
        stored_text = matched.replace(ATOM, "")
    result = _write(anchor, side, kind, (start, end), stored_text, rendered)
    if had_atom and not is_formula:
        # 回执里的 text 会有"两个空格"：那里原本是公式。不说一句，用户会以为文字被吞了
        # （2026-12-07 实测）。
        holes = [m.group(1) for m in _INLINE_MATH.finditer(str(text or ""))]
        holes += [m.group(1) for m in _DISPLAY_MATH.finditer(str(text or ""))]
        shown = "、".join(f"${_brief(one, 16)}$" for one in holes[:3])
        result["text_note"] = (f"`text` 里公式那一处已留空（原本是公式元素：{shown}），"
                               "不是文字被吞" if shown else
                               "`text` 里公式那一处已留空（它原本是公式元素，不是文字被吞）")
    if kind == "markdown":
        # 落在 Markdown 面的高亮**阅读视图里看不见**（只有 Markdown 视图画它）。这本身是
        # "行里对不上"的兜底，但要说明白，否则用户以为"高亮没出现"（实测报过多次）。
        # 用 `_span_row_hint` 而不是 `_miss_reason`：这里的文字确实在页面里，只是横跨了多段
        # （2026-12-07 实测：跨段落那句回 ok=true 且没有任何警告，用户以为标上了）。
        why = _span_row_hint(paper_id, anchor_page_no(anchor), text)
        # 中文句号后面不要再加空格（实测 2026-12-07 用户点名：「看不到它」。 这段」中间那个空格）。
        result["warning"] = ("这条标在 **Markdown 视图** —— 阅读视图（对照 / 译文）里**看不到它**。"
                             + (why or "要让它出现在正文里，就把范围缩到单个段落内再标。"))
    return result


def list_items(paper_id: str, *, kind: str = "highlight", query: str | None = None) -> dict:
    """列这一篇的高亮 / 笔记（都带 `page` + `scope`，笔记还带它挂靠的高亮原文）。"""
    if kind == "note":
        notes = svc.list_notes(paper_id)["items"]
        by_id = {h["id"]: h for h in svc.list_highlights(paper_id)["items"]}
        for note in notes:
            hl = by_id.get(str(note.get("highlight_id") or ""))
            note["highlight_text"] = (hl or {}).get("text")
        if query:
            notes = [n for n in notes if query in str(n.get("content_md") or "")]
        return {"items": notes, "count": len(notes)}
    items = svc.list_highlights(paper_id)["items"]
    if query:
        items = [h for h in items if query in str(h.get("text") or "")]
    return {"items": items, "count": len(items)}


def locate(paper_id: str, *, highlight_id: str = "", note_id: str = "") -> dict:
    """一条高亮 / 笔记"挂在哪、点它能跳到哪"（定位信息，供模型说清位置）。

    笔记会给它挂靠的那条高亮（用户口径：`Highlight └── Note`，高亮是笔记的位置锚点）。
    """
    if note_id:
        note = next((n for n in svc.list_notes(paper_id)["items"] if n["id"] == note_id), None)
        if note is None:
            raise NotFoundError(f"笔记不存在：{note_id}")
        hl_id = str(note.get("highlight_id") or "")
        hl = next((h for h in svc.list_highlights(paper_id)["items"] if h["id"] == hl_id), None)
        return {"ok": True, "kind": "note", "note": note,
                "highlight": hl, "anchor": note.get("block_id"),
                "page": note.get("page"), "scope": note.get("scope"),
                "hint": _locate_hint(note.get("scope"), note.get("page"), hl)}
    if not highlight_id:
        raise ValidationError("highlight_id 或 note_id 必填")
    hl = next((h for h in svc.list_highlights(paper_id)["items"] if h["id"] == highlight_id), None)
    if hl is None:
        raise NotFoundError(f"高亮不存在：{highlight_id}")
    note = next((n for n in svc.list_notes(paper_id)["items"]
                 if str(n.get("highlight_id") or "") == highlight_id), None)
    return {"ok": True, "kind": "highlight", "highlight": hl, "note": note,
            "anchor": hl.get("block_id"), "page": hl.get("page"), "scope": hl.get("scope"),
            "hint": _locate_hint(hl.get("scope"), hl.get("page"), None)}


def _locate_hint(scope, page, highlight) -> str:
    """一句人话的定位说明（模型照着说给用户听）。"""
    where = {"row": "正文行", "markdown": "Markdown 视图", "pdf": "原 PDF 页图"}.get(
        str(scope or "row"), "正文行")
    text = (highlight or {}).get("text") or ""
    if page:
        return f"第 {page} 页的{where}" + (f"，原文是「{text[:40]}」" if text else "")
    return f"{where}" + (f"，原文是「{text[:40]}」" if text else "")


def create_note(paper_id: str, *, highlight_id: str, content: str) -> dict:
    """给一条**已存在的高亮**加笔记（用户口径：笔记必须挂在高亮上，不做孤儿笔记）。"""
    highlight_id = str(highlight_id or "").strip()
    if not highlight_id:
        raise ValidationError("highlight_id 必填：笔记要挂在一处高亮上（先 highlight op=create）")
    hl = next((h for h in svc.list_highlights(paper_id)["items"] if h["id"] == highlight_id), None)
    if hl is None:
        raise NotFoundError(f"高亮不存在：{highlight_id}")
    return svc.create_note({
        "paper_id": paper_id, "block_id": hl.get("block_id"), "highlight_id": highlight_id,
        "selected_text": hl.get("text"), "start_offset": hl.get("start"),
        "end_offset": hl.get("end"), "content_md": content,
    })
