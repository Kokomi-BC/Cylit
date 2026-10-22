r"""列表项里「整行就是一条公式」→ 按块级算（第 4 页定理式可用的前提）。

线上实测：TurboQuant 第 4 页整页没有一条 ``$$…$$``，定理/命题的定义式全写成**列表项里的
行内公式**（``- $D_{\text{mse}}(Q) := … \geq 4^{-b}$``）。不提升的话这一页在 markdown 源里
``display`` 计数为 0，公式列表只能回落到解析卡，拿到 ``\pi \cdot \| y\| \sqrt{3}`` 这种碎片。

判据见 ``reader_service._promote_to_display``：列表标记后无散文、行尾只剩短尾巴、公式够长。
"""
from __future__ import annotations

import pytest

from backend.services.agent import project


#: 第 4 页真实行文本（页级翻译产物的 `original`）—— 不改造，照抄线上数据
P4_01 = ("- $\\mathbb{E}\\left[\\langle \\boldsymbol{y}, Q_{\\text{prod}}^{-1}"
         "\\left(Q_{\\text{prod}}(\\boldsymbol{x})\\right)\\rangle\\right]"
         " = \\langle \\boldsymbol{y}, \\boldsymbol{x}\\rangle$\n"
         "- $D_{\\text{prod}}(Q_{\\text{prod}}) := \\mathbb{E}\\left[\\left|"
         "\\langle \\boldsymbol{y}, \\boldsymbol{x}\\rangle - \\langle \\boldsymbol{y}, "
         "Q_{\\text{prod}}^{-1}\\left(Q_{\\text{prod}}(\\boldsymbol{x})\\right)\\rangle"
         "\\right|^2\\right] \\leq \\frac{\\sqrt{3\\pi^2} \\|\\boldsymbol{y}\\|_2^2}{d}"
         " \\cdot \\frac{1}{4^b}$ for any $b \\geq 0$.\n"
         "- For small bit-widths the bound refines: for $b = 1, 2, 3, 4$ we have "
         "$D_{\\text{prod}} \\approx \\frac{1.57}{d}$.")

P4_03 = ("- $D_{\\text{mse}}(Q) := \\mathbb{E}\\left[\\left\\|\\boldsymbol{x} - "
         "Q^{-1}\\left(Q(\\boldsymbol{x})\\right)\\right\\|_2^2\\right] \\geq "
         "\\frac{1}{4^b}$\n"
         "- $D_{\\text{prod}}(Q) = \\mathbb{E}\\left[\\left|\\langle \\boldsymbol{y}, "
         "\\boldsymbol{x}\\rangle - \\langle \\boldsymbol{y}, Q^{-1}\\left(Q(\\boldsymbol{x})"
         "\\right)\\rangle\\right|^2\\right] \\geq \\frac{\\|\\boldsymbol{y}\\|_2^2}{d}"
         " \\cdot \\frac{1}{4^b}$")

P4_02 = ("**Lower Bound.**  As shown in Theorem 3 in the appendix, we prove that for any "
         "randomized quantization algorithm $Q : \\mathbb{R}^d \\to \\{0, 1\\}^{b \\cdot d}$ "
         "with bit-width $b$, there exist hard instances.")


@pytest.fixture
def page4(monkeypatch):
    from backend.services.paper import reader_service

    def install(rows: list[dict], page: int = 4) -> None:
        payload = {"page": page, "model": "demo", "rows": rows}
        monkeypatch.setattr(reader_service, "page_translations", lambda pid: [payload])
        monkeypatch.setattr(reader_service, "_parse_formulas", lambda pid: [])
    return install


def _rows(*texts: str) -> list[dict]:
    return [{"id": f"p004-{i:02d}", "original": t, "translation": t}
            for i, t in enumerate(texts)]


def test_list_item_formula_counts_as_block_level(page4) -> None:
    """第 4 页那 4 条定义式（整行就是公式）→ display=True，页内块级从 0 变 4。"""
    from backend.services.paper import reader_service

    page4(_rows(P4_01, P4_02, P4_03))
    data = reader_service.markdown_formulas("demo")
    display = [i["latex"] for i in data["items"] if i["display"]]
    assert len(display) == 4, display
    assert data["display_count"] == 4
    # 提到的是那四条"整行公式"，不是 `- For small bit-widths … $b = 1, 2, 3, 4$` 那种半句
    assert not any("1.57" in one for one in display), display


def test_mid_sentence_formula_stays_inline(page4) -> None:
    """句首有散文 → 句中公式不提升（否则每一页都会冒出十几条"块级"）。"""
    from backend.services.paper import reader_service

    page4(_rows(P4_02))
    data = reader_service.markdown_formulas("demo")
    assert data["items"], "句中公式仍要进列表，只是不算块级"
    assert all(i["display"] is False for i in data["items"])
    assert data["display_count"] == 0


def test_long_tail_keeps_formula_inline() -> None:
    """行尾散文超过阈值 → 只是行内公式（`- $x$ and then …` 不是一条独立公式）。"""
    from backend.services.paper import reader_service

    rows = [{"id": "p004-00", "translation": "", "original":
             "- $\\alpha = \\beta + \\gamma$ and this sentence keeps going on and on "
             "with quite a lot of prose attached to it, well past the tail limit."}]
    payload = {"page": 4, "model": "demo", "rows": rows}
    import backend.services.paper.reader_service as rs
    original = rs.page_translations
    try:
        rs.page_translations = lambda pid: [payload]
        rs._parse_formulas = lambda pid: []
        data = rs.markdown_formulas("demo")
    finally:
        rs.page_translations = original
    assert data["display_count"] == 0


def test_display_count_matches_promoted_items(page4) -> None:
    """`display_count` 必须与条目里的 display 数量一致（回执靠它判断"给了几条完整公式"）。"""
    from backend.services.paper import reader_service

    page4(_rows(P4_01, P4_02, P4_03))
    data = reader_service.markdown_formulas("demo")
    assert data["display_count"] == sum(1 for i in data["items"] if i["display"])
    assert data["count"] == len(data["items"])


def test_page4_no_longer_falls_back_to_parse_cards(monkeypatch) -> None:
    """提升之后第 4 页有块级公式了 → 不再回落解析卡（碎片不再出现）。"""
    from backend.services.paper import reader_service

    md = [{"id": "f004-0001", "page": 4, "row_id": "p004-03",
           "latex": "D_{\\text{mse}}(Q) := \\mathbb{E}[\\|x - Q^{-1}(Q(x))\\|_2^2] "
                    "\\geq \\frac{1}{4^b}", "display": True, "context": "",
           "context_zh": "", "source": "markdown"}]
    cards = [{"id": "turboquanton-p04-x01", "page": 4, "row_id": "turboquanton-p04-x01",
              "latex": "\\pi \\cdot \\| y\\| \\sqrt{3}", "display": True,
              "grade": "B", "partial": True, "context": "", "context_zh": "",
              "source": "parse"}]
    monkeypatch.setattr(project.reader_service, "formulas_payload",
                        lambda pid, source="markdown": {
                            "items": md if source == "markdown" else cards,
                            "source": source})
    monkeypatch.setattr(project.paper_service, "get_paper",
                        lambda pid: {"id": pid, "page_count": 22})
    out = project.formulas("p1", page=4)
    assert [i["source"] for i in out["items"]] == ["markdown"]
    assert all("\\sqrt{3}" not in i["latex"] for i in out["items"])




# --------------------------------------------------------------------------
# 列表项里的长公式（第 7 页那三条定义式）：文字 + 公式混排也要算块级
# --------------------------------------------------------------------------

P7_00 = ("- *Expected inner-product* $\\mathbb{E}_{\\tilde{x}}\\left[\\langle y, \\tilde{x}\\rangle\\right] = \\langle y, x\\rangle$")
P7_01 = ("- *Inner-product distortion defined as* $D_{\\text{prod}} := \\mathbb{E}_{\\tilde{x}}\\left[\\left|\\langle y, x\\rangle - \\langle y, \\tilde{x}\\rangle\\right|^2\\right]$ *is bounded by* $D_{\\text{prod}} \\leq \\frac{\\sqrt{3\\pi^2}\\,\\|y\\|_2^2}{d} \\cdot \\frac{1}{4^b}$ *for any* $b \\geq 0$.")


def test_list_item_with_a_label_and_long_formula_counts_as_block_level(page4) -> None:
    """第 7 页那种「*标签* + 长公式」的列表项要算块级（以前整页 0 条，只能回落到解析碎片）。"""
    from backend.services.paper import reader_service

    page4([{"id": "p007-00", "original": P7_00, "translation": P7_00},
           {"id": "p007-01", "original": P7_01, "translation": P7_01}], page=7)
    data = reader_service.markdown_formulas("demo")
    display = [i["latex"] for i in data["items"] if i["display"]]
    assert len(display) == 3, display          # 00 一条 + 01 两条
    assert any("3\\pi^2" in one for one in display), display


def test_short_symbol_in_a_bullet_stays_inline(page4) -> None:
    """列表项里的**短**符号（`$b \\geq 0$` 这种）不该被当成块级公式。"""
    from backend.services.paper import reader_service

    page4([{"id": "p007-05", "original": "- the bound holds for $b \\geq 0$ always",
            "translation": "同上"}], page=7)
    data = reader_service.markdown_formulas("demo")
    assert data["display_count"] == 0



# --------------------------------------------------------------------------
# 两源合一：只有**覆盖得够多**的提取卡才挂 bbox/裁图
# --------------------------------------------------------------------------


def _card_item(pid: str, latex: str, page: int = 4, grade: str = "A") -> dict:
    from types import SimpleNamespace

    return SimpleNamespace(id=f"card-{page}", page_number=page, paper_id=pid, grade=grade,
                           is_inline=False, image_path="/x/crop.png",
                           latex_candidate="[]", latex_repaired="", latex_final=latex,
                           bbox_json="[1, 2, 3, 4]")


def test_card_attached_only_when_it_covers_the_formula(monkeypatch) -> None:
    """卡 ⊇ 整式 → 挂；卡只是整式的一小片（8%）→ **不挂**（宁可不给裁图）。"""
    from backend.services.paper import reader_service as rs

    whole = r"\mathbb{E}_{\tilde{x}}\left[\langle y, \tilde{x}\rangle\right] = \langle y, x\rangle"
    monkeypatch.setattr(rs, "_parse_formulas", lambda pid: [
        _card_item("p", whole),                       # 整式（卡比公式大 → 安全）
        _card_item("p", "Q(x))\rangle", page=5),      # 碎片
    ])
    items = [
        {"page": 4, "latex": whole + " \\cdot 1"},
        {"page": 5, "latex": "D_{\\text{prod}}(Q) := \\mathbb{E}[|\\langle y, x\\rangle|^2]"},
    ]
    monkeypatch.setattr(rs, "_figure_boxes", lambda pid, page: [])
    rs._attach_cards("p", items)
    assert items[0]["bbox"] == [1, 2, 3, 4] and items[0]["grade"] == "A"
    assert items[1].get("bbox") is None, "8% 的碎片不该挂裁图（会让模型看到残片）"
    assert items[1].get("grade") is None

def test_three_counts_share_one_denominator(monkeypatch) -> None:
    """`total == display_total + inline_total` 恒成立，且不随 include_inline 变。"""
    md = [{"id": f"f005-{i}", "page": 5, "row_id": f"p005-{i:02d}", "latex": t,
           "display": d, "context": "", "context_zh": "", "source": "markdown"}
          for i, (t, d) in enumerate([("a", False), ("b", True), ("c", False),
                                      ("d", True), ("e", False)])]
    # 第 6 页在 markdown 源里一条块级都没有 → 用解析卡兜底（混源）
    md.append({"id": "f006-1", "page": 6, "row_id": "p006-01", "latex": "x",
               "display": False, "context": "", "context_zh": "", "source": "markdown"})
    cards = [{"id": "card-6", "page": 6, "row_id": "card-6", "latex": "y = z",
              "display": True, "grade": "A", "partial": False, "context": "",
              "context_zh": "", "source": "parse"}]
    monkeypatch.setattr(project.reader_service, "formulas_payload",
                        lambda pid, source="markdown": {
                            "items": md if source == "markdown" else cards,
                            "source": source})
    monkeypatch.setattr(project.paper_service, "get_paper",
                        lambda pid: {"id": pid, "page_count": 22})

    for include_inline in (False, True):
        out = project.formulas("p1", include_inline=include_inline)
        assert out["total"] == out["display_total"] + out["inline_total"]
        assert out["source"] == "mixed"
        assert out["omitted_inline"] == (0 if include_inline else out["inline_total"])
    plain = project.formulas("p1")
    full = project.formulas("p1", include_inline=True)
    # 5 条第 5 页 + 1 条第 6 页行内 = 6，再加第 6 页那张兜底解析卡 = 7；display = 2 + 1 = 3
    assert (plain["total"], plain["display_total"], plain["inline_total"]) == (7, 3, 4)
    assert full["returned"] == 7 and plain["returned"] == 3
