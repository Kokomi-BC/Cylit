"""图片批注文档（大图查看器的手写笔迹 / 图片笔记）单测。

这一层最容易出事的是**脏数据**：坐标越界、工具名不认识、一笔只有一个点、
颜色是 `red` 这种名字……前端画得出来、存下去就是永久垃圾（渲染时再炸更难查）。
所以这里钉住"服务端洗成可信形状"的规则。
"""

from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.services.annotation import image_marks as ink


def test_missing_doc_is_an_empty_one() -> None:
    assert ink.normalize_doc(None) == {"v": 1, "strokes": [], "notes": []}


def test_one_point_stroke_is_dropped_and_bad_tool_falls_back() -> None:
    doc = ink.normalize_doc({"strokes": [
        {"tool": "pen", "pts": [[0.1, 0.1]]},                       # 只有一个点 → 丢
        {"tool": "airbrush", "pts": [[0.1, 0.1], [0.2, 0.2]]},       # 工具不认识 → pen
    ]})
    assert len(doc["strokes"]) == 1
    assert doc["strokes"][0]["tool"] == "pen"


def test_coordinates_and_width_are_clamped() -> None:
    doc = ink.normalize_doc({"strokes": [{
        "tool": "highlighter", "color": "#ABCDEF", "w": 99,
        "pts": [[-3, 12], [0.5, "0.25"], [float("nan"), 0.4]],
    }]})
    stroke = doc["strokes"][0]
    assert stroke["color"] == "#abcdef"
    assert stroke["w"] == ink.W_MAX
    assert stroke["pts"] == [[0.0, 1.0], [0.5, 0.25], [0.0, 0.4]]


def test_note_needs_text_and_gets_a_normalized_rect() -> None:
    doc = ink.normalize_doc({"notes": [
        {"text": "   ", "rect": [0.1, 0.1, 0.2, 0.2]},       # 空笔记 → 丢（等于"取消"）
        {"text": "这里和 3.2 节的假设冲突", "rect": [0.8, 0.6, 0.2, 0.1]},
        {"text": "只有一个点", "rect": [0.3, 0.3, 0.3, 0.3]},
    ]})
    assert len(doc["notes"]) == 2
    assert doc["notes"][0]["rect"] == [0.2, 0.1, 0.8, 0.6]     # 左上/右下归位
    small = doc["notes"][1]["rect"]
    assert small[2] - small[0] >= 0.03 and small[3] - small[1] >= 0.03


def test_document_is_capped() -> None:
    strokes = [{"tool": "pen", "pts": [[0.1, 0.1], [0.2, 0.2]]}] * (ink.STROKE_MAX + 50)
    assert len(ink.normalize_doc({"strokes": strokes})["strokes"]) == ink.STROKE_MAX
    too_many_points = {"tool": "pen", "pts": [[0.5, 0.5]] * (ink.POINT_MAX + 100)}
    assert len(ink.normalize_doc({"strokes": [too_many_points]})["strokes"][0]["pts"]) \
        == ink.POINT_MAX


def test_bad_shape_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ink.normalize_doc(["不是对象"])
    with pytest.raises(ValidationError):
        ink.normalize_doc({"strokes": "不是数组"})


def test_key_must_be_an_anchor_not_a_url() -> None:
    assert ink.check_key("media:9f8e7d") == "media:9f8e7d"
    assert ink.check_key("page:12") == "page:12"
    for bad in ("", "https://x/y.png", "media:", "media:" + "a" * 200, "MEDIA:x"):
        with pytest.raises(ValidationError):
            ink.check_key(bad)


def test_oversized_document_is_refused() -> None:
    one = {"tool": "pen", "w": 0.004, "color": "#111111",
           "pts": [[round(i / ink.POINT_MAX, 5), 0.5] for i in range(ink.POINT_MAX)]}
    strokes = [dict(one, id=f"s{i}") for i in range(ink.STROKE_MAX)]
    with pytest.raises(ValidationError):
        ink.normalize_doc({"strokes": strokes})
