"""公式知识库的来源与筛选（**全离线**，纯函数）。

用户口径（§1/§2/§8）：
- **唯一来源** = `reader_service.markdown_formulas()`（阅读器公式面板那份清单）；
  知识库侧**不再自己识别公式**（禁止第二套 `original_md` 正则）；
- **C 口径过滤** = 行间公式 ∪ 编号公式 ∪ 有独立语义的定义/关系式；
  挡掉 `$d$`、`$b$`、`$Q(\\cdot)$` 这类低信息量行内数学；
- **不变式**：知识库公式 ⊆ 阅读器公式清单（每条都能用 paper_id + page + row_id 回溯）；
- 锚点：解析块可靠对应 → `formula_block` / `exact`；否则 `page_row` / `row`；
  连 row 都没有才 `page` / `page`。
"""

from __future__ import annotations

import pytest

from backend.services.knowledge import formula_source as fs

EQ1 = r"(\text{MSE}) \quad D_{\text{mse}} := \mathbb{E}_Q\left[\left\|x - Q^{-1}(Q(x))\right\|_2^2\right] \tag{1}"
DEFINITION = r"W_i^Q \in \mathbb{R}^{d_{\text{model}} \times d_k}"
QMARK = r"q \cdot k = \sum_{i=1}^{d_k} q_i k_i"
SMALL = r"d"
PSEUDO = r"\text{idx} \leftarrow \text{QUANT}(x)"


def _item(latex: str, *, display: bool = False, page: int = 5,
          row_id: str = "p005-03", id_: str = "f005-0001") -> dict:
    return {"id": id_, "page": page, "row_id": row_id, "latex": latex,
            "display": display, "context": "...", "context_zh": "..."}


# --------------------------------------------------------------------------
# C 口径
# --------------------------------------------------------------------------


def test_display_formula_always_kept() -> None:
    assert fs.is_kb_formula(_item(r"a = \sum_i b_i", display=True)) is True


def test_numbered_formula_always_kept() -> None:
    """编号公式哪怕写成行内也收（Attention Eq.1 就是行内写的）。"""
    assert fs.is_kb_formula(_item(EQ1, display=False)) is True
    assert fs.formula_number(EQ1) == "1"


@pytest.mark.parametrize("latex", [DEFINITION, QMARK,
                                   r"p_\ell := \int f_X(x)\,dx",
                                   r"Q : \mathbb{S}^{d-1} \to \{0, 1\}^{b \cdot d}",
                                   r"z_i := \sqrt{\pi/2} \cdot s_i^\top y"])
def test_definition_shaped_inline_kept(latex: str) -> None:
    """有独立语义的定义/关系式要收（C 口径比"只收行间"多出来的正是这批）。"""
    assert fs.is_kb_formula(_item(latex)) is True


@pytest.mark.parametrize("latex", [SMALL, r"b", r"Q(\cdot)", r"i \in [d]",
                                   r"1.0 \cdot 10", PSEUDO,
                                   r"is the sequence length,"])
def test_low_information_inline_dropped(latex: str) -> None:
    """低信息量行内数学、伪代码行、散文片段都不进公式库。"""
    assert fs.is_kb_formula(_item(latex)) is False


def test_latex_command_names_are_not_prose() -> None:
    """`\\left`/`\\quad`/`\\frac` 不是英文词 —— 否则带括号的编号公式会被误杀。"""
    assert fs.has_definition_shape(EQ1) is True
    assert fs.is_kb_formula(_item(EQ1)) is True


# --------------------------------------------------------------------------
# 续行合并（过滤/规范化，不是重新识别公式）
# --------------------------------------------------------------------------


def test_group_continuations_merges_same_row_only() -> None:
    items = [
        _item(r"D_{\text{mse}} = \sum_j \mathbb{E}[|x_j - \tilde{x}_j|^2]", row_id="r1",
              id_="a"),
        _item(r"= \sum_j \mathbb{E}[|\langle e_j, x\rangle - \langle e_j, Q^{-1}Q(x)\rangle|^2]",
              row_id="r1", id_="b"),
        _item(r"\geq \frac{1}{4^b}.", row_id="r1", id_="c"),
        _item(r"\geq \frac{1}{4^b}.", row_id="r2", id_="d"),          # 不同行 → 不并
    ]
    out = fs.group_continuations(items)
    assert len(out) == 2
    assert out[0]["source_item_ids"] == ["a", "b", "c"]
    assert out[0]["latex"].startswith("\\begin{aligned}")
    assert out[1]["source_item_ids"] == ["d"]


def test_group_continuations_skips_short_operator_spans() -> None:
    """`$-\\infty$` 以 `-` 开头但不是续行（实测把 `W^O ∈ …` 粘错过）。"""
    out = fs.group_continuations([
        _item(r"W^O \in \mathbb{R}^{hd_v \times d_{\text{model}}}", row_id="r1", id_="a"),
        _item(r"-\infty", row_id="r1", id_="b"),
    ])
    assert len(out) == 2


# --------------------------------------------------------------------------
# 来源唯一 + 不变式（知识库 ⊆ 阅读器清单）
# --------------------------------------------------------------------------


def _fake_canonical(monkeypatch, items: list[dict]) -> None:
    import backend.services.paper.reader_service as rs

    monkeypatch.setattr(rs, "markdown_formulas",
                        lambda pid: {"items": items, "count": len(items),
                                     "source": "markdown",
                                     "display_count": sum(1 for i in items if i["display"])})


def test_kb_entries_come_from_canonical_list(monkeypatch) -> None:
    """知识库条目**只**来自 canonical 清单，且每条都能按 (page, row_id) 回溯。"""
    items = [
        _item(EQ1, display=True, page=4, row_id="p004-02", id_="f004-0000"),
        _item(SMALL, page=4, row_id="p004-03", id_="f004-0001"),           # C 挡掉
        _item(DEFINITION, page=5, row_id="p005-01", id_="f005-0000"),      # C 收
    ]
    _fake_canonical(monkeypatch, items)
    entries = fs.kb_formula_entries("demo", {}, [{"id": "b1", "page": 4, "order": 1}])
    assert len(entries) == 2
    canon = {(i["page"], i["row_id"]) for i in items}
    for e in entries:
        assert (e["page"], e["row_id"]) in canon, "每条都必须能回溯到 canonical 条目"
        assert "synthetic" not in e and "needs_review" not in e
        assert e["latex_source"] == "page_vision"


def test_kb_entry_anchor_without_parse_block_is_page_row(monkeypatch) -> None:
    _fake_canonical(monkeypatch, [_item(QMARK, page=5, row_id="p005-07")])
    entries = fs.kb_formula_entries("demo", {}, [{"id": "b1", "page": 5, "order": 1}])
    assert entries[0]["anchor_type"] == "page_row"
    assert entries[0]["anchor_precision"] == "row"
    assert entries[0]["block_id"] == "p005-07", "锚点直接用 row_id（阅读器原生支持）"


def test_kb_entry_anchor_with_parse_block_is_exact(monkeypatch) -> None:
    """解析块能可靠对上 → formula_block / exact，并带解析侧字段。"""
    _fake_canonical(monkeypatch, [_item(QMARK, page=5, row_id="p005-07")])
    formulas = {"b1": {"block_id": "b1", "latex_final": QMARK, "grade": "A",
                       "image_path": "/tmp/c.png", "raw_text": QMARK}}
    rows = [{"id": "b1", "page": 5, "order": 1}]
    entries = fs.kb_formula_entries("demo", formulas, rows)
    assert entries[0]["anchor_type"] == "formula_block"
    assert entries[0]["anchor_precision"] == "exact"
    assert entries[0]["formula_block_id"] == "b1"
    assert entries[0]["grade"] == "A" and entries[0]["crop"] == "/tmp/c.png"
    assert entries[0]["parsed_latex"] == QMARK


def test_kb_entry_without_row_falls_back_to_page(monkeypatch) -> None:
    item = _item(EQ1, page=9)
    item["row_id"] = None
    _fake_canonical(monkeypatch, [item])
    entries = fs.kb_formula_entries("demo", {}, [{"id": "b1", "page": 9, "order": 1}])
    assert entries[0]["anchor_type"] == "page" and entries[0]["anchor_precision"] == "page"


# --------------------------------------------------------------------------
# 端到端不变式（真实语料，需要页级产物；没有就跳过）
# --------------------------------------------------------------------------


def test_real_corpus_counts_and_subset_invariant() -> None:
    """TurboQuant 322→90 / Attention 89→12（C 过滤）；条目 ⊆ 清单且可回溯。"""
    from backend.repositories.mysql.session import healthcheck

    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用")
    from backend.services.paper import paper_service

    # ⚠️ TurboQuant 是 **92** 而不是 95：先前那次 95 的测量里 `\\to` 没加词边界，
    # 被 `^\\top` 命中，混进了 3 条伪代码赋值行（`\\tilde{x} \\leftarrow …`）——
    # 那正是用户明确要求"不进公式库"的东西。这里把正确数字钉住。
    # ⚠️ 重转录（"按 markdown 层级切分"那轮，提示词新增语义锚点 `breaks`）之后页级产物变了，
    # 页级公式清单跟着变：turbo 349→322 / 92→90，attention 90→89 / 13→12。
    # ⚠️ 再一次重转录（两路拆分那轮：A 路只转录、B 路只翻译，公式从 **A 路**的原文里抽）之后又变了一次：
    # turbo 322→**329** / 90（不变），attention 89→**88** / 12→**11**。
    # 数字变了不代表公式变多/变少 —— 是"模型这次把哪些行写成独立公式块"变了（`$$…$$` 的条数）。
    expect = {"turboquant-online-vector-iclr-26": (329, 90),
              "attention-is-all-you-need": (88, 11)}
    for pid, (canon_n, c_n) in expect.items():
        try:
            paper_service.get_paper(pid)
        except Exception:  # noqa: BLE001
            pytest.skip(f"库里没有 {pid}")
        items = fs.canonical_items(pid)
        if not items:
            pytest.skip(f"{pid} 没有页级公式清单")
        assert len(items) == canon_n, f"{pid} canonical 数量变了：{len(items)}"
        passed = [it for it in items if fs.is_kb_formula(it)]
        assert len(passed) == c_n, f"{pid} C 过滤数量变了：{len(passed)}"
        kept = fs.kb_formula_entries(pid, {}, [{"id": "x", "page": 0, "order": 0}])
        # 同行续行合并会让**条目**数略少于通过 C 的条数，但绝不许多出来
        assert 0 < len(kept) <= c_n, f"{pid} 条目数异常：{len(kept)}"
        canon_ids = {i["id"] for i in items}
        for e in kept:
            assert set(e["source_item_ids"]) <= canon_ids, "条目必须来自 canonical 清单"
