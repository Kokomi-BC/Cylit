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


def test_formula_noise_filter_still_used_by_kb() -> None:
    """页脚 `∗†` 那条：`latex_final="* †"` → 知识库侧要挡住（`formula_source.is_noise_latex`）。

    历史：以前这里测的是 `knowledge_service._formula_chunk_text`；公式条目改成
    **canonical 清单**（`markdown_formulas`）之后，正文拼装那一步不再存在，
    噪声判定统一走 `formula_source.is_noise_latex`（canonical 清单本身也挡一道）。
    """
    from backend.services.knowledge.formula_source import is_noise_latex

    assert is_noise_latex("* †") is True
    assert is_noise_latex("∗†") is True
    assert is_noise_latex(r"\text{MSE} = \frac{1}{n}\sum_i (x_i - \hat{x}_i)^2") is False


def test_formula_noise_never_becomes_kb_entry(monkeypatch) -> None:
    """就算 canonical 清单里混进噪声条目，也不会形成知识库公式条目。"""
    from backend.services.knowledge import formula_source as fs

    _fake = lambda pid: {"items": [  # noqa: E731
        {"id": "f001-0001", "page": 1, "row_id": "p001-01", "latex": "* †",
         "display": True, "context": "", "context_zh": ""},
        {"id": "f001-0002", "page": 1, "row_id": "p001-02",
         "latex": r"a = b + c", "display": True, "context": "", "context_zh": ""},
    ], "count": 2, "source": "markdown", "display_count": 2}
    monkeypatch.setattr(fs, "canonical_items", lambda pid: _fake(pid)["items"])
    entries = fs.kb_formula_entries("demo", {}, [])
    assert [e["latex"] for e in entries] == [r"a = b + c"]


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

