"""自适应并发调度器（规划 §3.1 A3，阶段 08 的实验主角）。

策略表（严格按规划实现）：

| 机制 | 规则 |
|---|---|
| 并发窗口 | N ∈ [1, 8]，默认 4 |
| 触发降速 | 429/超时/连接错误 → N 减半（≥1）+ 退避 |
| 退避 | ``base × 2^k × rand(0.8,1.2)``，上限 30s |
| 恢复 | 连续 5 次成功 → N +1，逐步回上限 |
| 熔断 | 连续失败 ≥8 → 暂停任务，原因写入 ``task.message`` |
| 幂等 | 同块按内容哈希去重，避免退避期重复计费 |
| 成本记录 | 每次调用记 token 与耗时 |

额外：遵守 ``Retry-After`` 响应头；失败项不阻塞后续项；时间线可导出（供实验曲线）。
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Iterable, TypeVar

from backend.core.errors import ProviderError, RateLimitError
from backend.core.logging import get_logger
from backend.core.utils import now_iso

log = get_logger(__name__)

T = TypeVar("T")
R = TypeVar("R")

# 退避等待时的轮询步长上限（秒）：保证取消/暂停请求最多等这么久就生效
IDLE_POLL_MAX = 0.25


@dataclass
class Attempt:
    """一次调用记录（成本与耗时，供第 5 章实验）。"""

    key: str
    attempt: int
    ok: bool
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    retryable: bool = False
    concurrency: int = 1
    at: str = ""


@dataclass
class ScheduleItem(Generic[T]):
    key: str
    payload: T
    attempts: int = 0


@dataclass
class ScheduleStats:
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: float = 0.0
    breaker_tripped: bool = False
    breaker_reason: str = ""
    concurrency_peak: int = 1
    concurrency_min: int = 1
    backoff_seconds: float = 0.0     # 累计退避等待时长（实验观察用）
    attempts_count: int = 0          # 实际调用次数（含重试）
    attempts: list[Attempt] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.done / self.total if self.total else 0.0

    def to_dict(self, include_attempts: bool = True) -> dict:
        d = {
            "total": self.total, "done": self.done, "failed": self.failed,
            "skipped": self.skipped, "retries": self.retries,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "duration_ms": round(self.duration_ms, 1),
            "success_rate": round(self.success_rate, 4),
            "breaker_tripped": self.breaker_tripped,
            "breaker_reason": self.breaker_reason,
            "concurrency_peak": self.concurrency_peak,
            "concurrency_min": self.concurrency_min,
            "backoff_seconds": self.backoff_seconds,
            "attempts_count": self.attempts_count,
        }
        if include_attempts:
            d["attempts"] = [a.__dict__ for a in self.attempts]
            d["timeline"] = self.timeline
        return d


@dataclass
class SchedulerConfig:
    initial: int = 4
    minimum: int = 1
    maximum: int = 8
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    breaker_threshold: int = 8
    recover_successes: int = 5
    max_attempts: int = 4
    jitter: float = 0.2
    seed: int = 20260917
    sleep_fn: Callable[[float], None] = time.sleep


class AdaptiveScheduler:
    """按 item 粒度并发调度，并自适应调整并发窗口。"""

    def __init__(self, config: SchedulerConfig | None = None) -> None:
        self.cfg = config or SchedulerConfig()
        self._lock = threading.Lock()
        self.concurrency = max(self.cfg.minimum,
                               min(self.cfg.maximum, self.cfg.initial))
        self._success_streak = 0
        self._failure_streak = 0
        self.tripped = False
        self.trip_reason = ""
        self._rng = random.Random(self.cfg.seed)
        self._timeline: list[dict] = []
        self._cache: dict[str, Any] = {}
        self._cache_hits = 0
        self._backoff_seconds = 0.0

    # ------------------------------------------------------------------
    def _log_concurrency(self, reason: str) -> None:
        self._timeline.append({"at": now_iso(), "concurrency": self.concurrency,
                               "reason": reason})

    def _on_success(self) -> None:
        with self._lock:
            self._failure_streak = 0
            self._success_streak += 1
            if (self._success_streak >= self.cfg.recover_successes
                    and self.concurrency < self.cfg.maximum):
                self.concurrency += 1
                self._success_streak = 0
                self._log_concurrency(f"连续 {self.cfg.recover_successes} 次成功，+1")

    def _on_failure(self, retryable: bool) -> None:
        with self._lock:
            self._success_streak = 0
            self._failure_streak += 1
            if retryable and self.concurrency > self.cfg.minimum:
                new_n = max(self.cfg.minimum, self.concurrency // 2)
                if new_n != self.concurrency:
                    self.concurrency = new_n
                    self._log_concurrency("失败降速：减半")
            if self._failure_streak >= self.cfg.breaker_threshold:
                self.tripped = True
                self.trip_reason = (f"连续失败 {self._failure_streak} 次"
                                    f"（阈值 {self.cfg.breaker_threshold}），已熔断暂停")
                self._log_concurrency("熔断")

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after and retry_after > 0:
            delay = retry_after
        else:
            delay = self.cfg.backoff_base * (2 ** max(0, attempt - 1))
        delay = min(delay, self.cfg.backoff_cap)
        jitter = 1.0 + self._rng.uniform(-self.cfg.jitter, self.cfg.jitter)
        return max(0.0, delay * jitter)

    # ------------------------------------------------------------------
    def run(
        self,
        items: Iterable[ScheduleItem[T]],
        call: Callable[[T], R],
        *,
        on_result: Callable[[ScheduleItem[T], R], None] | None = None,
        on_error: Callable[[ScheduleItem[T], Exception], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        idle_wait: Callable[[float], None] | None = None,
    ) -> ScheduleStats:
        """并发执行 ``items``；单块失败不阻塞其它块。

        ``on_result`` 每成功一块调用一次（用于落库 + SSE 推送 → 实现"≤2s 可见"）。
        """
        items = list(items)
        stats = ScheduleStats(total=len(items))
        if not items:
            return stats

        t0 = time.perf_counter()
        results: dict[str, Any] = {}
        succeeded: set[str] = set()
        failed: set[str] = set()
        pending: list[ScheduleItem[T]] = list(items)
        delayed: list[tuple[float, ScheduleItem[T]]] = []   # 退避队列
        inflight: dict[Any, ScheduleItem[T]] = {}
        last_error: Exception | None = None

        def promote_delayed() -> None:
            if not delayed:
                return
            now = time.perf_counter()
            ready = [it for t, it in delayed if t <= now]
            if ready:
                delayed[:] = [(t, it) for t, it in delayed if t > now]
                pending.extend(ready)

        def poll_wait(seconds: float) -> None:
            """退避等待：小步推进（保证取消/暂停最多等 IDLE_POLL_MAX 生效）。

            累计时长记入 ``backoff_seconds``，供测试与实验观察。
            """
            delay = min(max(0.0, seconds), IDLE_POLL_MAX)
            if delay > 0:
                self._backoff_seconds += delay
                self.cfg.sleep_fn(delay)

        def submit(pool, item: ScheduleItem[T]) -> None:
            fut = pool.submit(self._attempt, item, call)
            inflight[fut] = item

        def wait_for_slot() -> None:
            # 并发窗口就是 inflight 上限：超出则等一个完成
            while len(inflight) >= self.concurrency and inflight:
                done, _ = wait(list(inflight.keys()), timeout=0.2,
                               return_when=FIRST_COMPLETED)
                if not done:
                    if idle_wait:
                        idle_wait(0.05)
                    continue
                self._collect(done, inflight, stats, results, succeeded, failed,
                              delayed, on_result, on_error, on_progress, len(items))
                if should_stop and should_stop():
                    return

        with ThreadPoolExecutor(max_workers=self.cfg.maximum,
                                thread_name_prefix="sched") as pool:
            while pending or inflight or delayed:
                if should_stop and should_stop():
                    log.info("调度器收到停止信号，取消剩余 %d 项",
                             len(pending) + len(delayed))
                    stats.skipped += len(pending) + len(delayed)
                    pending.clear()
                    delayed.clear()
                    # 已提交的任务无法取消，收集其结果后再退出（保证统计自洽）
                    drain_until = time.perf_counter() + 30.0
                    while inflight and time.perf_counter() < drain_until:
                        done, _ = wait(list(inflight.keys()), timeout=0.2,
                                       return_when=FIRST_COMPLETED)
                        if done:
                            self._collect(done, inflight, stats, results, succeeded,
                                          failed, delayed, on_result, on_error,
                                          on_progress, len(items))
                    break
                if self.tripped:
                    stats.skipped += len(pending) + len(delayed)
                    stats.breaker_tripped = True
                    stats.breaker_reason = self.trip_reason
                    pending.clear()
                    delayed.clear()
                    break
                promote_delayed()
                wait_for_slot()
                promote_delayed()
                while pending and len(inflight) < self.concurrency:
                    submit(pool, pending.pop(0))
                if inflight:
                    done, _ = wait(list(inflight.keys()), timeout=0.2,
                                   return_when=FIRST_COMPLETED)
                    if done:
                        self._collect(done, inflight, stats, results, succeeded,
                                      failed, delayed, on_result, on_error,
                                      on_progress, len(items))
                    elif idle_wait:
                        idle_wait(0.05)
                elif delayed:
                    # 全部在退避等待中：按最近到期时间小步轮询（退避节拍由轮询推进）
                    soonest = min(t for t, _ in delayed) - time.perf_counter()
                    poll_wait(soonest)

        stats.duration_ms = (time.perf_counter() - t0) * 1000
        # 重试次数 = 失败的调用次数（每次失败后都会重试，除最后一次）
        stats.retries = sum(1 for a in stats.attempts if not a.ok)
        stats.attempts_count = len(stats.attempts)
        stats.timeline = list(self._timeline)
        stats.concurrency_peak = max([a.concurrency for a in stats.attempts] or [1])
        stats.concurrency_min = min([a.concurrency for a in stats.attempts] or [1])
        if stats.breaker_tripped:
            stats.breaker_reason = self.trip_reason
        # 供测试与实验观察：累计退避等待时长
        stats.backoff_seconds = round(self._backoff_seconds, 3)
        _ = results
        _ = last_error
        return stats

    # ------------------------------------------------------------------
    def _collect(self, done, inflight, stats, results, succeeded, failed, delayed,
                 on_result, on_error, on_progress, total) -> None:
        for fut in done:
            item = inflight.pop(fut)
            try:
                value, attempt, err = fut.result()
            except Exception as exc:  # noqa: BLE001
                value, attempt, err = None, None, exc
            if attempt is not None:
                stats.attempts.append(attempt)
                stats.prompt_tokens += attempt.prompt_tokens
                stats.completion_tokens += attempt.completion_tokens
            if err is not None:
                retryable = isinstance(err, ProviderError) and err.retryable
                item.attempts += 1
                if retryable and not self.tripped \
                        and item.attempts < self.cfg.max_attempts:
                    # 退避后重新入队（退避期间不占用并发窗口）
                    delay = self._backoff(item.attempts,
                                          getattr(err, "retry_after", None))
                    delayed.append((time.perf_counter() + delay, item))
                    log.warning("条目 %s 第 %d 次失败，%.1fs 后重试：%s",
                                item.key, item.attempts, delay, str(err)[:160])
                else:
                    failed.add(item.key)
                    stats.failed += 1
                    if on_error:
                        try:
                            on_error(item, err)
                        except Exception:  # noqa: BLE001
                            log.exception("on_error 回调异常")
                    if on_progress:
                        on_progress(stats.done + stats.failed, total)
            else:
                succeeded.add(item.key)
                stats.done += 1
                if on_result and value is not None:
                    try:
                        on_result(item, value)
                    except Exception:  # noqa: BLE001
                        log.exception("on_result 回调异常（%s）", item.key)
                if on_progress:
                    on_progress(stats.done + stats.failed, total)

    def _attempt(self, item: ScheduleItem[T], call: Callable[[T], R]
                 ) -> tuple[R | None, Attempt, Exception | None]:
        """执行一次调用（不做内部重试；重试与退避由 ``_collect`` 统一编排）。"""
        with self._lock:
            current_n = self.concurrency
        t0 = time.perf_counter()
        try:
            value = call(item.payload)
            latency = (time.perf_counter() - t0) * 1000
            attempt = Attempt(
                key=item.key, attempt=item.attempts + 1, ok=True,
                latency_ms=round(latency, 1), concurrency=current_n, at=now_iso(),
                prompt_tokens=int(getattr(value, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(value, "completion_tokens", 0) or 0),
            )
            self._on_success()
            return value, attempt, None
        except Exception as exc:  # noqa: BLE001
            latency = (time.perf_counter() - t0) * 1000
            retryable = isinstance(exc, ProviderError) and exc.retryable
            attempt = Attempt(
                key=item.key, attempt=item.attempts + 1, ok=False,
                latency_ms=round(latency, 1), error=str(exc)[:300],
                retryable=retryable, concurrency=current_n, at=now_iso(),
            )
            self._on_failure(retryable)
            return None, attempt, exc

    # ------------------------------------------------------------------
    # 幂等缓存（同块按内容哈希去重，避免退避期重复计费，规划 §3.1 A3）
    # ------------------------------------------------------------------
    def cache_get(self, key: str) -> Any | None:
        with self._lock:
            if key in self._cache:
                self._cache_hits += 1
                return self._cache[key]
        return None

    def cache_put(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = value

    @property
    def cache_hits(self) -> int:
        return self._cache_hits


def timeline_by_concurrency(stats: ScheduleStats) -> list[dict]:
    """把尝试记录整理成"并发时间线"（阶段 08 验收：记录并发时间线）。"""
    out = []
    for a in stats.attempts:
        out.append({"at": a.at, "key": a.key, "attempt": a.attempt,
                    "concurrency": a.concurrency, "ok": a.ok,
                    "latency_ms": a.latency_ms,
                    "error": a.error})
    return out
