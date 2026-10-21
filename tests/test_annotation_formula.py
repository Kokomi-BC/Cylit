"""公式高亮：坐标系里公式只占 1 个 U+FFFC，所以 LaTeX 探针要靠 find_formula_span 换位置。

agent 报过「公式没法加高亮」：它在公式那面试了整式与片段都不命中。原因不是渲染不支持 ——
前端 wrapRange 早就把含 ATOM 的选区整块包起来（块级公式还专门画在 .katex-html 外接框上），
缺的是「把 LaTeX 换成那个占位符的偏移量」这一步。
"""

from __future__ import annotations

import pytest

from backend.services.agent import annotations as ann


ROW_MD = "本页讨论量化误差 $$D := \\mathbb{E}\\langle y, x\\rangle$$ 的定义。"


def _payloads(*rows: dict) -> list[dict]:
    return [{"page": 1, "rows": list(rows)}]


@pytest.fixture()
def paper(monkeypatch):
    rows = [{"id": "p001-00", "original": ROW_MD, "translation": ROW_MD}]
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: _payloads(*rows))
    monkeypatch.setattr(ann.svc, "anchor_page", lambda block_id: 1)
    made: list[dict] = []

    def fake_create(payload: dict, user_id: int = 1) -> dict:
        made.append(dict(payload))
        return {"id": "h-test", "text": payload.get("selected_text"),
                "color": payload.get("color"), "side": payload.get("side")}

    monkeypatch.setattr(ann.svc, "create_highlight", fake_create)
    return made


def test_formula_span_points_at_the_atom():
    rendered = ann.render_like_dom(ROW_MD)
    assert ann.ATOM in rendered                       # 公式在坐标系里就是 1 个占位符
    span = ann.find_formula_span(ROW_MD, "$$D := \\mathbb{E}\\langle y, x\\rangle$$")
    assert span is not None
    start, end = span
    assert end - start == 1 and rendered[start:end] == ann.ATOM


def test_formula_span_accepts_a_bare_latex_fragment():
    """模型常常只给不带 $$ 的片段（甚至只是式子的一部分）。"""
    assert ann.find_formula_span(ROW_MD, "D := \\mathbb{E}\\langle y, x\\rangle") is not None
    assert ann.find_formula_span(ROW_MD, "D:=:=") is None       # 太短 / 不像公式：不猜


def test_create_highlight_lands_on_the_formula_in_the_chinese_side(paper):
    """公式默认标在**译文面**（用户平时看中文那一列），存的是 LaTeX 而不是隐形占位符。"""
    probe = "$$D := \\mathbb{E}\\langle y, x\\rangle$$"
    got = ann.create("p1", anchor="p001-00", text=probe)
    assert got["ok"] is True and got["scope"] == "row"
    payload = paper[-1]
    assert payload["side"] == "zh"
    assert payload["selected_text"] == probe
    rendered = ann.render_like_dom(ROW_MD)
    assert rendered[payload["start_offset"]:payload["end_offset"]] == ann.ATOM


def test_create_highlight_still_handles_plain_text(paper):
    """文字探针不受影响（回归）：中文句子仍自动落到译文面。"""
    got = ann.create("p1", anchor="p001-00", text="本页讨论量化误差")
    assert got["ok"] is True
    payload = paper[-1]
    assert payload["side"] == "zh" and payload["selected_text"] == "本页讨论量化误差"
