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

#: 归一化：只留字母数字与汉字（与 ``knowledge_service._block_sig`` 同一口径）
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
    """
    out: dict[str, str] = {}
    by_page: dict[int, list[dict]] = {}
    for block in blocks:
        block_id = block.get("id")
        if not block_id:
            continue
        page = int(block.get("page") or 1)
        by_page.setdefault(page, []).append(block)

    for page, page_blocks in by_page.items():
        rows = rows_by_page.get(page) or []
        if not rows:
            continue
        row_ids = [r.get("id") for r in rows]
        if any(not rid for rid in row_ids):
            continue
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


def rows_by_page(page_payloads: list[dict]) -> dict[int, list[dict]]:
    """页级产物列表 → ``{页号: 行列表}``（``build_block_row_map`` 的入参形状）。"""
    out: dict[int, list[dict]] = {}
    for payload in page_payloads or []:
        page = int(payload.get("page") or 0)
        if not page:
            continue
        out[page] = list(payload.get("rows") or [])
    return out
