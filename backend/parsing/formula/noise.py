"""公式噪声判定：**只有脚注符号 / 标点的"公式"不是公式**。

用户报的问题：知识库里出现了一条 `公式 attentionisa-p01-b06：* †`。
查下去是**页脚的脚注符号**（原文 `∗†`，指向作者单位的星号与剑号）被当成公式：

- 解析阶段：这一小块（11×7 pt）符号密度高 → 判成公式，`latex_final = "* †"`，还裁了一张图；
- 阅读器公式列表：页级产物的 Markdown 里写成了 `$$ * † $$`，于是列表里也多一条；
- 知识库 chunk：分块层把它做成 `公式 <块 id>：* †` 的 chunk（带缩略图）进了索引。

这类"公式"**没有任何数学内容**：判据 = LaTeX 里既没有字母/数字（含希腊字母，走 Unicode
`isalnum`），也没有任何真正有意义的数学运算符。三个地方共用这一条规则：

1. `parsing/structure/formula_block.py` —— 这样的行不参与公式候选（根因）；
2. `parsing/pipeline.py` —— 候选的原文与 LaTeX 全是噪声时，不建 FormulaBlock；
3. `services/paper/reader_service.py` 的公式列表、`services/knowledge/knowledge_service.py`
   的 chunk 化 —— 已经写进旧产物的噪声同样滤掉（不必重新解析）。

**刻意保守**：只否掉"连字母数字都没有"的极端情况。`\\alpha`、`α = β`、`x^2`、`E[x]` 都保留 ——
误删一条真公式的代价远大于多留一条噪声。
"""

from __future__ import annotations

#: 论文里常见的脚注/题注符号（星号、剑号、双剑号、节号、段落号）
FOOTNOTE_MARKS = "*∗†‡§¶"

#: 真正的数学运算符（有一个就说明不是"纯脚注符号"）
_STRONG_OPERATORS = set("=+-−–—×÷<>≤≥≈≠∑∏∫√±∓→←↔∈∉⊂⊆∪∩∂∇∞^_/%|")


def is_noise_formula(text: str | None) -> bool:
    """True = 这段"LaTeX"没有任何数学内容（只有脚注符号 / 标点 / 空白）。

    判据（宽进严出：宁可放过多留，也不要误删）：
    - 空 / 纯空白 → 噪声；
    - 含**任何**字母或数字（Unicode，`α`/`β` 这类希腊字母算字母）→ **不是**噪声；
    - 含任何强运算符（`=`、`+`、`∑`、`→` …）→ **不是**噪声；
    - 其余（`* †`、`‡`、`,`、`·` …）→ 噪声。
    """
    t = str(text or "").strip()
    if not t:
        return True
    if any(ch.isalnum() for ch in t):
        return False
    return not any(ch in _STRONG_OPERATORS for ch in t)
