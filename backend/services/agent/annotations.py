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


#: 公式探针：`$…$` / `$$…$$` 包裹，或者一段带反斜杠命令的 LaTeX（模型常常只给片段）
_DOLLAR_WRAP = re.compile(r"^\s*\${1,2}(.*)\${1,2}\s*$", re.DOTALL)


def _norm_latex(value: str) -> str:
    """公式比对前的规范化：空白一律去掉（`\frac {a} {b}` 与 `\frac{a}{b}` 是同一个公式）。"""
    return re.sub(r"\s+", "", str(value or ""))


def looks_like_formula(text: str) -> bool:
    """这段探针是**公式**还是正文句子。"""
    probe = str(text or "").strip()
    if not probe or _CJK_RE.search(probe):
        return False
    return "$" in probe or "\\" in probe


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
        best: tuple[int, tuple[int, int]] | None = None
        for start, end, latex in spans:
            hay = _norm_latex(latex)
            covered = min(len(want), len(hay))
            # 两边都够长才算"同一式子"的一部分：一方只有一两个字符就是巧合
            if covered < 8:
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


def near_misses(text: str, probe: str, *, limit: int = 3, width: int = 60) -> list[str]:
    """找不到时给模型几句"最像的原文"，让它照着改一版（与产物纠错工具同一套路）。"""
    probe = (probe or "").strip()
    if not probe or not text:
        return []
    head = probe[:8]
    out: list[str] = []
    at = text.find(head)
    if at < 0:
        # 头 8 个字也不在：退化成"按最长的公共片段找一行"
        best: tuple[int, int] = (0, -1)
        for line in text.split("\n"):
            common = 0
            for a, b in zip(line.strip(), probe):
                if a != b:
                    break
                common += 1
            if common > best[0]:
                best = (common, text.find(line.strip()[:20]))
        at = best[1] if best[1] >= 0 else -1
    if at < 0:
        return []
    out.append(text[max(0, at - 10):at + width].strip())
    return out[:limit]


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
    span = find_span(render_like_dom(md), text, occurrence=occurrence)
    if span is not None:
        return span, False, ""
    hit = find_formula_span(md, text)
    if hit is not None:
        return (hit[0], hit[1]), True, hit[2]
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
    side = normalize_side(side)
    if not side:
        side = side_of_text(text)
    # 公式**一律先落译文面**，即便调用方显式给了 `en`：用户读的是中文那一列，标在原文面的
    # 高亮根本画不出来。实测（2026-12-06）agent 每次都显式传 `side=en`，8 条公式高亮全落在
    # 英文列 —— 只做"没给时才优先中文"挡不住它。译文面确实没有这条公式时，下面的换面
    # 兜底会退回原文面。
    if is_formula_probe:
        side = "zh"
    kind = anchor_kind(anchor)
    if kind == "pdf":
        return {"ok": False, "error": "原 PDF 那面的文字层只存在于阅读器里，服务端算不出偏移量："
                                      "请让用户在 PDF 页上自己划词高亮（或者改标正文行 / Markdown 视图）。"}
    if kind == "markdown":
        # Markdown 锚点的文字能在**正文行**里找到 → 落在行上（用户默认读的就是那一面，
        # 高亮才看得见；见 `locate_in_rows`）。找不到才留在 Markdown 面。
        hit = locate_in_rows(paper_id, anchor_page_no(anchor), text,
                             side=side, occurrence=occurrence)
        if hit:
            log.info("高亮锚点从 Markdown 挪到正文行（用户读的是对照 / 译文）：%s → %s",
                     anchor, hit[0])
            anchor, kind = hit[0], "row"
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
    span = None
    is_formula = False
    formula_src = ""
    for candidate in order:
        cand_rendered, cand_hit = _locate(candidate)
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
        return {"ok": False, "error": f"找不到这个锚点对应的文字：{anchor}（行号可能在重译后变了）"}
    if span is None:
        near = near_misses(rendered, text)
        return {"ok": False,
                "error": f"这段文字在 {anchor} 里找不到（要逐字一致，Markdown 记号不算字）。",
                "near_miss": near}
    start, end = span
    # 公式那条存 **LaTeX 原文**而不是占位符：前端画标记时"选区含 ATOM 就跳过文字校验"
    # （`markRanges`），所以不影响定位；而列表 / 笔记里显示一个隐形的 U+FFFC 毫无意义。
    stored_text = (formula_src or str(text or "").strip()) if is_formula else rendered[start:end]
    created = svc.create_highlight({
        "paper_id": paper_id, "block_id": anchor,
        "selected_text": stored_text,   # 文字高亮存坐标系里那一段；公式存 LaTeX
        "start_offset": start, "end_offset": end,
        "color": color, "side": side, "scope": kind,
    })
    created["page"] = svc.anchor_page(anchor)
    log.info("agent 建高亮 %s %s[%d:%d] %r", paper_id, anchor, start, end, rendered[start:end][:30])
    return {"ok": True, "highlight": created, "page": created["page"], "scope": kind}


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
