"""调度器测试（阶段 08 验收：注入 429 与持续失败两种场景）。

对应规划 §3.1 A3 的六条目标：
A3-1 429 不立即失败、自动退避重试至完成
A3-2 并发动态调整（初始 4，受限降 1–2，恢复后回升）
A3-3 遵守 Retry-After
A3-4 连续失败达阈值时熔断并给出原因
A3-5 块级成功率（用注入失败率模拟）
A3-6 相比"固定并发无重试"失败块数显著下降
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.core.errors import ProviderError, RateLimitError
from backend.providers.generation.mock import MockGeneration
from backend.services.task.scheduler import (
    AdaptiveScheduler, ScheduleItem, SchedulerConfig,
)

NO_SLEEP = SchedulerConfig(sleep_fn=lambda _s: None)


def _cfg(**kw) -> SchedulerConfig:
    base = dict(initial=4, minimum=1, maximum=8, backoff_base=0.0,
                backoff_cap=0.0, breaker_threshold=8, recover_successes=5,
                max_attempts=4, jitter=0.0, sleep_fn=lambda _s: None)
    base.update(kw)
    return SchedulerConfig(**base)


# --------------------------------------------------------------------------
# A3-1 / A3-3：429 自动恢复 + 遵守 Retry-After
# --------------------------------------------------------------------------


def test_rate_limit_recovers_after_backoff():
    provider = MockGeneration({"parameters": {"rate_limit_every": 5}})
    sched = AdaptiveScheduler(_cfg())
    items = [ScheduleItem(key=f"i{i}", payload=i) for i in range(20)]
    stats = sched.run(items, lambda i: provider.generate(f"item {i}"))
    assert stats.total == 20
    assert stats.done == 20, "429 应被退避重试吸收，不产生失败块"
    assert stats.retries >= 1
    assert stats.concurrency_min < 4, "触发降速后并发应下调"


def test_retry_after_is_respected():
    """A3-3：退避时长取 ``Retry-After``，而不是默认的 base×2^k。"""
    sched = AdaptiveScheduler(_cfg(backoff_base=1.0, backoff_cap=30.0, jitter=0.0))
    # 直接校验退避计算（含 jitter=0 时等于 Retry-After；超过上限则截断到 cap）
    assert sched._backoff(1, 7.5) == pytest.approx(7.5)
    assert sched._backoff(2, None) == pytest.approx(2.0)     # base × 2^(k-1)
    assert sched._backoff(9, None) == pytest.approx(30.0)    # 截断到 cap
    assert sched._backoff(1, 999.0) == pytest.approx(30.0)   # Retry-After 也受 cap 约束


def test_retry_after_path_completes_with_monotonic_waits():
    """429 → 退避 → 重试成功的完整路径（轮询等待推进时间）。"""

    class FakeProvider:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt: str):
            self.calls += 1
            if self.calls <= 2:
                raise RateLimitError("429", retry_after=7.5)
            from backend.providers.base import GenerationResult

            return GenerationResult(text="ok")

    cfg = _cfg(backoff_base=1.0, backoff_cap=30.0, jitter=0.0)
    sleep_calls: list[float] = []
    cfg.sleep_fn = sleep_calls.append
    sched = AdaptiveScheduler(cfg)
    p = FakeProvider()
    stats = sched.run([ScheduleItem(key="k", payload=0)], lambda _i: p.generate("x"))

    assert stats.done == 1
    assert stats.retries == 2
    assert all(w <= 0.25 + 1e-9 for w in sleep_calls), "轮询步长应被限制"
    # 两次重试各需 7.5s（Retry-After），累计退避 ≈15s
    assert stats.backoff_seconds >= 14.0, stats.backoff_seconds


# --------------------------------------------------------------------------
# A3-4：熔断
# --------------------------------------------------------------------------


def test_breaker_trips_on_persistent_failure():
    provider = MockGeneration({"parameters": {"error_streak": 100}})
    sched = AdaptiveScheduler(_cfg(breaker_threshold=8, max_attempts=1))
    items = [ScheduleItem(key=f"i{i}", payload=i) for i in range(50)]
    stats = sched.run(items, lambda i: provider.generate(f"item {i}"))
    assert stats.breaker_tripped
    assert "熔断" in stats.breaker_reason or "连续失败" in stats.breaker_reason
    assert stats.skipped > 0, "熔断后剩余条目应被跳过而不是逐个失败"
    assert stats.concurrency_peak <= 4


def test_failure_does_not_block_other_items():
    """单块失败不阻塞后续块（规划 §3.1 A1-5）。"""
    provider = MockGeneration({"parameters": {"fail_every": 3}})
    sched = AdaptiveScheduler(_cfg(max_attempts=1, breaker_threshold=99))
    items = [ScheduleItem(key=f"i{i}", payload=i) for i in range(12)]
    stats = sched.run(items, lambda i: provider.generate(f"item {i}"))
    assert stats.done >= 6
    assert stats.failed >= 1
    assert stats.done + stats.failed == 12


# --------------------------------------------------------------------------
# A3-2：并发恢复
# --------------------------------------------------------------------------


def test_concurrency_recovers_after_successes():
    class FlakyOnce:
        def __init__(self) -> None:
            self.first = True

        def generate(self, prompt: str):
            from backend.providers.base import GenerationResult

            if self.first:
                self.first = False
                raise RateLimitError("429", retry_after=0.0)
            return GenerationResult(text="ok")

    p = FlakyOnce()
    sched = AdaptiveScheduler(_cfg(recover_successes=3))
    items = [ScheduleItem(key=f"i{i}", payload=i) for i in range(12)]
    stats = sched.run(items, lambda i: p.generate("x"))
    assert stats.done == 12
    reasons = [t["reason"] for t in stats.timeline]
    assert any("减半" in r for r in reasons)
    assert any("成功" in r for r in reasons), reasons


# --------------------------------------------------------------------------
# A3-5 / A3-6：自适应 vs 固定并发对比（阶段 08 的对照实验）
# --------------------------------------------------------------------------


def _fixed_concurrency_baseline(n: int, concurrency: int, provider) -> tuple[int, int]:
    """固定并发 + 无重试：失败即失败。"""
    done = failed = 0
    def call(i):
        return provider.generate(f"item {i}")
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for _ in pool.map(lambda i: _safe(call, i), range(n)):
            pass
    # 统计需要重新执行（pool.map 会吞异常）
    done = failed = 0
    for i in range(n):
        try:
            call(i)
            done += 1
        except Exception:  # noqa: BLE001
            failed += 1
    return done, failed


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception:  # noqa: BLE001
        return None


def test_adaptive_beats_fixed_on_failure_count():
    n = 40
    params = {"rate_limit_every": 5, "delay_ms": 0}

    adaptive_provider = MockGeneration({"parameters": dict(params)})
    sched = AdaptiveScheduler(_cfg())
    stats = sched.run(
        [ScheduleItem(key=f"i{i}", payload=i) for i in range(n)],
        lambda i: adaptive_provider.generate(f"item {i}"),
    )

    fixed_provider = MockGeneration({"parameters": dict(params)})
    fixed_failed = 0
    for i in range(n):
        try:
            fixed_provider.generate(f"item {i}")
        except Exception:  # noqa: BLE001
            fixed_failed += 1

    assert stats.done == n
    assert stats.failed == 0
    assert fixed_failed > 0, "固定并发无重试应当出现失败块"
    assert stats.failed < fixed_failed, "自适应并发的失败块数应显著低于固定并发"


# --------------------------------------------------------------------------
# 幂等缓存
# --------------------------------------------------------------------------


def test_idempotency_cache_dedupes_calls():
    calls = {"n": 0}

    def call(payload):
        calls["n"] += 1
        from backend.providers.base import GenerationResult

        return GenerationResult(text=f"v{payload}")

    sched = AdaptiveScheduler(_cfg())
    sched.cache_put("key-a", "cached")
    assert sched.cache_get("key-a") == "cached"
    assert sched.cache_get("missing") is None
    stats = sched.run([ScheduleItem(key="k1", payload=1)], call)
    assert stats.done == 1 and calls["n"] == 1


# --------------------------------------------------------------------------
# 停止信号（暂停/取消在块边界生效）
# --------------------------------------------------------------------------


def test_should_stop_skips_remaining():
    provider = MockGeneration({"parameters": {"delay_ms": 5}})
    sched = AdaptiveScheduler(_cfg(initial=2, maximum=2))
    items = [ScheduleItem(key=f"i{i}", payload=i) for i in range(30)]
    counter = {"n": 0}

    def call(i):
        counter["n"] += 1
        return provider.generate(f"item {i}")

    stats = sched.run(items, call, should_stop=lambda: counter["n"] >= 4)
    assert stats.skipped > 0
    assert stats.done + stats.failed + stats.skipped == 30
