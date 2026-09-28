"""MinerU 页码范围：语法、规范化，以及**切片内序号 → 原文档页号**的映射。

为什么必须有这个模块
--------------------
官方 API 的 `page_ranges` 只解析选中的页，而返回的 ``content_list.json`` 里
``page_idx`` 是**相对切片**的序号，不是原文档页号。真机实测（同一份 6 页 PDF）：

| 请求的 page_ranges | 选中的页 | 返回的 page_idx |
|---|---|---|
| ``3-4`` | 3,4 | ``0, 1`` |
| ``2,4-6`` | 2,4,5,6 | ``0, 1, 2, 3`` |

拿它当页号用，正文 / 插图 / 公式会**整体挂到错误的页上**（``3-4`` 的内容出现在第 1、2 页），
而阅读器里还看不出异常 —— 每页都有内容，只是页对不上。这是最难查的一类静默错误，
所以映射规则写在这里、并被单测钉死。

语法（官方口径）
----------------
``1-600`` 区间、``2,4-6`` 逗号串、``2--2``（``-2`` = 倒数第二页，即"从第 2 页到倒数第 2 页"）。
"""

from __future__ import annotations

import re

#: 给用户看的写法示例（校验失败时的报错也用它，服务层直接引用这一份）
PAGE_RANGES_HINT = "例：1-10、2,4-6、2--2（-2 = 倒数第二页）；留空 = 全部页"

#: 单个片段：`N` / `A-B` / `A--k`
_TOKEN_RE = re.compile(r"^(?:\d+|\d+-\d+|\d+--\d+)$")


def compact_page_ranges(text: object) -> str:
    """去掉空白、把 `A - B` 规范成 `A-B`（``A--k`` 里的双横线**不能**压成一个）。

    返回空串表示"没填"（= 全部页）。语法不合法时抛 ``ValueError``（调用方翻成人话）。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    out: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        compact = token if "--" in token else re.sub(r"\s*-\s*", "-", token)
        if not compact or not _TOKEN_RE.match(compact):
            raise ValueError(PAGE_RANGES_HINT)
        out.append(compact)
    return ",".join(out)


def looks_like_page_ranges(text: object) -> bool:
    try:
        compact_page_ranges(text)
    except ValueError:
        return False
    return True


def parse_page_ranges(text: object, total_pages: int) -> list[int]:
    """把页码范围展开成**有序的 1-based 页号列表**（空串 = 全部页）。

    - ``"3-4"`` + 6 页 → ``[3, 4]``
    - ``"2,4-6"`` + 6 页 → ``[2, 4, 5, 6]``
    - ``"2--2"`` + 6 页 → ``[2, 3, 4, 5]``（到倒数第二页为止）
    - 超出文档页数的片段会被裁到页数上限（并去重）—— MinerU 那边本来也解析不出来，
      这里裁掉是为了让后面的映射不越界。

    语法不合法抛 ``ValueError``。``total_pages <= 0`` 时只做语法解析、不裁剪。
    """
    compact = compact_page_ranges(text)
    if not compact:
        return list(range(1, max(0, total_pages) + 1))
    pages: list[int] = []
    for token in compact.split(","):
        if "--" in token:
            start_s, _, back_s = token.partition("--")
            start = int(start_s)
            back = int(back_s or 0)
            end = (total_pages - back + 1) if total_pages > 0 else start
            span = range(start, max(start, end) + 1)
        elif "-" in token:
            start_s, _, end_s = token.partition("-")
            span = range(int(start_s), int(end_s) + 1)
        else:
            span = [int(token)]
        for page in span:
            if page < 1:
                continue
            if total_pages > 0 and page > total_pages:
                continue
            if page not in pages:
                pages.append(page)
    return pages


__all__ = [
    "PAGE_RANGES_HINT", "compact_page_ranges", "looks_like_page_ranges",
    "parse_page_ranges",
]
