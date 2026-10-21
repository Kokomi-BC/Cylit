"""材料清单：**先按路/语言过滤，再截断**。

以前在 SQL 里 limit(MAX_ITEMS*3)，那一截是过滤之前的材料 —— 别的路/别的语言的行白占
配额，长论文的尾巴会整段从解读里消失（审计 2026-12-04 的 P1）。
"""

from __future__ import annotations

import json

from backend.services.understanding import understanding_service as us


class _Row:
    def __init__(self, cid: str, road: str, lang: str, ctype: str = "unit", page: int = 1):
        self.id = cid
        self.chunk_type = ctype
        self.page_no = page
        self.section = "1"
        self.content_preview = cid
        self.metadata_json = json.dumps({"road": road, "lang": lang})


def test_keeps_only_this_road_and_language() -> None:
    rows = [_Row("a-en", "a", "en"), _Row("b-zh", "b", "zh"),
            _Row("shared-any", "shared", "any"), _Row("a-zh", "a", "zh")]
    items = us._inventory_items(rows, "a", ("en", "any"))
    assert [i["chunk_id"] for i in items] == ["a-en", "shared-any"]


def test_filter_happens_before_the_cap() -> None:
    """B 路的行再多也吃不掉 A 路的配额：过滤后的条数才是被截断的那个数。"""
    rows = [_Row(f"b{i}", "b", "zh") for i in range(300)]
    rows += [_Row(f"a{i}", "a", "en") for i in range(us.MAX_ITEMS)]
    items = us._inventory_items(rows, "a", ("en", "any"))
    assert len(items) == us.MAX_ITEMS, "A 路一条都不该被 B 路的行挤掉"
    assert [i["chunk_id"] for i in items[:3]] == ["a0", "a1", "a2"]
