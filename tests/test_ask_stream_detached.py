"""问答 SSE：客户端断开**不能**把这一轮掐掉（否则回答不落库、会话只剩提问）。

用户口径 2026-12-06「后台跑对话为什么没有历史记录」：实测会话里只有用户那条提问，日志里
既没有落库失败也没有报错 —— 说明生成器在跑到落库那段之前就被取消了。原因是上游直接接在
请求上：客户端切走 → 生成器取消 → httpx 关流 → agent 服务那一轮连落库都没跑。
现在上游灌进**独立任务 + 队列**，读端断掉只是没人取队列。
"""

from __future__ import annotations

import asyncio

from backend.api import agent as api_agent


def test_upstream_keeps_running_after_the_client_goes_away(monkeypatch):
    consumed = {"n": 0, "done": False}

    async def fake_stream(body):
        for i in range(5):
            consumed["n"] += 1
            yield b"data: {}\n\n"
            await asyncio.sleep(0.01)
        consumed["done"] = True

    async def no_compress(conversation_id):
        return False

    monkeypatch.setattr(api_agent.bridge, "stream_ask", fake_stream)
    monkeypatch.setattr(api_agent, "_auto_compress", no_compress)

    async def scenario():
        gen = api_agent._ask_stream({"question": "q"}, "c1")
        first = await gen.__anext__()          # 客户端只读了一帧
        assert first.startswith(b"data:")
        await gen.aclose()                     # 切走 / 关面板 = 关掉 SSE
        for _ in range(50):                    # 上游应当继续跑完
            if consumed["done"]:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert consumed["done"] is True, f"上游只跑了 {consumed['n']}/5 就被掐掉了"
