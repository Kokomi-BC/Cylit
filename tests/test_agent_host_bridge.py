"""HostClient 的方法名唯一性 + `notes_search` 的接线（纯离线）。

为什么值得一条专门的测试：`HostClient` 里曾经**同名定义了两个 `notes`** —— 库级那条
（带 `kind` 过滤）与单篇那条（带 `note_id`）。Python 的类体按顺序赋值，**后定义的静默覆盖
前者**：`notes_search` 传 `kind=` 必然 `TypeError`，而"方法存在、源码里也看得到签名"这个
静态印象让人以为一切正常（2026-12 实测：整条库级笔记检索不可用，模型一句"我记不起来"）。

所以这里钉两件事：① 类体里不许有同名方法（这是那次故障的形状）；② `notes` 是**库级**那条
（收 `kind`），单篇那条叫 `paper_notes`，并且 `notes_search` 真的走库级那条。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

from backend.agent import host as host_mod
from backend.agent.host import HostClient

#: 工具层要用到的 deps 面很小：host / paper_id / registry / count_search / note_tool
from backend.agent.citations import RefRegistry


def _host_client_methods() -> list[str]:
    tree = ast.parse(Path(host_mod.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "HostClient":
            return [item.name for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return []


def test_host_client_has_no_duplicate_method_names() -> None:
    """同名方法会被**静默覆盖**，所以名字必须唯一（这是 notes 那次故障的形状）。"""
    names = _host_client_methods()
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == [], f"HostClient 里有重名方法（后定义的会覆盖前者）：{duplicates}"


def test_notes_is_the_library_level_bridge() -> None:
    """库级那条必须叫 `notes` 且收 `kind` —— `notes_search` 按这个名字调它。"""
    params = inspect.signature(HostClient.notes).parameters
    assert "kind" in params, "HostClient.notes 收不到 kind：库级笔记检索会直接 TypeError"
    assert "paper_id" in params


def test_paper_notes_is_the_single_paper_bridge() -> None:
    params = inspect.signature(HostClient.paper_notes).parameters
    assert "note_id" in params
    assert "kind" not in params, "单篇那条不该有 kind（那是库级过滤）"


class _FakeHost:
    """照真实签名实现库级那条：只收 keyword 参数，记下每次调用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def notes(self, *, query=None, paper_id=None, kind="all"):
        self.calls.append({"query": query, "paper_id": paper_id, "kind": kind})
        return {"items": [{
            "kind": "highlight", "paper_id": "p1", "paper_title": "某篇",
            "selected_text": "attention", "note": "", "block_id": "b1", "tags": [],
        }]}


class _Deps:
    def __init__(self, host: _FakeHost) -> None:
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


def _run_notes_search(**kwargs):
    from backend.agent import tools as agent_tools

    ctx = _Ctx(_Deps(_FakeHost()))
    return ctx, asyncio.run(agent_tools.notes_search(ctx, **kwargs))


def test_notes_search_reaches_the_kind_aware_bridge() -> None:
    """`notes_search(kind=...)` 必须落到库级那条（带 kind 的），不是单篇那条。"""
    host = _FakeHost()
    from backend.agent import tools as agent_tools

    ctx = _Ctx(_Deps(host))
    out = asyncio.run(agent_tools.notes_search(ctx, query="attention", kind="highlights"))

    assert host.calls == [{"query": "attention", "paper_id": "p1", "kind": "highlights"}]
    assert out["returned"] == 1
    assert out["items"][0]["kind"] == "highlight"


def test_notes_search_scope_all_drops_the_paper_filter() -> None:
    host = _FakeHost()
    from backend.agent import tools as agent_tools

    asyncio.run(agent_tools.notes_search(_Ctx(_Deps(host)), query="", scope="all"))
    assert host.calls[0]["paper_id"] is None
