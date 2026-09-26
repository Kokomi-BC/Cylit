"""理解阶段的升档重试：思考模式吃光预算导致正文为空时必须放大 max_tokens。

## 为什么要单独立一条

实测（turboquant，A 路技术解剖）：第一次调用 `finish=length`、reasoning 8000 tokens、
**正文为空**直接失败；同一时刻 B 路 35 秒就成功产出 3497 字 + 71 条引用。
所以这不是"理解不可行"，是预算不够 —— 而这条路径**只会在真实模型上暴露**，
离线单测必须用假 provider 把两种响应都造出来，否则以后改坏没人知道。
"""
from __future__ import annotations

import pytest

from backend.core.errors import ProviderError
from backend.services.understanding import understanding_service as us


class _Res:
    def __init__(self, text: str, model: str = "fake") -> None:
        self.text = text
        self.model = model
        self.prompt_tokens = 10
        self.completion_tokens = 20


class _Provider:
    """第一次"只出思维链"报错，第二次成功。"""
    model_name = "fake"

    def __init__(self, fail_times: int = 1, msg: str = "模型只产出思维链（reasoning 8000 tokens，finish=length）而正文为空：请提高 max_tokens") -> None:
        self.calls: list[int] = []
        self._fail_times = fail_times
        self._msg = msg

    def generate(self, prompt, *, system=None, temperature=0.0, max_tokens=0):  # noqa: ANN001
        self.calls.append(max_tokens)
        if len(self.calls) <= self._fail_times:
            raise ProviderError(self._msg)
        return _Res('## 技术解剖\n正文（来源 Chunk: A001）\n```json\n{"refs": ["A001"]}\n```')


def test_escalates_then_succeeds() -> None:
    provider = _Provider(fail_times=1)
    res = us._generate(provider, "p", system="s", path="a")
    assert len(provider.calls) == 2, "第一次失败后应当放大预算重试"
    assert provider.calls[1] > provider.calls[0]
    assert "技术解剖" in res.text


def test_other_errors_are_not_retried() -> None:
    """网络 500 之类的错误不该被当成"预算不够"反复放大（会白烧三次调用）。"""
    provider = _Provider(fail_times=9, msg="HTTP 500 Internal Server Error")
    with pytest.raises(ProviderError):
        us._generate(provider, "p", system="s", path="a")
    assert len(provider.calls) == 1


def test_gives_up_after_three_attempts() -> None:
    provider = _Provider(fail_times=9)
    with pytest.raises(RuntimeError):
        us._generate(provider, "p", system="s", path="b")
    assert len(provider.calls) == 3
