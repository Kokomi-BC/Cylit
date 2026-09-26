"""图内文字兜底：把"图里画出来的字当正文"的行丢掉。

## 为什么要有这一层

实测（attention 附录 图 3 / 图 4 注意力可视化）：**B 路（翻译）**把图内那句示例英文当正文译了出来：

```
It is in this spirit that a majority of American governments have passed new laws since 2009
making the registration or voting process more difficult . <EOS> <pad> <pad> <pad> <pad> <pad>
The Law will never be perfect , but its application should be just - this is what we are missing , in my opinion . <EOS> <pad>
```

读者会在中文栏里看到英文原句 + `<EOS> <pad>`；A 路（提示词里有"插图只写图注本身"）是干净的。
提示词侧已补上对等规则（管新产物），这里是**确定性兜底**（管已经产出的行，也防模型再犯）。

判定刻意**窄**：只丢含 `<EOS>` / `<pad>` / `<unk>` / `<s>` / `</s>` 这类训练标记的行 ——
论文正文不会出现它们，所以不可能误伤正常句子（含公式、引用、单位标记的行都不受影响）。
"""

from __future__ import annotations

from backend.services.translation.page_translate import strip_figure_noise


def test_drops_lines_with_training_markers() -> None:
    md = ("**图 3：** 一个注意力机制的示例。\n\n"
          "It is in this spirit that a majority of American governments have passed new laws "
          "since 2009 making the registration or voting process more difficult . <EOS> <pad> <pad>\n\n"
          "**图 4：** 两个注意力头。")
    out, dropped = strip_figure_noise(md)
    assert dropped == 1
    assert "<EOS>" not in out and "<pad>" not in out
    assert "图 3" in out and "图 4" in out, "正常图注不许被误伤"
    assert "It is in this spirit" not in out


def test_keeps_normal_prose_and_math() -> None:
    md = ("我们证明 $D_{\\text{mse}} \\le 2^{-2b}$（见式 (3)）。\n\n"
          "| 方法 | KV 大小 | 指标 |\n|---|---|---|\n| TurboQuant | 3.5 | 1.02 |\n\n"
          "参考文献 [12] Shannon (1948)。")
    out, dropped = strip_figure_noise(md)
    assert dropped == 0 and out == md


def test_keeps_lines_that_merely_contain_angle_brackets() -> None:
    """窄判定：`<` 出现在别处（比如 MathML 式的 `<x>` 或比较符号）不该被丢。"""
    md = "当 $a < b$ 时成立。\n\n不等式 $\\langle y, x \\rangle$ 的记号如常。"
    out, dropped = strip_figure_noise(md)
    assert dropped == 0 and out == md


def test_multiple_marker_lines_and_blank_line_cleanup() -> None:
    md = "第一段。\n\n<pad> <pad> <pad>\n\n<EOS>\n\n第二段。"
    out, dropped = strip_figure_noise(md)
    assert dropped == 2
    assert out == "第一段。\n\n第二段。", "丢行后不许留下连续多空行"
    assert "\n\n\n" not in out


def test_empty_and_no_marker_input_is_untouched() -> None:
    assert strip_figure_noise("") == ("", 0)
    assert strip_figure_noise("普通一段。") == ("普通一段。", 0)


def test_case_insensitive_and_closing_tags() -> None:
    out, dropped = strip_figure_noise("示例句 <PAD>\n\n另一句 </s>")
    assert dropped == 2 and out == ""
