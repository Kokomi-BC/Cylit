"""Agent 的高亮 / 笔记手脚（《修改计划》第六步）单测。

这一层最容易错的是**坐标系**：`Highlight` 存的是"某一面**渲染文字**里的偏移量"，
而模型只会给"要标哪段文字"。所以这里钉住"Markdown → 渲染文字"的近似规则，
以及"按文字找偏移量 → 落库 → 定位信息"这条链。
"""

from __future__ import annotations

import pytest

from backend.services.agent import annotations as ann


# --------------------------------------------------------------------------
# 1. 坐标系：Markdown → 前端 `renderedText` 的近似
# --------------------------------------------------------------------------


def test_render_strips_markers_but_keeps_the_words() -> None:
    assert ann.render_like_dom("这是**重点**内容") == "这是重点内容"
    assert ann.render_like_dom("## 标题") == "标题"
    assert ann.render_like_dom("- 列表项") == "列表项"
    assert ann.render_like_dom("> 引用") == "引用"
    assert ann.render_like_dom("见 [图 3](http://x) 说明") == "见 图 3 说明"
    # 图片在前端是 `<img>`，**不产生文字**（alt 不算）
    assert ann.render_like_dom("![](../figures/x.png)结论") == "结论"


def test_render_counts_a_formula_as_one_char() -> None:
    """公式整体算 1 个字符（与 `domMarks.ATOM` 同一口径）——差一个字符后面全错位。"""
    out = ann.render_like_dom("公式 $x^2 + y^2$ 占一个字符")
    assert out == f"公式 {ann.ATOM} 占一个字符"
    assert len(out) == len("公式 ") + 1 + len(" 占一个字符")
    disp = ann.render_like_dom("$$\nL = L\n$$")
    assert disp.count(ann.ATOM) == 1


def test_render_joins_soft_breaks_like_the_frontend() -> None:
    """段内软换行要接回：中文之间不补空格、西文之间一个空格（与前端同一套规则）。"""
    assert ann.render_like_dom("这是第一句。\n这是第二句。") == "这是第一句。这是第二句。"
    assert ann.render_like_dom("the quick brown\nfox") == "the quick brown fox"
    # 下一行是新块（列表项）就不接
    assert ann.render_like_dom("- 甲\n- 乙") == "甲\n乙"


def test_find_span_and_near_misses() -> None:
    text = "第一段。第二段。第三段。"
    assert ann.find_span(text, "第二段") == (4, 7)
    assert ann.find_span(text, "第二段", occurrence=2) is None
    assert ann.find_span(text, "   ") is None
    assert ann.near_misses("Attention Is All You Need", "Attention Is All Yon") != []


# --------------------------------------------------------------------------
# 2. 按文字建高亮（agent 的入口）
# --------------------------------------------------------------------------


def test_anchor_kind() -> None:
    assert ann.anchor_kind("p004-02") == "row"
    assert ann.anchor_kind("md:p3") == "markdown"
    assert ann.anchor_kind("pdf:p1") == "pdf"


def test_create_refuses_the_pdf_face_with_a_usable_hint(monkeypatch) -> None:
    """PDF 那面的文字层在前端 pdf.js 里 —— 服务端算不出偏移量，必须**说清怎么办**。"""
    got = ann.create("p1", anchor="pdf:p1", text="Attention")
    assert got["ok"] is False and "自己划词" in got["error"]


def test_create_computes_offsets_over_the_rendered_text(monkeypatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(ann, "anchor_text", lambda pid, anchor, side="en": "这是**重点**内容，要标起来")
    monkeypatch.setattr(ann.svc, "create_highlight",
                        lambda payload, user_id=1: captured.update(payload) or {
                            "id": "h1", "block_id": payload["block_id"],
                            "text": payload["selected_text"], "start": payload["start_offset"],
                            "end": payload["end_offset"], "color": payload["color"],
                            "side": payload["side"], "scope": payload["scope"], "page": 1})
    got = ann.create("p1", anchor="p001-02", text="重点内容", color="green")
    assert got["ok"] is True and got["page"] == 1 and got["scope"] == "row"
    # 偏移量算在**渲染文字**上（`**` 不算字）→ 2..6；存下来的也是那一段
    assert (captured["start_offset"], captured["end_offset"]) == (2, 6)
    assert captured["selected_text"] == "重点内容"
    # 色名是**输入别名**，落库值统一是 hex（`annotation_service.HIGHLIGHT_PALETTE`）
    assert captured["color"] == "#3ccd82" and captured["scope"] == "row"
    assert got["highlight"]["id"] == "h1"


def test_create_gives_near_misses_when_the_text_is_not_found(monkeypatch) -> None:
    monkeypatch.setattr(ann, "anchor_text", lambda pid, anchor, side="en": "Attention Is All You Need")
    got = ann.create("p1", anchor="p001-00", text="Attention Is All Yon")
    assert got["ok"] is False and "找不到" in got["error"] and got["near_miss"]


def test_create_reports_a_missing_anchor(monkeypatch) -> None:
    monkeypatch.setattr(ann, "anchor_text", lambda pid, anchor, side="en": None)
    got = ann.create("p1", anchor="p999-99", text="任意")
    assert got["ok"] is False and "找不到这个锚点" in got["error"]


def test_create_resolves_the_face_from_the_quoted_language(monkeypatch) -> None:
    """面没指定时按**引用的文字**判：引中文 → 译文面（用户报过"未产生高亮"）。

    高亮只画在它那一面：界面按译文读（`pside=zh`，读者最常这么看）时只渲染中文那一列，
    标在原文面的高亮**根本画不出来** —— 锚点对、文字对，用户就是看不见。
    """
    captured: dict = {}

    def fake_create(payload, user_id=1):
        captured.update(payload)
        return {"id": "h1", "block_id": payload["block_id"], "text": payload["selected_text"],
                "start": payload["start_offset"], "end": payload["end_offset"],
                "color": payload["color"], "side": payload["side"], "scope": payload["scope"],
                "page": 9}

    monkeypatch.setattr(ann.svc, "create_highlight", fake_create)
    monkeypatch.setattr(ann, "anchor_text", lambda pid, anchor, side="en": "乘积量化用 k-means")
    got = ann.create("p1", anchor="p009-08", text="乘积量化", side="")
    assert got["ok"] is True and captured["side"] == "zh"
    assert (captured["start_offset"], captured["end_offset"]) == (0, 4)

    captured.clear()
    monkeypatch.setattr(ann, "anchor_text", lambda pid, anchor, side="en": "Product Quantization")
    ann.create("p1", anchor="p009-08", text="Product Quantization", side="")
    assert captured["side"] == "en"

    # **中文引文一律落译文面**，调用方显式给 `en` 也不听：中文只存在于译文行，标到原文面
    # 就画不出来。实测（2026-12-07）4 条中文高亮全被判成 en 面 → 只能退回 `md:pN`（阅读视图
    # 里看不见）。这条与"公式一律先落译文面"是同一条规则。
    captured.clear()
    monkeypatch.setattr(ann, "anchor_text", lambda pid, anchor, side="en": "乘积量化用 k-means")
    ann.create("p1", anchor="p009-08", text="乘积量化", side="en")
    assert captured["side"] == "zh"

    # 英文引文仍然听调用方的 `en`
    captured.clear()
    monkeypatch.setattr(ann, "anchor_text", lambda pid, anchor, side="en": "Product Quantization")
    ann.create("p1", anchor="p009-08", text="Product Quantization", side="en")
    assert captured["side"] == "en"


# --------------------------------------------------------------------------
# 3. 笔记必须挂在高亮上（用户口径：不做孤儿笔记）
# --------------------------------------------------------------------------


def test_create_note_requires_an_existing_highlight(monkeypatch) -> None:
    monkeypatch.setattr(ann.svc, "list_highlights", lambda pid, user_id=1: {"items": [
        {"id": "h1", "block_id": "p001-02", "text": "重点内容", "start": 2, "end": 6}]})
    saved: dict = {}
    monkeypatch.setattr(ann.svc, "create_note",
                        lambda payload, user_id=1: saved.update(payload) or {"id": "n1", **payload})
    note = ann.create_note("p1", highlight_id="h1", content="我的笔记")
    assert note["id"] == "n1"
    # 锚点/引用文字都从高亮继承，不重新算
    assert saved["block_id"] == "p001-02" and saved["selected_text"] == "重点内容"
    assert saved["highlight_id"] == "h1" and saved["content_md"] == "我的笔记"

    with pytest.raises(Exception):
        ann.create_note("p1", highlight_id="", content="x")
    with pytest.raises(Exception):
        ann.create_note("p1", highlight_id="nope", content="x")


def test_locate_reads_back_through_the_note_to_its_highlight(monkeypatch) -> None:
    monkeypatch.setattr(ann.svc, "list_highlights", lambda pid, user_id=1: {"items": [
        {"id": "h1", "block_id": "pdf:p3", "text": "Attention Is All You Need", "page": 3,
         "scope": "pdf"}]})
    monkeypatch.setattr(ann.svc, "list_notes", lambda pid=None, user_id=1, q=None: {"items": [
        {"id": "n1", "block_id": "pdf:p3", "highlight_id": "h1", "content_md": "笔记",
         "page": 3, "scope": "pdf"}]})
    got = ann.locate("p1", note_id="n1")
    assert got["kind"] == "note" and got["highlight"]["id"] == "h1"
    assert got["page"] == 3 and got["scope"] == "pdf"
    assert "第 3 页" in got["hint"] and "原 PDF" in got["hint"]

    back = ann.locate("p1", highlight_id="h1")
    assert back["kind"] == "highlight" and back["note"]["id"] == "n1"
