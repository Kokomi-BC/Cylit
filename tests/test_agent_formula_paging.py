r"""`formula_get` 的**分页**：`offset` / `limit` + `total` / `next_offset` / `has_more`（纯离线）。

用户口径（已拍板）：条目多时**分页返回**，不要一次塞 42 条，也不要"只给 40 条然后让人再补
一页"。所以：

- `total` 是未分页前的条目总数，`offset` / `limit` 是这次口径，`next_offset` 没有下一页就是 None；
- `display_total` / `inline_total` / `missing_pages` 都不随分页变（按**整份列表**算）；
- `offset < 0` 当 0；`limit` 夹到 [1, 200]（`limit=0` 夹成 1，不是"回默认 40"）；
- 宿主自己一次只肯给 40 条时（`truncated` + `missing_pages`），工具**自己把这几页补回来**，
  否则 `offset` 一过 40 就只有空页，分页等于假的。
"""

from __future__ import annotations

import asyncio

from backend.agent import tools as agent_tools


def _item(page: int, row_id: str, latex: str) -> dict:
    return {"page": page, "row_id": row_id, "latex": latex, "display": True,
            "grade": None, "partial": False, "source": "markdown",
            "context": "", "context_zh": ""}


#: 6 条块级公式，一页一条（第 3~8 页）—— 分页用例的固定素材
SIX = [_item(page, f"p{page:03d}-01", f"f_{{{page}}}") for page in range(3, 9)]


class _FakeHost:
    """按真实回执的形状回公式；`cap` 模拟宿主自己的 40 条截断（truncated + missing_pages）。"""

    def __init__(self, items: list[dict], *, cap: int | None = None,
                 missing_pages: list[int] | None = None) -> None:
        self.items = list(items)
        self.cap = cap
        self.missing_pages = list(missing_pages or [])
        self.calls: list[dict] = []

    def _payload(self, items: list[dict], truncated: bool, missing: list[int]) -> dict:
        return {"total": len(self.items), "returned": len(items), "source": "markdown",
                "truncated": truncated, "display_total": len(self.items),
                "inline_total": 0, "omitted_inline": 0,
                "missing_pages": list(missing),
                "items": [dict(one) for one in items]}

    async def formulas(self, paper_id, *, page=None, query=None, include_inline=False):
        self.calls.append({"page": page})
        if page:   # 按页补：宿主真能给"这一页的公式"
            return self._payload([one for one in self.items
                                 if int(one["page"]) == int(page)], False, [])
        if self.cap:
            return self._payload(self.items[:self.cap], True, self.missing_pages)
        return self._payload(self.items, False, [])


class _Deps:
    def __init__(self, host) -> None:
        from backend.agent.citations import RefRegistry

        self.host = host
        self.paper_id = "p1"
        self.registry = RefRegistry("p1")
        self.query_cache: dict = {}
        self.row_spans: dict = {}

    def count_search(self) -> None:
        return None

    def note_tool(self, *args, **kwargs) -> None:
        return None


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def _call(host, **kwargs):
    return asyncio.run(agent_tools.formula_get(_Ctx(_Deps(host)), **kwargs))


# --------------------------------------------------------------------------
# ① 翻页元数据
# --------------------------------------------------------------------------


def test_next_offset_walks_the_whole_list_and_then_stops() -> None:
    """5 条 / 每页 2 条：0 → 2 → 4 → 没有下一页。"""
    host = _FakeHost(SIX[:5])
    first = _call(host, limit=2)
    assert (first["total"], first["offset"], first["limit"]) == (5, 0, 2)
    assert first["returned"] == 2 and first["next_offset"] == 2 and first["has_more"] is True
    second = _call(host, offset=first["next_offset"], limit=2)
    assert second["next_offset"] == 4 and second["has_more"] is True
    last = _call(host, offset=second["next_offset"], limit=2)
    assert last["returned"] == 1 and last["next_offset"] is None and last["has_more"] is False
    assert [one["page"] for one in last["items"]] == [7]


def test_a_single_page_has_no_next_offset() -> None:
    """默认 limit=40（向后兼容）：条目装得下时就是"一页到底"。"""
    out = _call(_FakeHost(SIX))
    assert out["returned"] == 6 and out["has_more"] is False and out["next_offset"] is None
    assert out["limit"] == agent_tools.FORMULA_PAGE_DEFAULT == 40


# --------------------------------------------------------------------------
# ② 参数夹取
# --------------------------------------------------------------------------


def test_negative_offset_starts_from_the_head() -> None:
    out = _call(_FakeHost(SIX), offset=-7)
    assert out["offset"] == 0 and [one["page"] for one in out["items"]] == [3, 4, 5, 6, 7, 8]


def test_zero_limit_is_clamped_to_one_not_back_to_the_default() -> None:
    out = _call(_FakeHost(SIX), limit=0)
    assert out["limit"] == 1 and out["returned"] == 1 and out["next_offset"] == 1


def test_limit_above_200_is_clamped() -> None:
    out = _call(_FakeHost(SIX), limit=999)
    assert out["limit"] == 200 and out["returned"] == 6 and out["has_more"] is False


def test_offset_past_the_end_returns_an_empty_page_and_says_so() -> None:
    out = _call(_FakeHost(SIX), offset=99)
    assert out["items"] == [] and out["returned"] == 0 and out["has_more"] is False
    assert out["total"] == 6 and "99" in (out.get("hint") or "")


# --------------------------------------------------------------------------
# ③ 计数与 missing_pages 按整份列表算（不随这一页变）
# --------------------------------------------------------------------------


def test_counts_do_not_shrink_with_the_slice() -> None:
    """`display_total` / `total` 说的是"有多少"，不是"这次给了多少"。"""
    host = _FakeHost(SIX)
    for offset in (0, 2, 4):
        out = _call(host, offset=offset, limit=2)
        assert (out["total"], out["display_total"], out["inline_total"]) == (6, 6, 0)


def test_missing_pages_covers_the_whole_list_not_just_this_page() -> None:
    """第一页只给了第 3、4 页 → `missing_pages` 仍然是**全量**剩下的 5/6/7/8 页。"""
    out = _call(_FakeHost(SIX), limit=2)
    assert out["missing_pages"] == [5, 6, 7, 8] and out["truncated"] is True
    later = _call(_FakeHost(SIX), offset=2, limit=2)
    assert [one["page"] for one in later["items"]] == [5, 6]
    assert later["missing_pages"] == [7, 8]


# --------------------------------------------------------------------------
# ④ 宿主自己的 40 条截断：工具把缺的页补回来，分页才翻得过去
# --------------------------------------------------------------------------


def test_host_truncation_is_backfilled_so_the_next_page_is_not_empty() -> None:
    """宿主只肯给前 4 条（模拟 40 条上限）→ 工具补第 7、8 页，offset=4 拿得到剩下的。"""
    host = _FakeHost(SIX, cap=4, missing_pages=[7, 8])
    out = _call(host, offset=4, limit=2)
    assert [one["page"] for one in out["items"]] == [7, 8]
    assert (out["total"], out["returned"], out["has_more"]) == (6, 2, False)
    assert out["truncated"] is False and out["missing_pages"] == []
    # 补页是按宿主点名的页要的，不是把整篇重扫一遍
    assert [call["page"] for call in host.calls if call["page"]] == [7, 8]


def test_unfillable_host_pages_still_show_up_in_missing_pages() -> None:
    """补不齐（宿主点名了页却给不出新条目）→ 别装作收齐了：missing_pages 照旧报。"""
    host = _FakeHost(SIX, cap=4, missing_pages=[99])
    out = _call(host, limit=2)
    assert out["truncated"] is True and 99 in out["missing_pages"]
    assert out["has_more"] is True and out["next_offset"] == 2
