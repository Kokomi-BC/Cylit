r"""正文改动之后，高亮与笔记要**跟着对**（2026-12-07 用户实测发现的真问题）。

现象：把第 4、7、19 页的 `\sqrt{3\pi^2}` 改成 `\sqrt{3}\,\pi^2`，正文与索引都更新了，
可高亮的 `text` 与偏移还是旧的 —— 引文与正文对不上、阅读视图可能画不出那个锚点，
挂在它上面的笔记 `selected_text` 也留着旧写法。

第一版按"编辑差异"映射失败（改动日志记的是整页 Markdown，高亮偏移是行内坐标），
现在不依赖旧文本：拿那一行**现在**的 Markdown 重算。
"""
from __future__ import annotations

from backend.services.agent import annotations as ann

PAPER = "demo-paper"


def _install(monkeypatch, items: list[dict], rows: dict[str, str]):
    saved: dict = {}
    monkeypatch.setattr(ann.svc, "list_highlights",
                        lambda pid: {"items": [dict(one) for one in items]})
    monkeypatch.setattr(ann.svc, "anchor_page", lambda block: 4)
    monkeypatch.setattr(ann, "anchor_text",
                        lambda pid, block, side="en": rows.get(block))

    def fake_update(hid, payload):
        saved[hid] = payload
        return {"id": hid, **payload}

    monkeypatch.setattr(ann.svc, "update_highlight", fake_update)
    return saved


def test_formula_highlight_takes_the_current_latex(monkeypatch) -> None:
    r"""公式内部的改动在渲染文字里看不见（整条公式只占一个 ATOM），但存的是 LaTeX。"""
    old = {"id": "h-1", "block_id": "p004-03", "side": "zh", "start": 4, "end": 5,
           "text": r"\sqrt{3\pi^2}", "created_at": "2026-12-07T09:00:00"}
    saved = _install(monkeypatch, [old], {"p004-03": r"see $\sqrt{3}\,\pi^2$ here"})
    out = ann.realign_page(PAPER, 4, "translation")
    assert out["formula"] == 1 and out["moved"] == 0
    assert saved["h-1"] == {"selected_text": r"\sqrt{3}\,\pi^2"}


def test_text_highlight_moves_when_the_row_changed_in_front(monkeypatch) -> None:
    hl = {"id": "h-2", "block_id": "p004-03", "side": "zh", "start": 6, "end": 10,
          "text": "beta", "created_at": "2026-12-07T09:00:00"}
    saved = _install(monkeypatch, [hl], {"p004-03": "NEW alpha beta gamma"})
    out = ann.realign_page(PAPER, 4, "translation")
    assert out["moved"] == 1
    assert saved["h-2"]["start_offset"] == 10 and saved["h-2"]["end_offset"] == 14
    assert saved["h-2"]["selected_text"] == "beta"


def test_untouched_highlight_is_left_alone(monkeypatch) -> None:
    hl = {"id": "h-3", "block_id": "p004-03", "side": "zh", "start": 6, "end": 10,
          "text": "beta", "created_at": "2026-12-07T09:00:00"}
    saved = _install(monkeypatch, [hl], {"p004-03": "alpha beta gamma"})
    out = ann.realign_page(PAPER, 4, "translation")
    assert out["moved"] == 0 and out["formula"] == 0 and saved == {}


def test_other_side_and_page_are_skipped(monkeypatch) -> None:
    saved = _install(monkeypatch, [
        {"id": "h-en", "block_id": "p004-03", "side": "en", "start": 0, "end": 3,
         "text": "abc", "created_at": "2026-12-07T09:00:00"},
        {"id": "h-zh", "block_id": "p004-03", "side": "zh", "start": 0, "end": 3,
         "text": "abc", "created_at": "2026-12-07T09:00:00"},
    ], {"p004-03": "XYZabc"})
    out = ann.realign_page(PAPER, 4, "translation")
    assert out["moved"] == 1 and "h-zh" in saved and "h-en" not in saved


def test_only_before_limits_history_replay(monkeypatch) -> None:
    saved = _install(monkeypatch, [
        {"id": "h-old", "block_id": "p004-03", "side": "zh", "start": 0, "end": 3,
         "text": "abc", "created_at": "2026-12-07T09:00:00"},
        {"id": "h-new", "block_id": "p004-03", "side": "zh", "start": 0, "end": 3,
         "text": "abc", "created_at": "2026-12-07T11:00:00"},
    ], {"p004-03": "XYZabc"})
    out = ann.realign_page(PAPER, 4, "translation", only_before="2026-12-07T10:00:00")
    assert out["moved"] == 1 and "h-old" in saved and "h-new" not in saved


def test_missing_quote_is_reported_as_stale_not_guessed(monkeypatch) -> None:
    hl = {"id": "h-4", "block_id": "p004-03", "side": "zh", "start": 0, "end": 5,
          "text": "gone!", "created_at": "2026-12-07T09:00:00"}
    saved = _install(monkeypatch, [hl], {"p004-03": "完全是别的内容"})
    out = ann.realign_page(PAPER, 4, "translation")
    assert out["stale"] == 1 and saved == {}