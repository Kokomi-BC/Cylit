"""公式噪声：**只有脚注符号的"公式"不是公式**（全离线）。

用户报的：知识库里有一条 `公式 attentionisa-p01-b06：* †` —— 那是页脚的脚注符号 `∗†`
被解析器当成了公式（小块 + 符号密度高），接着裁了图、进了公式列表、最后进了知识库。

三处共用同一条判据（`backend/parsing/formula/noise.py`），这里把三处都钉住：
1. 解析：这样的行不进公式候选；
2. 阅读器公式列表：`$$ * † $$` 不进列表；
3. 知识库 chunk：不建 `公式 <块 id>：* †` 这种 chunk。
"""

from __future__ import annotations

from backend.parsing.formula.noise import is_noise_formula
from backend.services.knowledge.knowledge_service import _formula_chunk_text

# --------------------------------------------------------------------------
# 判据本身
# --------------------------------------------------------------------------


def test_footnote_marks_are_noise() -> None:
    for text in ["* †", "∗†", "*", "†", "‡ § ¶", "  *  †  ", ",", "· ·", "", "   "]:
        assert is_noise_formula(text), text


def test_real_math_is_kept() -> None:
    for text in [
        "x^2", "E[||x - Q^{-1}(Q(x))||^2]", r"D_{\text{mse}}", r"\alpha",
        "α = β + γ", "0.5", "n", "→", "≤", "b \\in \\{1,2\\}", "-", "d_k",
    ]:
        assert not is_noise_formula(text), text


def test_greek_and_digits_survive() -> None:
    """希腊字母是 Unicode 字母、数字也算内容 —— 不能被当成噪声删掉。"""
    assert not is_noise_formula("λ")
    assert not is_noise_formula("２")          # 全角数字
    assert is_noise_formula("†††")


# --------------------------------------------------------------------------
# 知识库 chunk：不建噪声公式 chunk
# --------------------------------------------------------------------------


def test_formula_chunk_skips_footnote_marks() -> None:
    """页脚 `∗†` 那条：`latex_final="* †"`、候选 `["* †"]`、原文 `∗†` → 不建 chunk。"""
    assert _formula_chunk_text({
        "block_id": "attentionisa-p01-b06", "raw_text": "∗†",
        "latex_candidate": ["* †"], "latex_final": "* †",
        "latex_repaired": None, "grade": "A",
    }) is None


def test_formula_chunk_keeps_real_formula() -> None:
    text = _formula_chunk_text({
        "block_id": "p04-b21", "raw_text": "MSE = 1/n ∑ (x - x̂)²",
        "latex_candidate": [r"\text{MSE} = \frac{1}{n}\sum_i (x_i - \hat{x}_i)^2"],
        "latex_final": r"\text{MSE} = \frac{1}{n}\sum_i (x_i - \hat{x}_i)^2",
        "grade": "A",
    })
    assert text and text.startswith("公式 p04-b21：")


def test_formula_chunk_falls_back_to_raw_text_when_latex_missing() -> None:
    """LaTeX 为空时用原文兜底；只有原文也是纯符号才丢。"""
    assert _formula_chunk_text({"block_id": "p01-b01", "raw_text": "x = 1",
                                "latex_candidate": [], "latex_final": None}) == "公式 p01-b01：x = 1"
    assert _formula_chunk_text({"block_id": "p01-b02", "raw_text": "†",
                                "latex_candidate": [], "latex_final": None}) is None


# --------------------------------------------------------------------------
# 解析：这样的行不进公式候选
# --------------------------------------------------------------------------


def _line(text: str, *, math_font: bool = True):
    from backend.parsing.models import Span, TextLine

    font = "CMMI10" if math_font else "Times New Roman"
    return TextLine(spans=[Span(text=text, x0=0, y0=0, x1=10, y1=8, font=font, size=8.0)])


def test_formula_line_detector_rejects_footnote_marks() -> None:
    """`∗†` 那一行：符号密度与数学字体都"很像公式"，但没有任何数学内容 → 不判为公式行。"""
    from backend.parsing.structure.formula_block import _line_is_formula_like

    ok, _score, _reason = _line_is_formula_like(_line("∗†"))
    assert ok is False
    # 真公式行照旧判得出来（别把门槛改成"什么都不要"）
    ok2, _s2, _r2 = _line_is_formula_like(_line("E = mc^2"))
    assert ok2 is True


# --------------------------------------------------------------------------
# 阅读器公式列表：`$$ * † $$` 不进列表
# --------------------------------------------------------------------------


def test_markdown_formulas_skip_footnote_noise(monkeypatch) -> None:
    from backend.services.paper import reader_service

    payload = {"page": 1, "model": "demo", "rows": [
        {"id": "p001-04", "original": "Google Brain  ∗†", "translation": "谷歌大脑  ∗†"},
        {"id": "p001-05", "original": "$$ * † $$", "translation": "$$ * † $$"},
        {"id": "p001-09", "original": "as in $$ E = mc^2 $$ we get", "translation": "如 $$ E = mc^2 $$ 所示"},
    ]}
    monkeypatch.setattr(reader_service, "page_translations", lambda pid: [payload])

    data = reader_service.markdown_formulas("demo-paper")
    latexes = [i["latex"] for i in data["items"]]
    assert latexes == ["E = mc^2"]                      # 脚注那条不在
    assert all("* †" not in x for x in latexes)
