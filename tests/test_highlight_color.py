"""高亮配色：多彩、尽量不一样（`annotations.spare_color`）。

用户口径（2026-12-06）：模型不给颜色时不能一律 yellow，要挑当前**用得最少**的那个；一片同色
等于没有颜色。模型显式给色时一律尊重（它可能按语义配色）。
"""

from __future__ import annotations

from backend.services.agent import annotations as ann


def test_unused_palette_returns_the_first():
    assert ann.spare_color([]) == ann.PALETTE[0]
    assert ann.spare_color(None) == ann.PALETTE[0]


def test_first_unused_color_wins():
    # 已经用了调色板前两个 → 给第三个（而不是回退到第一个已经用过的）
    used = list(ann.PALETTE[:2])
    assert ann.spare_color(used) == ann.PALETTE[2]


def test_all_used_falls_back_to_the_least_used():
    used = list(ann.PALETTE) + [ann.PALETTE[0], ann.PALETTE[0]]   # blue 用了 3 次
    assert ann.spare_color(used) == ann.PALETTE[1]                # 其余各 1 次 → 稳定的第二个


def test_unknown_and_custom_colors_are_ignored():
    """hex / 历史名（pink）不占调色板的坑，但也不能让分配崩掉。"""
    assert ann.spare_color(["#ff8800", "pink", "", None]) == ann.PALETTE[0]


def test_create_without_color_picks_the_spare_one(monkeypatch):
    rows = [{"id": "p001-00", "original": "一段普通正文。", "translation": "一段普通正文。"}]
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: [{"page": 1, "rows": rows}])
    monkeypatch.setattr(ann.svc, "anchor_page", lambda block_id: 1)
    made: list[dict] = []
    monkeypatch.setattr(ann.svc, "create_highlight",
                        lambda payload, user_id=1: made.append(dict(payload)) or {"id": "h1"})
    monkeypatch.setattr(ann, "spare_color_for", lambda pid: "indigo")
    assert ann.create("p1", anchor="p001-00", text="一段普通正文")["ok"] is True
    assert made[-1]["color"] == "indigo"
    # 显式给色就尊重它（模型可能按语义配色）
    assert ann.create("p1", anchor="p001-00", text="一段普通正文", color="#00bcd4")["ok"] is True
    assert made[-1]["color"] == "#00bcd4"
