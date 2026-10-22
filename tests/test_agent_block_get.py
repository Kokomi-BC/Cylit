"""`block_get` 的 id 名字空间与兜底（纯离线）。

背景（2026-12 实测）：写侧把 `chunk.block_id` 填成了**页级行 id**（`p017-00`），读侧拿它去
`sess.get(Block, …)` 必然 `NOT_FOUND` —— 633 条存量 chunk 里 614 条中招，正文页同样坏。
真机上表现为"检索命中的 ref 永远展不开"，而 `page_markdown_get` 派生的 ref 永远正常。

这里钉四件事：
1. 行 id 形态的命中**只下发 row_id**，绝不进 block_id；
2. 真 Block id 仍走块路径（别为了绕过 bug 把块路径写死）；
3. 块路径 404 时兜底走行路径，并把**跨多行的 chunk 拼成整段**；
4. 两条路都走不通时，**报错里不许出现内部 id**（模型会原样复述给用户）。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agent.citations import RefRegistry
from backend.agent.host import HostError
from backend.services.agent.project import anchor_ids, as_row_id


# --------------------------------------------------------------------------
# 1. 投影层：行 id 与块 id 分开下发
# --------------------------------------------------------------------------


def test_row_id_shaped_block_id_is_demoted_to_row_id() -> None:
    ids = anchor_ids({"block_id": "p017-00", "page_no": 17,
                      "metadata": {"row_ids": ["p017-00", "p017-01", "p017-02"],
                                   "row_span": ["p017-00", "p017-02"], "pages": [17]}})
    assert ids["block_id"] is None, "行 id 被当成 Block id 下发 → 下游必然 NOT_FOUND"
    assert ids["row_id"] == "p017-00"
    assert ids["row_ids"] == ["p017-00", "p017-01", "p017-02"]
    assert ids["page_no"] == 17
    assert as_row_id("row-p017-02") == "p017-02"


def test_real_block_id_is_kept() -> None:
    ids = anchor_ids({"block_id": "blk_7f3a", "page_no": 4, "metadata": {}})
    assert ids["block_id"] == "blk_7f3a"
    assert ids["row_id"] is None


def test_page_comes_from_metadata_when_the_chunk_has_no_page() -> None:
    ids = anchor_ids({"block_id": "p009-03", "metadata": {"pages": [9]}})
    assert ids["page_no"] == 9


# --------------------------------------------------------------------------
# 2~4. 工具层：块路径 / 行路径 / 兜底 / 失败文案
# --------------------------------------------------------------------------


class FakeHost:
    """只实现 block_get 用到的两条：`block` 与 `row`。"""

    def __init__(self, *, block_raises: HostError | None = None,
                 rows: dict[str, dict] | None = None) -> None:
        self.block_raises = block_raises
        self.rows = rows or {}
        self.block_calls: list[str] = []
        self.row_calls: list[tuple[int, str]] = []

    async def block(self, paper_id: str, block_id: str) -> dict:
        self.block_calls.append(block_id)
        if self.block_raises is not None:
            raise self.block_raises
        return {"page": 4, "section": "2", "type": "text",
                "text": "块路径的正文", "translation": "块路径的译文"}

    async def row(self, paper_id: str, page: int, row_id: str) -> dict:
        self.row_calls.append((page, row_id))
        return self.rows.get(row_id) or {}


class FakeDeps:
    def __init__(self, host) -> None:
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


def _cite(deps, **kwargs):
    """在引用表里登记一条（模拟 `paper_search` 的 `_register_chunk` 产出）。"""
    defaults = {"key": "k", "kind": "chunk", "snippet": "片段", "page": 17}
    defaults.update(kwargs)
    return deps.registry.assign(**defaults)


def _call_block_get(deps, ref: int):
    from backend.agent import tools as agent_tools

    return asyncio.run(agent_tools.block_get(_Ctx(deps), ref))


def test_row_id_hit_is_read_by_row_not_by_block() -> None:
    """行 id 形态的命中：走行路径，一次 `block` 都不打。"""
    host = FakeHost(rows={"p017-00": {"page": 17, "type": "text",
                                      "text": "第一行", "translation": "第一行译文"}})
    deps = FakeDeps(host)
    cit = _cite(deps, row_id="p017-00", page=17)
    out = _call_block_get(deps, cit.ref)
    assert host.block_calls == []
    assert host.row_calls == [(17, "p017-00")]
    assert out["text"] == "第一行"


def test_real_block_id_still_uses_the_block_path() -> None:
    host = FakeHost()
    deps = FakeDeps(host)
    cit = _cite(deps, block_id="blk_7f3a", page=4)
    out = _call_block_get(deps, cit.ref)
    assert host.block_calls == ["blk_7f3a"]
    assert host.row_calls == []
    assert out["text"] == "块路径的正文"


def test_block_404_falls_back_to_the_row_path_and_joins_the_span() -> None:
    """存量索引里 block_id 就是行 id：404 之后按行展开，**整段拼起来**（不是只给首行）。"""
    host = FakeHost(block_raises=HostError("主后端 HTTP 404", status=404, code="NOT_FOUND"),
                    rows={"p017-00": {"page": 17, "type": "text", "text": "第一行"},
                          "p017-01": {"page": 17, "type": "text", "text": "第二行"}})
    deps = FakeDeps(host)
    deps.row_spans["p017-00"] = ["p017-00", "p017-01"]
    cit = _cite(deps, block_id="p017-00", page=17)
    out = _call_block_get(deps, cit.ref)
    assert host.block_calls == ["p017-00"]
    assert host.row_calls == [(17, "p017-00"), (17, "p017-01")]
    assert out["text"] == "第一行\n\n第二行"
    assert "已按行展开" in out["note"]


def test_failure_message_never_leaks_internal_ids() -> None:
    host = FakeHost(block_raises=HostError("主后端 HTTP 404", status=404, code="NOT_FOUND"))
    deps = FakeDeps(host)
    cit = _cite(deps, block_id="p017-00", page=17)
    out = _call_block_get(deps, cit.ref)
    assert out["ok"] is False
    assert "p017-00" not in out["error"], "内部 id 泄给模型，它会原样复述给用户"
    assert "ref=" not in out["error"]
    assert "第 17 页" in out["error"], "只给死路没有用，要告诉它去读哪一页"


def test_non_row_shaped_block_id_404_is_still_an_error() -> None:
    """真块 id 的 404 不该被行兜底吞掉：那说明块真没了，让调用方看到失败。"""
    host = FakeHost(block_raises=HostError("主后端 HTTP 404", status=404, code="NOT_FOUND"))
    deps = FakeDeps(host)
    cit = _cite(deps, block_id="blk_gone", page=4)
    out = _call_block_get(deps, cit.ref)
    assert out["ok"] is False          # _guarded 把 HostError 变成失败结果
    assert host.row_calls == []
