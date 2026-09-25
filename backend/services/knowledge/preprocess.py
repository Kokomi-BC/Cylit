"""知识库索引层预处理：**只服务知识库**，不动解析产物、不动阅读器视图。

为什么单独一层（而不是改 parser）：
- 阅读器读的是页级产物/IR，作者、单位、脚注、Markdown 表现格式在**阅读视图里有意义** ——
  为了知识库干净就去改 parser，等于把阅读器也一起改了。
- 反过来，这一层生效只要**重建索引**（本地 embedding），不用重新解析（视觉模型）。

规则都是确定性纯函数，便于离线单测；四条边界写死在代码里：
1. **公式（``$…$`` / ``$$…$$``）一字不改** —— 清洗只在数学片段之外进行；
2. **引用编号保留** —— ``[13](url)`` 只去掉 URL，留 ``[13]``；
3. **摘要/正文段落有保护条款** —— 长段落 + 句末标点且不命中强特征时永不丢弃；
4. **宁可不合并、不可丢内容** —— 判定不了的一律保留。
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# 数学片段：清洗的"禁区"
# --------------------------------------------------------------------------
# 行间公式 `$$…$$` 可以跨行；行内 `$…$` 不跨行（跨行多半是排版噪声）。
_MATH_SPAN = re.compile(r"\$\$.+?\$\$|(?<!\$)\$(?!\$)[^$\n]*\$(?!\$)", re.DOTALL)

# --------------------------------------------------------------------------
# Markdown 表现格式
# --------------------------------------------------------------------------
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 引用链接 `[13](https://…)` / `[13, 14](…)` → 只留 `[13]`（用户口径：保留引用编号）
_CITE_LINK = re.compile(r"\[(\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDER = re.compile(r"__(.+?)__", re.DOTALL)
_HEADING_MARK = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*")
# 单个 `*` / `_` 不动：会误伤 `x_1`、乘法号、脚注 `*`（宁可留一点格式符）

_NUMERIC_ROW = re.compile(r"^[\d\s.,%·×xX+\-–—*/\\()\[\]:;=<>≤≥|]+$")
_WORD3 = re.compile(r"[A-Za-z\u4e00-\u9fff]{3,}")

# --------------------------------------------------------------------------
# 前沿元数据
# --------------------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
_ARXIV = re.compile(r"arXiv:\s*\d{4}\.\d{4,5}", re.I)
_URL_ONLY = re.compile(r"^\s*https?://\S+\s*$")
_FOOTNOTE_PREFIX = re.compile(
    r"^\s*(?:[*†‡§¶]|equal contribution|work performed|corresponding author|"
    r"under review|preprint\b|to appear in)", re.I)
_VENUE = re.compile(
    r"(?:conference on|proceedings of|neurips|\bnips\b|iclr|icml|cvpr|emnlp|"
    r"\bacl\b|workshop on|long beach|annual meeting)", re.I)
_INSTITUTION = re.compile(
    r"(?:universit|research|institut|laborator|\blabs?\b|inc\.|college|academy|"
    r"school of|google|microsoft|meta\b|deepmind|openai|tencent|alibaba|bytedance|"
    r"tsinghua|peking|nvidia|amazon|apple)", re.I)
_SENT_END = re.compile(r"[.!?。！？]\s*$")
_ABSTRACT_HEAD = re.compile(r"^\s*(?:abstract|摘要)\s*[:：]?\s*$", re.I)
_SECTION_NUM_HEAD = re.compile(r"^\s*\d+(?:\.\d+)*\s*[A-Z\u4e00-\u9fff]")


def strip_presentation(text: str) -> str:
    """去掉 Markdown 表现格式，**公式与引用编号原样保留**。"""
    src = text or ""
    if not src:
        return ""
    out: list[str] = []
    pos = 0
    for m in _MATH_SPAN.finditer(src):
        if m.start() > pos:
            out.append(_clean_plain(src[pos:m.start()]))
        out.append(m.group(0))              # 数学片段：一字不改
        pos = m.end()
    if pos < len(src):
        out.append(_clean_plain(src[pos:]))
    return "".join(out).strip()


def _clean_plain(part: str) -> str:
    if not part:
        return part
    s = _IMAGE.sub("", part)
    s = _CITE_LINK.sub(r"[\1]", s)          # `[13](url)` → `[13]`
    s = _LINK.sub(r"\1", s)                 # `[text](url)` → `text`
    s = _BOLD.sub(r"\1", s)
    s = _BOLD_UNDER.sub(r"\1", s)
    s = _HEADING_MARK.sub("", s)            # 行首 `##` 去掉，标题文字保留
    return s


def heading_level(text: str) -> int:
    """从 Markdown 标题符推层级（`##`→2）；不是标题返回 0。"""
    m = re.match(r"\s*(#{1,6})", text or "")
    return len(m.group(1)) if m else 0


def is_table_number_heading(text: str) -> bool:
    """表格里的数字行被误判成 heading 的兜底判据。

    实测：turboquant 38 个 heading 里 12 个是 `1645.2945.16`，attention 48 个里
    23 个是 `4128128 5.0025.5` / `## GNMT + RL [38]`。这些"标题"每命中一次就 flush
    一次正文 buffer，把正文切碎，还把表格数字塞进正文 chunk。
    判据与 parser 侧 `classify_text` 保持同一口径：**没有 3 个连续字母/汉字**的行不是标题。
    """
    t = strip_presentation(text or "").strip()
    if not t:
        return True
    if _NUMERIC_ROW.match(t):
        return True
    return not _WORD3.search(t)


def _norm_alpha(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def _author_names(authors: str) -> list[str]:
    if not authors:
        return []
    parts = re.split(r"[;,、]| and |\band\b", authors)
    return [n for n in (_norm_alpha(p) for p in parts) if len(n) >= 6]


def is_author_block(text: str, authors: str) -> bool:
    """作者名单块：短、不以句末标点结尾、命中作者名。

    只用**完整姓名**匹配（`Ashish Vaswani`），所以正文里"Vaswani et al. (2017)"
    这种姓氏引用不会命中 —— 这是这条规则能放心用的前提。
    实测必要性：attention 把最后一个作者 `Illia Polosukhin` 单独排成一块，
    只按"命中 ≥2 个作者"的判据会漏掉它，于是作者名混进了摘要 chunk。
    """
    t = (text or "").strip()
    if not t or len(t) > 300 or _SENT_END.search(t):
        return False
    names = _author_names(authors)
    if not names:
        return False
    blob = _norm_alpha(t)
    hit = sum(1 for n in names if n in blob)
    if hit == 0:
        return False
    if hit >= 2 or hit / len(names) >= 0.6:
        return True
    # 单个作者名单独成块（很短、词数很少）也是作者行
    return len(t.split()) <= 4 and len(t) <= 60


def is_front_matter(text: str, *, page_no: int, before_body: bool,
                    authors: str = "") -> bool:
    """摘要/正文起点之前的噪声块（作者、单位、邮箱、arXiv 章、会议行、脚注）。

    只在**前两页**生效；两类判据：
    - 强特征（邮箱 / arXiv 号 / 纯 URL / 脚注前缀 / 会议行）不限定位置（仍限前两页）；
    - 弱特征（机构名、作者名单）只在 ``before_body`` 时生效。
    """
    t = strip_presentation(text or "").strip()
    if not t or page_no > 2:
        return False

    if _EMAIL.search(t) or _ARXIV.search(t) or _URL_ONLY.match(t):
        return True
    if _FOOTNOTE_PREFIX.match(t):
        return True
    if len(t) <= 200 and _VENUE.search(t[:80]):
        return True

    # 保护条款：像正文的段落（够长 + 句末标点）不因为"在正文起点之前"被误杀。
    # 实测必要性：attention 的摘要段落**没有 `## Abstract` 标题**，它就在正文起点之前。
    if len(t) >= 200 and _SENT_END.search(t):
        return False

    if not before_body:
        return False
    if len(t) <= 260 and _INSTITUTION.search(t):
        return True
    return is_author_block(t, authors)


def first_body_index(rows: list[dict]) -> int:
    """正文起点：第一个 `Abstract/摘要` 标题或编号大节标题的下标（找不到返回 0）。"""
    for i, r in enumerate(rows):
        if r.get("type") != "heading":
            continue
        t = strip_presentation(r.get("text") or "").strip()
        if _ABSTRACT_HEAD.match(t) or _SECTION_NUM_HEAD.match(t):
            return i
    return 0


def attached_caption_ids(rows: list[dict], caption_block_ids: set[str]) -> set[str]:
    """已被 figure/table 吸收的 caption 块 id（只认 ``type == "caption"`` 的块）。

    为什么限定类型：attention 的 Table 2 把 `caption_block_id` 指到了一段**正文段落**
    （"Table 2 summarizes our results…"）—— 那是在说"表 2 汇总了…"，不是表注。
    按 id 一刀切会把这段正文从知识库里删掉。
    """
    out: set[str] = set()
    for r in rows:
        if r.get("type") == "caption" and r.get("id") in caption_block_ids:
            out.add(str(r["id"]))
    return out


# --------------------------------------------------------------------------
# 长块按句子边界二次切分（保证 ≤ hard，且不切公式）
# --------------------------------------------------------------------------
_STRONG_END = "。！？；"
_SOFT_END = "，,、）)】]}>”\"'"


def _math_spans(s: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _MATH_SPAN.finditer(s)]


def _in_math(spans: list[tuple[int, int]], pos: int) -> bool:
    return any(a < pos < b for a, b in spans)


def _best_cut(s: str, hard: int, spans: list[tuple[int, int]]) -> int:
    """在 ``hard`` 之前找最靠后的切点：句末 > 软边界 > 硬切；都不落在公式内部。"""
    head = s[:hard]
    best = 0
    for m in re.finditer(r"[。！？；]", head):
        if not _in_math(spans, m.end()):
            best = m.end()
    if best:
        return best
    for m in re.finditer(r"[.!?;](?=\s|$)", head):
        if not _in_math(spans, m.end()):
            best = m.end()
    if best:
        return best
    if "\n" in head:
        idx = head.rfind("\n")
        if idx > 0 and not _in_math(spans, idx):
            return idx
    for i in range(len(head), 0, -1):
        if head[i - 1] in _SOFT_END and not _in_math(spans, i):
            return i
    if not _in_math(spans, hard):
        return hard
    # 切点正落在公式内部 → 退到该公式起点（公式整体保留在下一段）
    for a, b in spans:
        if a < hard < b:
            return a if a > 0 else b
    return hard


def split_long_text(text: str, hard: int) -> list[str]:
    """把一个超长正文块切成若干 ≤ ``hard`` 的片段（切点优先句子边界）。

    例外（写进文档、也写进测试）：一个**单独**的数学片段自身超过 ``hard`` 时，
    该片段整体保留（公式不可拆，优先级高于长度上限）。
    """
    src = (text or "").strip()
    if not src or hard <= 0 or len(src) <= hard:
        return [src] if src else []
    out: list[str] = []
    rest = src
    while len(rest) > hard:
        spans = _math_spans(rest)
        cut = _best_cut(rest, hard, spans)
        if cut <= 0:
            cut = hard
        piece, rest = rest[:cut].strip(), rest[cut:].lstrip()
        if piece:
            out.append(piece)
    if rest.strip():
        out.append(rest.strip())
    return out
