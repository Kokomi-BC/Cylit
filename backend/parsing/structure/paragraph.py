"""段落切分与页眉页脚剔除（规划 §13.2）。

**重要**：PyMuPDF 的 ``get_text("dict")`` 文本块边界与语义段落**并不一致**
（一张页面往往被切成十几个窄块，段落被切碎、跨栏文本还会混进同一个块）。
因此本模块直接在**行序列**上工作：

1. 用页面栏结构 + 行几何排序得到阅读顺序的行序列（``reading_order`` 负责排序）；
2. ``is_body_line`` 过滤图内文字、页码、公式编号等非正文行；
3. ``group_lines_into_paragraphs`` 按「行距 + 字体一致 + 句末标点 + 缩进」规则成段；
4. ``classify_text`` 判定 heading / caption / ref / para（**严格**，宁可漏判标题）。

非正文行不丢弃：带上标记返回，供插图/表格区域吸收或作为审计信息。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from backend.core.utils import ends_sentence, is_cjk_text, join_lines, normalize_text
from backend.parsing.models import (
    Block, RawBlock, Rect, TextLine, is_greek, is_math_operator,
)

# 段间行距阈值：行距 > line_gap_ratio × 字号 视为段落边界
LINE_GAP_RATIO = 0.62
# 页眉页脚带高度比例
HEADER_BAND_RATIO = 0.055
FOOTER_BAND_RATIO = 0.055
# 重复文本阈值（跨页出现次数）
REPEAT_MIN_PAGES = 3
# 正文行最小字符数（低于此值且独立成行 → 可能是页码/公式编号/图内文字）
MIN_BODY_LINE_CHARS = 3


@dataclass
class PageMargins:
    header_band: float
    footer_band: float


@dataclass
class HeaderFooter:
    """跨页重复文本检测结果。"""

    repeated_texts: Counter = field(default_factory=Counter)

    def is_boilerplate(self, text: str) -> bool:
        t = normalize_text(text)
        if not t:
            return False
        return self.repeated_texts.get(t, 0) >= REPEAT_MIN_PAGES \
            or self.repeated_texts.get(_boilerplate_key(t), 0) >= REPEAT_MIN_PAGES


def compute_margins(page_height: float) -> PageMargins:
    return PageMargins(
        header_band=page_height * HEADER_BAND_RATIO,
        footer_band=page_height * (1 - FOOTER_BAND_RATIO),
    )


_DIGIT_TRANS = str.maketrans("0123456789", "##########")


def _boilerplate_key(text: str) -> str:
    t = normalize_text(text)
    if not t or len(t) > 120:
        return ""
    return t.translate(_DIGIT_TRANS).lower()


def detect_boilerplate(page_blocks: dict[int, list[RawBlock]],
                       page_heights: dict[int, float]) -> HeaderFooter:
    """收集候选页眉/页脚文本：位于顶部/底部条带内的独立短行。"""
    counter: Counter = Counter()
    for page_no, blocks in page_blocks.items():
        h = page_heights.get(page_no, 792.0)
        m = compute_margins(h)
        for ab in blocks:
            for ln in ab.lines:
                r = ln.rect
                if r.height > h * 0.12:
                    continue
                if not (r.y1 <= m.header_band + 2 or r.y0 >= m.footer_band - 2):
                    continue
                key = _boilerplate_key(ln.text)
                if key:
                    counter[key] += 1
    return HeaderFooter(repeated_texts=counter)


def is_in_margin_band(line: TextLine, page_height: float) -> tuple[bool, bool]:
    m = compute_margins(page_height)
    r = line.rect
    return (r.y1 <= m.header_band + 2, r.y0 >= m.footer_band - 2)


# --------------------------------------------------------------------------
# 正文行判定
# --------------------------------------------------------------------------

_PAGE_NUMBER = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*$", re.IGNORECASE)
_EQ_NUMBER = re.compile(r"^\s*\(\s*\d{1,3}[a-z]?\s*\)\s*$")
_FOOTNOTE = re.compile(r"^\s*[∗*†‡§¶]\s*\S")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


@dataclass
class LineProfile:
    """页面的行统计剖面（用于判定正文字号 / 栏宽 / 行距基线）。"""

    body_size: float = 10.0
    text_left: float = 0.0
    text_right: float = 0.0
    page_width: float = 612.0
    page_height: float = 792.0
    blank_left: float = 0.0
    blank_right: float = 0.0
    blank_top: float = 0.0
    blank_bottom: float = 0.0
    median_gap: float = 1.0      # 页内相邻行的典型行距
    median_lead: float = 11.0    # 页内典型行距（baseline → baseline）

    @property
    def body_width(self) -> float:
        return max(0.0, self.text_right - self.text_left)

    @property
    def para_gap_threshold(self) -> float:
        """段落分界行距阈值（gap 口径）。"""
        return max(self.median_gap * 1.8, 0.45 * max(self.body_size, 6.0))

    @property
    def para_lead_threshold(self) -> float:
        """段落分界用 **lead**（baseline→baseline）口径的阈值。

        相对正文中位行距的倍数，并限制在 [1.35, 3.0] 倍字号之间。
        """
        bs = max(self.body_size, 6.0)
        return min(max(self.median_lead * 1.45, 1.05 * bs), 3.0 * bs)


def build_line_profile(lines: list[TextLine], page_width: float, page_height: float
                       ) -> LineProfile:
    weights: Counter = Counter()
    sizes_for_weight: list[tuple[float, int]] = []
    lefts: list[float] = []
    rights: list[float] = []
    for ln in lines:
        n = len(ln.text.strip())
        if n < 25:
            continue
        weights[round(ln.size, 1)] += n
        sizes_for_weight.append((ln.size, n))
        lefts.append(ln.rect.x0)
        rights.append(ln.rect.x1)
    if not weights:
        for ln in lines:
            n = len(ln.text.strip())
            if n < 8:
                continue
            weights[round(ln.size, 1)] += n
            lefts.append(ln.rect.x0)
            rights.append(ln.rect.x1)
    if not weights:
        for ln in lines:
            weights[round(ln.size, 1)] += max(1, len(ln.text.strip()))
            lefts.append(ln.rect.x0)
            rights.append(ln.rect.x1)
    prof = LineProfile(page_width=page_width, page_height=page_height)
    prof.body_size = weights.most_common(1)[0][0] if weights else 10.0
    # 行距基线：只用"正文字号"的行，避免标题/图注/公式行污染统计。
    # 判据用 **baseline→baseline 的 lead**（比 gap 稳定：inline 公式的上下标
    # 会让行 bbox 变高，导致 gap 出现负值，无法作为段落判据）。
    body_lines = [ln for ln in lines
                  if abs(ln.size - prof.body_size) <= 0.6 and len(ln.text.strip()) >= 25]
    body_lines.sort(key=lambda ln: (round(ln.rect.x0, 1), ln.rect.y0))
    leads: list[float] = []
    for a, b in zip(body_lines, body_lines[1:]):
        if abs(a.rect.x0 - b.rect.x0) > 24:      # 不同栏/不同块，跳过
            continue
        lead = b.rect.y0 - a.rect.y0
        # 只收"单倍行距"样本，排除段间距与跨栏跳变
        if 0.6 * prof.body_size < lead <= min(1.8 * prof.body_size, 30.0):
            leads.append(round(lead, 1))
    if leads:
        leads.sort()
        prof.median_lead = leads[len(leads) // 2]
        prof.median_gap = round(prof.median_lead - prof.body_size * 1.0, 2)
    if lefts:
        lefts.sort()
        rights.sort()
        idx = max(0, int(len(lefts) * 0.02))
        prof.text_left = lefts[idx]
        prof.text_right = rights[min(len(rights) - 1, int(len(rights) * 0.98))]
    prof.blank_left = prof.text_left
    prof.blank_right = page_width - prof.text_right
    return prof


def is_body_line(line: TextLine, prof: LineProfile) -> tuple[bool, str]:
    """判断是否为正文行（可参与段落组装）。返回 ``(是否正文, 原因)``。"""
    text = line.text.strip()
    if not text:
        return False, "empty"
    if _PAGE_NUMBER.match(text):
        return False, "page_number"
    if _EQ_NUMBER.match(text):
        return False, "equation_number"
    if _EMAIL.search(text) and len(text) < 60:
        return False, "email"
    # 图内文字 / 极小字号的短行
    if line.size and line.size < prof.body_size * 0.72 and len(text) < 40:
        return False, "small_font"
    # 极窄行（宽度 < 正文宽度 12%）且字数少 → 图内标签或竖排坐标轴
    if prof.body_width > 0 and line.rect.width < prof.body_width * 0.12 \
            and len(text) < 25:
        return False, "narrow"
    if _FOOTNOTE.match(text):
        return False, "footnote"
    return True, "body"


def _line_gap(prev: TextLine, cur: TextLine) -> float:
    return cur.rect.y0 - prev.rect.y1


def _same_style(a: TextLine, b: TextLine) -> bool:
    """字体/字号是否一致。

    注意：**不能比较字体集合**——同一段落里公式 span 用数学字体、正文用正文
    字体是常态，比较集合会把段落切碎。改为比较"主导字体与主导字号"。
    """
    if abs(a.size - b.size) > 0.6:
        return False
    return _dominant_font(a) == _dominant_font(b)


def _dominant_font(line: TextLine) -> str:
    fonts = [(s.font or "").lower() for s in line.spans if s.text.strip()]
    if not fonts:
        return ""
    return max(set(fonts), key=fonts.count)


def _is_indented(line: TextLine, left: float, body_width: float) -> bool:
    if body_width <= 0:
        return False
    return (line.rect.x0 - left) > 0.018 * body_width


def _line_math_ratio(line: TextLine) -> float:
    chars = [c for c in line.text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if is_greek(c) or is_math_operator(c)) / len(chars)


def group_lines_into_paragraphs(
    lines: list[TextLine],
    prof: LineProfile,
    *,
    start_indent_ratio: float = 0.018,
) -> list[list[TextLine]]:
    """把**已按阅读顺序排好的**正文行序列组装成段落。

    断裂条件（任一成立即新建段）：
    - 行距 > ``LINE_GAP_RATIO`` × 字号；
    - 字体 / 字号变化；
    - 前一行以句末标点结尾 **且** 当前行有首行缩进；
    - 当前行 x 起点明显左退（跨栏/跨块回退）；
    - 前一行是"公式型"短行而后一行是普通行（公式独占一段）。
    """
    body_lines = [ln for ln in lines if ln.text.strip()]
    if not body_lines:
        return []
    groups: list[list[TextLine]] = [[body_lines[0]]]
    for prev, cur in zip(body_lines, body_lines[1:]):
        size = max(cur.size, prev.size, 1.0)
        gap = _line_gap(prev, cur)
        lead = cur.rect.y0 - prev.rect.y0
        prev_text = prev.text.strip()
        new_para = False

        if gap < -size * 0.5 and lead < 0:
            new_para = True                       # 行倒序：换块/换栏
        elif prev.rect.x0 - cur.rect.x0 > 0.06 * max(1.0, prof.body_width):
            new_para = True                       # 起点明显左退：新块
        elif lead >= prof.para_lead_threshold:
            new_para = True                       # 行距显著大于单倍行距 → 换段
        elif gap > 0.30 * size and ends_sentence(prev_text) \
                and not is_cjk_text(prev_text):
            new_para = True                       # 行距偏高 + 上一行收句
        elif _is_display_formula_break(prev, cur, prof):
            new_para = True                       # 独立展示公式与正文分开
        elif ends_sentence(prev_text) and _is_indented(
                cur, prof.text_left, prof.body_width) and not is_cjk_text(prev_text):
            new_para = True                       # 首行缩进

        if new_para:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    return [g for g in groups if normalize_text("".join(l.text for l in g))]


def _is_display_formula_break(prev: TextLine, cur: TextLine, prof: LineProfile) -> bool:
    """判断 ``prev → cur`` 是否是「独立展示公式 → 正文」的边界。

    只有当"下一行明显是正文"（长行）且"上一行是短数学行"且"lead 偏大"时才成立；
    否则（正文中夹一个数学符号）不切分，避免把段落切碎。
    """
    prev_text = prev.text.strip()
    cur_text = cur.text.strip()
    if _line_math_ratio(prev) <= 0.18 or len(prev_text) >= 80:
        return False
    if _line_math_ratio(cur) >= 0.10:
        return False
    if ends_sentence(prev_text):
        return False
    if len(cur_text) < 45:
        return False
    return (cur.rect.y0 - prev.rect.y0) > prof.median_lead * 1.2


def segment_paragraphs(block: RawBlock, body_left: float, body_width: float
                       ) -> list[list[TextLine]]:
    """兼容接口：对单个原始块的行走旧的分段逻辑（保留给工具脚本使用）。"""
    prof = LineProfile(text_left=body_left, text_right=body_left + body_width)
    return group_lines_into_paragraphs(
        [ln for ln in block.lines if ln.text.strip()], prof)


# --------------------------------------------------------------------------
# 类型判别（heading / caption / ref / para）
# --------------------------------------------------------------------------

_HEADING_NUM = re.compile(
    r"^\s*(?P<num>\d{1,2}(?:\.\d{1,2}){0,3}\.?)\s*(?P<title>\S.*)$"
)
_HEADING_WORDS = re.compile(
    r"^\s*(?:Abstract|Introduction|Related Works?|Background|Preliminaries|"
    r"Problem Definition|Method(?:s|ology)?|Approach|Our Approach|Experiments?|"
    r"Evaluation|Results?|Discussion|Conclusion(?:s)?|References|"
    r"Acknowledg(?:e)?ments?|Appendix|Limitations?|Reproducibility Statement|"
    r"Ethics Statement)\b\s*:?\s*$",
    re.IGNORECASE,
)
_CAPTION_START = re.compile(
    r"^\s*(?:Figure|Fig\.?|Table|Algorithm|Listing|Equation|Eq\.?)\s*\d+",
    re.IGNORECASE)
_REF_LINE = re.compile(r"^\s*\[\d{1,3}\]")
_TITLELIKE_MAX_CHARS = 110

# --------------------------------------------------------------------------
# 参考文献区 / 附录 的判定谓词（pipeline 的状态机与这里共用同一口径）
# --------------------------------------------------------------------------
# 明确写成 Appendix / Supplementary 的标题（用户口径：只有这两类，或 A/B/C… 这种单字母大节，
# 才算"进入附录"；Lemma / Proof / Implementation 单独出现**不算**）
_APPENDIX_EXPLICIT = re.compile(
    r"(?:\bappendix\b|\bsupplementar(?:y|ies)\b|\bsupplement\b|附录|补充材料)", re.I)
# 单字母大节编号：`A EXTENDED PRIOR WORK`、`B.1 SHANNON LOWER BOUND`、`C.1TURBOQUANT`
_APPENDIX_LETTER = re.compile(r"^\s*([A-H])(?:\.\d+)*[\s.．:]*[A-Z\u4e00-\u9fff]")
# 常规编号大节：`1Introduction`、`3 Model Architecture`、`2.1 EMPIRICAL VALIDATION`
_SECTION_NUM_HEAD = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2})*[\s.．:]*[A-Z\u4e00-\u9fff]")
# 文献条目特征：编号、年份、出处/URL
_BIB_NUMBERED = re.compile(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[.)])\s")
_BIB_YEAR = re.compile(r"(?:19|20)\d{2}")
_BIB_URL = re.compile(r"https?://|\bdoi\b|arxiv:\s*\d{4}\.\d{4,5}", re.I)
_BIB_HINTS = ("arxiv", "proceedings", "conference", "journal", "workshop", "symposium",
              "vol.", "pp.", "et al", "press", "springer",
              "acm", "ieee", "corr", "advances in", "transactions on", "university")
# 行首的 Markdown 标题符：IR 里的 text_clean 没有它，但 md 正文（知识库读的那份）有
_HEAD_MARK = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*")
# 正文（尤其是附录里的 Lemma / Proof）的行首特征：这些块**不能被改写成 ref**
_PROSE_START = re.compile(
    r"^\s*(?:lemma|theorem|proof|corollary|proposition|claim|definition|remark|"
    r"assumption|axiom|observation|for |given |let |we |thus|hence|note that|since |"
    r"first,|second,|finally,|however|moreover|therefore|in this|as |to |the |this )",
    re.I)


def _plain(text: str) -> str:
    """规范化 + 去掉行首 Markdown 标题符（IR 里没有、md 正文里有）。"""
    return _HEAD_MARK.sub("", normalize_text(text))


def looks_like_reference_entry(text: str) -> bool:
    """看起来像**文献条目**（而不是正文段落）。

    为什么需要：turboquant 的参考文献是 author-year 无编号（实测 217 条 ref 里 0 条以 `[n]` 开头），
    不能只认编号；但反过来也不能"在参考文献区里就当文献"，否则附录 A–E 的 Lemma/证明会被一起丢掉。
    """
    t = _plain(text)
    if not t:
        return False
    if _BIB_NUMBERED.match(t):
        return True
    low = t.lower()
    if _BIB_YEAR.search(t) or _BIB_URL.search(t):
        return True
    return sum(1 for hint in _BIB_HINTS if hint in low) >= 2


def is_prose_like(text: str) -> bool:
    """像正文段落（尤其是附录里的 Lemma / 证明 / 实现说明）—— 用来**兜住**误判。

    判据刻意保守：以数学结构词或句子开头的常见词起头，并且有足够长度。
    文献条目几乎不会以 `Lemma` / `For a random variable` 起头，所以不会把文献放进来。
    """
    t = _plain(text)
    return bool(t) and len(t) >= 60 and bool(_PROSE_START.match(t))


def is_appendix_heading(text: str, *, after_refs: bool = False) -> bool:
    """是否"进入附录"的标题。

    用户口径：**只有**明确 Appendix / Supplementary，或 `A` / `A.1` 这种单字母大节才触发；
    `Lemma` / `Proof` / `Theorem` / `Implementation` 单独出现**不触发**（它们属于所在章节）。
    单字母形式只在**参考文献之后**才算（否则 `ABSTRACT` 这种全大写词会被误判成 "A BSTRACT"）。
    """
    t = _plain(text)
    if not t:
        return False
    if _APPENDIX_EXPLICIT.search(t):
        return True
    if not after_refs:
        return False
    m = _APPENDIX_LETTER.match(t)
    if not m:
        return False
    rest = t[m.end(1):].lstrip(".．: ")
    return len(rest) >= 3


def is_section_exit_heading(text: str) -> bool:
    """常规编号大节标题（`3 Model Architecture`）—— 出现在参考文献区之后说明该区已结束。"""
    return bool(_SECTION_NUM_HEAD.match(_plain(text)))


def is_number_only_heading(text: str) -> bool:
    """表格里的数字行（`1645.2945.16`、`4128128 5.0025.5`）—— 不是标题。

    实测：turboquant 38 个 heading 里 12 个是这种，attention 48 个里 23 个
    （`## GNMT + RL [38]`）；它们每命中一次就 flush 一次正文 buffer，把正文切碎。
    判据与索引层 `preprocess.is_table_number_heading` 保持一致。
    """
    t = _plain(text)
    if not t:
        return True
    return not re.search(r"[A-Za-z\u4e00-\u9fff]{3,}", t)


def classify_text(
    text: str,
    *,
    font_size: float,
    body_size: float,
    is_bold: bool,
    line_count: int,
    char_count: int,
    page_width: float,
    rect_width: float,
    line_math_ratio: float = 0.0,
    in_ref_section: bool = False,
) -> str:
    """判定块的类型（规划 §10 ② 的 type 取值）。

    严格规则（避免把正文误判为标题）：
    - ``ref``：在参考文献区，或以 ``[n]`` 开头
    - ``caption``：以 Figure/Table/Algorithm + 编号开头
    - ``heading``：**必须**满足「编号编号（1 / 1.1）+ 短 + 不以句号结尾」
      或「全大写短行」或「字号明显更大且短且不以句末标点结尾」
    - 其余一律 ``para``
    """
    t = normalize_text(text)
    if not t:
        return "para"

    if _CAPTION_START.match(t) and line_count <= 12:
        return "caption"
    if in_ref_section:
        # 参考文献区里也只有**像文献条目**的块才是 ref：附录里的 Lemma / 证明
        # （`For a random variable x we denote…`）不能因为"在参考文献区之后"就被丢掉。
        return "ref" if not is_prose_like(t) else "para"
    if _REF_LINE.match(t) and line_count <= 8:
        return "ref"

    # 公式密集合成行不判标题
    if line_math_ratio > 0.25:
        return "para"

    # 表格里的数字行不是标题（`1645.2945.16` / `4128128 5.0025.5`）：
    # 判成标题会每来一行就 flush 一次正文 buffer，把正文切碎、还把表格数字混进正文。
    if is_number_only_heading(t):
        return "para"

    short = char_count <= _TITLELIKE_MAX_CHARS and line_count <= 3
    if not short:
        return "para"

    ends = ends_sentence(t)
    words = len(t.split())

    if _HEADING_WORDS.match(t):
        return "heading"

    m = _HEADING_NUM.match(t)
    if m and len(m.group("num")) <= 6 and words <= 14 and not ends:
        # "2" 之类的纯编号（无标题文字）不算 heading
        if len(m.group("title").strip()) >= 3:
            return "heading"
        return "para"

    big = font_size >= body_size + 1.2
    if big and not ends and words <= 16:
        return "heading"
    # 全大写短行（论文标题/ABSTRACT 等）
    if t.isupper() and len(t) >= 4 and words <= 16 and not ends:
        return "heading"
    if is_bold and font_size >= body_size + 0.4 and words <= 10 and not ends:
        return "heading"
    return "para"


# --------------------------------------------------------------------------
# 组装 Block
# --------------------------------------------------------------------------


def build_blocks_from_groups(
    *,
    paper_id: str,
    page_no: int,
    groups: list[tuple[list[TextLine], int]],
    body_size: float,
    page_width: float,
    page_height: float,
    start_order: int = 1,
    kinds: list[str] | None = None,
) -> list[Block]:
    """把 ``[(行组, 栏号)]`` 组装为 ``Block`` 列表。

    ``kinds`` 可由调用方（pipeline 的公式合并阶段）指定每组的类型；
    未指定时按几何与字体特征自动判别。
    """
    blocks: list[Block] = []
    order = start_order
    for idx, (seg, col) in enumerate(groups):
        text_raw = "\n".join(ln.text for ln in seg)
        text_clean = join_lines([ln.text for ln in seg])
        if len(normalize_text(text_clean)) < 2:
            continue
        rect = _seg_rect(seg)
        sizes = [ln.size for ln in seg if ln.size > 0]
        font_size = (max(set([round(s, 1) for s in sizes]), key=sizes.count)
                     if sizes else 0.0)
        fonts = [s.font for ln in seg for s in ln.spans if s.font]
        is_bold = any(s.is_bold for ln in seg for s in ln.spans)
        math_ratio = sum(_line_math_ratio(ln) for ln in seg) / max(1, len(seg))
        btype = (kinds[idx] if kinds and idx < len(kinds) else None) or classify_text(
            text_clean, font_size=font_size, body_size=body_size, is_bold=is_bold,
            line_count=len(seg), char_count=len(text_clean),
            page_width=page_width, rect_width=rect.width,
            line_math_ratio=math_ratio,
        )
        b = Block(
            id="", paper_id=paper_id, page_no=page_no, order_in_page=order,
            order_global=0, type=btype, col=max(col, 0), rect=rect,
            text=text_raw, text_clean=text_clean, source_text=text_raw,
            source_kind="text", font_names=sorted(set(fonts)),
            font_size=font_size, line_count=len(seg),
        )
        b.finalize()
        blocks.append(b)
        order += 1
    return blocks


def build_blocks_for_page(
    *,
    paper_id: str,
    page_no: int,
    raw_blocks: list[RawBlock],
    columns: list[int],
    body_left: float,
    body_width: float,
    body_size: float,
    page_width: float,
    page_height: float,
    boilerplate: HeaderFooter,
    in_ref_section: bool = False,
) -> tuple[list[Block], int]:
    """兼容旧接口：把一页的原始块切成 Block（按块内分段，不跨块合并）。"""
    blocks: list[Block] = []
    order = 1
    for ab, col in zip(raw_blocks, columns):
        if boilerplate.is_boilerplate(ab.text):
            header, footer = is_in_margin_band(ab.lines[0], page_height) if ab.lines \
                else (False, False)
            if header or footer:
                continue
        prof = LineProfile(text_left=body_left,
                           text_right=body_left + body_width,
                           body_size=body_size, page_width=page_width,
                           page_height=page_height)
        groups = group_lines_into_paragraphs(
            [ln for ln in ab.lines if is_body_line(ln, prof)[0]], prof)
        new_blocks = build_blocks_from_groups(
            paper_id=paper_id, page_no=page_no,
            groups=[(g, col) for g in groups],
            body_size=body_size, page_width=page_width, page_height=page_height,
            start_order=order,
        )
        blocks.extend(new_blocks)
        order += len(new_blocks)
    if in_ref_section:
        for b in blocks:
            if b.type == "para" and not is_prose_like(b.text_clean or b.text):
                b.type = "ref"
    return blocks, order


def _seg_rect(seg: list[TextLine]) -> Rect:
    r = Rect(1e9, 1e9, -1e9, -1e9)
    for ln in seg:
        lr = ln.rect
        r = Rect(min(r.x0, lr.x0), min(r.y0, lr.y0), max(r.x1, lr.x1), max(r.y1, lr.y1))
    if r.x0 > 1e8:
        return Rect(0, 0, 0, 0)
    return r


def estimate_body_style(page_blocks: list[RawBlock]) -> tuple[float, float, float]:
    """兼容接口：返回 ``(body_left, body_width, body_size)``。"""
    lines = [ln for ab in page_blocks for ln in ab.lines]
    prof = build_line_profile(lines, 612.0, 792.0)
    return prof.text_left, prof.body_width, prof.body_size
