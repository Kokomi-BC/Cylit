"""公式卡的 LaTeX 清理与 `partial` 标记（纯离线，不碰数据库）。

背景（2026-12，线上 97 条卡都核过）：`latex_candidate` 是**被切段的数组**，
而 `latex_final` / `latex_repaired` 常常为空 —— 于是只能拿第一段，且第一段里
还可能带着正文行号（`"5: y \\leftarrow \\Pi \\cdot x"`）。数据本身不动，只做两件事：
剥掉行首「行号 + 冒号/」前缀（**只去前缀，不拼段**），并把「只拿到一段」标成 `partial`。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from backend.services.paper import reader_service as rs


def _card(**kwargs) -> SimpleNamespace:
    base = dict(id="c01", page_number=5, paper_id="p", grade="A", is_inline=False,
                image_path="/x/crop.png", latex_candidate=None,
                latex_repaired="", latex_final="")
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_line_number_prefix_is_stripped() -> None:
    item = rs._parse_formula_item(_card(
        latex_candidate=json.dumps(["5: y " + chr(92) + "leftarrow " + chr(92) + "Pi "
                                    + chr(92) + "cdot x", "k" + chr(92) + "in [2]"])))
    assert item["latex"] == "y " + chr(92) + "leftarrow " + chr(92) + "Pi " + chr(92) + "cdot x"


def test_plain_formula_starting_with_a_digit_is_not_touched() -> None:
    """`2 x` 这种以数字开头的正常公式不能剥（只认带冒号/点号的行号前缀）。"""
    item = rs._parse_formula_item(_card(latex_candidate=json.dumps(["2 x"])))
    assert item["latex"] == "2 x"


def test_multi_piece_candidate_without_final_is_partial() -> None:
    item = rs._parse_formula_item(_card(
        latex_candidate=json.dumps(["C(f, b) :=min", "|x -c| dx.", "ci-1+ci"])))
    assert item["partial"] is True
    assert item["latex"] == "C(f, b) :=min"        # 只取第一段，不猜着拼


def test_card_with_final_latex_is_not_partial() -> None:
    item = rs._parse_formula_item(_card(
        latex_candidate=json.dumps(["a", "b"]), latex_final="a + b"))
    assert item["partial"] is False
    assert item["latex"] == "a + b"


def test_single_piece_candidate_is_not_partial() -> None:
    item = rs._parse_formula_item(_card(latex_candidate=json.dumps(["d " + chr(92) + "times d"])))
    assert item["partial"] is False
