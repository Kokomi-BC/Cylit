"""目录建树：**编号优先、level 兜底**（回归测试，纯函数、不依赖数据库）。

背景（真 bug）：页级产物里的 ``#`` 深度是**模型**写的（原生页则由解析器按字号判定），
两者都不稳定 —— TurboQuant 那篇实测是 ``1 TURBOQUANT…`` 1 级、``2 EXPERIMENTS`` 2 级、
``1.2 INNER-PRODUCT…`` 3 级、``B.1 SHANNON…`` 2 级。旧实现只按 ``level`` 建树，于是：
``2 EXPERIMENTS`` 被挂到 ``1`` 下面、``1.2`` 因为"编号前缀对不上"被甩成顶级，
排到 ``REFERENCES`` / 附录 ``A`` 之后 —— 用户看到的就是"目录顺序不对"。

这里用合成数据把四种情形钉住：
① 编号体系 + 乱 level → 严格按编号建树；
② 无编号章节（ABSTRACT / REFERENCES / 附录）→ 顶级；
③ 编号找不到父节点（``0.1`` 但没有 ``0``）→ 顶级，不乱挂；
④ 整篇无编号（老论文 / 扫描件）→ 退回按 ``level`` 的栈式嵌套。
"""

from __future__ import annotations

from backend.services.paper.reader_service import (
    _adopt_appendix_letters,
    _apply_semantic_levels,
    _nest_outline,
)


def _flat(rows: list[tuple[str, str, int, int]]) -> list[dict]:
    """``[(text, number, page, level)]`` → 后端 ``build_outline`` 里的扁平结构。"""
    return [
        {
            "block_id": f"p{page:03d}-{i:02d}", "page": page, "level": level,
            "number": number, "text": text, "text_zh": "", "children": [],
        }
        for i, (text, number, page, level) in enumerate(rows)
    ]


def _build(rows: list[tuple[str, str, int, int]]) -> list[dict]:
    """跟 ``build_outline`` 一样地跑三步：认领附录字母 → 推语义层级 → 建树。"""
    flat = _flat(rows)
    _adopt_appendix_letters(flat)
    return _nest_outline(flat, _apply_semantic_levels(flat))


def _shape(nodes: list[dict], depth: int = 0) -> list[str]:
    """树 → ``["  x TEXT", ...]``（缩进 = depth），方便整体断言。"""
    out: list[str] = []
    for node in nodes:
        out.append(f"{'  ' * depth}{node['text']}")
        out.extend(_shape(node.get("children") or [], depth + 1))
    return out


#: 真实的乱层级（从左到右：text / number / page / 产物给的 level）
_TURBOQUANT = [
    ("TURBOQUANT: ONLINE VECTOR QUANTIZATION", "", 1, 1),
    ("ABSTRACT", "", 1, 2),
    ("0.1 PROBLEM DEFINITION", "0.1", 2, 3),
    ("1 TURBOQUANT: HIGH PERFORMANCE QUANTIZATION", "1", 4, 1),
    ("1.1 MSE OPTIMAL TURBOQUANT", "1.1", 5, 2),
    ("1.2 INNER-PRODUCT OPTIMAL TURBOQUANT", "1.2", 6, 3),
    ("2 EXPERIMENTS", "2", 7, 2),
    ("2.1 EMPIRICAL VALIDATION", "2.1", 7, 3),
    ("2.2 NEEDLE-IN-A-HAYSTACK", "2.2", 8, 2),
    ("REFERENCES", "", 10, 2),
    ("A EXTENDED PRIOR WORK", "", 15, 2),
    ("B MATHEMATICAL PRELIMINARIES", "", 16, 1),
    ("B.1 SHANNON LOWER BOUND ON DISTORTION", "B.1", 16, 2),
    ("C PROOFS AND TECHNICAL REMARKS", "", 17, 2),
    ("C.1 TURBOQUANT_mse", "C.1", 18, 3),
    ("D ADDITIONAL EXPERIMENT", "", 20, 2),
]


def test_numbered_outline_is_number_driven():
    """层级与父子关系只看编号：`1.2` 归 `1`、`2` 与 `1` 平级、附录子节归附录。"""
    flat = _flat(_TURBOQUANT)
    _adopt_appendix_letters(flat)
    # 附录大标题 "B MATHEMATICAL PRELIMINARIES" 从子节 B.1 反推出编号 B
    assert [item["number"] for item in flat][11] == "B"
    assert _apply_semantic_levels(flat) is True
    assert [item["level"] for item in flat] == [1, 1, 2, 1, 2, 2, 1, 2, 2, 1, 1, 1, 2, 1, 2, 1]

    assert _shape(_nest_outline(flat, True)) == [
        "TURBOQUANT: ONLINE VECTOR QUANTIZATION",
        "ABSTRACT",
        "0.1 PROBLEM DEFINITION",          # 没有 `0` 这个父标题 → 顶级，且不挂到 ABSTRACT 下
        "1 TURBOQUANT: HIGH PERFORMANCE QUANTIZATION",
        "  1.1 MSE OPTIMAL TURBOQUANT",
        "  1.2 INNER-PRODUCT OPTIMAL TURBOQUANT",   # 出现在 1 组内，而不是附录之后
        "2 EXPERIMENTS",                    # 与 1 平级，不再被挂到 1 下面
        "  2.1 EMPIRICAL VALIDATION",
        "  2.2 NEEDLE-IN-A-HAYSTACK",
        "REFERENCES",
        "A EXTENDED PRIOR WORK",
        "B MATHEMATICAL PRELIMINARIES",
        "  B.1 SHANNON LOWER BOUND ON DISTORTION",
        "C PROOFS AND TECHNICAL REMARKS",
        "  C.1 TURBOQUANT_mse",
        "D ADDITIONAL EXPERIMENT",
    ]


def test_unnumbered_sections_do_not_steal_children():
    """有编号体系的论文里，无编号章节一律顶级：附录 `D` 不该被挂到 `B` 底下。"""
    flat = _flat([
        ("1 INTRODUCTION", "1", 1, 1),
        ("B MATHEMATICAL PRELIMINARIES", "", 16, 1),
        ("D ADDITIONAL EXPERIMENT", "", 20, 2),   # 模型把它写成了 `##`
        ("E IMPLEMENTATION", "", 20, 2),
    ])
    _adopt_appendix_letters(flat)
    assert _apply_semantic_levels(flat) is True
    assert flat[1]["number"] == ""                  # 没有 B.1 子节 → 不认领字母 B
    assert [item["level"] for item in flat] == [1, 1, 1, 1]
    assert _shape(_nest_outline(flat, True)) == [
        "1 INTRODUCTION",
        "B MATHEMATICAL PRELIMINARIES",
        "D ADDITIONAL EXPERIMENT",
        "E IMPLEMENTATION",
    ]


def test_appendix_letter_is_adopted_only_when_it_has_subsections():
    """`A NOVEL FRAMEWORK` 没有 A.1 子节 → 不被误判成附录 A。"""
    flat = _flat([
        ("1 INTRODUCTION", "1", 1, 1),
        ("A NOVEL FRAMEWORK FOR X", "", 2, 2),
    ])
    _adopt_appendix_letters(flat)
    assert flat[1]["number"] == ""

    with_children = _flat([
        ("C PROOFS AND TECHNICAL REMARKS", "", 17, 2),
        ("C.1 TURBOQUANT_mse", "C.1", 18, 3),
    ])
    _adopt_appendix_letters(with_children)
    assert with_children[0]["number"] == "C"


def test_outline_without_any_number_falls_back_to_level():
    """整篇没有编号（老论文 / 扫描件）时才按 level 做栈式嵌套。"""
    flat = _flat([
        ("INTRODUCTION", "", 1, 1),
        ("METHOD", "", 2, 1),
        ("DETAIL", "", 2, 2),
        ("RESULTS", "", 3, 1),
    ])
    _adopt_appendix_letters(flat)
    assert _apply_semantic_levels(flat) is False
    assert _shape(_nest_outline(flat, False)) == [
        "INTRODUCTION",
        "METHOD",
        "  DETAIL",
        "RESULTS",
    ]


def test_deep_numbering_caps_at_four_levels():
    """`1.2.3.4.5` 这种超深编号收敛到 4 级，与前端缩进上限一致。"""
    flat = _flat([("1.2.3.4.5 DEEP", "1.2.3.4.5", 1, 6)])
    assert _apply_semantic_levels(flat) is True
    assert flat[0]["level"] == 4
