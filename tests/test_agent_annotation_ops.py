r"""标注的三件补充：重叠提醒、重复笔记提醒、改锚点（笔记跟着走）。

用户口径 2026-12-07：
① 新建时如果与已有高亮重叠、或写了几乎一样的笔记，**要提醒**（只提醒不拒绝）；
② 标错一行时能**挪**锚点，而不是删了重建 —— 删高亮会连带删掉它挂的笔记。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.deps import Deps
from backend.agent.tools import highlight_manage
from backend.services.agent import annotations as ann

PAPER = "demo-paper"
ZH_TEXT = "随机旋转在量化与低精度 transformer 文献中是一种有效的技术"
ROW = "我们的量化器首先对 $d$ 维输入向量进行随机旋转。随机旋转在量化与低精度 " \
      "transformer 文献中是一种有效的技术，此前已被多次使用。"


def _install(monkeypatch, pages: list[dict]) -> None:
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: pages)


def _payload() -> list[dict]:
    return [{"page": 2, "rows": [{"id": "p002-04", "original": "Random rotation ...",
                                  "translation": ROW}],
             "original_md": "Random rotation ...", "translation_md": ROW}]


class _Capture:
    """替掉 `svc.create_highlight`：只记下要落库的那一条，不碰数据库。"""

    def __init__(self) -> None:
        self.grabbed: dict = {}

    def __call__(self, payload, user_id=1):
        self.grabbed.update(payload)
        return {"id": "h-new", "block_id": payload["block_id"],
                "text": payload["selected_text"], "side": payload["side"],
                "scope": payload["scope"], "color": payload["color"],
                "start": payload["start_offset"], "end": payload["end_offset"]}


def _prepare(monkeypatch, existing: list[dict] | None = None):
    capture = _Capture()
    monkeypatch.setattr(ann.svc, "create_highlight", capture)
    monkeypatch.setattr(ann, "spare_color_for", lambda pid: "yellow")
    monkeypatch.setattr(ann.svc, "list_highlights",
                        lambda pid: {"items": existing or [], "count": len(existing or [])})
    _install(monkeypatch, _payload())
    return capture


def test_overlap_is_reported_but_not_refused(monkeypatch) -> None:
    """已有高亮压在同一段上：照样建，但回执要说清是哪一条。"""
    capture = _prepare(monkeypatch, [{"id": "h-old", "block_id": "p002-04", "side": "zh",
                                      "start": 0, "end": 30, "text": "随机旋转在量化与低精度"}])
    out = ann.create(PAPER, anchor="md:p2", text=ZH_TEXT)
    assert out["ok"] is True and capture.grabbed, "重叠只提醒，不拦"
    assert out["overlap"][0]["id"] == "h-old"
    assert "重叠" in out["warning"]


def test_no_overlap_no_warning(monkeypatch) -> None:
    _prepare(monkeypatch, [{"id": "h-far", "block_id": "p009-01", "side": "zh",
                            "start": 0, "end": 5, "text": "别处"}])
    out = ann.create(PAPER, anchor="md:p2", text=ZH_TEXT)
    assert "overlap" not in out


def test_relocate_only_reports_the_target(monkeypatch) -> None:
    """改锚点要先算落点：这一步**绝不能**顺手建出一条新的。"""
    capture = _prepare(monkeypatch)
    out = ann.relocate(PAPER, anchor="md:p2", text=ZH_TEXT)
    assert out["ok"] is True and out["dry_run"] is True
    assert out["target"]["anchor"] == "p002-04" and out["target"]["side"] == "zh"
    assert capture.grabbed == {}, "dry_run 不许落库"


def test_duplicate_note_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(ann.svc, "list_highlights", lambda pid: {"items": [
        {"id": "h-1", "block_id": "p002-04", "text": "随机旋转", "start": 0, "end": 4}]})
    monkeypatch.setattr(ann.svc, "create_note", lambda payload: {
        "id": "n-new", "page": 2, "scope": "row", "selected_text": "随机旋转"})
    monkeypatch.setattr(ann.svc, "list_notes", lambda pid: {"items": [
        {"id": "n-old", "content_md": "随机旋转在这里是关键的一步"}]})
    note = ann.create_note(PAPER, highlight_id="h-1", content="随机旋转在这里是关键的一步")
    assert note["similar"][0]["id"] == "n-old"
    assert "很像" in note["warning"]


def test_different_note_is_not_flagged(monkeypatch) -> None:
    monkeypatch.setattr(ann.svc, "list_highlights", lambda pid: {"items": [
        {"id": "h-1", "block_id": "p002-04", "text": "随机旋转", "start": 0, "end": 4}]})
    monkeypatch.setattr(ann.svc, "create_note", lambda payload: {"id": "n-new", "page": 2})
    monkeypatch.setattr(ann.svc, "list_notes", lambda pid: {"items": [
        {"id": "n-old", "content_md": "完全另一回事：这张表里的召回数字对不上"}]})
    note = ann.create_note(PAPER, highlight_id="h-1", content="随机旋转在这里是关键的一步")
    assert "similar" not in note and "warning" not in note


class _MoveHost:
    def __init__(self, located: dict | None = None) -> None:
        self.payload: dict = {}
        self.located = located or {"ok": True, "page": 6,
                                   "target": {"anchor": "p006-03", "side": "zh", "scope": "row",
                                              "start": 5, "end": 9, "text": "随机旋转"}}

    async def highlights(self, paper_id, **kw):
        return {"ok": True, "highlight": {"id": kw.get("highlight_id"), "text": "随机旋转"}}

    async def annotation_locate(self, paper_id, payload):
        self.locate_payload = payload
        return self.located

    async def highlight_update(self, highlight_id, payload):
        self.payload = payload
        return {"id": highlight_id, "block_id": payload.get("block_id")}


def _ctx(host) -> SimpleNamespace:
    return SimpleNamespace(deps=Deps(cfg=SimpleNamespace(), host=host, paper_id="p", registry=None))


def test_update_with_anchor_moves_the_same_highlight() -> None:
    host = _MoveHost()
    out = asyncio.run(highlight_manage(_ctx(host), op="update", highlight_id="h-1",
                                       anchor="md:p6", text="随机旋转"))
    assert out["ok"] is True
    assert host.payload["block_id"] == "p006-03" and host.payload["start_offset"] == 5
    assert host.payload["side"] == "zh" and host.payload["scope"] == "row"
    assert out["moved_to"]["page"] == 6 and "第 6 页" in out["summary"]
    assert "笔记跟着走" in out["note"]


def test_update_reuses_the_existing_quote_when_text_is_omitted() -> None:
    host = _MoveHost()
    out = asyncio.run(highlight_manage(_ctx(host), op="update", highlight_id="h-1",
                                       anchor="md:p6"))
    assert out["ok"] is True and host.locate_payload["text"] == "随机旋转"


def test_move_fails_without_changing_anything() -> None:
    host = _MoveHost({"ok": False, "error": "这段文字在 md:p6 里找不到"})
    out = asyncio.run(highlight_manage(_ctx(host), op="update", highlight_id="h-1",
                                       anchor="md:p6", text="随机旋转"))
    assert out["ok"] is False and "找不到" in out["error"]
    assert host.payload == {}, "定位失败就什么都不改"