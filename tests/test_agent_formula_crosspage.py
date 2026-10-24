r"""公式清单的**跨页重复陈述**去重（纯离线）。

用户实测（2026-12-07，TurboQuant）：同一条公式在清单里出现两三次，得自己认出"这是同一条"：
- \D_{\text{mse}} \leq \frac{\sqrt{3}\pi}{2} \cdot \frac{1}{4^b}\（第 6 页 / 第 18 页各一条）；
- \D_{\text{mse}} \approx 0.36, 0.117, 0.03, 0.009\（第 3 / 6 / 18 页）；
- 内积界 \D_{\text{prod}} \leq …\（第 7 页 / 第 19 页各一条）。

口径：**归一化后相同 → 只留首次出现那条**，其余页码进 \also_on\。
归一化只削"排版差异"（见 \project._latex_norm\ 的注释）：空白、\\left\/\\right\、
间距宏、\\mathrm\/\\text\ 写法、\\tag\ 编号、末尾句读。
**不同**的式子（哪怕只差一个因子）绝不合并 —— 合并错比留一条重复糟得多。
"""

from __future__ import annotations

import pytest

from backend.services.agent import project


def _item(page: int, row_id: str, latex: str, *, display: bool = True) -> dict:
    return {"page": page, "row_id": row_id, "latex": latex, "display": display,
            "context": "上下文", "context_zh": "", "model": "m", "source": "markdown"}


@pytest.fixture
def fake_payload(monkeypatch):
    def install(items: list[dict]) -> None:
        monkeypatch.setattr(project.reader_service, "formulas_payload",
                            lambda paper_id, source="markdown": {
                                "items": items if source == "markdown" else [],
                                "source": source})
        monkeypatch.setattr(project.paper_service, "get_paper",
                            lambda paper_id: {"id": paper_id, "page_count": 22})
    return install


# --------------------------------------------------------------------------
# ① 跨页重复陈述 → 一条 + also_on
# --------------------------------------------------------------------------

#: 第 6 页与第 18 页那一对（线上原文）
BOUND = r"D_{\text{mse}} \leq \frac{\sqrt{3}\pi}{2} \cdot \frac{1}{4^b}"


def test_same_display_formula_on_two_pages_becomes_one_with_also_on(fake_payload) -> None:
    fake_payload([_item(6, "p006-02", BOUND), _item(18, "p018-03", BOUND)])
    out = project.formulas("p1")
    assert len(out["items"]) == 1
    one = out["items"][0]
    assert one["page"] == 6 and one["also_on"] == [18]
    # 计数跟着去重（同一口径：都数"去重后、筛选范围内的全部条目"）
    assert out["total"] == 1 and out["display_total"] == 1 and out["returned"] == 1


def test_three_pages_keep_the_first_and_name_the_rest_in_order(fake_payload) -> None:
    fake_payload([_item(3, "p003-14", BOUND), _item(6, "p006-02", BOUND),
                  _item(18, "p018-04", BOUND)])
    out = project.formulas("p1")
    assert [(i["page"], i["also_on"]) for i in out["items"]] == [(3, [6, 18])]


def test_page_filtered_view_has_no_also_on(fake_payload) -> None:
    r"""按页取时只有那一页的条目，\also_on\ 自然是空的（不影响按页补）。"""
    fake_payload([_item(6, "p006-02", BOUND), _item(18, "p018-03", BOUND)])
    out = project.formulas("p1", page=18)
    assert [i["page"] for i in out["items"]] == [18] and out["items"][0]["also_on"] == []


# --------------------------------------------------------------------------
# ② 归一化规则：削什么、不削什么
# --------------------------------------------------------------------------


@pytest.mark.parametrize("other", [
    r"D_{\text{mse}} \leq  \frac{\sqrt{3}\pi}{2}\cdot\frac{1}{4^b}",           # 空白不同
    r"D_{\text{mse}}\leq\frac{\sqrt{3}\pi}{2}\cdot\frac{1}{4^b}.",             # 末尾句号
    r"D_{\text{mse}} \leq \frac{\sqrt{3}\pi}{2} \cdot \frac{1}{4^b} \tag{7}",  # 编号不是内容
])
def test_formatting_only_differences_are_the_same_formula(fake_payload, other) -> None:
    """只差排版（空白 / 句号 / \tag \编号）→ 同一条。"""
    fake_payload([_item(6, "p006-02", BOUND), _item(18, "p018-03", other)])
    assert project._latex_norm(BOUND) == project._latex_norm(other)
    assert len(project.formulas("p1")["items"]) == 1


def test_left_right_only_change_the_bracket_size(fake_payload) -> None:
    r"""\\left(\ / \\right)\ 只调括号尺寸（第 16 页与附录那一对就是这样）。"""
    plain = r"f_X(x) = \frac{\Gamma(d/2)}{\sqrt{\pi}} (1-x^2)^{(d-3)/2}."
    sized = r"f_X(x) = \frac{\Gamma(d/2)}{\sqrt{\pi}} \left(1-x^2\right)^{(d-3)/2}"
    fake_payload([_item(16, "p016-04", plain), _item(22, "p022-01", sized)])
    assert project._latex_norm(plain) == project._latex_norm(sized)
    assert len(project.formulas("p1")["items"]) == 1


def test_spacing_macros_are_not_content(fake_payload) -> None:
    left = r"\int f_X(x)\,dx"
    right = r"\int f_X(x)\quad dx"
    fake_payload([_item(6, "p006-02", left), _item(18, "p018-03", right)])
    assert project._latex_norm(left) == project._latex_norm(right)
    assert len(project.formulas("p1")["items"]) == 1


def test_mathrm_and_text_render_the_same_thing(fake_payload) -> None:
    fake_payload([_item(6, "p006-02", r"Q_{\mathrm{mse}}(x)"),
                  _item(18, "p018-03", r"Q_{\text{mse}}(x)")])
    assert len(project.formulas("p1")["items"]) == 1


def test_different_formulas_are_never_merged(fake_payload) -> None:
    r"""第 4 页与第 20 页的下界只差一个因子（\\|y|_2^2/d\ vs \1/d\）—— 必须分开。"""
    four = (r"D_{\text{prod}}(Q) = \mathbb{E}\left[\left|\langle y, x\rangle - "
            r"\langle y, Q^{-1}(Q(x))\rangle\right|^2\right] \geq "
            r"\frac{\|y\|_2^2}{d} \cdot \frac{1}{4^b}")
    twenty = (r"D_{\text{prod}}(Q) = \mathbb{E}\left[\left|\langle y, x\rangle - "
              r"\langle y, Q^{-1}(Q(x))\rangle\right|^2\right] \geq "
              r"\frac{1}{d} \cdot \frac{1}{4^b}")
    fake_payload([_item(4, "p004-03", four), _item(20, "p020-04", twenty)])
    out = project.formulas("p1")
    assert len(out["items"]) == 2 and [i["page"] for i in out["items"]] == [4, 20]
    assert all(i["also_on"] == [] for i in out["items"])


def test_a_similar_looking_but_different_relation_is_kept(fake_payload) -> None:
    r"""前缀相同、关系符不同（\leq\ vs \approx\）→ 两条。"""
    fake_payload([_item(6, "p006-02", r"D_{\text{mse}} \leq \frac{1}{4^b}"),
                  _item(18, "p018-03", r"D_{\text{mse}} \approx \frac{1}{4^b}")])
    assert len(project.formulas("p1")["items"]) == 2


# --------------------------------------------------------------------------
# ③ 只合并块级；行内符号的页码本身就是价值
# --------------------------------------------------------------------------


def test_inline_symbols_are_not_merged_across_pages(fake_payload) -> None:
    fake_payload([_item(5, "p005-01", "b - 1", display=False),
                  _item(9, "p009-02", "b - 1", display=False),
                  _item(12, "p012-01", "b - 1", display=False)])
    out = project.formulas("p1", include_inline=True)
    assert [i["page"] for i in out["items"]] == [5, 9, 12]
    assert out["inline_total"] == 3


def test_same_page_different_rows_are_still_two_entries(fake_payload) -> None:
    r"""同一页的两个排版位置：\also_on\ 记页码给不出区分度，引用要分别给号（老行为）。"""
    fake_payload([_item(5, "p005-03", r"x \in \mathbb{R}^d"),
                  _item(5, "p005-08", r"x \in \mathbb{R}^d")])
    assert len(project.formulas("p1")["items"]) == 2


# --------------------------------------------------------------------------
# ④ 截断时说清「还差哪几页」
# --------------------------------------------------------------------------


def test_missing_pages_names_the_pages_that_did_not_fit(fake_payload) -> None:
    fake_payload([_item(3, f"p003-{i:02d}", f"f_{{{i}}}") for i in range(1, 4)]
                 + [_item(20, "p020-01", "g_1"), _item(21, "p021-01", "g_2")])
    out = project.formulas("p1", limit=3)
    assert out["truncated"] is True and out["returned"] == 3 and out["total"] == 5
    assert out["missing_pages"] == [20, 21]
    # 没截断时是空列表（"还差哪几页"与 truncated 一一对应）
    assert project.formulas("p1")["missing_pages"] == []
