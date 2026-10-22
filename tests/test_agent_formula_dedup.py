r"""`formula_get` 的列表去重（纯离线）。

实测（2026-12，线上 p3/p5）：同一条公式在一段正文里出现两三次 ——
`p005-03` 的 `$\boldsymbol{\Pi} \cdot x$` 三次、`p003-04` 的 `$Q_{\\text{prod}}$` 两次 ——
读时抽取是**按出现次数**给的，于是列表里同一个 ref 重复出现，还各自占掉一个引用号。

规则：同 `(page, row_id, latex)` 只留**第一条**（顺序与其它条目不变）；
latex 只是排版空白不同（` \n` vs ` `）也算同一条。
"""

from __future__ import annotations

import pytest

from backend.services.agent import project


def _item(page: int, row_id: str, latex: str, context: str = "上下文") -> dict:
    return {"page": page, "row_id": row_id, "latex": latex, "display": False,
            "context": context, "context_zh": "", "model": "m", "source": "markdown"}


@pytest.fixture
def fake_payload(monkeypatch):
    def install(items: list[dict]) -> None:
        monkeypatch.setattr(project.reader_service, "formulas_payload",
                            lambda paper_id, source="markdown": {"items": items,
                                                                 "source": "markdown"})
        monkeypatch.setattr(project.paper_service, "get_paper",
                            lambda paper_id: {"id": paper_id, "page_count": 22})
    return install


def test_repeated_formula_in_one_row_is_collapsed(fake_payload) -> None:
    """同一行里出现三次的同一条公式只留一条；其它条目一条不少、顺序不变。"""
    fake_payload([
        _item(5, "p005-00", "j \\in [d]"),
        _item(5, "p005-03", "\\boldsymbol{\\Pi} \\cdot x"),
        _item(5, "p005-03", "\\boldsymbol{\\Pi} \\cdot x"),     # 同段重复
        _item(5, "p005-03", "\\boldsymbol{\\Pi} \\cdot x"),     # 同段重复
        _item(5, "p005-07", "Q_{\\text{mse}}"),
    ])
    out = project.formulas("p1")
    latexes = [item["latex"] for item in out["items"]]
    assert latexes == ["j \\in [d]", "\\boldsymbol{\\Pi} \\cdot x",
                       "Q_{\\text{mse}}"]
    assert out["total"] == 3 and out["returned"] == 3


def test_dedup_treats_whitespace_only_differences_as_the_same_formula(fake_payload) -> None:
    fake_payload([_item(3, "p003-04", "Q_{\\text{prod}}"),
                  _item(3, "p003-04", "Q_{\\text{prod}}  ")])
    assert len(project.formulas("p1")["items"]) == 1


def test_same_formula_on_different_rows_is_kept(fake_payload) -> None:
    """不同行里的同一条公式**不算重复**：它们指向不同的正文位置，引用要分别给号。"""
    fake_payload([_item(5, "p005-03", "x"), _item(5, "p005-08", "x")])
    assert len(project.formulas("p1")["items"]) == 2
