"""任务系统（规划 §10 ⑨、§21、风险 R13）。

设计要点：
- **进程内 TaskRunner**（工作线程池 + MySQL 任务表 + 租约），不引入 Celery/Redis；
- 任务状态机：pending → processing → （paused | completed | failed | cancelled）；
- **租约** ``lease_expire_at`` 防止重复执行；进程被杀后租约过期由下一轮回收（R13）；
- **断点** ``cursor_json`` 幂等：重启后从断点继续，已完成项不重做；
- **暂停/取消** 只在段落/块边界生效，不在 LLM 调用中途强杀（规划 §17.2）。
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from sqlalchemy import and_, or_

from backend.core.config import get_settings
from backend.core.errors import NotFoundError, TaskError
from backend.core.events import EVENT_TASK_PROGRESS, bus
from backend.core.logging import get_logger
from backend.core.utils import iso_utc, json_dumps, json_loads, new_id, now
from backend.repositories.mysql.models import Task
from backend.repositories.mysql.session import new_session

log = get_logger(__name__)

# 状态常量（规划 §10 ⑨）
PENDING = "pending"
PROCESSING = "processing"
PAUSED = "paused"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (COMPLETED, FAILED, CANCELLED)

# 队列名 ↔ 短码（数据库 ``task.queue`` 为 VARCHAR(8)，规划 §10 ⑨）
QUEUE_ALIASES = {
    "system": "sys", "md": "md", "pdf": "pdf",
    "knowledge": "kb", "kb": "kb",
}
QUEUE_LONG = {
    "sys": "system", "md": "md", "pdf": "pdf", "kb": "knowledge",
}


def queue_code(name: str | None) -> str:
    """把长队列名转成数据库短码。"""
    return QUEUE_ALIASES.get((name or "system").lower(), "sys")


def queue_name(code: str | None) -> str:
    """把数据库短码还原为长队列名（对外展示用）。"""
    return QUEUE_LONG.get((code or "sys").lower(), "system")


@dataclass
class TaskContext:
    """交给 handler 的上下文：负责进度上报、断点、暂停/取消检查。"""

    task_id: str
    task_type: str
    queue: str
    paper_id: str | None
    params: dict
    session: object
    _cancel_check: Callable[[], bool] = field(default=lambda: False, repr=False)
    _pause_check: Callable[[], bool] = field(default=lambda: False, repr=False)
    _last_progress_write: float = 0.0
    _cursor: dict = field(default_factory=dict)
    _flag_cache: dict = field(default_factory=dict)
    _flag_at: float = 0.0
    _flag_ttl: float = 2.0
    done_items: int = 0          # 最近一次已知完成量（防止进度回退）
    total_items: int = 0

    # ---------------- 断点 ----------------
    @property
    def cursor(self) -> dict:
        return self._cursor

    def set_cursor(self, **kwargs) -> None:
        self._cursor.update(kwargs)
        self._flush(cursor=True)

    # ---------------- 进度 ----------------
    def progress(self, done: int, total: int, message: str | None = None,
                 *, force: bool = False, **extra) -> None:
        if done is not None:
            self.done_items = max(self.done_items, int(done))
        if total:
            self.total_items = max(self.total_items, int(total))
        self._flush(done=self.done_items, total=self.total_items or None,
                    message=message, force=force, **extra)

    def _flush(self, *, done: int | None = None, total: int | None = None,
               message: str | None = None, cursor: bool = False,
               force: bool = False, **extra) -> None:
        """写库限流（避免每块都 UPDATE）；关键节点用 ``force=True``。

        ⚠️ 只有**真正带进度信息**的写入才刷新限流时间戳。以前一律刷新，于是
        "先 `set_cursor` 再 `progress`"这个最常见的顺序里，`set_cursor` 刚把时间戳写成
        `now`，紧跟的 `progress` 就落在 0.5s 窗口内被**丢掉** —— 实测整篇翻译跑完，
        任务面板一直显示初始的 `0/22`，直到最后 `force=True` 才跳到 22/22
        （用户报过同类的"卡片还是旧数字"，那次修的是论文卡片，任务面板没修）。
        """
        t = time.time()
        carries_progress = (done is not None or total is not None
                            or message is not None)
        if (not force and not cursor and carries_progress
                and t - self._last_progress_write < 0.5):
            return
        if carries_progress or force:
            self._last_progress_write = t
        task_service.update_progress(self.task_id, done=done, total=total,
                                     message=message, cursor=self._cursor,
                                     **extra)

    # ---------------- 控制 ----------------
    def _flags(self) -> dict:
        """控制标志带 TTL 的本地缓存：块边界调用开销 ≈ 0。"""
        t = time.time()
        if t - self._flag_at < self._flag_ttl and self._flag_cache:
            return self._flag_cache
        self._flag_cache = {
            "cancel": self._cancel_check(),
            "pause": self._pause_check(),
        }
        self._flag_at = t
        return self._flag_cache

    def should_stop(self) -> bool:
        """在**块边界**调用：返回 True 时 handler 应尽快退出。"""
        return bool(self._flags().get("cancel"))

    def should_pause(self) -> bool:
        return bool(self._flags().get("pause"))

    def check_control(self) -> str | None:
        """返回 ``'cancel'`` / ``'pause'`` / ``None``。"""
        f = self._flags()
        if f.get("cancel"):
            return "cancel"
        if f.get("pause"):
            return "pause"
        return None


# --------------------------------------------------------------------------
# 任务服务：CRUD 与状态流转
# --------------------------------------------------------------------------


class TaskService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ---------------- 查询 ----------------
    def list(self, *, status: str | None = None, type_: str | None = None,
             queue: str | None = None, paper_id: str | None = None,
             limit: int = 100) -> list[dict]:
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            q = sess.query(Task)
            if status:
                q = q.filter(Task.status == status)
            if type_:
                q = q.filter(Task.type == type_)
            if queue:
                q = q.filter(Task.queue == queue_code(queue))
            if paper_id:
                q = q.filter(Task.paper_id == paper_id)
            rows = q.order_by(Task.created_at.desc()).limit(limit).all()
            return [self.to_dict(r) for r in rows]

    def get(self, task_id: str) -> dict:
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(Task, task_id)
            if row is None:
                raise NotFoundError(f"任务不存在：{task_id}")
            return self.to_dict(row, with_log=True)

    def find_active(self, type_: str, paper_id: str | None = None,
                    queue: str | None = None) -> dict | None:
        """查同类型进行中的任务（幂等：重复触发返回同一任务，规划 §20 约定）。"""
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            q = sess.query(Task).filter(Task.type == type_,
                                        Task.status.in_([PENDING, PROCESSING, PAUSED]))
            if paper_id:
                q = q.filter(Task.paper_id == paper_id)
            if queue:
                q = q.filter(Task.queue == queue_code(queue))
            row = q.order_by(Task.created_at.desc()).first()
            return self.to_dict(row) if row else None

    # ---------------- 创建 ----------------
    def create(self, type_: str, queue: str, paper_id: str | None = None,
               params: dict | None = None, total_items: int = 0,
               message: str | None = None, task_id: str | None = None) -> dict:
        from backend.repositories.mysql.session import session_scope

        tid = task_id or new_id("t")
        with session_scope() as sess:
            row = Task(
                id=tid[:32], type=type_, queue=queue_code(queue), paper_id=paper_id,
                status=PENDING, total_items=total_items, done_items=0,
                progress=0.0, params_json=json_dumps(params or {}),
                cursor_json=json_dumps({}), message=message,
            )
            sess.add(row)
            sess.flush()
            d = self.to_dict(row)
        bus.publish("tasks", EVENT_TASK_PROGRESS, d)
        return d

    def get_or_create(self, type_: str, queue: str, paper_id: str | None = None,
                      params: dict | None = None, **kwargs) -> tuple[dict, bool]:
        """幂等创建：已有进行中任务则返回它（``created=False``）。"""
        existing = self.find_active(type_, paper_id, queue)
        if existing:
            return existing, False
        return self.create(type_, queue, paper_id, params, **kwargs), True

    # ---------------- 更新 ----------------
    def update_progress(self, task_id: str, *, done: int | None = None,
                        total: int | None = None, message: str | None = None,
                        cursor: dict | None = None, stats: dict | None = None,
                        log_line: str | None = None) -> None:
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(Task, task_id)
            if row is None:
                return
            if done is not None:
                row.done_items = int(done)
            if total is not None:
                row.total_items = int(total)
            if row.total_items:
                row.progress = round(min(100.0, row.done_items / row.total_items * 100), 2)
            if message is not None:
                row.message = message[:500]
            if cursor is not None:
                row.cursor_json = json_dumps(cursor)
            if stats is not None:
                row.stats_json = json_dumps(stats)
            if log_line:
                tail = (row.log_tail or "").split("\n")[-40:]
                tail.append(log_line[:400])
                row.log_tail = "\n".join(tail)
            d = self.to_dict(row)
        bus.publish("tasks", EVENT_TASK_PROGRESS, d)

    def request_cancel(self, task_id: str) -> dict:
        return self._set_flags(task_id, cancel=True)

    def request_pause(self, task_id: str) -> dict:
        return self._set_flags(task_id, pause=True)

    def _set_flags(self, task_id: str, *, cancel: bool = False,
                   pause: bool = False) -> dict:
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(Task, task_id)
            if row is None:
                raise NotFoundError(f"任务不存在：{task_id}")
            if row.status in TERMINAL:
                return self.to_dict(row)
            if cancel:
                row.cancel_requested = True
            if pause:
                row.pause_requested = True
            row.message = ("已请求取消，等待当前块结束…" if cancel
                           else "已请求暂停，等待当前块结束…")
            d = self.to_dict(row)
        bus.publish("tasks", EVENT_TASK_PROGRESS, d)
        return d

    def resume(self, task_id: str) -> dict:
        """继续：清除暂停标志，状态回到 pending（断点在 cursor_json）。"""
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(Task, task_id)
            if row is None:
                raise NotFoundError(f"任务不存在：{task_id}")
            if row.status == COMPLETED and not row.cursor_json:
                raise TaskError("任务已全部完成，无需继续")
            row.pause_requested = False
            row.cancel_requested = False
            row.status = PENDING
            row.message = "已恢复，将从断点继续"
            row.lease_expire_at = None
            row.worker = None
            d = self.to_dict(row)
        bus.publish("tasks", EVENT_TASK_PROGRESS, d)
        return d

    # ---------------- 租约 ----------------
    def acquire_next(self, queues: list[str], worker: str,
                     lease_seconds: int | None = None) -> dict | None:
        """领取一个可执行任务（pending，或租约过期的 processing）。"""
        s = get_settings()
        lease = lease_seconds or s.task_lease_seconds
        codes = [queue_code(q) for q in queues]
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            now_dt = now()
            q = (sess.query(Task)
                 .filter(Task.queue.in_(codes))
                 .filter(Task.cancel_requested.is_(False))
                 .filter(or_(
                     Task.status == PENDING,
                     and_(Task.status == PROCESSING,
                          or_(Task.lease_expire_at.is_(None),
                              Task.lease_expire_at < now_dt)),
                 ))
                 .order_by(Task.created_at.asc()))
            row = q.first()
            if row is None:
                return None
            row.status = PROCESSING
            row.worker = worker
            row.lease_expire_at = now_dt + timedelta(seconds=lease)
            row.pause_requested = False
            d = self.to_dict(row)
        bus.publish("tasks", EVENT_TASK_PROGRESS, d)
        return d

    def renew_lease(self, task_id: str, worker: str,
                    lease_seconds: int | None = None) -> bool:
        s = get_settings()
        lease = lease_seconds or s.task_lease_seconds
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(Task, task_id)
            if row is None or row.worker != worker:
                return False
            row.lease_expire_at = now() + timedelta(seconds=lease)
            return True

    def finish(self, task_id: str, status: str, *, message: str | None = None,
               error: str | None = None, stats: dict | None = None,
               done_items: int | None = None, total_items: int | None = None) -> dict:
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(Task, task_id)
            if row is None:
                raise NotFoundError(f"任务不存在：{task_id}")
            row.status = status
            row.lease_expire_at = None
            row.worker = None
            if message:
                row.message = message[:500]
            if error:
                row.error = error[:4000]
            if stats is not None:
                row.stats_json = json_dumps(stats)
            # 终态必须反映真实完成量（限流写入可能落后）
            if total_items is not None:
                row.total_items = int(total_items)
            if done_items is not None:
                row.done_items = int(done_items)
            if row.total_items:
                row.progress = round(
                    min(100.0, row.done_items / row.total_items * 100), 2)
            if status == COMPLETED:
                row.progress = 100.0
                if not row.total_items:
                    row.done_items = row.done_items or 0
            row.finished_at = now()
            d = self.to_dict(row)
        bus.publish("tasks", EVENT_TASK_PROGRESS, d)
        return d

    def recycle_stale(self, queues: list[str]) -> int:
        """回收租约过期的 processing 任务 → pending（进程被杀后的恢复，R13）。"""
        from backend.repositories.mysql.session import session_scope

        codes = [queue_code(q) for q in queues]
        n = 0
        with session_scope() as sess:
            rows = (sess.query(Task)
                    .filter(Task.queue.in_(codes), Task.status == PROCESSING,
                            or_(Task.lease_expire_at.is_(None),
                                Task.lease_expire_at < now()))
                    .all())
            for row in rows:
                row.status = PENDING
                row.worker = None
                row.message = "租约过期，已重新入队（上次执行可能被中断）"
                n += 1
        if n:
            log.warning("回收 %d 个租约过期的任务", n)
        return n

    # ---------------- 序列化 ----------------
    @staticmethod
    def to_dict(row: Task, with_log: bool = False) -> dict:
        d = {
            "id": row.id, "type": row.type, "queue": queue_name(row.queue),
            "queue_code": row.queue,
            "paper_id": row.paper_id, "status": row.status,
            "progress": round(float(row.progress or 0), 2),
            "done_items": row.done_items, "total_items": row.total_items,
            "cursor": json_loads(row.cursor_json, {}) or {},
            "params": json_loads(row.params_json, {}) or {},
            "cancel_requested": bool(row.cancel_requested),
            "pause_requested": bool(row.pause_requested),
            "message": row.message, "error": row.error,
            "stats": json_loads(row.stats_json, {}) or {},
            # 带 UTC 标记（`iso_utc`）：库里是 UTC naive，直接 isoformat 会让前端按本地时区解析，
            # 任务中心的时间就差了 8 小时（用户报的"任务时间错误"）。
            "created_at": iso_utc(row.created_at),
            "updated_at": iso_utc(row.updated_at),
            "finished_at": iso_utc(row.finished_at),
        }
        if with_log:
            d["log_tail"] = (row.log_tail or "").split("\n")[-40:]
        return d

    def delete(self, task_id: str) -> dict:
        """删除任务记录（运行中的不许删：必须先取消，避免"记录没了但线程还在跑"）。"""
        from backend.core.errors import ConflictError
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(Task, task_id)
            if row is None:
                raise NotFoundError(f"任务不存在：{task_id}")
            if row.status not in TERMINAL:
                raise ConflictError(f"任务正在 {row.status}，先取消再删除")
            sess.delete(row)
        bus.publish("tasks", EVENT_TASK_PROGRESS, {"id": task_id, "deleted": True})
        return {"deleted": task_id}

    def cleanup_finished(self, statuses: list[str] | None = None,
                         paper_id: str | None = None) -> dict:
        """清空已结束的任务记录。

        ``statuses=None`` → 清 ``TERMINAL``（completed / failed / cancelled）。
        **显式给了状态列表却一个都不合法时返回 0**，不再回落到"全清" ——
        否则前端传个拼错的状态就会把用户的任务历史清光。

        ⚠️ ``paper_id`` 是给测试与"按论文清理"用的安全阀：**不传就是全库清理**。
        写测试时必须传 ``paper_id`` —— 我在这一轮真的踩过：端到端测试里裸调
        ``cleanup_finished()`` 把用户的任务记录（含 ``stats_json`` 里的 token/耗时
        实验数据）整表清空了。**默认参数越"顺手"，破坏力越大。**
        """
        from backend.repositories.mysql.session import session_scope

        if statuses is None:
            wanted = list(TERMINAL)
        else:
            wanted = [s for s in statuses if s in TERMINAL]
            if not wanted:
                return {"deleted": 0, "statuses": [],
                        "note": "没有可清理的状态（只认 completed / failed / cancelled）"}
        with session_scope() as sess:
            q = sess.query(Task).filter(Task.status.in_(wanted))
            if paper_id:
                q = q.filter(Task.paper_id == paper_id)
            n = q.delete(synchronize_session=False)
        bus.publish("tasks", EVENT_TASK_PROGRESS, {"cleaned": int(n), "statuses": wanted})
        return {"deleted": int(n), "statuses": wanted, "paper_id": paper_id}

    def stats(self, paper_id: str | None = None) -> dict:
        from backend.repositories.mysql.session import session_scope
        from sqlalchemy import func

        with session_scope() as sess:
            q = sess.query(Task.status, func.count(Task.id)).group_by(Task.status)
            if paper_id:
                q = q.filter(Task.paper_id == paper_id)
            counts = {status: count for status, count in q.all()}
        return {"by_status": counts, "total": sum(counts.values())}


# --------------------------------------------------------------------------
# 任务执行器
# --------------------------------------------------------------------------


class TaskRunner:
    """进程内工作线程池：按队列领取任务并执行 handler。"""

    def __init__(self, queues: list[str] | None = None, workers: int | None = None
                 ) -> None:
        s = get_settings()
        self.queues = queues or ["system", "md", "pdf", "knowledge"]
        self.workers = workers or s.task_worker_count
        self.handlers: dict[str, Callable[[TaskContext], dict | None]] = {}
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._running: dict[str, TaskContext] = {}
        self._lock = threading.RLock()
        self._idle_since = time.time()

    # ---------------- 注册 ----------------
    def register(self, task_type: str,
                 handler: Callable[[TaskContext], dict | None]) -> None:
        self.handlers[task_type] = handler

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for i in range(self.workers):
            t = threading.Thread(target=self._loop, name=f"taskrunner-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("TaskRunner 启动：%d 个工作线程，队列 %s", self.workers, self.queues)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()
        log.info("TaskRunner 已停止")

    @property
    def running_tasks(self) -> list[str]:
        with self._lock:
            return list(self._running.keys())

    # ---------------- 主循环 ----------------
    def _loop(self) -> None:
        s = get_settings()
        worker = threading.current_thread().name
        while not self._stop.is_set():
            try:
                task = task_service.acquire_next(self.queues, worker)
            except Exception as exc:  # noqa: BLE001
                log.warning("领取任务失败（数据库不可用？）：%s", exc)
                self._stop.wait(3.0)
                continue
            if task is None:
                try:
                    task_service.recycle_stale(self.queues)
                except Exception:  # noqa: BLE001
                    pass
                self._idle_since = time.time()
                self._stop.wait(s.task_poll_interval)
                continue
            self._execute(task, worker)

    def _execute(self, task: dict, worker: str) -> None:
        task_id = task["id"]
        handler = self.handlers.get(task["type"])
        if handler is None:
            task_service.finish(task_id, FAILED,
                                message=f"未注册的任务类型：{task['type']}",
                                error="handler not registered")
            return

        sess = new_session()
        ctx = TaskContext(
            task_id=task_id, task_type=task["type"], queue=task["queue"],
            paper_id=task.get("paper_id"), params=task.get("params") or {},
            session=sess,
            _cancel_check=lambda: self._flag(task_id, "cancel_requested"),
            _pause_check=lambda: self._flag(task_id, "pause_requested"),
        )
        ctx._cursor = dict(task.get("cursor") or {})
        ctx.done_items = int(task.get("done_items") or 0)
        ctx.total_items = int(task.get("total_items") or 0)
        with self._lock:
            self._running[task_id] = ctx
        log.info("任务开始：%s(%s) queue=%s paper=%s",
                 task_id, task["type"], task["queue"], task.get("paper_id"))
        heartbeat = threading.Thread(target=self._heartbeat, args=(task_id, worker),
                                     daemon=True)
        heartbeat.start()
        started = time.time()
        try:
            result = handler(ctx) or {}
            elapsed = round(time.time() - started, 2)
            done_items = result.pop("_done_items", None) or ctx.done_items
            total_items = result.pop("_total_items", None) or ctx.total_items or None
            if ctx.should_stop():
                task_service.finish(task_id, CANCELLED,
                                    message=result.get("message") or "已取消（已完成部分保留）",
                                    stats={"elapsed_s": elapsed, **result},
                                    done_items=done_items, total_items=total_items)
            elif ctx.should_pause():
                task_service.finish(task_id, PAUSED,
                                    message=result.get("message") or "已暂停（可从断点继续）",
                                    stats={"elapsed_s": elapsed, **result},
                                    done_items=done_items, total_items=total_items)
            else:
                task_service.finish(task_id, COMPLETED,
                                    message=result.get("message") or "完成",
                                    stats={"elapsed_s": elapsed, **result},
                                    done_items=done_items, total_items=total_items)
                log.info("任务完成：%s（%.1fs）", task_id, elapsed)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()[-2000:]
            log.exception("任务失败：%s", task_id)
            task_service.finish(task_id, FAILED, message=f"{type(exc).__name__}: {exc}",
                                error=tb)
        finally:
            with self._lock:
                self._running.pop(task_id, None)
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    def _heartbeat(self, task_id: str, worker: str, interval: float = 30.0) -> None:
        """周期性续租 + 检查取消/暂停（规划 R13 租约 + 心跳回收）。"""
        while task_id in self._running and not self._stop.is_set():
            self._stop.wait(interval)
            if task_id not in self._running:
                return
            try:
                task_service.renew_lease(task_id, worker)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _flag(task_id: str, field: str) -> bool:
        from backend.repositories.mysql.session import session_scope

        try:
            with session_scope() as sess:
                row = sess.get(Task, task_id)
                if row is None:
                    return True
                return bool(getattr(row, field))
        except Exception:  # noqa: BLE001
            return False


task_service = TaskService()
task_runner = TaskRunner()
