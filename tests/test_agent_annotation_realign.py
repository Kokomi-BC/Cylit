r"""正文改动之后，高亮与笔记要**跟着挪**（2026-12-07 用户实测发现的真问题）。

现象：把第 4、7、19 页的 `\sqrt{3\pi^2}` 改成 `\sqrt{3}\,\pi^2`，正文与索引都更新了，
可高亮的 `text` 与偏移还是旧的 —— 引文与正文对不上、阅读视图可能画不出那个锚点，
挂在它上面的笔记 `selected_text` 也留着旧写法。

做法：两边渲染成坐标系文字（公式各占一个 ATOM），按编辑差异把旧区间映射到新区间。
"""
from __future__ import annotations

from backend.services.agent import annotations as ann

PAPER = "demo-paper"
HL = {"id": "h-1", "block_id": "p004-03", "side": "zh", "start": 10, "end": 26,
      "text": "alpha beta gamma", "created_at": "2026-12-07T10:00:00"}


def test_index_map_shifts_after_an_insertion() -> None:
    old, new = "abcdef", "abcXYdef"
    m = ann._index_map(old, new)
    assert m[0] == 0 and m[3] == 5 and m[6] == 8


def test_index_map_keeps_equal_text_untouched() -> None:
    m = ann._index_map("same", "same")
    assert m == [0, 1, 2, 3, 4]


def _install(monkeypatch, items: list[dict]):
    saved: dict = {}
    monkeypatch.setattr(ann.svc, "list_highlights",
                        lambda pid: {"items": [dict(one) for one in items]})
    monkeypatch.setattr(ann.svc, "anchor_page", lambda block: 4)

    def fake_update(hid, payload):
        saved[hid] = payload
        return {"id": hid, **payload}

    monkeypatch.setattr(ann.svc, "update_highlight", fake_update)
    return saved


def test_realign_moves_the_span_and_updates_the_text(monkeypatch) -> None:
    """正文前面插了一段 → 高亮的偏移整体右移，引用文字照样对得上。"""
    saved = _install(monkeypatch, [HL])
    prev = "0123456789alpha beta gammarest"
    new = "0123456789NEW-in-front-alpha beta gammarest"
    out = ann.realign_after_edit(PAPER, 4, "translation", prev, new)
    assert out["moved"] == 1
    got = saved["h-1"]
    assert got["start_offset"] == prev.find("alpha") + len("NEW-in-front-")
    assert got["end_offset"] - got["start_offset"] == len("alpha beta gamma")
    assert got["selected_text"] == "alpha beta gamma"


def test_realign_updates_the_quoted_words_after_an_edit_inside_the_span(monkeypatch) -> None:
    r"""改动**落在高亮内部**（就是用户那个 `\sqrt{3\pi^2}` 的情形）：引文要换成新写法。"""
    old_text = "see sqrt(3pi^2) here"
    new_text = "see sqrt(3) pi^2 here"
    hl = {"id": "h-2", "block_id": "p004-03", "side": "zh", "start": 4, "end": 15,
          "text": "sqrt(3pi^2)", "created_at": "2026-12-07T10:00:00"}
    saved = _install(monkeypatch, [hl])
    out = ann.realign_after_edit(PAPER, 4, "translation", old_text, new_text)
    assert out["moved"] == 1 and saved["h-2"]["selected_text"] == "sqrt(3) pi^2"


def test_realign_is_a_noop_when_the_text_did_not_change(monkeypatch) -> None:
    saved = _install(monkeypatch, [HL])
    out = ann.realign_after_edit(PAPER, 4, "translation", "abc", "abc")
    assert out["moved"] == 0 and saved == {}


def test_realign_skips_other_sides_and_untouched_spans(monkeypatch) -> None:
    """追加在高亮之后 → 这条不用动；两侧只处理被改的那一面。"""
    saved = _install(monkeypatch, [
        {"id": "h-en", "block_id": "p004-03", "side": "en", "start": 0, "end": 3,
         "text": "abc", "created_at": "2026-12-07T10:00:00"},
        {"id": "h-zh", "block_id": "p004-03", "side": "zh", "start": 0, "end": 3,
         "text": "abc", "created_at": "2026-12-07T10:00:00"},
    ])
    out = ann.realign_after_edit(PAPER, 4, "translation", "abc", "abcXYZ")
    assert out["moved"] == 0 and saved == {}
    out2 = ann.realign_after_edit(PAPER, 4, "translation", "abc", "XYZabc")
    assert out2["moved"] == 1 and "h-zh" in saved and "h-en" not in saved


def test_realign_respects_only_before_for_history_replay(monkeypatch) -> None:
    """补历史数据时只处理那次改动**之前**就存在的高亮，别把新建的一起挪。"""
    saved = _install(monkeypatch, [
        {"id": "h-old", "block_id": "p004-03", "side": "zh", "start": 3, "end": 6,
         "text": "def", "created_at": "2026-12-07T09:00:00"},
        {"id": "h-new", "block_id": "p004-03", "side": "zh", "start": 3, "end": 6,
         "text": "def", "created_at": "2026-12-07T11:00:00"},
    ])
    out = ann.realign_after_edit(PAPER, 4, "translation", "abcdef", "abcQQdef",
                                 only_before="2026-12-07T10:00:00")
    assert out["moved"] == 1 and "h-old" in saved and "h-new" not in saved