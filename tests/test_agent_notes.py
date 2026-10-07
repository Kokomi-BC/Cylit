"""agent 建高亮 / 笔记的两条硬规矩（纯函数 + 打桩，零模型调用）。

背景（用户报告 2026-10-07）：让 agent"给这一段加笔记"，结果 ① 高亮**在用户眼前不存在**
（标到了 `md:pN` 那一面，而用户读的是对照 / 译文视图），② 侧栏里同一条内容被渲染成**两条**
（一条「待写」高亮 + 一条「笔记」）。两条都是"面"和"归并"的口径问题，这里把它们钉住。
"""
from __future__ import annotations

from backend.services.agent import annotations as ann


# --------------------------------------------------------------------------
# 1. 高亮落在**哪一面**：Markdown 锚点要尽量落到正文行上
# --------------------------------------------------------------------------


def test_markdown_anchor_moves_to_the_row_when_the_text_is_there(monkeypatch) -> None:
    """`md:p9` + 这一页某一行里有这段文字 → 用**行锚点**（用户读的对照/译文视图才画得出来）。"""
    monkeypatch.setattr(ann, "_page_payloads", lambda _pid: [{
        "page": 9,
        "rows": [
            {"id": "p009-07", "original": "Another paragraph.", "translation": "另一段。"},
            {"id": "p009-08",
             "original": "Product Quantization (PQ) Douze et al. (2024) uses k-means.",
             "translation": "乘积量化用 k-means。"},
        ],
    }])
    hit = ann.locate_in_rows("p", 9, "Product Quantization (PQ) Douze et al. (2024) uses k-means.")
    assert hit is not None
    assert hit[0] == "p009-08"
    assert hit[2][hit[1][0]:hit[1][1]].startswith("Product Quantization")


def test_markdown_anchor_stays_when_no_row_has_it(monkeypatch) -> None:
    """这一页的行里找不到 → 返回 None（调用方保留 Markdown 面，不乱挪到别的页去）。"""
    monkeypatch.setattr(ann, "_page_payloads", lambda _pid: [{
        "page": 9, "rows": [{"id": "p009-07", "original": "Another paragraph.", "translation": ""}],
    }])
    assert ann.locate_in_rows("p", 9, "这句话不在这一页里") is None
    assert ann.locate_in_rows("p", 0, "随便") is None


def test_anchor_kind_and_page_parsing() -> None:
    assert ann.anchor_kind("p009-08") == "row"
    assert ann.anchor_kind("md:p9") == "markdown"
    assert ann.anchor_kind("pdf:p3") == "pdf"
    assert ann.anchor_page_no("md:p9") == 9
    assert ann.anchor_page_no("pdf:p12") == 12
    assert ann.anchor_page_no("p009-08") == 0       # 行锚点不在这里解析（只有 Markdown 分支用它）


# --------------------------------------------------------------------------
# 2. 阅读器 payload 必须带 `highlight_id`：侧栏靠它把笔记挂到高亮上
# --------------------------------------------------------------------------


def test_reader_payload_notes_carry_highlight_id_and_scope() -> None:
    """阅读器 payload 的 `notes` 少了 `highlight_id`，侧栏就把一条内容渲染成两条。

    这是 `utils/notesFeed` 的归并键（`n.highlight_id === h.id`）—— 漏了它，
    那处高亮显示成「待写」、笔记另起一条（用户报"产生了两条莫名的笔记"）。
    这里直接读源码断言那两个字段在 payload 构造里出现（构造要 DB，纯函数测不动）。
    """
    import inspect

    from backend.services.paper import reader_service

    src = inspect.getsource(reader_service.build_reader)
    assert '"highlight_id": n.highlight_id' in src
    assert '"scope": _note_scope(n.block_id)' in src


def test_note_scope_is_shared_by_both_serializers() -> None:
    """`scope` 的口径只有一处（`annotation_service.note_scope`），两份序列化器不许各写一套。"""
    from backend.services.annotation import annotation_service as ann_svc

    assert ann_svc.note_scope("pdf:p3") == "pdf"
    assert ann_svc.note_scope("md:p8") == "markdown"
    assert ann_svc.note_scope("p009-08") == "row"
    assert ann_svc.note_scope(None) == "row"
    assert ann_svc.note_scope("md:p9") == ann_svc._note_dict.__globals__["note_scope"]("md:p9")
