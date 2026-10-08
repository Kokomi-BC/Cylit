"""理解阶段的输出预算与**重试**：基线是厂商上限，"正文为空"改成同预算再要一次。

## 为什么要单独立一条

实测（turboquant，A 路精读，2026-10-08 用户报的那条）：
模型只吐思维链、`finish=stop`、正文为空 → 老代码里这条错误 `retryable=False`，
而理解是"一次调用 = 整个任务"、任务级失败**不会被重新排队**
（`task_service._execute` 直接 `finish(FAILED)`）→ 任务**永久失败**。
同一份任务在相邻两次运行里都成功，纯粹是模型偶发"只想不写"。

所以现在：预算基线仍是模型上限（`MAX_OUTPUT_TOKENS` = 384K，用户口径"不应该有 token 生成限制"），
**可重试的错误在同预算下最多再试 `_ATTEMPTS` 次**，不可重试的错误（密钥/400）一次就抛。
离线单测必须用假 provider 把两种响应都造出来，否则以后改坏没人知道。
"""
from __future__ import annotations

import pytest

from backend.core.errors import ProviderError
from backend.services.understanding import understanding_service as us

_REASONING_ONLY = ("模型只产出思维链（reasoning 8000 tokens，finish=stop）而正文为空"
                   "（本次 max_tokens=393216）：这次是模型想完就停了、没写正文（偶发）。")


class _Res:
    def __init__(self, text: str, model: str = "fake") -> None:
        self.text = text
        self.model = model
        self.prompt_tokens = 10
        self.completion_tokens = 20


class _Provider:
    """前 `fail_times` 次按 `msg` 报错（默认"只出思维链"）。"""

    model_name = "fake"

    def __init__(self, fail_times: int = 1, msg: str = _REASONING_ONLY,
                 retryable: bool = True) -> None:
        self.calls: list[int] = []
        self._fail_times = fail_times
        self._msg = msg
        self._retryable = retryable

    def generate(self, prompt, *, system=None, temperature=0.0, max_tokens=0):  # noqa: ANN001
        self.calls.append(max_tokens)
        if len(self.calls) <= self._fail_times:
            raise ProviderError(self._msg, retryable=self._retryable)
        return _Res('## 技术解剖\n正文（来源 Chunk: A001）\n```json\n{"refs": ["A001"]}\n```')


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """重试之间的等待在单测里没意义（也不该让测试变慢）。"""
    monkeypatch.setattr(us.time, "sleep", lambda _s: None)


def test_budget_is_vendor_max() -> None:
    """每次调用都带模型自己的上限（官方"输出长度 最大 384K"），不再出现"8192 起步、逐级放大"。"""
    from backend.services.translation.page_translate import MAX_OUTPUT_TOKENS

    provider = _Provider(fail_times=0)
    us._generate(provider, "p", system="s", path="a")
    assert provider.calls == [MAX_OUTPUT_TOKENS] == [393216]


def test_reasoning_only_is_retried_at_the_same_budget() -> None:
    """"只出思维链"是偶发 → **同预算**再要一次就能成功（这是用户报的那条失败）。"""
    provider = _Provider(fail_times=1)
    res = us._generate(provider, "p", system="s", path="a")
    assert res.text.startswith("## 技术解剖")
    assert provider.calls == [393216, 393216], "重试必须同预算（上限已经抬不动）"


def test_retry_is_bounded() -> None:
    """一直只想不写 → 试满 `_ATTEMPTS` 次才抛（不会无限烧调用）。"""
    provider = _Provider(fail_times=99)
    with pytest.raises(ProviderError):
        us._generate(provider, "p", system="s", path="a")
    assert len(provider.calls) == us._ATTEMPTS == 3


def test_non_retryable_errors_are_not_retried() -> None:
    """密钥错 / 400 这类不可重试的错误一次就抛（重试没有意义，只是白烧）。"""
    provider = _Provider(fail_times=9, msg="HTTP 401 Unauthorized", retryable=False)
    with pytest.raises(ProviderError):
        us._generate(provider, "p", system="s", path="a")
    assert len(provider.calls) == 1


def test_provider_marks_reasoning_only_as_retryable() -> None:
    """**根因那一处**：`finish=stop` 的"只出思维链"过去标成不可重试 → 任务永久失败。

    现在两种形态（`length` / `stop`）都标可重试，并且错误里**不许**再出现
    "请提高 max_tokens（建议 ≥8192）"这种把人带偏的建议（预算本来就是模型上限）。
    """
    from types import SimpleNamespace

    from backend.providers.generation.openai_compatible import OpenAICompatibleGeneration

    def _resp(finish: str, content: str, reasoning: str, reasoning_tokens: int):
        msg = SimpleNamespace(content=content, reasoning_content=reasoning)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=900,
                                completion_tokens_details=SimpleNamespace(
                                    reasoning_tokens=reasoning_tokens))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)],
                               model="fake", usage=usage)

    provider = OpenAICompatibleGeneration({"model_name": "fake"})
    for finish in ("stop", "length"):
        resp = _resp(finish, "", "让我想想……", 6954)
        with pytest.raises(ProviderError) as exc:
            provider._to_result(resp, 1.0, params={"max_tokens": 393216})
        assert exc.value.retryable is True, f"finish={finish} 必须可重试"
        text = str(exc.value)
        assert "393216" in text and "思维链" in text
        assert "请提高 max_tokens" not in text, "预算已是模型上限，不能再说这句话"

    # 正常响应照旧（正文非空就什么都不报）
    ok = provider._to_result(_resp("stop", "好的", "", 0), 1.0, params={"max_tokens": 393216})
    assert ok.text == "好的"
