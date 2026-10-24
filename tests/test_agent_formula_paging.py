r"""`formula_get` 的**分页**：`offset` / `limit` + `total` / `next_offset` / `has_more`（纯离线）。

用户口径（已拍板）：条目多时**分页返回**，不要一次塞 42 条，也不要"只给 40 条然后让人再补
一页"。分页的天花板在宿主，所以**按后端解决**（四层一起改）：

- `project.formulas` 真吃 `offset` / `limit`，回执带 `offset` / `limit` / `next_offset` /
  `has_more`，`truncated` 收紧成 `has_more`，`missing_pages` 改成"窗口**之后**那几条在哪几页"；
- 路由与 `HostClient.formulas` 透传这两个参数；
- 工具**按 offset 窗口一次把整份列表拿回来**（每窗口 200 条、最多 5 个窗口），
  不再按 `missing_pages` 逐页补 —— 行内几千条那批以前补不完。

这里钉住：
1. 工具的分页元数据（`total` / `offset` / `limit` / `next_offset` / `has_more`）与参数夹取；
2. 宿主 42 条时**一个请求**（`limit=200`）就把整份列表拿回来，第 0 页 40 条、下一页 2 条；
3. 宿主 1200 条时停在 5 个窗口（1000 条），`truncated` / `missing_pages` 说清还差哪几页，
   **不发第 6 个请求**；
4. 老形状回执（没有 `next_offset` / `has_more`）→ 不抛异常，退回"按页补"的提示；
5. `project.formulas` 自己的窗口：`next_offset` / `missing_pages` / `truncated` / limit 夹取。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agent import tools as agent_tools
from backend.services.agent import project


def _item(page: int, row_id: str, latex: str) -> dict:
    return {"page": page, "row_id": row_id, "latex": latex, "display": True,
            "grade": None, "partial": False, "source": "markdown",
            "context": "", "context_zh": ""}


#: 6 条块级公式，一页一条（第 3~8 页）—— 分页用例的固定素材
SIX = [_item(page, f"p{page:03d}-01", f"f_{{{page}}}") for page in range(3, 9)]


class _FakeHost:
    """按真实回执的形状回公式：宿主**自己**吃 `offset` / `limit`，并回 `next_offset` / `has_more`。

    契约变了就跟着改：上一版假宿主只认 `page`，靠 `truncated` + `missing_pages` 让工具逐页补；
    真宿主现在有真分页，假宿主不回这两个键就等于在测一个已经不存在的宿主。
    """

    def __init__(self, items: list[dict], *, per_call: int | None = None) -> None:
        self.items = list(items)
        #: 模拟"宿主一次只肯给这么多条"（老 40 条上限的等价物，用来压窗口循环）
        self.per_call = per_call
        self.calls: list[dict] = []

    async def formulas(self, paper_id, *, page=None, query=None, include_inline=False,
                       offset=None, limit=None):
        self.calls.append({"page": page, "offset": offset, "limit": limit})
        listed = [one for one in self.items if not page or int(one["page"]) == int(page)]
        start = max(0, int(offset or 0))
        cap = max(1, min(int(limit or project.FORMULA_LIMIT), project.FORMULA_LIMIT_MAX))
        if self.per_call:
            cap = min(cap, int(self.per_call))
        end = start + cap
        kept = listed[start:end]
        has_more = len(listed) > end
        return {"total": len(self.items), "returned": len(kept), "source": "markdown",
                "truncated": has_more, "display_total": len(self.items),
                "inline_total": 0, "omitted_inline": 0,
                "offset": start, "limit": cap,
                "next_offset": end if has_more else None, "has_more": has_more,
                "missing_pages": sorted({int(one.get("page") or 0) for one in listed[end:]
                                         if one.get("page")}),
                "items": [dict(one) for one in kept]}


class _LegacyHost:
    """**老宿主**：回执里没有 `next_offset` / `has_more`（只有 `truncated` + `missing_pages`）。"""

    def __init__(self, items: list[dict], *, cap: int = 2,
                 missing_pages: list[int] | None = None) -> None:
        self.items = list(items)
        self.cap = cap
        self.missing_pages = list(missing_pages or [])
        self.calls: list[dict] = []

    async def formulas(self, paper_id, *, page=None, query=None, include_inline=False,
                       offset=None, limit=None):
        self.calls.append({"page": page, "offset": offset, "limit": limit})
        kept = self.items[:self.cap] if not page else [
            one for one in self.items if int(one["page"]) == int(page)]
        return {"total": len(self.items), "returned": len(kept), "source": "markdown",
                "truncated": True, "display_total": len(self.items),
                "inline_total": 0, "omitted_inline": 0,
                "missing_pages": list(self.missing_pages),
                "items": [dict(one) for one in kept]}


class _StuckHost:
    """**永远给不完**的宿主：一直说"还有"，却一条新条目也给不出来。"""

    def __init__(self, items: list[dict]) -> None:
        self.items = list(items)
        self.calls: list[dict] = []

    async def formulas(self, paper_id, *, page=None, query=None, include_inline=False,
                       offset=None, limit=None):
        self.calls.append({"page": page, "offset": offset, "limit": limit})
        return {"total": len(self.items), "returned": 2, "source": "markdown",
                "truncated": True, "display_total": len(self.items),
                "inline_total": 0, "omitted_inline": 0,
                "offset": 0, "limit": 200, "next_offset": 2, "has_more": True,
                "missing_pages": [99],
                "items": [dict(one) for one in self.items[:2]]}


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
# ④ 工具的取数：按 offset 窗口一次把整份列表拿回来（不再逐页补）
# --------------------------------------------------------------------------


def _many(total: int, *, per_page: int, offset_page: int = 1) -> list[dict]:
    """`total` 条块级公式，每页 `per_page` 条（页号从 `offset_page` 起），LaTeX 各不相同。"""
    # LaTeX 用 w_ 前缀：与 SIX 的 f_ 前缀不撞，否则跨页去重（同一条公式重复陈述）会先合并掉
    return [_item(offset_page + index // per_page,
                  f"p{offset_page + index // per_page:03d}-{index % per_page:02d}",
                  f"w_{{{index}}}") for index in range(total)]


def test_one_window_request_covers_a_42_item_paper() -> None:
    """宿主 42 条：工具**一个请求**（`limit=200`）就拿到整份列表，第 0 页给 40 条。"""
    host = _FakeHost(SIX + _many(36, per_page=1, offset_page=9))   # 6 + 36 = 42 条
    assert len(host.items) == 42
    first = _call(host, limit=40)
    assert (first["total"], first["returned"]) == (42, 40)
    assert (first["offset"], first["limit"]) == (0, 40)
    assert first["next_offset"] == 40 and first["has_more"] is True
    assert first["truncated"] is True
    # 窗口之后那 2 条落在第 43、44 页（SIX 到第 8 页，_many 从第 9 页起、共 36 条）
    assert first["missing_pages"] == [43, 44]
    # 宿主只被问了**一次**，而且一开口就要 200 条 —— 不是 40 条然后一页页补
    assert len(host.calls) == 1
    assert (host.calls[0]["offset"], host.calls[0]["limit"]) == (0, agent_tools._FORMULA_FETCH_LIMIT)
    second = _call(host, offset=40, limit=40)
    assert second["returned"] == 2 and second["next_offset"] is None
    assert second["has_more"] is False and second["truncated"] is False
    assert len(host.calls) == 2        # 第二次调用同样只发一个窗口


def test_windows_stop_at_the_budget_and_say_what_is_missing() -> None:
    """宿主 1200 条（超过 5 个窗口）→ 停在 1000 条，说清还差哪几页，**不发第 6 个请求**。"""
    host = _FakeHost(_many(1200, per_page=12))
    out = _call(host, limit=40)
    assert len(host.calls) == agent_tools._FORMULA_FETCH_WINDOWS == 5
    assert [call["offset"] for call in host.calls] == [0, 200, 400, 600, 800]
    assert all(call["limit"] == agent_tools._FORMULA_FETCH_LIMIT for call in host.calls)
    assert out["total"] == 1200 and out["returned"] == 40
    assert out["truncated"] is True and out["has_more"] is True
    # 第 1000 条落在第 84 页；预算用尽后剩下的 85~100 页要由 missing_pages 说清
    assert set(range(84, 101)) <= set(out["missing_pages"])
    assert 100 in out["missing_pages"]


def test_a_host_that_never_finishes_does_not_loop_or_pretend_to_be_complete() -> None:
    """宿主一直说"还有"却给不出新条目 → 不无限循环、也不装作收齐（missing_pages 照报）。"""
    host = _StuckHost(SIX)
    out = _call(host, limit=2)
    assert len(host.calls) == agent_tools._FORMULA_FETCH_WINDOWS
    assert out["truncated"] is True and 99 in out["missing_pages"]


def test_legacy_receipt_falls_back_to_the_per_page_hint() -> None:
    """老形状回执（没有 next_offset / has_more）→ 不抛异常，退回"照 missing_pages 按页补"。"""
    host = _LegacyHost(SIX, cap=2, missing_pages=[7, 8])
    out = _call(host, limit=2)
    assert len(host.calls) == 1, "老宿主没有 offset 可翻，不该多发请求"
    assert out["truncated"] is True and out["missing_pages"] == [7, 8]
    hint = out.get("hint") or ""
    assert "7、8" in hint and "formula_get(page=" in hint


# --------------------------------------------------------------------------
# ⑤ project.formulas 自己的窗口（后端的真分页）
# --------------------------------------------------------------------------


def _md(page: int, row: int, latex: str, display: bool = True) -> dict:
    return {"id": f"f{page:03d}-{row:04d}", "page": page, "row_id": f"p{page:03d}-{row:02d}",
            "latex": latex, "display": display, "context": "", "context_zh": "",
            "source": "markdown"}


@pytest.fixture
def fake_sources(monkeypatch):
    def install(md: list[dict]) -> None:
        monkeypatch.setattr(project.reader_service, "formulas_payload",
                            lambda paper_id, source="markdown": {
                                "items": md if source == "markdown" else []})
        monkeypatch.setattr(project.paper_service, "get_paper",
                            lambda paper_id: {"id": paper_id, "page_count": 60})
    return install


#: 42 条块级公式，一页一条（第 1~42 页）—— 窗口用例的固定素材
FORTY_TWO = [_md(page, 1, f"g_{{{page}}}") for page in range(1, 43)]


def test_project_first_window_points_at_the_next_offset(fake_sources) -> None:
    fake_sources(FORTY_TWO)
    out = project.formulas("p1", offset=0, limit=40)
    assert (out["total"], out["returned"]) == (42, 40)
    assert (out["offset"], out["limit"]) == (0, 40)
    assert out["next_offset"] == 40 and out["has_more"] is True
    assert out["truncated"] is True
    assert out["missing_pages"] == [41, 42]


def test_project_last_window_has_no_next_offset(fake_sources) -> None:
    fake_sources(FORTY_TWO)
    out = project.formulas("p1", offset=40, limit=40)
    assert out["returned"] == 2 and out["next_offset"] is None and out["has_more"] is False
    assert out["truncated"] is False and out["missing_pages"] == []


def test_project_truncated_only_tracks_has_more(fake_sources) -> None:
    """`include_inline=False` 过滤掉的行内**不算**截断（仍由 omitted_inline 说）。"""
    fake_sources([_md(1, 1, "a"), _md(2, 1, "b"), _md(3, 1, "c"),
                  _md(4, 1, "x", display=False), _md(5, 1, "y", display=False)])
    out = project.formulas("p1")
    assert out["truncated"] is False and out["missing_pages"] == [] and out["has_more"] is False
    assert (out["total"], out["display_total"], out["inline_total"]) == (5, 3, 2)
    assert out["omitted_inline"] == 2 and out["returned"] == 3


def test_project_limit_is_clamped_to_one_and_five_hundred(fake_sources) -> None:
    fake_sources(FORTY_TWO)
    # 下界：`int(limit or FORMULA_LIMIT)` 把 0 当"没给"（回默认 40），负数才夹成 1
    low = project.formulas("p1", limit=-5)
    assert (low["limit"], low["returned"], low["next_offset"]) == (1, 1, 1)
    high = project.formulas("p1", limit=9999)
    assert high["limit"] == project.FORMULA_LIMIT_MAX == 500
    assert high["returned"] == 42 and high["has_more"] is False
