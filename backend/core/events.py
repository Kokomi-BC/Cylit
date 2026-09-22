"""事件总线：任务线程 → SSE 推送的进程内通道（规划 §20 SSE 事件类型）。

线程安全：生产者是 TaskRunner 的工作线程（同步），消费者是 SSE 的 async 生成器。
实现上用 ``queue.Queue`` + ``asyncio.to_thread`` 取值，另附环形历史以便断线重连补发。
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections import defaultdict, deque
from typing import Any, AsyncIterator

from backend.core.utils import json_dumps, now_iso

# 规划 §20 定义的事件类型
EVENT_PAGE_TRANSLATED = "page_translated"     # 产线 A 逐页译文（页级粒度）
EVENT_BLOCK_TRANSLATED = "block_translated"   # 旧版逐块译文（保留兼容）
EVENT_PDF_PAGE_DONE = "pdf_page_done"         # 产线 B 逐页
EVENT_TASK_PROGRESS = "task_progress"
EVENT_RAG_TOKEN = "rag_token"
EVENT_PAPER_STATUS = "paper_status"
EVENT_ANNOTATION = "annotation_created"

_TOPICS = ("global", "tasks", "papers", "rag")


class EventBus:
    def __init__(self, history_size: int = 200) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[queue.Queue]] = defaultdict(list)
        self._history: dict[str, deque] = {
            t: deque(maxlen=history_size) for t in _TOPICS
        }
        self._seq = 0

    # ---------------- 生产 ----------------
    def publish(self, topic: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "type": event_type,
                "topic": topic,
                "at": now_iso(),
                "payload": payload,
            }
            self._history.setdefault(topic, deque(maxlen=200)).append(event)
            subs = list(self._subs.get(topic, ())) + list(self._subs.get("global", ()))
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:  # 慢消费者丢弃，不阻塞任务
                pass

    # ---------------- 消费 ----------------
    def subscribe(self, topic: str = "global") -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs[topic].append(q)
        return q

    def unsubscribe(self, topic: str, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs.get(topic, ()):
                self._subs[topic].remove(q)

    def history(self, topic: str, since_seq: int = 0) -> list[dict]:
        with self._lock:
            return [e for e in self._history.get(topic, ()) if e["seq"] > since_seq]

    async def stream(
        self,
        topic: str = "global",
        *,
        since_seq: int = 0,
        heartbeat: float = 15.0,
        wanted: set[str] | None = None,
    ) -> AsyncIterator[str]:
        """产出 SSE 文本帧（``event:`` / ``data:``）。"""
        for e in self.history(topic, since_seq):
            if wanted and e["type"] not in wanted:
                continue
            yield _frame(e)

        q = self.subscribe(topic)
        try:
            while True:
                try:
                    e = await asyncio.to_thread(q.get, True, heartbeat)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                if wanted and e["type"] not in wanted:
                    continue
                yield _frame(e)
        finally:
            self.unsubscribe(topic, q)


def _frame(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json_dumps(event)}\n\n"


bus = EventBus()
