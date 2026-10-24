r"""宿主的公式清单：合并掉的那一条要**带着自己的 LaTeX** 一起走（纯离线）。

用户实测 C：`formula_get` 里第 6 页那条 `also_on=[18]` 而 `also_on_detail=[]` ——
想引用第 18 页那一版还得自己 `page_markdown_get` 去翻，且"另一版长什么样"根本拿不到。

成因在分层：宿主 `project.formulas` **先**合并一次，被合并掉的那条从这里起就不在列表里，
工具层再也看不到它的 LaTeX。所以详情只能由宿主带出去（`also_on_detail`），
且页码必须与 `also_on` **一一对应**（升序、去重、不含条目自己那一页）。
"""

from __future__ import annotations

import pytest

from backend.services.agent import project


def _item(page: int, row_id: str, latex: str, *, display: bool = True, **extra) -> dict:
    item = {"page": page, "row_id": row_id, "latex": latex, "display": display,
            "context": "上下文", "context_zh": "", "model": "m", "source": "markdown"}
    item.update(extra)
    return item


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


BOUND = r"D_{\text{mse}} \leq \frac{\sqrt{3}\pi}{2} \cdot \frac{1}{4^b}"
#: 逐字不同（多一个句号）、归一化后相同的另一版
BOUND_ALT = r"D_{\text{mse}} \leq \frac{\sqrt{3}\pi}{2} \cdot \frac{1}{4^b}."


def test_merged_copy_keeps_its_own_latex(fake_payload) -> None:
    """跨页合并：页码进 `also_on`，那一版自己的 LaTeX 进 `also_on_detail`（逐字可以不同）。"""
    fake_payload([_item(6, "p006-02", BOUND), _item(18, "p018-03", BOUND_ALT)])
    out = project.formulas("p1")
    assert len(out["items"]) == 1, out["items"]
    one = out["items"][0]
    assert one["page"] == 6 and one["also_on"] == [18]
    assert one["also_on_detail"] == [{"page": 18, "latex": BOUND_ALT}], \
        "另一版逐字不同，正是要一起给的理由"


def test_detail_pages_match_also_on_one_to_one(fake_payload) -> None:
    """三处各一次 → 一条 + 两页；`also_on` 与 `also_on_detail` 逐项对应且升序。"""
    fake_payload([_item(6, "p006-02", BOUND), _item(18, "p018-03", BOUND),
                  _item(21, "p021-07", BOUND)])
    one = project.formulas("p1")["items"][0]
    assert one["also_on"] == [18, 21]
    assert [d["page"] for d in one["also_on_detail"]] == [18, 21]
    assert all(d["latex"] == BOUND for d in one["also_on_detail"])


def test_also_on_never_contains_the_entries_own_page(fake_payload) -> None:
    """万一回执里带了自指（自己那页在 `also_on` 里）→ 出口就剔掉，不原样转发。"""
    fake_payload([_item(6, "p006-02", BOUND, also_on=[6, 18],
                        also_on_detail=[{"page": 6, "latex": BOUND},
                                        {"page": 18, "latex": BOUND}])])
    one = project.formulas("p1")["items"][0]
    assert one["page"] == 6 and one["also_on"] == [18], one
    assert [d["page"] for d in one["also_on_detail"]] == [18]


def test_no_merge_means_empty_detail(fake_payload) -> None:
    """没合并过就没有详情（别凭空空一个数组以外的东西出来）。"""
    fake_payload([_item(6, "p006-02", BOUND)])
    one = project.formulas("p1")["items"][0]
    assert one["also_on"] == [] and one["also_on_detail"] == []
