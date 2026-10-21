"""页级翻译**只问 generation**（纯文本）：读图模型已彻底移出翻译链路。

历史事故（2026-12-04）：新导入的论文在没有配置读图模型时，`TRANSCRIBE_EN` 与
`TRANSLATE_ZH` 两条任务都直接 0/15 失败，错误是「未配置可用的 … 模型」。
当时的修法是"优先读图模型、失败再退回 generation"，于是整条链路仍然**依赖**
读图模型的有无。现在的口径是：翻译/转录是纯文本任务，链路里只有 generation，
读图模型一个都不问（`get_vision` 换成"一调就抛"的桩也照跑）。
"""

from __future__ import annotations

from backend.core.errors import ModelNotConfiguredError
from backend.services.translation import translation_service as ts


class _FakeModelManager:
    """翻译链路会用到的 model_manager 门面；`get_vision` 一调就炸。"""

    def __init__(self, available: dict[str, dict]) -> None:
        self.available = available
        self.asked: list[str] = []
        self.vision_calls = 0

    def get_config(self, model_id, model_type="generation"):
        self.asked.append(model_type)
        if model_type in self.available:
            return self.available[model_type]
        raise ModelNotConfiguredError(f"未配置可用的 {model_type} 模型")

    def get_generation(self, model_id=None):
        return self.get_config(model_id, "generation")

    def get_vision(self, model_id=None):
        self.vision_calls += 1
        raise AssertionError("翻译链路不该调用 get_vision")


def test_max_tokens_hint_asks_generation_only(monkeypatch) -> None:
    fake = _FakeModelManager({"generation": {"parameters": {"max_tokens": 8192}}})
    monkeypatch.setattr(ts, "model_manager", fake)
    assert ts._max_tokens_hint("a") == 8192
    assert fake.asked == ["generation"], "只问 generation，不再先问读图模型"


def test_generation_wins_even_when_a_vision_model_exists(monkeypatch) -> None:
    """配了读图模型也不影响预算：翻译用的就是 generation 那一行。"""
    fake = _FakeModelManager({"vision": {"parameters": {"max_tokens": 4096}},
                              "generation": {"parameters": {"max_tokens": 8192}}})
    monkeypatch.setattr(ts, "model_manager", fake)
    assert ts._max_tokens_hint(None) == 8192
    assert fake.asked == ["generation"]


def test_none_when_no_generation_is_configured(monkeypatch) -> None:
    fake = _FakeModelManager({})
    monkeypatch.setattr(ts, "model_manager", fake)
    assert ts._max_tokens_hint("b") is None
    assert fake.asked == ["generation"]


def test_translation_chain_never_touches_a_vision_model(monkeypatch) -> None:
    """判据：把 `get_vision` 换成"一调就抛"的桩，翻译侧三个入口照样正常返回。"""
    gen = {"id": "gen-1", "parameters": {"max_tokens": 8192}}
    fake = _FakeModelManager({"generation": gen})
    monkeypatch.setattr(ts, "model_manager", fake)

    assert ts._path_provider("a") is gen
    assert ts._max_tokens_hint("a") == 8192
    assert ts._current_model_id("a") == "gen-1"

    assert fake.vision_calls == 0, "读图模型已彻底移出翻译链路"
    assert "vision" not in fake.asked
