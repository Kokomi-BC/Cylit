"""块 id → 正文行 id 的映射（"跳到对应段落"的锚点解析，规划 §20.2）。

**为什么需要**：解析块（`block.id`，如 ``attentionisa-p01-b06``）与正文行
（页级产物的位置编号，如 ``p001-06``）是**两套命名空间**：

- 行 id 由页级产物的行序决定（``p{页号}-{行序}``），页面**已翻译**时正文行来自
  页级 Markdown 配对（见 ``reader_service.page_translations`` 里的 ``pair_rows``），
  跟解析块毫无对应关系；页**未翻译**时才退回 ``block.id``（``usePageRows.rowFromBlock``）。
- 而检索/RAG 侧存的是解析块 id（chunk 的 ``block_id``），指向的还是**块**。

于是「知识库点『跳回原文』」在已翻译的论文上永远是空跳：前端拿 ``block_id`` 去
``rowById`` 里查，查不到，静默返回（真实事故）。这里给出确定性的桥：

1. **归一化**后取块文本的前 ``HEAD_CHARS`` 个字符，在**同页**的行里找包含它的那行
   （块与行同源于一页文本，顺序也一致，所以主路径几乎必中）；
2. 主路径不中（行首被模型改写、块文本被截断）时，退化为"取块文本中间/尾部窗口"再找一次；
3. 都不中就**不给映射**（前端退化为按页跳转并明确提示，而不是乱跳一行）。

只用纯函数 + 合成数据即可测试（``tests/test_anchor_map.py``）。映射**读时计算**，
与 ``pair_rows`` 一样"老产物不用重翻也能拿到新行为"；行序变了映射跟着变，不会留旧值。
"""

from __future__ import annotations

import re

#: 归一化：只留字母数字与汉字（与 ``page_translate.anchor_key`` 同一口径）
_SIG_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

#: 主路径用的"块文本头"长度（归一化后的字符数）
HEAD_CHARS = 24
#: 退化路径用的窗口长度
WINDOW_CHARS = 12
#: 归一化后短于这个长度的块（公式符号、页码、"†"、Common Word）不做文本匹配：
#: 这么短的串在别处撞车太容易，宁可交给按页兜底，也不要跳错一段。
MIN_SIG_CHARS = 8


def normalize(text: str | None) -> str:
    """块文本 / 行文本 → 可比较的签名（去空白、去 Markdown 与 LaTeX 转义、转小写）。"""
    return _SIG_RE.sub("", (text or "").lower())


def _windows(sig: str) -> list[str]:
    """块签名的候选窗口：头、中、尾（长块时"头"可能已被模型改写过）。"""
    if len(sig) <= WINDOW_CHARS:
        return [sig]
    spots = [0, max(0, len(sig) // 2 - WINDOW_CHARS // 2), len(sig) - WINDOW_CHARS]
    out: list[str] = []
    for start in spots:
        win = sig[start:start + WINDOW_CHARS]
        if win not in out:
            out.append(win)
    return out


def _find_row(sig: str, row_sigs: list[str], start: int, min_chars: int) -> int | None:
    """在 ``row_sigs[start:]`` 里找包含块签名的行；返回行下标或 ``None``。

    **保序**：从 ``start`` 往后找，块顺序与行顺序一致时不会把第 3 段的锚点配回第 1 段
    （同一句在前文出现过时尤其重要）。
    """
    head = sig[:HEAD_CHARS]
    if len(head) >= min_chars:
        for i in range(start, len(row_sigs)):
            if head in row_sigs[i]:
                return i
    for win in _windows(sig):
        if len(win) < min_chars:
            continue
        for i in range(start, len(row_sigs)):
            if win in row_sigs[i]:
                return i
    return None


def build_block_row_map(
    blocks: list[dict],
    rows_by_page: dict[int, list[dict]],
    *,
    min_chars: int = MIN_SIG_CHARS,
) -> dict[str, str]:
    """``{block_id: row_id}``：块 → 它落在哪一正正文行。

    ``blocks``：形如 ``{"id", "page", "text"}``（``reader_service`` 的 ``out_blocks``）；
    ``rows_by_page``：``{页号: [{"id", "original"}, ...]}``（页级产物的行，**按行序**）。

    没有译文行的页（``rows_by_page`` 里没有该页）**不进映射** —— 那些页的正文行 id
    本来就是 ``block.id``，前端直命即可。

    **降级而不是静默**（两条都是实测过的"一个坏输入毒死一整页"）：

    - 没有页码的块**不映射**：以前缺省成第 1 页，于是那些块被拿去和第 1 页的行硬配
      （宁可不跳，也不能乱跳一页）；
    - 一页里有一行缺 id 时**只跳过那一行**：以前是整页 continue，其他块全都失去锚点。
    """
    out: dict[str, str] = {}
    by_page: dict[int, list[dict]] = {}
    for block in blocks:
        block_id = block.get("id")
        if not block_id:
            continue
        page = str(block.get("page") or "").strip()
        if not page:
            continue
        try:
            page_no = int(page)
        except (TypeError, ValueError):
            continue
        by_page.setdefault(page_no, []).append(block)

    for page, page_blocks in by_page.items():
        # 坏行（缺 id）只跳过它自己：它的 id 本来也不能当锚点用，其余行照常映射
        rows = [r for r in (rows_by_page.get(page) or []) if r.get("id")]
        if not rows:
            continue
        row_ids = [r.get("id") for r in rows]
        row_sigs = [normalize(r.get("original")) for r in rows]
        cursor = 0
        for block in page_blocks:
            sig = normalize(block.get("text"))
            if len(sig) < min_chars:
                continue
            idx = _find_row(sig, row_sigs, cursor, min_chars)
            if idx is None:
                # 回退：块顺序与行顺序在"页眉/脚注"这类内容上未必严格一致
                idx = _find_row(sig, row_sigs, 0, min_chars)
            if idx is None:
                continue
            out[str(block["id"])] = str(row_ids[idx])
            cursor = idx
    return out


# --------------------------------------------------------------------------
# 5. 锚点 id 的**唯一定义**（批注 / 高亮 / 笔记 / 检索结果 → 页码）
# --------------------------------------------------------------------------
#
# 锚点前缀有五套，以前各写一套规则、互不认识：md:p3 / pdf:p3（视图页锚点，页码写在 id 里）、
# row-<行 id>（公式/表格类 chunk 的锚点）、blk-<块 id>（解析器给的 markdown 锚点，见
# parsing/md/generator.py）、fig-p010-01（图目录 id）与 fig:p10:n1（前端 ReaderChat 合成的
# 引用锚点）。reader_service._anchor_page 当时只认 md/pdf 两种，于是 row- 那一类锚点的
# page 恒为 None（侧栏里那条批注无法"跳到第几页"）。这里把口径收成一处，谁也别再自己写一套。

#: 伪前缀：不是 id 的一部分，只是"这是哪一类锚点"的标记
PSEUDO_PREFIXES = ("row-", "blk-")

#: 视图页锚点：md:p3 / pdf:p3 —— 页码就写在 id 里
_PAGE_ANCHOR_RE = re.compile(r"^(md|pdf):p(\d+)$")
#: 图锚点：fig-p010-01（图目录 id）与 fig:p10:n1（前端合成），都取紧跟 p 后的页码段
_FIG_PAGE_RE = re.compile(r"^fig[-:]p(\d+)")


def normalize_block_id(raw) -> str:
    """去掉伪前缀（幂等；None → ""）。row-p006-03 → p006-03，blk-X → X。

    **幂等**：一次调用就把所有层的伪前缀剥干净（row-blk-X → X），所以
    f(f(x)) == f(x) 恒成立 —— 调用方不必关心自己拿到的是第几手 id。
    """
    text = "" if raw is None else str(raw).strip()
    changed = True
    while changed:
        changed = False
        for prefix in PSEUDO_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
                changed = True
    return text


def _anchor(kind: str, row_id: str = "", page: int | None = None) -> dict:
    return {"kind": kind, "row_id": row_id, "page": page}


def resolve_anchor(block_id, *, row_pages: dict[str, int] | None = None,
                   block_pages: dict[str, int] | None = None,
                   fig_pages: dict[str, int] | None = None) -> dict:
    """锚点 id → {kind, row_id, page}（锚点解析的**唯一定义**，纯函数、不查库）。

    kind 取值：md-page / pdf-page / row / block / figure / unknown。"查不到页"是 None，
    与"第 1 页"必须分得开 —— 否则会静默跳错页（这是这条规则唯一在乎的事）。

    规则：

    1. md:p3 / pdf:p3 → 页码直接来自 id（kind 对应）；
    2. row-<id> → kind=row，row_id=<id>，页从 row_pages 取（id 里没有页号，猜不出来）；
    3. fig-p010-01 / fig:p10:n1 → kind=figure，页先查 fig_pages，查不到再取 id 里的
       页码段，都没有就 None；
    4. 其余 → 归一化后依次查 row_pages / block_pages，命中即定 kind（row / block），
       都不命中 kind=unknown；
    5. None / 空 → {kind: unknown, row_id: "", page: None}。
    """
    raw = "" if block_id is None else str(block_id).strip()
    if not raw:
        return _anchor("unknown")
    rows = row_pages or {}
    blocks = block_pages or {}
    figs = fig_pages or {}

    if (m := _PAGE_ANCHOR_RE.match(raw)):
        return _anchor("md-page" if raw.startswith("md:") else "pdf-page",
                       page=int(m.group(2)))

    if raw.startswith(("fig-", "fig:")):
        page = figs.get(raw)
        if page is None and (m := _FIG_PAGE_RE.match(raw)):
            page = int(m.group(1))
        return _anchor("figure", page=page)

    normalized = normalize_block_id(raw)
    if raw.startswith("row-"):
        return _anchor("row", normalized, rows.get(normalized))

    for table, kind in ((rows, "row"), (blocks, "block")):
        page = table.get(normalized)
        if page is None and normalized != raw:
            # 兼容"表里存的就是带前缀的原样 id"的旧数据
            page = table.get(raw)
        if page is not None:
            return _anchor(kind, normalized, page)
    return _anchor("unknown", normalized)


def rows_by_page(page_payloads: list[dict]) -> dict[int, list[dict]]:
    """页级产物列表 → ``{页号: 行列表}``（``build_block_row_map`` 的入参形状）。"""
    out: dict[int, list[dict]] = {}
    for payload in page_payloads or []:
        page = int(payload.get("page") or 0)
        if not page:
            continue
        out[page] = list(payload.get("rows") or [])
    return out
