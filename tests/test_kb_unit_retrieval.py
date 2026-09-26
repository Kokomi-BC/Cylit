"""检索候选集（⑦）与 Small-to-Big 上下文扩展（⑧）的离线单测。

两者都是"策略层"，全部用 monkeypatch 顶掉向量库/设置，不连数据库、不调模型：

- `retrieval.search`：默认候选集 = `unit, formula, figure, table`（正文 chunk 不进候选，
  否则同一段内容两条命中）；过滤后空结果 → 自动放开全部类型重搜一次；
- `rag_service.build_context`：命中单元 → 扩到 ±1 邻居（`kb.unit_expand`），**按父 chunk 去重**，
  超预算就退回"只用命中单元"。
"""

from __future__ import annotations

from backend.services.knowledge import rag_service, retrieval


# --------------------------------------------------------------------------
# ⑦ 默认候选集与空结果兜底
# --------------------------------------------------------------------------


def _fake_hits(rows_by_type: dict[str, list[dict]]):
    """按 `chunk_types` 过滤的假检索（向量/词法共用）。"""
    def _fn(query, *, chunk_types=None, **kw):
        if not chunk_types:
            return [r for rows in rows_by_type.values() for r in rows]
        return [r for t in chunk_types for r in rows_by_type.get(t, [])]
    return _fn


def test_default_candidates_exclude_parent_text_chunks(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_vector(query, *, chunk_types=None, **kw):
        calls.append(tuple(chunk_types or ()))
        return _fake_hits({
            "unit": [{"chunk_id": "u1", "chunk_type": "unit", "content": "unit"}],
            "text": [{"chunk_id": "t1", "chunk_type": "text", "content": "parent"}],
        })(query, chunk_types=chunk_types)

    monkeypatch.setattr(retrieval, "vector_search", fake_vector)
    monkeypatch.setattr(retrieval, "lexical_search", lambda q, **kw: [])
    monkeypatch.setattr(retrieval, "rerank_hits",
                        lambda q, cands, **kw: (list(cands), {"degraded": False}))
    _no_settings(monkeypatch)
    monkeypatch.setattr(retrieval, "_has_chunk_type", lambda t: True)   # 已建单元层

    result = retrieval.search("问题", top_k=5)
    assert calls and calls[0] == retrieval.DEFAULT_SEARCH_TYPES
    assert [i["chunk_id"] for i in result["items"]] == ["u1"]
    assert result["retrieval"]["type_fallback"] is False
    assert result["retrieval"]["chunk_types"] == list(retrieval.DEFAULT_SEARCH_TYPES)


def test_default_candidates_fall_back_when_empty(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_vector(query, *, chunk_types=None, **kw):
        calls.append(tuple(chunk_types or ()))
        if chunk_types:                        # 老索引：没有 unit 行
            return []
        return [{"chunk_id": "t1", "chunk_type": "text", "content": "parent"}]

    monkeypatch.setattr(retrieval, "vector_search", fake_vector)
    monkeypatch.setattr(retrieval, "lexical_search", lambda q, **kw: [])
    monkeypatch.setattr(retrieval, "rerank_hits",
                        lambda q, cands, **kw: (list(cands), {"degraded": False}))
    _no_settings(monkeypatch)
    monkeypatch.setattr(retrieval, "_has_chunk_type", lambda t: True)

    result = retrieval.search("问题", top_k=5)
    assert calls == [retrieval.DEFAULT_SEARCH_TYPES, ()]
    assert [i["chunk_id"] for i in result["items"]] == ["t1"]
    assert result["retrieval"]["type_fallback"] is True
    assert any("放开全部类型" in n for n in result["retrieval"]["notes"])


def test_old_index_without_units_keeps_all_types(monkeypatch):
    """老索引还没建单元层：默认候选集不能生效（否则正文也被排除，检索反而变差）。"""
    calls: list[tuple[str, ...]] = []

    def fake_vector(query, *, chunk_types=None, **kw):
        calls.append(tuple(chunk_types or ()))
        return [{"chunk_id": "t1", "chunk_type": "text", "content": "parent"}]

    monkeypatch.setattr(retrieval, "vector_search", fake_vector)
    monkeypatch.setattr(retrieval, "lexical_search", lambda q, **kw: [])
    monkeypatch.setattr(retrieval, "rerank_hits",
                        lambda q, cands, **kw: (list(cands), {"degraded": False}))
    _no_settings(monkeypatch)
    monkeypatch.setattr(retrieval, "_has_chunk_type", lambda t: False)

    result = retrieval.search("问题", top_k=5)
    assert calls == [()], "没有单元层 → 不限类型，一次搜完"
    assert [i["chunk_id"] for i in result["items"]] == ["t1"]
    assert any("还没有 unit 行" in n for n in result["retrieval"]["notes"])


def test_explicit_chunk_types_are_respected(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_vector(query, *, chunk_types=None, **kw):
        calls.append(tuple(chunk_types or ()))
        return []

    monkeypatch.setattr(retrieval, "vector_search", fake_vector)
    monkeypatch.setattr(retrieval, "lexical_search", lambda q, **kw: [])
    monkeypatch.setattr(retrieval, "rerank_hits",
                        lambda q, cands, **kw: (list(cands), {"degraded": False}))
    _no_settings(monkeypatch)
    monkeypatch.setattr(retrieval, "_has_chunk_type", lambda t: True)

    result = retrieval.search("问题", chunk_types=["formula"], top_k=3)
    assert calls == [("formula",)]          # 显式指定 → 不兜底、不二次搜索
    assert result["retrieval"]["type_fallback"] is False


def test_retrieval_types_setting_overrides_default(monkeypatch):
    """`kb.retrieval_types` 覆盖默认候选集。

    这条以前读的是**裸键** `retrieval_types`，而文档写的是 `kb.retrieval_types` ——
    于是"文档承诺可调的开关"从来没生效过。现在两种写法都认，`kb.` 优先。
    """
    monkeypatch.setattr(retrieval, "_setting_raw",
                        lambda key: "unit,text" if key == "kb.retrieval_types"
                        else retrieval._MISSING)
    assert retrieval.default_search_types() == ["unit", "text"]


# --------------------------------------------------------------------------
# ⑧ Small-to-Big：±1 邻居 + 按父去重 + 预算
# --------------------------------------------------------------------------


def _unit(chunk_id: str, *, parent: str, prev: str | None, next_: str | None,
          content: str, lang: str = "en", section: str = "§1") -> dict:
    return {"chunk_id": chunk_id, "paper_id": "p", "paper_title": "P", "page": 1,
            "section": section, "chunk_type": "unit", "lang": lang, "content": content,
            "snippet": content[:120], "scores": {"rerank": 0.5},
            "metadata": {"parent_chunk_id": parent, "prev_unit_id": prev,
                         "next_unit_id": next_}}


def _install_store(monkeypatch, rows: dict[str, str]):
    def get_rows(ids):
        return [{"chunk_id": i, "content": rows[i], "chunk_type": "unit",
                 "metadata": {}, "lang": "en"} for i in ids if i in rows]
    monkeypatch.setattr(rag_service.vector_store, "get_rows", get_rows)


def _no_settings(monkeypatch) -> None:
    """setting 表里什么行都没有 → 一律走默认值。

    走**真实解析入口** `retrieval._setting_raw`（唯一的数据库入口），而不是去 patch
    某个具体读取点：patch 读取点会掩盖"读取点写错了键名"这类 bug（就是本次修掉的那个）。
    """
    monkeypatch.setattr(retrieval, "_setting_raw",
                        lambda key: retrieval._MISSING)


def _set_unit_expand(monkeypatch, value: int) -> None:
    """把 `kb.unit_expand` 钉成给定值（用文档里的写法，顺带验证前缀解析）。"""
    monkeypatch.setattr(
        retrieval, "_setting_raw",
        lambda key: value if key == f"{retrieval.SETTING_PREFIX}unit_expand"
        else retrieval._MISSING)


def test_expand_includes_prev_and_next_unit(monkeypatch):
    _install_store(monkeypatch, {"u1": "UNIT1", "u0": "PREV", "u2": "NEXT",
                                 "u1:zh": "", "u0:zh": "", "u2:zh": ""})
    _set_unit_expand(monkeypatch, 1)
    items = [_unit("u1", parent="p:text:a", prev="u0", next_="u2", content="UNIT1")]
    out = rag_service._expand_units(items, max_chars=1000)
    assert len(out) == 1
    assert out[0]["content"] == "PREV\n\nUNIT1\n\nNEXT"
    assert out[0]["metadata"]["_expand_mode"] == 1
    assert out[0]["metadata"]["_expanded_units"] == ["u0", "u1", "u2"]


def test_expand_dedupes_hits_sharing_the_same_parent(monkeypatch):
    _install_store(monkeypatch, {"u1": "UNIT1", "u2": "UNIT2",
                                 "u1:zh": "", "u2:zh": ""})
    _set_unit_expand(monkeypatch, 1)
    items = [_unit("u1", parent="same", prev=None, next_="u2", content="UNIT1"),
             _unit("u2", parent="same", prev="u1", next_=None, content="UNIT2")]
    out = rag_service._expand_units(items, max_chars=1000)
    assert len(out) == 1, "同一父块的两条命中必须合并成一个来源（白烧预算）"
    assert out[0]["content"] == "UNIT1\n\nUNIT2"
    assert out[0]["metadata"]["_expanded_units"] == ["u1", "u2"]


def test_expand_mode_zero_is_noop(monkeypatch):
    _install_store(monkeypatch, {"u1": "UNIT1"})
    _set_unit_expand(monkeypatch, 0)
    items = [_unit("u1", parent="p", prev="u0", next_="u2", content="UNIT1")]
    out = rag_service._expand_units(items, max_chars=1000)
    assert out[0]["content"] == "UNIT1"
    assert (out[0]["metadata"] or {}).get("_expand_mode") in (None, 0)


def test_expand_mode_two_uses_parent_chunk(monkeypatch):
    _install_store(monkeypatch, {"u1": "UNIT1", "p:text:a": "PARENT-CHUNK"})
    _set_unit_expand(monkeypatch, 2)
    items = [_unit("u1", parent="p:text:a", prev=None, next_=None, content="UNIT1")]
    out = rag_service._expand_units(items, max_chars=1000)
    assert out[0]["content"] == "PARENT-CHUNK"
    assert out[0]["metadata"]["_expand_mode"] == 2


def test_expand_respects_budget(monkeypatch):
    _install_store(monkeypatch, {"u1": "UNIT1", "u0": "P" * 400, "u2": "N" * 400,
                                 "u0:zh": "", "u1:zh": "", "u2:zh": ""})
    _set_unit_expand(monkeypatch, 1)
    items = [_unit("u1", parent="p", prev="u0", next_="u2", content="UNIT1")]
    out = rag_service._expand_units(items, max_chars=200)
    assert out[0]["content"] == "UNIT1", "超预算 → 退回只用命中单元"
    assert out[0]["metadata"]["_expand_mode"] == "1-budget-skip"


def test_expand_leaves_atomic_chunks_untouched(monkeypatch):
    _install_store(monkeypatch, {})
    _set_unit_expand(monkeypatch, 1)
    items = [{"chunk_id": "p:formula:f001:any", "chunk_type": "formula",
              "content": "$$a=b$$", "lang": "any", "metadata": {}}]
    out = rag_service._expand_units(items, max_chars=1000)
    assert out[0]["content"] == "$$a=b$$"


def test_build_context_expands_and_reports(monkeypatch):
    _install_store(monkeypatch, {"u1": "UNIT1", "u0": "PREV", "u2": "NEXT",
                                 "u1:zh": "单元一", "u0:zh": "上一段", "u2:zh": "下一段"})
    _set_unit_expand(monkeypatch, 1)
    items = [_unit("u1", parent="p:text:a", prev="u0", next_="u2", content="UNIT1",
                   lang="zh")]
    context = rag_service.build_context(items, max_chars=5000)
    assert context["sources"], "至少要出一条来源"
    source = context["sources"][0]
    assert source["chunk_type"] == "unit" and source["expand_mode"] == 1
    assert source["expanded_units"] == ["u0", "u1", "u2"]
    assert "上一段" in context["text"] and "下一段" in context["text"]
