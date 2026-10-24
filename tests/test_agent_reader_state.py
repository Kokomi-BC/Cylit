r"""阅读状态（`reader_state`）：契约、降级、透传。

用户口径：让 agent 知道**读者此刻在读第几页、看的是哪一面**，从而知道笔记该加到哪一面
（`highlight_manage` 的 `side` 以前按**文字语言**猜，中文读者高亮一段英文会被标到 `en` 面，
而他在译文视图里根本看不到）。

钉死的东西：

1. 没带 `reader` → `available=false`、字段全空、`hint` 明说"不要猜"；
2. 带了 → 字段逐一对上，`note_side` = 读者正在看的那一面；
3. `page_side="both"`（对照）→ `note_side` 给 null 且 `hint` 说清为什么；
4. 非法值**丢弃但不报错**（`page:"abc"` / `page_side:"xx"`），其余字段照常；
5. `/agent/ask` 把 `reader` 原样转给 agent 进程，老前端（不带）转发体里**没有**这个 key。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import backend.api.agent as api_agent
from backend.agent import tools as agent_tools
from backend.agent.deps import Deps, ReaderState, normalize_reader
from backend.agent.tools import reader_state
from backend.core.errors import PaperMindError

FULL = {"page": 7, "page_side": "zh", "order": "zh-first", "view": "rows",
        "side_tab": "insight", "visible_pages": [7, 8]}


def _deps(**kw) -> Deps:
    """最小 Deps：`reader_state` 只看 `reader`（`host=None` 就是在钉"它不打后端"）。"""
    kws = dict(cfg=SimpleNamespace(), host=None, paper_id="demo-paper", registry=None)
    kws.update(kw)
    return Deps(**kws)  # type: ignore[arg-type]


def _call(raw: object = None) -> dict:
    deps = _deps(reader=normalize_reader(raw))
    return asyncio.run(reader_state(SimpleNamespace(deps=deps)))


# --------------------------------------------------------------------------
# 1. 没带 reader：不知道就是不知道
# --------------------------------------------------------------------------


def test_without_reader_nothing_is_invented() -> None:
    out = _call(None)

    assert out["available"] is False
    assert out["page"] is None and out["page_known"] is False
    assert out["page_side"] is None and out["order"] is None
    assert out["view"] is None and out["side_tab"] is None
    assert out["visible_pages"] is None
    assert out["note_side"] is None, "没带阅读状态时不许给落笔建议（那就是猜）"
    assert "不知道读者在哪" in out["hint"]
    assert "page_markdown_get" in out["hint"], "hint 要给出替代办法，不能只说不知道"


def test_reader_state_never_touches_the_backend() -> None:
    """只读工具：`host=None` 也能跑通 —— 它读的是请求带来的快照。"""
    deps = _deps(reader=normalize_reader(FULL))
    out = asyncio.run(reader_state(SimpleNamespace(deps=deps)))

    assert out["available"] is True
    assert [one["tool"] for one in deps.trace] == ["reader_state"]
    assert deps.trace[0]["status"] == "ok"


# --------------------------------------------------------------------------
# 2. 带 reader：字段逐一对上，并按"读者在看的那一面"给建议
# --------------------------------------------------------------------------


def test_full_reader_state_maps_one_to_one() -> None:
    out = _call(FULL)

    assert out["available"] is True
    assert out["page"] == 7 and out["page_known"] is True
    assert out["page_side"] == "zh"
    assert out["order"] == "zh-first"
    assert out["view"] == "rows"
    assert out["side_tab"] == "insight"
    assert out["visible_pages"] == [7, 8]
    assert out["note_side"] == "zh", "读者在看译文面，默认落笔面就是它"
    assert "zh" in out["hint"]


def test_english_reader_gets_english_side() -> None:
    """中文读者高亮一段英文（旧口径会判成 zh）—— 有了状态就按他看的那一面标。"""
    out = _call({"page": 3, "page_side": "en"})
    assert out["note_side"] == "en"


def test_contrast_view_gives_no_default_side() -> None:
    out = _call({"page": 7, "page_side": "both", "order": "en-first", "view": "rows"})

    assert out["available"] is True and out["page"] == 7
    assert out["note_side"] is None, "对照视图两侧都在，宿主不替模型选面"
    assert "对照" in out["hint"], "要给模型一句话解释为什么 note_side 是 null"


def test_view_hint_does_not_contradict_itself() -> None:
    """视图那句提示不能自相矛盾（用户口径 2026-12）。

    老写法是「rows = 正文行，高亮才画得出来；markdown / pdf 面上的高亮在阅读视图里看不到」——
    读者正看 pdf 时，前半句说"当前视图 pdf"、后半句说"pdf 面上的高亮看不到"，读起来像 bug 描述
    串进了提示语。现在按**锚点**说清各自在哪看得到，并且不再出现"阅读视图"这个会被当前视图顶掉的词。
    """
    for view, anchor_tip in (("pdf", "落 pdf:pN"), ("markdown", "落 md:pN"),
                             ("rows", "正文行")):
        out = _call({"page": 4, "page_side": "zh", "view": view})
        hint = str(out["hint"])
        assert f"当前视图：{view}" in hint
        assert anchor_tip in hint, f"{view} 的提示里要说清 {anchor_tip} 落在哪看得到"
        assert "阅读视图里看不到" not in hint, "别再用那句会与当前视图打架的说法"


def test_page_unknown_stays_available() -> None:
    """`page == 0`：仍然"带了状态"（视图/页签是知道的），但页码要标明未知。"""
    out = _call({"page": 0, "page_side": "zh", "view": "markdown"})

    assert out["available"] is True
    assert out["page"] is None and out["page_known"] is False
    assert "页未知" in out["hint"]
    assert "markdown" in out["hint"], "视图要转述给模型（它决定读者当下看不看得到高亮）"


# --------------------------------------------------------------------------
# 3. 非法值：丢弃而不是报错
# --------------------------------------------------------------------------


def test_illegal_fields_are_dropped_not_raised() -> None:
    reader = normalize_reader({"page": "abc", "page_side": "xx", "order": "middle",
                               "view": "rows", "side_tab": " insight ",
                               "visible_pages": ["8", 9, -2, True]})

    assert reader.available is True, "还有认得的字段，就不能整个当没带"
    assert reader.page == 0, "page 不是正整数 → 当作没报（不猜）"
    assert reader.page_side == "" and reader.order == "", "闭集外的值一律丢弃"
    assert reader.view == "rows" and reader.side_tab == "insight"
    assert reader.visible_pages == (9,), "非正整数页码与字符串页码都丢掉"


def test_all_garbage_is_equivalent_to_absent() -> None:
    """一个字段都认不出来 = 没带：否则工具会拿空壳状态说"知道读者在哪"。"""
    out = _call({"page": "abc", "page_side": "xx", "visible_pages": "7-8"})

    assert out["available"] is False
    assert out["page"] is None and out["note_side"] is None
    assert "不知道读者在哪" in out["hint"]


def test_normalize_is_idempotent() -> None:
    once = normalize_reader(FULL)
    twice = normalize_reader(once.as_body())
    assert twice == once, "主后端洗一遍、agent 再洗一遍，结果必须一样"
    assert normalize_reader(once) is once


def test_reader_state_is_mounted_for_qa() -> None:
    assert reader_state in agent_tools.AGENT_TOOLS
    assert reader_state not in agent_tools.META_TOOLS, "填元数据不需要阅读状态"


# --------------------------------------------------------------------------
# 4. 透传：/agent/ask → agent 进程的请求体
# --------------------------------------------------------------------------


def _client(monkeypatch) -> tuple[TestClient, dict]:
    """一条只走"校验 + 组装 body"的 `/agent/ask`：会话与上游全部换成空操作。"""
    seen: dict = {}

    async def _fake_stream(body: dict, conversation_id: str):
        seen["body"] = body
        seen["conversation_id"] = conversation_id
        if False:            # 生成器（空响应即可，本用例只看转发的 body）
            yield b""

    monkeypatch.setattr(api_agent, "_ask_stream", _fake_stream)
    monkeypatch.setattr(api_agent, "_limit_gate", lambda *a, **kw: None)
    monkeypatch.setattr(api_agent.paper_service, "get_paper", lambda pid: {"paper_id": pid})
    monkeypatch.setattr(api_agent.conversations, "ensure_conversation",
                        lambda cid, pid, title=None: "conv-1")
    monkeypatch.setattr(api_agent.conversations, "update_conversation",
                        lambda cid, patch: {"model_config_id": None, "reasoning_effort": ""})
    monkeypatch.setattr(api_agent.conversations, "append_message",
                        lambda *a, **kw: {"message_id": "m-1", "role": "user"})
    monkeypatch.setattr(api_agent.conversations, "history_for_agent",
                        lambda cid: [{"role": "user", "content": "hi"}])
    monkeypatch.setattr(api_agent.conversations, "frozen_preferences",
                        lambda cid: {"agent_style": "", "agent_style_preset": "",
                                     "agent_reply_length": ""})

    app = FastAPI()
    app.include_router(api_agent.router, prefix="/api/v1")

    @app.exception_handler(PaperMindError)
    async def _handle(_request, exc: PaperMindError):
        return JSONResponse(status_code=exc.http_status,
                            content={"data": None, "error": exc.to_dict(), "trace_id": ""})

    return TestClient(app), seen


def test_ask_forwards_reader_to_agent(monkeypatch) -> None:
    http, seen = _client(monkeypatch)

    body = {"paper_id": "demo-paper", "question": "这一段标一下",
            "reader": {**FULL, "page_side": "both"}}
    response = http.post("/api/v1/agent/ask", json=body)

    assert response.status_code == 200, response.text
    assert seen["body"]["reader"] == {"page": 7, "page_side": "both",
                                      "order": "zh-first", "view": "rows",
                                      "side_tab": "insight", "visible_pages": [7, 8]}


def test_ask_without_reader_keeps_the_old_body(monkeypatch) -> None:
    """老前端不带 `reader`：转发体里**没有**这个 key（行为与从前逐字节一样）。"""
    http, seen = _client(monkeypatch)

    response = http.post("/api/v1/agent/ask",
                         json={"paper_id": "demo-paper", "question": "什么是注意力"})

    assert response.status_code == 200, response.text
    assert "reader" not in seen["body"]


def test_ask_drops_illegal_reader_fields_but_keeps_valid_ones(monkeypatch) -> None:
    """非法值不报 500：认得的留下，认不得的丢掉。"""
    http, seen = _client(monkeypatch)

    response = http.post("/api/v1/agent/ask", json={
        "paper_id": "demo-paper", "question": "标一下",
        "reader": {"page": "abc", "page_side": "xx", "order": "en-first",
                   "view": "cards", "side_tab": "insight"}})

    assert response.status_code == 200, response.text
    assert seen["body"]["reader"] == {"order": "en-first", "side_tab": "insight"}


def test_ask_treats_fully_broken_reader_as_absent(monkeypatch) -> None:
    http, seen = _client(monkeypatch)

    response = http.post("/api/v1/agent/ask", json={
        "paper_id": "demo-paper", "question": "标一下",
        "reader": {"page": None, "page_side": 42}})

    assert response.status_code == 200, response.text
    assert "reader" not in seen["body"]


def test_service_parses_reader_into_ask_request() -> None:
    """agent 服务那一侧：`payload.reader` → `AskRequest.reader`（同一个解析口）。"""
    from backend.agent import service as agent_service

    assert hasattr(agent_service, "normalize_reader")
    state = agent_service.normalize_reader(FULL)
    assert isinstance(state, ReaderState)
    assert (state.page, state.page_side, state.note_side) == (7, "zh", "zh")


def test_reader_state_arrives_in_deps(monkeypatch) -> None:
    """runtime 把 `AskRequest.reader` 原样写进 `Deps.reader`（工具真的读得到）。"""
    from pathlib import Path

    from backend.agent import runtime as agent_runtime
    from backend.agent.runtime import AskRequest

    created: list = []

    class _RecordingDeps(agent_runtime.Deps):     # type: ignore[misc]
        def __init__(self, **kw) -> None:
            super().__init__(**kw)
            created.append(self)

    class _BrokenHost:
        """宿主一上来就坏：本用例只关心 deps 有没有拿到 reader（不关心一问一答）。"""

        async def archive(self, paper_id: str) -> dict:
            raise RuntimeError("宿主不可用")

    monkeypatch.setattr(agent_runtime, "Deps", _RecordingDeps)
    skills_dir = Path(__file__).resolve().parent.parent / "backend" / "agent" / "skills"
    runtime = agent_runtime.AgentRuntime(
        cfg=SimpleNamespace(skills_dir=skills_dir), host=_BrokenHost())  # type: ignore[arg-type]
    req = AskRequest(paper_id="demo-paper", question="hi",
                     reader=normalize_reader({"page": 5, "page_side": "en"}))

    async def _drive() -> list:
        return [event async for event in runtime.ask(req)]

    events = asyncio.run(_drive())

    assert events and events[-1]["type"] == "error", "宿主坏了要变成一帧 error，不抛栈"
    assert created, "runtime 没有建 Deps"
    assert created[0].reader.page == 5, "reader 没写进 Deps —— 工具就永远看不到它"
    assert created[0].reader.note_side == "en"


def test_old_ask_request_defaults_to_unknown_reader() -> None:
    from backend.agent.runtime import AskRequest

    assert AskRequest(paper_id="demo-paper").reader.available is False
    assert AskRequest(paper_id="demo-paper").reader.note_side is None
