"""知识库展示层的 LaTeX 兜底（公式定界符）单测。

**只留仍在用的那三个纯函数**：旧路径（解析块 + 按比例取译文：`_lookup_zh` / `_slice_zh` /
`_row_content` / `_chunk_variants` …）在"按 markdown 层级切分"那一轮已删除（dev-log §17.64），
它们的测试一并删除；对应职责现在由 `tests/test_kb_row_chunking.py`（行级分块）覆盖。

为什么这三个还留着：页级行文本里的公式**定界符**是转写产物的一部分，实测会缺或会断行
（`Q : R→{0, 1}`、多行 `\begin{aligned}`），补定界符属于**展示层**，与分块策略无关：

- `_scan_math`：把一行切成 `plain` / `math` 片段，并报告行末**未闭合**的定界符；
- `_wrap_bare_latex`：给裸 LaTeX 补定界符（不碰已成对的、不碰跨行未闭合的）；
- `_inline_formula_block`：多行公式改写成展示公式（`$$` 单独成段），单行用 `$…$`。
"""

from __future__ import annotations

from backend.services.knowledge.knowledge_service import (
    _inline_formula_block, _scan_math, _wrap_bare_latex,
)


# --------------------------------------------------------------------------
# _scan_math：`$$…$$` 必须整体配对（这是"裸 LaTeX 漏进正文"那个 bug 的根因）
# --------------------------------------------------------------------------


def test_scan_math_pairs_display_delimiters_as_a_whole() -> None:
    line = r"前 $$Q^{-1}(Q(x))$$ 后"
    pieces, leftover = _scan_math(line)
    assert leftover is None
    assert [k for k, _ in pieces] == ["plain", "math", "plain"]
    assert pieces[1][1] == r"$$Q^{-1}(Q(x))$$"


def test_scan_math_pairs_inline_delimiters() -> None:
    pieces, leftover = _scan_math(r"取 $c_i$ 与 $\Pi x$ 两处")
    assert leftover is None
    assert [s for k, s in pieces if k == "math"] == ["$c_i$", "$\\Pi x$"]
    assert "".join(s for _, s in pieces) == r"取 $c_i$ 与 $\Pi x$ 两处"


def test_scan_math_reports_unclosed_delimiter() -> None:
    """未闭合要报出来（调用方据此**跨行不再补**，否则会把公式切碎）。"""
    assert _scan_math(r"文本 $\left[ x")[1] == "$"
    assert _scan_math(r"文本 $$\left[ x")[1] == "$$"
    assert _scan_math("没有公式的一行")[1] is None


# --------------------------------------------------------------------------
# _wrap_bare_latex：补定界符，但绝不切碎已有公式
# --------------------------------------------------------------------------


def test_wrap_bare_latex_keeps_display_formula_intact() -> None:
    src = "结论：\n$$D_{\\text{mse}} := \\mathbb{E}\\left[\\|x - Q(x)\\|^2\\right]$$\n后文。"
    out = _wrap_bare_latex(src)
    assert out.count("$$") == 2
    assert r"\left[\|x - Q(x)\|^2\right]" in out


def test_wrap_bare_latex_skips_display_formula_in_middle_of_line() -> None:
    """句中 `$$…$$` 会被包成 `$…$` 之外的东西 —— 关键是不能在公式**内部**再包一层。"""
    out = _wrap_bare_latex(r"公式 $$a = b$$ 之后")
    assert out.count("$") == 4 or out == r"公式 $$a = b$$ 之后"
    assert r"$a = b$" in out or r"$$a = b$$" in out


def test_wrap_bare_latex_leaves_unbalanced_line_alone() -> None:
    src = r"这里断了 $\left[ x + y"
    assert _wrap_bare_latex(src) == src


def test_wrap_bare_latex_still_wraps_bare_line() -> None:
    """裸写的公式片段要补上定界符（前端渲染链不认裸 LaTeX）。

    判据是"含 `^{`/`_{`、无中文、不过长"的连续片段（守则②）—— 所以这里用一个带下标的裸片段。
    """
    src = r"其中 $\Pi x$ 与 Q_{\text{mse}}^{-1}(x) 都出现。"
    out = _wrap_bare_latex(src)
    assert r"$Q_{\text{mse}}^{-1}(x)$" in out
    assert out.count("$") == 4            # 两处行内公式，共 2 对定界符


def test_wrap_bare_latex_whole_line_of_bare_tex_becomes_display() -> None:
    """整行无 `$`、无中文、含 ≥2 个高置信命令 → 整行包 `$$…$$`（守则①）。"""
    src = r"\mathcal{C}(f_X, b) := \min_{c_1, c_2} \mathbb{E}\left[\|x - Q(x)\|^2\right]"
    out = _wrap_bare_latex(src)
    assert out == f"$${src}$$"


# --------------------------------------------------------------------------
# _inline_formula_block：定界符选择（单行 `$…$` / 多行 `$$…$$`）
# --------------------------------------------------------------------------


def test_inline_formula_block_uses_display_delimiters_for_multiline() -> None:
    multi = "\\begin{aligned}\na &= b \\\\\nc &= d\n\\end{aligned}"
    out = _inline_formula_block(multi)
    assert out.startswith("\n\n$$") and out.rstrip().endswith("$$")
    assert "\\begin{aligned}" in out


def test_inline_formula_block_single_line_uses_inline_delimiters() -> None:
    assert _inline_formula_block("a = b") == "$a = b$"


def test_inline_formula_block_strips_tag_inline_only() -> None:
    """行内模式下 `\\tag` 会让 KaTeX 直接报错 —— 内联时必须去掉。"""
    assert _inline_formula_block("a = b \\tag{3}") == "$a = b$"
