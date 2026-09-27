"""理解阶段的输出预算：**基线就是厂商上限**，"正文为空"不再原地放大重试。

## 为什么要单独立一条

实测（turboquant，A 路技术解剖）：第一次调用 `finish=length`、reasoning 8000 tokens、
**正文为空**直接失败；同一时刻 B 路 35 秒就成功产出 3497 字 + 71 条引用。
当时的修法是"按阶梯放大 max_tokens"（8192 → … → 65536）。

2026-09-27 用户口径改成**不设人为上限**：基线就是 65536，阶梯只剩一级 ——
所以"正文为空"这类错误现在**不在本函数里重试**（同预算再要一次没有意义），
异常抛给调度器由它重试该条。这条路径只在真实模型上暴露，离线单测必须用假 provider 把
两种响应都造出来，否则以后改坏没人知道。
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
    """前 `fail_times` 次"只出思维链"报错。"""
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


def test_budget_is_vendor_max() -> None:
    """每次调用都带模型自己的上限（官方"输出长度 最大 384K"），不再出现"8192 起步、逐级放大"。"""
    from backend.services.translation.page_translate import MAX_OUTPUT_TOKENS

    provider = _Provider(fail_times=0)
    us._generate(provider, "p", system="s", path="a")
    assert provider.calls == [MAX_OUTPUT_TOKENS] == [393216]


def test_empty_body_is_left_to_the_scheduler() -> None:
    """预算已经到顶：正文为空时只调一次就抛错，交给调度器重试（而不是原地再烧一轮）。"""
    provider = _Provider(fail_times=1)
    with pytest.raises(ProviderError):
        us._generate(provider, "p", system="s", path="a")
    assert len(provider.calls) == 1, "同预算重试没有意义，应该抛给调度器"


def test_other_errors_are_not_retried() -> None:
    """网络 500 之类的错误同样只调用一次（不白烧）。"""
    provider = _Provider(fail_times=9, msg="HTTP 500 Internal Server Error")
    with pytest.raises(ProviderError):
        us._generate(provider, "p", system="s", path="a")
    assert len(provider.calls) == 1
