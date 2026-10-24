"""高亮配色：多彩、尽量不一样（`annotations.spare_color`）。

用户口径（2026-12-06）：模型不给颜色时不能一律 yellow，要挑当前**用得最少**的那个；一片同色
等于没有颜色。模型显式给色时一律尊重（它可能按语义配色）。
"""

from __future__ import annotations

from backend.services.agent import annotations as ann
from backend.services.annotation import annotation_service as svc


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


# --------------------------------------------------------------------------
# 色值口径：**库里只存 hex**（色名是输入别名，见 `annotation_service.HIGHLIGHT_PALETTE`）
# --------------------------------------------------------------------------


def test_names_are_stored_as_hex() -> None:
    """色名进门就换成 hex —— 同一个颜色在库里只有一种写法。

    2026-12 起色板只有多巴胺那八个；历史色名（yellow / red …）已从色板删除，
    但仍**收**：它们走 `RETIRED_COLOR_ALIASES` 翻译成新 id 的 hex（yellow → lemon），
    落库依然是 hex —— 「删掉老名字」不等于「用户说黄的听不见」。
    """
    for name, value in svc.HIGHLIGHT_PALETTE.items():
        assert svc._norm_color(name) == value
    assert svc._norm_color("yellow") == svc.HIGHLIGHT_PALETTE["lemon"] == "#ffc93c"
    assert svc._norm_color("  RED ") == svc.HIGHLIGHT_PALETTE["strawberry"]   # 大小写/空白不影响
    assert svc._norm_color("黄") == svc.HIGHLIGHT_PALETTE["lemon"]            # 中文色词


def test_custom_hex_passes_through_unchanged() -> None:
    assert svc._norm_color("#7c3aed") == "#7c3aed"
    assert svc._norm_color("#7C3AED") == "#7c3aed"
    assert svc._norm_color("rgb(124, 58, 237)") == "rgb(124, 58, 237)"


def test_unknown_value_falls_back_to_the_first_palette_hex() -> None:
    """认不出的写法兜底成**色板第一个**（以前是写死的黄，那个色已经不在色板里了）。

    兜底前会打一条 warning（带着原始取值）—— 静默改色最坑，至少日志里查得到是谁传了什么。
    """
    first = svc.HIGHLIGHT_PALETTE[svc.HIGHLIGHT_COLORS[0]]
    for bad in ("", None, "not-a-color", "chartreuse", "#12345"):
        assert svc._norm_color(bad) == first


def test_name_and_its_hex_give_the_same_output() -> None:
    """同一个颜色的两种输入 → **同一个**落库值（迁移与写入不会分叉）。"""
    for name, value in svc.HIGHLIGHT_PALETTE.items():
        assert svc._norm_color(name) == svc._norm_color(value)
    # 旧名 → 新 id 的 hex；与新 id 自己输进去完全一致
    assert svc._norm_color("red") == svc._norm_color("strawberry")
    assert svc._norm_color("pink") == svc._norm_color("#FF5CC8")


def test_norm_color_is_idempotent() -> None:
    """迁移脚本 / 前端回写跑第二遍不改任何值。"""
    for raw in list(svc.HIGHLIGHT_PALETTE) + ["#7c3aed", "rgb(1, 2, 3)", "nope"]:
        once = svc._norm_color(raw)
        assert svc._norm_color(once) == once


def test_palette_matches_the_frontend_palette() -> None:
    """后端色板与前端 `HL_PALETTE` 必须是**同一组 hex**。

    前端那份是 TS（这边执行不了），所以读文本里的 `HL_PALETTE` 字面量块逐条比对；
    两边的意义分工不同 —— 前端要"落库之前就画出来"，后端要"落库只存 hex" —— 但**色值只有一份**。
    谁改了而另一边没跟上，这条会红。
    """
    import re
    from pathlib import Path

    ts = Path(__file__).resolve().parents[1] / "frontend/src/utils/highlightColors.ts"
    if not ts.is_file():
        import pytest
        pytest.skip("前端色板不在这个工作区")
    text = ts.read_text(encoding="utf-8")
    block = re.search(r"HL_PALETTE[^=]*=\s*\{(.*?)\}", text, re.S)
    assert block, "没从 highlightColors.ts 里找到 HL_PALETTE 字面量块"
    pairs = dict(re.findall(r"(\w+):\s*'(#[0-9a-fA-F]{6})'", block.group(1)))
    assert pairs, "HL_PALETTE 里没读到 name → hex"
    assert set(pairs) == set(svc.HIGHLIGHT_PALETTE)
    for name, hex_value in pairs.items():
        assert svc.HIGHLIGHT_PALETTE[name] == hex_value.lower()


def test_normalize_color_returns_hex_for_names() -> None:
    """agent 那条路（`annotations.normalize_color`）同样换成 hex，且**色板 / 旧名翻译优先**。

    2026-12 历史色名已从色板删除，但用户嘴上还会说「标成黄的」、模型也会照说 `yellow`：
    这类词不进色板、不进菜单，只做**翻译**（yellow → lemon 的 hex），落库仍然是 hex。
    """
    assert ann.normalize_color("lemon") == svc.HIGHLIGHT_PALETTE["lemon"] == "#ffc93c"
    assert ann.normalize_color("yellow") == svc.HIGHLIGHT_PALETTE["lemon"]    # 旧名翻译
    assert ann.normalize_color("黄色") == svc.HIGHLIGHT_PALETTE["lemon"]      # 中文色词
    assert ann.normalize_color("pink") == svc.HIGHLIGHT_PALETTE["sakura"]     # 别名优先于 CSS 词
    assert ann.normalize_color("teal") == ann.CSS_COLOR_NAMES["teal"]
    assert ann.normalize_color("nope") == ""


def test_spare_color_counts_migrated_hex_values() -> None:
    """库里的色值迁移成 hex 之后，"用得最少"的统计不能失灵。

    2026-12 色板整体换成多巴胺那八个之后，**只数字板里的颜色**：库里的历史 hex
    （`#55a5ff` 这类旧色名对应的值）不再参与计数 —— 否则「最少用过的颜色」会被一批
    用户早就改掉的旧色占位，分配出来的新色永远偏向第一个。
    """
    first, second = ann.PALETTE[0], ann.PALETTE[1]
    hex_first = svc.HIGHLIGHT_PALETTE[first]
    assert ann.spare_color([hex_first, hex_first]) == second        # 同一个色两次 → 下一个
    assert ann.spare_color([hex_first, first]) == second            # hex 与色名一起数
    assert ann.spare_color(["#55a5ff"] * 3) == first                # 历史 hex（老蓝）不计入
