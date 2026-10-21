"""没有 vision 模型时，页级翻译**不许**在开始前就挂。

真实事故（2026-12-04）：新导入的论文在没有配置 vision 模型时，`TRANSCRIBE_EN` 与
`TRANSLATE_ZH` 两条任务都直接 0/15 失败，错误是「未配置可用的 vision 模型」。
翻译本身有纯文本 generation 的退路（`_path_provider` 会 catch 并退回），但**输出预算**
那一行直接去取 vision 配置、没兜住 —— 于是退路根本没机会生效。
"""

from __future__ import annotations

from backend.core.errors import ModelNotConfiguredError
from backend.services.translation import translation_service as ts


class _FakeModelManager:
    def __init__(self, available: dict[str, dict]) -> None:
        self.available = available
        self.asked: list[str] = []

    def get_config(self, model_id, mtype):
        self.asked.append(mtype)
        if mtype in self.available:
            return self.available[mtype]
        raise ModelNotConfiguredError(f"未配置可用的 {mtype} 模型")


def test_falls_back_to_generation_when_vision_is_missing(monkeypatch) -> None:
    fake = _FakeModelManager({"generation": {"parameters": {"max_tokens": 8192}}})
    monkeypatch.setattr(ts, "model_manager", fake)
    assert ts._max_tokens_hint("a") == 8192
    assert fake.asked == ["vision", "generation"], "先问 vision，再退回 generation"


def test_vision_wins_when_configured(monkeypatch) -> None:
    fake = _FakeModelManager({"vision": {"parameters": {"max_tokens": 4096}},
                              "generation": {"parameters": {"max_tokens": 8192}}})
    monkeypatch.setattr(ts, "model_manager", fake)
    assert ts._max_tokens_hint(None) == 4096
    assert fake.asked == ["vision"]


def test_none_when_nothing_is_configured(monkeypatch) -> None:
    fake = _FakeModelManager({})
    monkeypatch.setattr(ts, "model_manager", fake)
    assert ts._max_tokens_hint("b") is None
    assert fake.asked == ["vision", "generation"]
