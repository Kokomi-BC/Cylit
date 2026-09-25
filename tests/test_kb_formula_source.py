"""公式换源（页级视觉转写 ↔ 解析产物）的对齐规则（**全离线**，纯函数）。

背景：知识库的公式条目曾经是 `公式 …：f(x) =`、`C(f, b) :=min` 这种残缺内容 ——
解析产物把一条展示公式切成了好几块，而 `latex_final` 只取候选[0]。
**页级视觉转写（`md/pages.json` 的 `original_md`，阅读器 Markdown 视图用的那份）里公式是完整的**，
所以换源即可（不需要新的视觉调用）。难点在对齐，这里守住三条：

1. 同一条公式的两种写法要能对上（命令名不同、有无 `\\left\\right`、`\\text{mse}` 之类）；
2. **短碎片不许配**（`b\\cdot d`、`\\times 10` 撞谁都能"覆盖"）；
3. **平分不许配**（p04-b11 实测：`D_mse` 与 `D_prod` 都能覆盖到 1.00）——
   宁可退回解析版并标 `needs_review`，也不贴错公式。
"""

from __future__ import annotations

import pytest

from backend.services.knowledge import formula_source as fs

P_MSE = r"D_{\text{mse}}(Q) := \mathbb{E}\left[\left\|x - Q^{-1}(Q(x))\right\|_2^2\right] \geq \frac{1}{4^b}."
P_PROD = (r"D_{\text{prod}}(Q) = \mathbb{E}\left[\left|\langle \boldsymbol{y}, \boldsymbol{x}\rangle"
          r" - \langle \boldsymbol{y}, Q^{-1}(Q(\boldsymbol{x}))\rangle\right|^2\right] \geq \frac{1}{d}")


# --------------------------------------------------------------------------
# 页级公式抽取
# --------------------------------------------------------------------------


def test_vision_blocks_take_display_and_inline() -> None:
    """页级转写经常把展示公式写成**行内** `$…$`（实测 p5），只收 `$$` 会漏一大半。"""
    md = ("正文 $x \\in \\mathbb{S}^{d-1}$ 一句。\n\n"
          "$$D_{\\text{mse}}(Q) := \\mathbb{E}[\\|x - Q^{-1}(Q(x))\\|_2^2] \\geq \\frac{1}{4^b}.$$\n\n"
          "还有 $\\mathcal{C}(f_X, b) := \\min_{-1 \\leq c_1 \\leq 1} \\sum_{i=1}^{2^b} \\int |x-c| f(x)dx$ 这句。")
    blocks = fs.vision_math_blocks(md)
    assert len(blocks) == 3
    assert any("D_{\\text{mse}}" in b for b in blocks)
    assert any("\\mathcal{C}(f_X" in b for b in blocks)
    assert fs.vision_math_blocks("") == []


def test_vision_blocks_are_deduped_and_ordered() -> None:
    md = "$$a = b$$\n\n文本 $a = b$ 又出现一次\n\n$$c = d$$"
    assert fs.vision_math_blocks(md) == ["a = b", "c = d"]


# --------------------------------------------------------------------------
# 打分：同一条公式的两种写法
# --------------------------------------------------------------------------


def test_score_matches_same_formula_written_differently() -> None:
    """文本层写法 vs 视觉转写写法：命令名、`\\left/\\right`、`\\text{}` 都不同也要认出来。"""
    parsed = "D(Q) := Ex -QQ(x))\\geq"
    sc = fs.score_pair(parsed, P_MSE)["score"]
    assert sc >= fs.HIGH_SCORE, f"同一公式的两种写法应高分，实际 {sc}"


def test_score_rejects_short_fragments() -> None:
    """`b\\cdot d`、`\\times 10` 这类短碎片：撞任何长公式都能"覆盖" → 直接判 0。"""
    for frag in (r"b\cdot d", r"\times 10", r"j=1", r"\frac{1}{1}", r"f(x) ="):
        assert fs.score_pair(frag, P_MSE)["score"] == 0.0, frag


def test_score_rejects_unrelated_formula() -> None:
    assert fs.score_pair(r"\int_0^1 f(x) dx = 1", P_MSE)["score"] < fs.MID_SCORE


# --------------------------------------------------------------------------
# 对齐
# --------------------------------------------------------------------------


P_QJL = (r"Q_{\text{qjl}}(x) := \text{sign}(S \cdot x) \quad \text{for any } x \in \mathbb{R}^d")


def test_align_picks_the_right_block_and_allows_many_to_one() -> None:
    """两条不同公式各配各的；同一条公式的多个碎片都指向它（N:1）。"""
    parsed = [
        {"id": "b1", "text": "Q(x) := sign (S \\cdot x)for any x \\in R,"},
        {"id": "b2", "text": "D(Q) := Ex -QQ(x))\\geq"},
        {"id": "b3", "text": "D(Q) = E\\langle y, x\\rangle -\\langle y, QQ(x))\\rangle \\geq"},
    ]
    res = fs.align_page(parsed, [P_QJL, "$$ " + P_MSE + " $$"])
    assert [r["index"] for r in res] == [0, 1, 1], "第一条配 QJL，后两条（同一条公式的碎片）都配 MSE"
    assert all(r["score"] >= fs.MID_SCORE for r in res)
    assert res[0]["score"] >= fs.HIGH_SCORE, "一对一那条应当是高置信"


def test_align_resolves_tie_by_reading_order() -> None:
    """平分时取**阅读顺序靠前**的那一条。

    为什么敢这么定：同一页里展示公式的顺序与解析块的阅读顺序一致，而像
    `D(Q) := Ex -QQ(x))\\geq` 这种被切碎又丢了下标的碎片，text 层面分不出它到底是
    `D_mse` 还是 `D_prod`（两条都能覆盖到 1.00）—— 顺序是唯一还活着的证据。
    实测 p20-b04（用户的截图那条）正是被"平分就拒绝"卡住，改成取最早后命中正确的 `D_mse`。
    """
    res = fs.align_page([{"id": "b1", "text": "D(Q) := Ex -QQ(x))\\geq"}], [P_MSE, P_PROD])
    assert res[0]["index"] == 0, "平分取最早（阅读顺序）"
    assert res[0]["second_score"] >= fs.MID_SCORE


def test_align_refuses_short_fragment() -> None:
    """短碎片（`b\\cdot d`）连候选都不进 —— 撞谁都能"覆盖"，不配。"""
    res = fs.align_page([{"id": "b1", "text": r"b\cdot d"}], [P_MSE, P_PROD])
    assert res[0]["index"] is None and res[0]["score"] == 0.0


def test_align_refuses_without_vision_blocks() -> None:
    res = fs.align_page([{"id": "b1", "text": P_MSE}], [])
    assert res[0]["index"] is None and res[0]["score"] == 0.0


def test_align_never_goes_backwards() -> None:
    """单调：第二个碎片不会回头配到第一个碎片**前面**那一块。"""
    parsed = [{"id": "b1", "text": "Q(x) := sign (S \\cdot x)for any x \\in R,"},
              {"id": "b2", "text": "D(Q) := Ex -QQ(x))\\geq"}]
    res = fs.align_page(parsed, [P_QJL, P_MSE])
    assert res[0]["index"] == 0 and res[1]["index"] == 1


# --------------------------------------------------------------------------
# 续行合并 / 编号 / 补漏候选（用户口径第 1、3、4 条）
# --------------------------------------------------------------------------

EQ1_BLOCK = r"(\text{MSE}) \quad D_{\text{mse}} := \mathbb{E}_Q\left[\left\|x - Q^{-1}(Q(x))\right\|_2^2\right] \tag{1}"


def test_group_display_blocks_merges_continuations() -> None:
    """一条多行推导被页级 md 拆成几块时，续行（以 `=` / `\\geq` 开头）要并回同一条。"""
    groups = fs.group_display_blocks([
        r"D_{\text{mse}} = \sum_{j=1}^d \mathbb{E}[|x_j - \tilde{x}_j|^2]",
        r"= \sum_{j=1}^d \mathbb{E}[|\langle e_j, x\rangle - \langle e_j, Q^{-1}(Q(x))\rangle|^2]",
        r"\geq \frac{1}{4^b}.",
        r"D_{\text{prod}}(Q) = \mathbb{E}[\ldots]",
    ])
    assert len(groups) == 2, "前 3 块合成一条，第 4 块另起"
    assert len(groups[0][1]) == 3 and "1}{4^b}" in groups[0][0]
    assert groups[0][0].startswith("\\begin{aligned}")


def test_group_display_blocks_ignores_short_operator_spans() -> None:
    """`$-\\infty$` 这种行内小式子以 `-` 开头，但**不是**续行（实测把 `W^O ∈ …` 粘错过）。"""
    groups = fs.group_display_blocks([r"W^O \in \mathbb{R}^{hd_v \times d_{\text{model}}}", r"-\infty"])
    assert len(groups) == 2


def test_formula_number_is_extracted() -> None:
    assert fs.formula_number(EQ1_BLOCK) == "1"
    assert fs.formula_number(r"a = b \tag*{3.2}") == "3.2"
    assert fs.formula_number(r"a = b") is None


def test_display_check_ignores_latex_command_names() -> None:
    """踩过的坑：`\\left`/`\\right`/`\\quad` 被当成英文词 → 带括号的编号公式全被判成散文。"""
    assert fs.is_display_formula(EQ1_BLOCK) is True
    assert fs.is_display_formula(
        r"lrate = d_{\text{model}}^{-0.5} \cdot \min(step\_num^{-0.5}, warmup\_steps^{-1.5}) \tag{3}"
    ) is True
    assert fs.is_display_formula(
        r"minimize the following expected distortion measures for any worst-case vectors here"
    ) is False


def test_synthetic_candidate_rules() -> None:
    """只补"有编号"或"行间 `$$`"，行内小式子/伪代码行一律不补。"""
    assert fs.is_synthetic_candidate(EQ1_BLOCK) is True                  # 有编号
    assert fs.is_synthetic_candidate(
        r"Q : \mathbb{R}^d \to \{0, 1\}^{b \cdot d}", display=True) is True        # 行间展示
    assert fs.is_synthetic_candidate(r"\text{idx} \leftarrow \text{QUANT}(x)") is False
    assert fs.is_synthetic_candidate(r"W_i^Q \in \mathbb{R}^{d \times d_k}") is False
    assert fs.is_synthetic_candidate(r"is the sequence length,") is False


def test_synthetic_formulas_anchor_same_page(monkeypatch) -> None:
    """补漏公式挂到**同页最近的解析块**（approximate），编号单独进 metadata。"""
    monkeypatch.setattr(fs, "page_vision_math", lambda pid: {7: [EQ1_BLOCK]})
    monkeypatch.setattr(fs, "vision_display_map", lambda pid: {7: {EQ1_BLOCK}})
    monkeypatch.setattr(fs, "vision_groups", lambda pid: {7: [(EQ1_BLOCK, [0])]})
    rows = [{"id": "p07-b01", "page": 7, "order": 11},
            {"id": "p07-b02", "page": 7, "order": 12},
            {"id": "p08-b01", "page": 8, "order": 20}]
    out = fs.synthetic_formulas(rows, {}, "demo")
    assert len(out) == 1
    item = out[0]
    assert item["formula_number"] == "1" and item["page"] == 7
    assert item["anchor_type"] == "nearest_parsed_block"
    assert item["anchor_precision"] == "approximate"
    assert item["block_id"].startswith("p07-"), "锚点必须在同一页"


def test_synthetic_formulas_fall_back_to_page_anchor(monkeypatch) -> None:
    """同页连解析块都没有 → `page` / `page` 锚点（绝不跨页找）。"""
    monkeypatch.setattr(fs, "page_vision_math", lambda pid: {9: [EQ1_BLOCK]})
    monkeypatch.setattr(fs, "vision_display_map", lambda pid: {9: {EQ1_BLOCK}})
    monkeypatch.setattr(fs, "vision_groups", lambda pid: {9: [(EQ1_BLOCK, [0])]})
    out = fs.synthetic_formulas([{"id": "p03-b01", "page": 3, "order": 5}], {}, "demo")
    assert out[0]["anchor_type"] == "page" and out[0]["anchor_precision"] == "page"
    assert out[0]["block_id"] is None


def test_synthetic_formulas_dedupe_same_latex(monkeypatch) -> None:
    """跨页重复的同一条公式只留最早一条，其它页记进 ``also_on_pages``。"""
    monkeypatch.setattr(fs, "page_vision_math", lambda pid: {6: [EQ1_BLOCK], 18: [EQ1_BLOCK]})
    monkeypatch.setattr(fs, "vision_display_map",
                        lambda pid: {6: {EQ1_BLOCK}, 18: {EQ1_BLOCK}})
    monkeypatch.setattr(fs, "vision_groups",
                        lambda pid: {6: [(EQ1_BLOCK, [0])], 18: [(EQ1_BLOCK, [0])]})
    rows = [{"id": "p06-b01", "page": 6, "order": 1},
            {"id": "p18-b01", "page": 18, "order": 2}]
    out = fs.synthetic_formulas(rows, {}, "demo")
    assert len(out) == 1 and out[0]["page"] == 6
    assert out[0]["also_on_pages"] == [18]


# --------------------------------------------------------------------------
# 残缺判定
# --------------------------------------------------------------------------


@pytest.mark.parametrize("latex,cands,expect", [
    ("D(Q) := Ex -QQ(x))\\geq", ["a", "b"], True),      # 多候选（被切碎）
    ("C(f, b) :=min", ["C(f, b) :=min"], True),         # 结尾不是完整式子（这里靠不配平/多候选之外）
    ("\\frac{\\sqrt{} }{\\sqrt{} }", ["x"], True),      # 空根号
    ("f(x) =", ["f(x) ="], True),                       # 结尾是等号
    (P_MSE, [P_MSE], False),                            # 完整
    ("", [], True),
])
def test_looks_incomplete(latex, cands, expect) -> None:
    assert fs.looks_incomplete(latex, cands) is expect


# --------------------------------------------------------------------------
# assign_formulas：分组 + 兜底
# --------------------------------------------------------------------------


def test_assign_formulas_groups_fragments_and_marks_review(monkeypatch) -> None:
    monkeypatch.setattr(fs, "page_vision_math", lambda pid: {20: [P_MSE, P_PROD]})
    formulas = {
        "b1": {"block_id": "b1", "latex_final": "D(Q) := Ex -QQ(x))\\geq",
               "latex_candidate": ["D(Q) := Ex -QQ(x))\\geq", "1/1"]},
        "b2": {"block_id": "b2", "latex_final": "b\\cdot d", "latex_candidate": ["b\\cdot d"]},
    }
    rows = [{"id": "b1", "page": 20}, {"id": "b2", "page": 20}]
    out = fs.assign_formulas(rows, formulas, "demo")
    # b1 平分 → 取阅读顺序最早的 P_MSE；b2 是短碎片 → 不配、且要标待修复
    assert out["b1"]["source"] == "page_vision" and out["b1"]["latex"] == P_MSE
    assert out["b2"]["source"] == "parsed" and out["b2"]["needs_review"] is True
    assert fs.is_fragment("b\\cdot d") is True
    assert out["b1"]["group_first"] is True and out["b1"]["block_ids"] == ["b1"]


def test_assign_formulas_switches_source_when_decisive(monkeypatch) -> None:
    """页级只有一条候选、分数高且领先 → 换源，并标出整组的块 id。"""
    monkeypatch.setattr(fs, "page_vision_math",
                        lambda pid: {20: ["x " + P_MSE + " y"]})
    formulas = {
        "b1": {"block_id": "b1", "latex_final": "D(Q) := Ex -QQ(x))\\geq",
               "latex_candidate": ["D(Q) := Ex -QQ(x))\\geq"]},
        "b2": {"block_id": "b2", "latex_final": "D(Q) := Ex -QQ(x))\\geq",
               "latex_candidate": ["D(Q) := Ex -QQ(x))\\geq"]},
    }
    rows = [{"id": "b1", "page": 20}, {"id": "b2", "page": 20}]
    out = fs.assign_formulas(rows, formulas, "demo")
    assert out["b1"]["source"] == "page_vision" and out["b1"]["needs_review"] is False
    assert out["b1"]["group_first"] is True and out["b2"]["group_first"] is False
    assert out["b1"]["block_ids"] == ["b1", "b2"]


def test_assign_formulas_without_page_products(monkeypatch) -> None:
    """没做过页级翻译的论文：全部退回解析版（不能因为缺页级产物就少建 chunk）。"""
    monkeypatch.setattr(fs, "page_vision_math", lambda pid: {})
    formulas = {"b1": {"block_id": "b1", "latex_final": "x = y", "latex_candidate": ["x = y"]}}
    out = fs.assign_formulas([{"id": "b1", "page": 3}], formulas, "demo")
    assert out["b1"]["source"] == "parsed" and out["b1"]["group_first"] is True
