"""任务进度的写库限流：**cursor 写入不许吞掉紧随其后的 progress 写入**。

## 为什么单独立一条

页级任务的 emit 顺序是固定的「先 `set_cursor`，再 `progress`」。而 `_flush` 的限流判据是
"距上次写入 < 0.5s 就跳过" —— 早期实现里 `set_cursor` 也刷新了那个时间戳，于是紧跟的
`progress` 恒定落在窗口内被丢掉。表现是：**整篇翻译跑完，任务面板一直显示初始的 `0/22`**，
直到最后 `force=True` 那一次才跳到 22/22。

用户报过同类的"跑到一半卡片还是旧数字"；那次修的是**论文卡片**（`set_translate_progress`
每页都写在 progress 之前，不受影响），**任务面板**这一条一直没修 —— 直到两路全量重跑时才看得
出来（22 页的任务面板 10 分钟里一直 0/22）。
"""
from __future__ import annotations

import pytest

from backend.services.task import task_service as ts


@pytest.fixture
def ctx(monkeypatch):
    """一个假的 TaskContext：只记下每次真正写库的内容。"""
    writes: list[dict] = []

    class FakeSvc:
        def update_progress(self, task_id, **kw):
            writes.append({"task_id": task_id, **kw})

    monkeypatch.setattr(ts, "task_service", FakeSvc())
    real = ts.TaskContext
    obj = real.__new__(real)
    obj.task_id = "t1"
    obj._last_progress_write = 0.0
    obj._cursor = {}
    obj.done_items = 0
    obj.total_items = 0
    obj._flag_at = 0.0
    obj._flag_cache = {}
    obj._flag_ttl = 0.5
    return obj, writes


def test_cursor_write_does_not_swallow_the_following_progress(ctx):
    obj, writes = ctx
    obj.set_cursor(done_pages=[3])
    obj.progress(1, 22, message="已转录 1/22 页")
    assert writes, "set_cursor 之后紧跟的 progress 必须真的写进去"
    last = writes[-1]
    assert last["done"] == 1 and last["total"] == 22
    assert last["message"] == "已转录 1/22 页"
    assert last["cursor"] == {"done_pages": [3]}, "cursor 要跟着一起写"


def test_progress_writes_are_still_throttled(ctx):
    """限流本身要保留：同一瞬间连发多条 progress 不该条条写库。"""
    obj, writes = ctx
    obj.progress(1, 22, message="a")
    n = len(writes)
    for i in range(5):
        obj.progress(i + 2, 22, message=f"x{i}")
    assert len(writes) - n <= 1


def test_force_bypasses_throttle(ctx):
    obj, writes = ctx
    obj.progress(1, 22, message="a")
    n = len(writes)
    obj.progress(2, 22, message="关键节点", force=True)
    assert len(writes) == n + 1
    assert writes[-1]["message"] == "关键节点"


def test_cursor_only_write_does_not_carry_progress_fields(ctx):
    obj, writes = ctx
    obj.set_cursor(done_pages=[1, 2])
    assert writes[-1]["done"] is None and writes[-1]["total"] is None
    assert writes[-1]["message"] is None
