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
