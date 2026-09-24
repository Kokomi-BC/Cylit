"""默认模型选择（纯函数，离线）：同一类型出现多个 `is_default` 时谁说了算。

真实事故：内置的 `heuristic-reranker` 被重新插入时又盖了一次 `is_default`，
而 `get_config` 是按 id 排序取第一个 → 取到启发式 → **重排在用户完全无感的情况下静默降级**
（检索质量掉了，界面只显示一个"提示"）。这里把保留规则锁住。
"""

from __future__ import annotations

from backend.services.model.model_manager import MOCK_PROVIDERS, pick_default


def _cfg(mid: str, provider: str, status: str = "unknown") -> dict:
    return {"id": mid, "provider": provider, "status": status,
            "model_type": "reranker", "enabled": True}


def test_pick_default_prefers_real_model_over_builtin() -> None:
    chosen = pick_default([
        _cfg("heuristic-reranker", "heuristic", "ok"),      # id 排序在前，但它是降级档
        _cfg("reranker-94cd6b92", "dashscope", "ok"),
    ])
    assert chosen["id"] == "reranker-94cd6b92"


def test_pick_default_prefers_tested_ok_among_real_models() -> None:
    chosen = pick_default([
        _cfg("reranker-a", "dashscope", "error"),
        _cfg("reranker-b", "dashscope", "ok"),
        _cfg("reranker-c", "dashscope", "unknown"),
    ])
    assert chosen["id"] == "reranker-b"


def test_pick_default_falls_back_to_builtin_when_only_builtin() -> None:
    assert pick_default([_cfg("heuristic-reranker", "heuristic")])["id"] == "heuristic-reranker"
    assert pick_default([]) is None


def test_pick_default_is_deterministic() -> None:
    rows = [_cfg("reranker-b", "dashscope", "ok"), _cfg("reranker-a", "dashscope", "ok")]
    assert pick_default(rows)["id"] == "reranker-a"          # 同档时按 id 稳定
    assert pick_default(list(reversed(rows)))["id"] == "reranker-a"


def test_mock_providers_cover_every_offline_fallback() -> None:
    """降级档名单要和工厂里那几条离线 provider 对齐（漏一个就会出现"mock 压过真模型"）。"""
    from backend.providers.factory import EMBEDDING_PROVIDERS, GENERATION_PROVIDERS, RERANKER_PROVIDERS

    for name, cls in {**EMBEDDING_PROVIDERS, **GENERATION_PROVIDERS, **RERANKER_PROVIDERS}.items():
        if cls.__name__ in ("MockEmbedding", "MockGeneration", "HeuristicReranker"):
            assert name in MOCK_PROVIDERS, f"{name} 是降级档但不在 MOCK_PROVIDERS 里"


# --------------------------------------------------------------------------
# 内置占位模型：删掉之后**不再自动补回来**（用户报过"删不掉的占位模型"）
# --------------------------------------------------------------------------


def test_missing_builtins_skips_suppressed_ids() -> None:
    from backend.services.model.model_manager import builtin_ids, missing_builtins

    ids = builtin_ids()
    assert {"mock-generation", "mock-embedding", "heuristic-reranker", "mock-vision"} <= ids

    # 空库、没删过任何东西 → 四个内置档都要补
    assert {d["id"] for d in missing_builtins(set(), set())} == ids
    # 用户删过 mock-vision → 它不再被补；其余照旧
    todo = {d["id"] for d in missing_builtins(set(), {"mock-vision"})}
    assert todo == ids - {"mock-vision"}
    # 已经存在的也不重复插
    todo = {d["id"] for d in missing_builtins(ids - {"mock-vision"}, set())}
    assert todo == {"mock-vision"}


def test_parse_suppressed_tolerates_bad_values() -> None:
    from backend.services.model.model_manager import parse_suppressed

    assert parse_suppressed(None) == set()
    assert parse_suppressed("") == set()
    assert parse_suppressed("not-json") == set()
    assert parse_suppressed({"a": 1}) == set()          # 结构不对 → 当空，不抛
    assert parse_suppressed('["mock-vision", 3, null, ""]') == {"mock-vision"}
    assert parse_suppressed(["mock-vision", "mock-generation"]) == {"mock-vision", "mock-generation"}


def test_builtin_rows_are_marked_in_config() -> None:
    """界面上要能区分"内置占位"，才谈得上给出"删掉不会再自动重建"的提示。"""
    from backend.services.model.model_manager import ModelManager

    class Row:
        id = "mock-vision"
        name = "离线占位视觉模型（Mock）"
        provider = "mock"
        model_type = "vision"
        model_name = "mock-vision"
        base_url = None
        device = None
        parameters_json = "{}"
        secret_ref = None
        enabled = True
        is_default = False
        status = "unknown"

    assert ModelManager()._row_to_config(Row())["builtin"] is True
    Row.id = "vision-d978f800"
    assert ModelManager()._row_to_config(Row())["builtin"] is False
