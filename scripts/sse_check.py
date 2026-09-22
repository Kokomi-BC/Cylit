"""验证页级 SSE 事件：触发 1 页重译 → 从事件流历史里确认 page_translated 的形状。

用法：
    python scripts/_sse_check.py [paper_id] [page]            # 触发一次重译再验证
    python scripts/_sse_check.py [paper_id] [page] --read-only # 只回放事件历史（不花钱）
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8710"
args = [a for a in sys.argv[1:] if not a.startswith("--")]
READ_ONLY = "--read-only" in sys.argv
paper = args[0] if args else "turboquant-iclr26"
page = int(args[1]) if len(args) > 1 else 7


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode())


if not READ_ONLY:
    task = post(f"/api/v1/papers/{paper}/translate",
                {"mode": "retranslate", "pages": [page], "brief": False})["data"]
    print(f"task={task['id']} status={task['status']}")
    deadline = time.time() + 300
    while time.time() < deadline:
        t = get(f"/api/v1/tasks/{task['id']}")["data"]
        if t["status"] in ("completed", "failed", "cancelled"):
            print(f"task {t['status']}: {t.get('message')}")
            break
        time.sleep(3)

# 事件总线有 200 条历史，直接回放即可看到刚发布的 page_translated。
# SSE 是长连接：socket 超时是**预期**的（历史播完就等着心跳），拿到帧即算成功。
frames: list[dict] = []
req = urllib.request.Request(f"{BASE}/api/v1/papers/{paper}/translations/stream")
try:
    with urllib.request.urlopen(req, timeout=6) as resp:
        event = None
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                frames.append({"event": event, "data": json.loads(line.split(":", 1)[1])})
                event = None
except (TimeoutError, OSError):
    pass

hits = [f for f in frames if f["event"] == "page_translated"
        and f["data"]["payload"].get("page") == page]
print(f"收到 {len(frames)} 帧；page_translated(p{page}) {len(hits)} 帧")
for f in frames:
    pl = f["data"].get("payload") or {}
    print(f"  - {f['event']}: paper={pl.get('paper_id')} page={pl.get('page')} "
          f"rows={len(pl.get('rows') or [])} status={pl.get('status')}")
if hits:
    p = hits[-1]["data"]["payload"]
    print("payload keys:", sorted(p.keys()))
    print(f"page={p['page']}/{p['page_total']} rows={len(p['rows'])} "
          f"align={p['align']} model={p['model']} done={p['done']}/{p['total']}")
    row = p["rows"][0]
    print("row keys:", sorted(row.keys()))
    print(f"first row: {row['id']} {row['type']} L{row['level']} "
          f"EN={row['original'][:40]!r} ZH={row['translation'][:30]!r}")
else:
    print("未在事件历史里找到 page_translated —— 请检查事件发布")
    raise SystemExit(1)
