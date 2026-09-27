"""论文元数据的**离线**抽取：标题 / 作者 / 会议或期刊 / 年份 / 摘要。

为什么单独成模块：过去只有 ``pipeline.extract_metadata`` 抽标题与作者，于是论文库里
「会议 / 期刊」「年份」「摘要」「作者」经常永远是空的，只能手打（用户口径：
*论文元数据要么从论文自动获取自动填写，要么模型翻译后与术语一同生成*）。
这里把"PDF 里读得出来"的部分一次读全，供**导入**、**解析**、**元数据补全**三个入口复用。

两条硬约束：
- **纯启发式、不调模型**：解析流水线里不能为了元数据额外付一次调用与延迟；
- **拿不准就留空**：宁可空着交给模型（或交给用户），也不要写错一个会议名／年份 ——
  空的字段上层还会去问模型，错的字段没人会去改。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from backend.core.logging import get_logger
from backend.core.utils import join_lines, normalize_space, normalize_text

log = get_logger(__name__)

ABSTRACT_MAX = 2000          # `paper.abstract` 是 String(2000)
VENUE_MAX = 128              # `paper.venue` 是 String(128)
AUTHORS_MAX = 1024           # `paper.authors` 是 String(1024)
NAME_LINE_MAX = 70           # 作者行长度上限：超过这个长度的行一定不是人名

# 摘要标题：允许全大写、字母间被排开（`A B S T R A C T`）与中文「摘要」
_ABSTRACT_HEAD = re.compile(
    r"^\s*(?:a\s*b\s*s\s*t\s*r\s*a\s*c\s*t|abstract|摘要)\s*[:.．:：]?\s*$", re.I)

# 章节标题（摘要的结束边界）：编号可省，词表覆盖论文里最常见的几节
_SECTION_HEAD = re.compile(
    r"^\s*(?:(?:\d+|[IVX]{1,4})\s*[.、]?\s*)?"
    r"(?:introduction|related\s+work|background|preliminar(?:y|ies)|"
    r"method(?:s|ology)?|approach|materials?\s+and\s+methods?|"
    r"experiments?|evaluation|results?|analysis|discussion|"
    r"conclusions?|references|bibliography|acknowledg(?:e)?ments?|"
    r"keywords|index\s+terms|ccs\s+concepts|appendix|摘要)\b", re.I)

# 会议/期刊：按"可信度"从高到低排列，命中靠前的就用它（宁可要 ICLR 2026，不要 arXiv 号）
_VENUE_RULES: list[tuple[str, re.Pattern[str]]] = [
    # "Published as a conference paper at ICLR 2026"（ICLR / NeurIPS 模板的固定抬头）
    ("published-at", re.compile(r"published as a conference paper at\s+(.{2,80})", re.I)),
    # "31st Conference on Neural Information Processing Systems (NIPS 2017), Long Beach, CA, USA."
    ("conf-paren", re.compile(r"conference on[^()]{3,90}\(([A-Za-z][A-Za-z&.\- ]{1,20}?\s*\d{4})\)", re.I)),
    # "In Proceedings of the 61st Annual Meeting of the ACL (ACL 2023)" / 无括号时退化为整段
    ("proceedings", re.compile(r"(?:in\s+)?proceedings of[^,;]{0,120}", re.I)),
    # 光秃秃的会议缩写 + 年份："ACL 2023" / "NeurIPS'21"
    ("acronym", re.compile(
        r"\b(?:ICLR|ICML|NeurIPS|NIPS|ACL|EMNLP|NAACL|COLING|CVPR|ICCV|ECCV|AAAI|IJCAI|"
        r"KDD|SIGIR|WWW|WSDM|CIKM|ICRA|ACMMM|TACL|TMLR|JMLR|TPAMI|UAI|AISTATS)\b"
        r"[\s'’\-]{0,2}\d{2,4}")),
    ("journal", re.compile(r"\b(?:IEEE|ACM)\s+(?:Transactions|Journal)[^,;.]{0,80}", re.I)),
    ("journal", re.compile(r"\bJournal of [A-Z][^,;.]{0,80}")),
    ("journal", re.compile(r"\b(?:Nature|Science)\s+[A-Z][^,;.]{0,60}")),
    # arXiv 只当"预印本"兜底，别把它当正式会议
    ("arxiv", re.compile(r"arXiv\s*:?\s*\d{4}\.\d{4,5}", re.I)),
]

# 会议缩写归一：老名字 → 现在通用的名字（用户认的是后者）
_VENUE_ALIASES = {"NIPS": "NeurIPS", "NIPS*": "NeurIPS"}

# 作者行里必须排除的东西：邮箱、机构、页眉页脚
_AFFIL_WORDS = re.compile(
    r"\b(?:universit(?:y|e|ies|ät|é)|institut(?:e|o|ion)?|department|dept\.?|school|college|"
    r"faculty|laborator(?:y|ies)|lab\b|centre?|center|academy|google|deepmind|brain|research|"
    r"microsoft|meta\b|openai|anthropic|apple|amazon|nvidia|bytedance|alibaba|tencent|baidu|"
    r"huawei|samsung|intel|ibm|adobe|salesforce|mit\b|stanford|berkeley|cmu\b|princeton|"
    r"harvard|oxford|cambridge|toronto|montreal|mila|eth\b|epfl|inria|cnrs|max\s+planck|"
    r"ai\b|inc\.?|ltd\.?|llc\b|corp\.?|gmbh|group|team|fellows?)\b", re.I)
_NOISE_LINE = re.compile(
    r"(?:@|https?://|www\.|doi|arxiv|issn|isbn|copyright|©|\(c\)|all rights reserved|"
    r"permission|preprint|under review|submitted|equal contribution|work performed)",
    re.I)

# 连字（ligature）：PDF 文本层常把 fi/fl 合成一个字符，摘要里出现率高，必须还原
_LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
              "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
_MD_MARKS = re.compile(r"^[#>\-*\s]+|[*_`\s]+$")


def _deligature(text: str) -> str:
    if not text:
        return ""
    for k, v in _LIGATURES.items():
        text = text.replace(k, v)
    return text


def _strip_md(text: str) -> str:
    """去掉 Markdown 装饰（`## Abstract` / `**Abstract**`）—— 视觉 OCR 的产物是 Markdown。"""
    return _MD_MARKS.sub("", _deligature(text or "").strip())


def _is_abstract_head(text: str) -> bool:
    return bool(_ABSTRACT_HEAD.match(_strip_md(text)))


@dataclass
class PaperMeta:
    """一篇 PDF 里能读出来的元数据；``sources`` 记录每个字段是怎么来的（便于解释与调试）。"""

    title: str = ""
    authors: str = ""
    venue: str = ""
    year: int | None = None
    abstract: str = ""
    sources: dict[str, str] = field(default_factory=dict)

    def fills(self) -> dict:
        """只返回**非空**字段（供"只填空字段"的写回逻辑使用）。"""
        out: dict = {}
        for k in ("title", "authors", "venue", "year", "abstract"):
            v = getattr(self, k)
            if v not in (None, "", 0):
                out[k] = v
        return out

    def to_dict(self) -> dict:
        return {**self.fills(), "sources": self.sources}


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def extract_paper_meta(doc, pdf_path: str | Path | None = None) -> PaperMeta:
    """从已打开的 PDF 抽元数据（**不抛异常**：抽不到就是空字段）。"""
    meta = PaperMeta()
    pdf_meta = dict(getattr(doc, "metadata", None) or {})
    try:
        meta.title, meta.authors = _title_authors(doc, pdf_meta, pdf_path)
        if meta.title:
            meta.sources["title"] = "pdf-meta" if _clean_title(pdf_meta.get("title")) else "first-page"
        if meta.authors:
            meta.sources["authors"] = ("pdf-meta" if normalize_space(pdf_meta.get("author") or "")
                                       else "first-page")
        # 第 1 页负责会议/年份/摘要（页眉、脚注、摘要都在这里）；找不到再看第 2 页
        first_text = _page_text(doc, 0)
        venue, year, year_src = _venue_and_year(first_text, pdf_meta)
        if venue:
            meta.venue = venue
            meta.sources["venue"] = "first-page"
        if year:
            meta.year = year
            meta.sources["year"] = year_src
        meta.abstract = _abstract_from_doc(doc)
        if meta.abstract:
            meta.sources["abstract"] = "first-page"
    except Exception as exc:  # noqa: BLE001 —— 元数据失败绝不能拖垮导入/解析
        log.warning("元数据抽取异常（忽略）：%s", exc)
    return meta


def meta_from_text(text: str, *, title: str = "") -> PaperMeta:
    """从**任意一段文本**抽元数据（扫描件的视觉 OCR 结果走这里）。

    扫描页没有文本层，``extract_paper_meta`` 只能抽出文件名当标题；页面被视觉模型
    读成 Markdown 之后，摘要/会议/年份依然能从这段文字里读出来。
    """
    meta = PaperMeta(title=title)
    text = text or ""
    venue, year, year_src = _venue_and_year(text, {})
    if venue:
        meta.venue = venue
        meta.sources["venue"] = "vision"
    if year:
        meta.year = year
        meta.sources["year"] = year_src
    abstract = _abstract_from_text(text)
    if abstract:
        meta.abstract = abstract
        meta.sources["abstract"] = "vision"
    return meta


def extract_metadata(doc, pdf_path: str | Path) -> tuple[str, str]:
    """兼容旧签名：只要 ``(标题, 作者)``。"""
    m = extract_paper_meta(doc, pdf_path)
    return m.title, m.authors


# --------------------------------------------------------------------------
# 标题 / 作者
# --------------------------------------------------------------------------


def _title_authors(doc, pdf_meta: dict, pdf_path: str | Path | None) -> tuple[str, str]:
    title = _clean_title(pdf_meta.get("title"))
    authors = normalize_space(_deligature(pdf_meta.get("author") or ""))
    if not title:
        title = _title_from_first_page(doc) or ""
    if not title and pdf_path is not None:
        title = Path(str(pdf_path)).stem
    if not authors:
        authors = _authors_from_first_page(doc)
    return title[:300], authors[:AUTHORS_MAX]


def _clean_title(raw) -> str:
    t = normalize_space(_deligature(raw or ""))
    if not t or t.lower().endswith(".pdf") or len(t) < 6:
        return ""
    # 有些导出工具把标题写成 "Microsoft Word - xxx.doc"
    t = re.sub(r"^microsoft\s+word\s*-\s*", "", t, flags=re.I)
    return t


def _title_from_first_page(doc, max_lines: int = 3) -> str:
    """首页最大字号那几行 —— PDF 元数据里的标题不可信时的兜底。

    **必须滤掉旋转文本**：arXiv 的左侧竖排水印字号比标题还大（实测 20pt vs 17.2pt），
    没有这一步就会把 `arXiv:1706.03762v5 [cs.CL] 6 Dec 2017` 当成论文标题
    （这篇论文在库里以前就一直显示成这个）。
    """
    if getattr(doc, "page_count", 0) == 0:
        return ""
    page = doc[0]
    width = float(page.rect.width or 0)
    cands: list[tuple[float, float, str]] = []
    for text, y0, _y1, size, x0 in _page_lines(page):
        if len(text) < 8 or _NOISE_LINE.search(text):
            continue
        if y0 > page.rect.height * 0.35:          # 只看页面上部 35%
            continue
        if width and (x0 < width * 0.05 or x0 > width * 0.95):   # 页边竖排的刊号/水印
            continue
        cands.append((size, y0, text))
    if not cands:
        return ""
    max_size = max(c[0] for c in cands)
    top = [c for c in cands if c[0] >= max_size - 0.6]
    top.sort(key=lambda c: c[1])
    return _join_title_lines([c[2] for c in top[:max_lines]])


#: 标题里跨行断词的连字符（`SAM-` + `PLING` → `SAMPLING`）
_LINEBREAK_HYPHEN = re.compile(r"([A-Za-z])-\s+(?=[A-Za-z])")


def _join_title_lines(parts: list[str]) -> str:
    """把标题的几行拼起来：**跨行的断词连字符要接回去**。

    实测 `12382-beyond-speedup-utilizing`（ICLR 2026）的标题在 PDF 里是两行：

        BEYOND SPEEDUP - UTILIZING KV CACHE FOR SAM-
        PLING AND REASONING

    直接 `" ".join` 得到 `…FOR SAM- PLING AND REASONING`（用户："好像还是不对"）；
    真实断词应该是 `SAMPLING`。只接"连字符+空白+字母"这一种，`NEAR-OPTIMAL` 这种
    写在**同一行**里的连字符不受影响。
    """
    joined = " ".join(parts)
    spaced = normalize_space(joined)
    return _LINEBREAK_HYPHEN.sub(r"\1", spaced)[:300]


def _authors_from_first_page(doc) -> str:
    """首页「标题 → 摘要」之间的作者行。

    为什么敢这么切：这一段里除了人名就是机构／邮箱，而机构与邮箱有明显的形状
    （含 University/Research/@ 等），排除后基本只剩人名。判不准就返回空 ——
    作者写错比留空更糟。
    """
    if getattr(doc, "page_count", 0) == 0:
        return ""
    page = doc[0]
    lines = _page_lines(page)
    title_bottom = 0.0
    max_size = max((ln[3] for ln in lines), default=0.0)
    for text, _y0, y1, size, _x0 in lines:
        if size >= max_size - 0.6 and len(text) >= 8:
            title_bottom = max(title_bottom, y1)
    top_limit = page.rect.height * 0.45
    stop = top_limit
    for text, y0, _y1, _size, _x0 in lines:       # 摘要标题处收尾
        if y0 > title_bottom and _is_abstract_head(text):
            stop = min(stop, y0)
            break
    names: list[str] = []
    for text, y0, _y1, _size, _x0 in lines:
        if y0 < title_bottom or y0 > stop:
            continue
        cand = _name_line(text)
        if cand and cand not in names:
            names.append(cand)
        if len(names) >= 12:
            break
    return ", ".join(names)


def _name_line(text: str) -> str:
    """一行看起来像人名就返回去掉角标的名字，否则返回空串。"""
    line = normalize_space(text)
    if not line or len(line) > NAME_LINE_MAX:
        return ""
    if _NOISE_LINE.search(line) or _AFFIL_WORDS.search(line):
        return ""
    if any(ch.isdigit() for ch in line) and not re.search(r"[∗†‡*]", line):
        return ""                                  # 纯数字行/年份/页码；带角标的名字除外
    bare = re.sub(r"[∗†‡*§¶]+", "", line).strip(" ,;")
    if len(bare) < 4 or len(bare) > 55:
        return ""
    words = bare.split()
    if not 1 < len(words) <= 6:
        return ""
    if not all(re.search(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff]", w) for w in words):
        return ""                                  # 每个词都得有字母（排除 "and"、符号行）
    caps = sum(1 for w in words if w[:1].isupper() or w[:1] in "ŁÖÜÉ")
    if caps < len(words) - 1:
        return ""                                  # 人名几乎每个词都大写收尾词可小写（van/de）
    return bare


# --------------------------------------------------------------------------
# 会议 / 期刊 / 年份
# --------------------------------------------------------------------------


def _venue_and_year(text: str, pdf_meta: dict) -> tuple[str, int | None, str]:
    """先定会议/期刊，再定年份（会议串里往往就带年份）。第三个返回值说明年份来自哪。"""
    lines = [normalize_space(ln) for ln in (text or "").splitlines()]
    best: tuple[int, str] | None = None
    for ln in lines:
        if not ln or len(ln) > 200:
            continue
        for priority, (kind, pat) in enumerate(_VENUE_RULES):
            m = pat.search(ln)
            if not m:
                continue
            raw = m.group(1) if m.groups() else m.group(0)
            value = _normalize_venue(raw, kind)
            if not value:
                continue
            if best is None or priority < best[0]:
                best = (priority, value)
            break                                  # 同一行只取最高优先级的那条规则
    venue = best[1] if best else ""
    year, src = _year_in(venue), "venue"
    if year is None:
        year, src = _year_from_text(text), "first-page"
    if year is None:
        year, src = _year_from_pdf_date(pdf_meta), "pdf-date"
    return venue[:VENUE_MAX], year, (src if year else "")


def _normalize_venue(raw: str, kind: str) -> str:
    v = normalize_space(_deligature(raw)).strip(" .,;:-—–")
    if kind == "arxiv":
        return "arXiv preprint"
    if kind == "acronym":
        # "NeurIPS'21" → "NeurIPS 2021"
        v = re.sub(r"['’](\d{2})\b", lambda m: " 20" + m.group(1), v)
        v = re.sub(r"\s*(\d{4})$", r" \1", v)
    if kind in ("published-at", "conf-paren"):
        v = re.sub(r"\s*(\d{4})$", r" \1", v)
    tokens = v.split()
    if tokens:
        head = _VENUE_ALIASES.get(tokens[0].upper().strip(".,"))
        if head:
            tokens[0] = head
            v = " ".join(tokens)
    if len(v) < 3 or _NOISE_LINE.search(v):
        return ""
    return v


def _year_in(text: str) -> int | None:
    if not text:
        return None
    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", text)]
    years = [y for y in years if _year_ok(y)]
    return years[-1] if years else None


def _year_from_text(text: str) -> int | None:
    """正文里的年份候选：版权行 > arXiv 编号 > 任意 19xx/20xx。"""
    if not text:
        return None
    for pat in (r"(?:©|\(c\)|copyright)\s*(\d{4})",
                r"arXiv\s*:?\s*(\d{2})\d{2}\.\d{4,5}",
                r"(?:submitted|published|received|accepted)[^\n]{0,40}?(19\d{2}|20\d{2})"):
        m = re.search(pat, text, re.I)
        if not m:
            continue
        if pat.startswith(r"arXiv"):
            yy = int(m.group(1))
            year = 2000 + yy if yy <= 90 else 1900 + yy
        else:
            year = int(m.group(1))
        if _year_ok(year):
            return year
    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", text)]
    years = [y for y in years if _year_ok(y)]
    # 首页出现最多的年份就是它（页眉页脚常重复）；并列时取最小，避免把"参考文献的 2024"当成年份
    if not years:
        return None
    counts: dict[int, int] = {}
    for y in years:
        counts[y] = counts.get(y, 0) + 1
    top = max(counts.values())
    return min(y for y, c in counts.items() if c == top)


def _year_from_pdf_date(pdf_meta: dict) -> int | None:
    raw = str(pdf_meta.get("creationDate") or pdf_meta.get("modDate") or "")
    m = re.search(r"D:(\d{4})", raw)
    if not m:
        return None
    year = int(m.group(1))
    return year if _year_ok(year) else None


def _year_ok(year: int) -> bool:
    return 1950 <= year <= datetime.now().year + 1


# --------------------------------------------------------------------------
# 摘要
# --------------------------------------------------------------------------


def _abstract_from_doc(doc, max_pages: int = 2) -> str:
    """在首两页找「Abstract」：先按**块**切（保真），失败再按纯文本行切。"""
    for pno in range(min(max_pages, getattr(doc, "page_count", 0))):
        page = doc[pno]
        text = _abstract_from_blocks(page)
        if not text:
            text = _abstract_from_text(_page_text(doc, pno))
        if len(text) >= 120:
            return text[:ABSTRACT_MAX]
    return ""


def _abstract_from_blocks(page) -> str:
    blocks = _page_blocks(page)
    for i, blk in enumerate(blocks):
        head_lines = [ln for ln in blk["lines"] if ln.strip()]
        if not head_lines or not _is_abstract_head(head_lines[0]):
            continue
        # 标题和正文有时在同一个块里（PyMuPDF 的块切分不保证按段落），先收同块里剩下的行
        body: list[str] = [ln for ln in head_lines[1:] if not _is_section_heading({"lines": [ln]})]
        j = i + 1
        while j < len(blocks):
            nxt = blocks[j]
            first = next((ln for ln in nxt["lines"] if ln.strip()), "")
            if not first:
                j += 1
                continue
            if _is_abstract_head(first) or _is_section_heading(nxt):
                break
            # 摘要一般是**一段**：上一块已经把话说完了就不再往后吃（否则会把引言吞进来）
            if body and _ends_sentence(body[-1]):
                break
            if body and nxt["y0"] - blocks[j - 1]["y1"] > _para_gap(blocks):
                break
            body += nxt["lines"]
            j += 1
        text = _clean_abstract(join_lines(body))
        if len(text) >= 120:
            return text
    return ""


def _abstract_from_text(text: str) -> str:
    """纯文本兜底（视觉 OCR 出来的 Markdown 也走这里）。"""
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if not _is_abstract_head(ln):
            continue
        body: list[str] = []
        for nxt in lines[i + 1:i + 60]:
            s = _strip_md(nxt)
            if not s:
                if body and _ends_sentence(body[-1]):
                    break
                continue
            if _is_section_heading({"lines": [s]}) or _is_abstract_head(s):
                break
            body.append(s)
            if body and _ends_sentence(body[-1]) and len(join_lines(body)) >= 200:
                break
        out = _clean_abstract(join_lines(body))
        if len(out) >= 120:
            return out
    return ""


def _clean_abstract(text: str) -> str:
    t = normalize_text(_deligature(text))
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(?:abstract|摘要)\s*[:.．]?\s*", "", t, flags=re.I)
    return t


def _is_section_heading(blk: dict) -> bool:
    text = normalize_space(" ".join(blk["lines"]))
    if not text or len(text) > 90:
        return False
    if len(blk["lines"]) > 2:                     # 标题不会占三行
        return False
    return bool(_SECTION_HEAD.match(text))


def _para_gap(blocks: list[dict]) -> float:
    """页面块间距的中位数 × 2：比"正常段落间距"明显更大就认为是换节。"""
    gaps = [b["y0"] - a["y1"] for a, b in zip(blocks, blocks[1:]) if b["y0"] > a["y1"]]
    if not gaps:
        return 40.0
    gaps.sort()
    return max(24.0, gaps[len(gaps) // 2] * 2.0)


# --------------------------------------------------------------------------
# 页面结构小工具（都吃 PyMuPDF 的 dict 输出）
# --------------------------------------------------------------------------


def _page_blocks(page) -> list[dict]:
    """页面文本块（保持 PyMuPDF 给的阅读顺序，**不要按 y 重排**：双栏页会串栏）。"""
    out: list[dict] = []
    for b in (page.get_text("dict") or {}).get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        lines: list[str] = []
        sizes: list[float] = []
        for ln in b.get("lines", []):
            spans = ln.get("spans", [])
            text = "".join(s.get("text", "") for s in spans)
            if text.strip():
                lines.append(text)
            sizes += [float(s.get("size", 0) or 0) for s in spans]
        if not lines:
            continue
        bbox = b.get("bbox") or (0, 0, 0, 0)
        out.append({"lines": lines, "y0": float(bbox[1]), "y1": float(bbox[3]),
                    "x0": float(bbox[0]), "size": max(sizes) if sizes else 0.0})
    return out


def _page_lines(page) -> list[tuple[str, float, float, float, float]]:
    """``[(行文本, y0, y1, 最大字号, x0)]``（按阅读顺序，即 PyMuPDF 给出的块/行顺序）。

    **竖排/旋转文本一律丢掉**：arXiv 水印、页边刊号、旋转的图表标签都不是正文语义，
    留着只会污染标题与作者的判定。
    """
    out: list[tuple[str, float, float, float, float]] = []
    for b in (page.get_text("dict") or {}).get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        for ln in b.get("lines", []):
            direction = ln.get("dir") or (1.0, 0.0)
            if abs(float(direction[1])) > 0.01:        # 非水平 → 旋转文本
                continue
            spans = ln.get("spans", [])
            text = normalize_space("".join(s.get("text", "") for s in spans))
            if not text:
                continue
            bbox = ln.get("bbox") or b.get("bbox") or (0, 0, 0, 0)
            size = max((float(s.get("size", 0) or 0) for s in spans), default=0.0)
            out.append((text, float(bbox[1]), float(bbox[3]), size, float(bbox[0])))
    return out


def _page_text(doc, pno: int) -> str:
    try:
        return doc[pno].get_text("text") or ""
    except Exception:  # noqa: BLE001
        return ""


def _ends_sentence(text: str) -> bool:
    return bool(re.search(r"[.。!！?？:：;；)”\"']\s*$", (text or "").strip()))
