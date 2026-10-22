"""检索方式（`paper_search` 的 text / rag / auto）的离线测试。

为什么要专门钉这三层：这条链上**每一层都能静默失效** ——

1. `retrieval.search` 如果无视 `sources`，"只搜词法"其实仍在跑向量，结果只是"好像没那么准"；
2. `project.search` 的 `select` 映射写错（比如 rag → lexical），同样没有报错；
3. 工具层的缓存键如果只有 (query, top_k)，text 与 rag 的结果会互相串味，模型会得出
   "换个方式也搜不到"的错误结论（上一轮在 `need_content` 上踩过同一个坑）。

所以这里逐层计数/记录参数，不依赖任何模型与索引。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.errors import ValidationError
from backend.services.knowledge import retrieval
from backend.services.agent import project


def _hit(cid: str) -> dict:
    return {"chunk_id": cid, "paper_id": "p1", "chunk_type": "unit", "page_no": 1,
            "block_id": f"b-{cid}", "section": "1", "lang": "en",
            "content": "Attention is all you need", "metadata": {}}


@pytest.fixture
def counted(monkeypatch):
    """把两路召回换成计数器；重排原样吃融合结果，避免触发"空结果兜底重搜"。"""
    seen = {"vector": 0, "lexical": 0}

    def fake_vector(query, **kwargs):
        seen["vector"] += 1
        return [_hit("v1")]

    def fake_lexical(query, **kwargs):
        seen["lexical"] += 1
        return [_hit("l1")]

    def fake_rerank(query, fused, **kwargs):
        return ([{**fused[0], "rerank_score": 1.0}] if fused else []), {"degraded": False}

    monkeypatch.setattr(retrieval, "vector_search", fake_vector)
    monkeypatch.setattr(retrieval, "lexical_search", fake_lexical)
    monkeypatch.setattr(retrieval, "rerank_hits", fake_rerank)
    monkeypatch.setattr(retrieval, "default_search_types", lambda: ["unit"])
    monkeypatch.setattr(retrieval, "_has_chunk_type", lambda chunk_type: True)
    return seen


# --------------------------------------------------------------------------
# 1. retrieval.search 的 sources
# --------------------------------------------------------------------------


def test_vector_only_never_calls_lexical(counted):
    result = retrieval.search("attention", sources=["vector"])
    assert counted == {"vector": 1, "lexical": 0}
    assert result["retrieval"]["vector_hits"] == 1
    assert result["retrieval"]["lexical_hits"] == 0


def test_lexical_only_never_calls_vector(counted):
    result = retrieval.search("attention", sources=["lexical"])
    assert counted == {"vector": 0, "lexical": 1}
    assert result["retrieval"]["lexical_hits"] == 1


@pytest.mark.parametrize("sources", [None, []])
def test_default_runs_both_paths(counted, sources):
    """不传与空列表都等于"两路都跑"（空列表不当成"一路都不要"）。"""
    retrieval.search("attention", sources=sources)
    assert counted == {"vector": 1, "lexical": 1}


@pytest.mark.parametrize("sources", [["semantic"], ["vector", "bm25"], ["VECTOR!"]])
def test_unknown_source_is_rejected(sources):
    with pytest.raises(ValidationError, match="vector / lexical"):
        retrieval.search("attention", sources=sources)


def test_both_sources_explicitly_still_runs_both(counted):
    retrieval.search("attention", sources=["vector", "lexical"])
    assert counted == {"vector": 1, "lexical": 1}


def test_rrf_fuse_is_safe_with_a_single_path():
    """单路喂给融合是安全的：分数就是 1/(k+rank)，不会因为"另一路 0 条"被归一化吃掉。"""
    fused = retrieval.rrf_fuse({"vector": [_hit("v1"), _hit("v2")]})
    assert [node["chunk_id"] for node in fused] == ["v1", "v2"]
    assert fused[0]["sources"] == {"vector": 1}
    assert fused[0]["rrf"] > fused[1]["rrf"] > 0


# --------------------------------------------------------------------------
# 2. project.search 的 select → sources 映射
# --------------------------------------------------------------------------


@pytest.fixture
def captured(monkeypatch):
    """记录 project.search 传给 retrieval 的 sources，并回一个最小结果。"""
    seen: dict = {}

    def fake_search(query, **kwargs):
        seen.update(kwargs)
        return {"items": [], "retrieval": {"vector_hits": 2, "lexical_hits": 3,
                                           "fused": 4, "rerank": {"degraded": False}}}

    monkeypatch.setattr(retrieval, "search", fake_search)
    monkeypatch.setattr(project.paper_service, "get_paper",
                        lambda paper_id: {"id": paper_id, "page_count": 12})
    return seen


@pytest.mark.parametrize("select,sources", [("rag", ["vector"]),
                                            ("text", ["lexical"]),
                                            ("auto", None),
                                            (" AUTO ", None)])
def test_select_maps_to_the_right_sources(captured, select, sources):
    out = project.search("p1", "attention", select=select)
    assert captured["sources"] == sources
    # mode 仍然是"查询指令口径"，不跟着 select 变
    assert captured["mode"] == "qa"
    assert out["select"] == select.strip().lower()
    assert out["retrieval"]["vector_hits"] == 2
    assert out["retrieval"]["lexical_hits"] == 3


def test_select_defaults_to_auto(captured):
    assert project.search("p1", "attention")["select"] == "auto"
    assert captured["sources"] is None


@pytest.mark.parametrize("value", ["semantic", "hybrid", "vector"])
def test_unknown_select_is_rejected_with_the_legal_values(captured, value):
    with pytest.raises(ValidationError) as err:
        project.search("p1", "attention", select=value)
    assert "auto" in err.value.message and "rag" in err.value.message
    assert "text" in err.value.message


# --------------------------------------------------------------------------
# 3. 工具层：mode 透传 + 缓存键分槽
# --------------------------------------------------------------------------


class FakeHost:
    """按 host.py 的同一条过滤规则（`v not in (None, "")`）记录 payload。"""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def search(self, paper_id, query, *, top_k, chunk_types=None, mode=None):
        payload = {k: v for k, v in (("query", query), ("top_k", top_k),
                                     ("chunk_types", chunk_types), ("mode", mode))
                   if v not in (None, "")}
        self.payloads.append(payload)
        return {"page_total": 12, "items": [{"chunk_id": f"c{len(self.payloads)}",
                                             "block_id": "b1", "page_no": 3,
                                             "section": "2", "chunk_type": "unit",
                                             "content": "snippet"}]}


class FakeDeps:
    def __init__(self, host) -> None:
        from backend.agent.citations import RefRegistry

        self.host = host
        self.paper_id = "p1"
        self.registry = RefRegistry("p1")
        self.query_cache: dict = {}
        self.page_total = 0

    def count_search(self) -> None:
        return None

    def note_tool(self, *args, **kwargs) -> None:
        return None


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def _call(ctx, **kwargs):
    from backend.agent import tools as agent_tools

    return asyncio.run(agent_tools.paper_search(ctx, "attention", **kwargs))


def test_mode_is_only_sent_when_the_model_asks_for_it():
    host = FakeHost()
    out = _call(_Ctx(FakeDeps(host)))
    assert "mode" not in host.payloads[0]
    assert out["returned"] == 1


def test_mode_is_passed_through():
    host = FakeHost()
    _call(_Ctx(FakeDeps(host)), mode="rag")
    assert host.payloads[0]["mode"] == "rag"


def test_cache_key_changes_with_mode():
    """三种方式各占一个缓存槽：同一 query 换方式必须真的再打一次后端。"""
    host = FakeHost()
    ctx = _Ctx(FakeDeps(host))
    _call(ctx)
    _call(ctx, mode="text")
    _call(ctx, mode="rag")
    assert len(host.payloads) == 3
    # 同一种方式重复调用才命中缓存
    _call(ctx, mode="text")
    assert len(host.payloads) == 3
