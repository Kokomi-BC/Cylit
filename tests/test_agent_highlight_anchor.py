r"""高亮落点：中文引文要落到译文行，公式要落到**整式**而不是碎片。

线上实测（2026-12-07，TurboQuant）：4 条中文高亮全落到 `md:pN`（阅读视图里看不见），
公式高亮全部落到行内碎片（`$_{\text{prod}}$`、`$\boldsymbol{x}$`）。两个根因：

1. 调用方显式给 `side="en"` 时宿主照办 → `locate_in_rows` 只在英文行里找中文 → 找不到 →
   退回 Markdown 面（而 MD 面在阅读视图里不画高亮）；
2. `locate_in_rows` 按行序"第一个命中就返回"，而碎片常常出现在整式之前 → 覆盖率 8% 的碎片
   抢在覆盖率 100% 的整式前面。
"""
from __future__ import annotations

import pytest

from backend.services.agent import annotations as ann

PAPER = "demo-paper"

#: 第 6 页真实形状：先出现碎片那一行，整式在后面的行（顺序即真实顺序）
FRAGMENT = "残差量化 $_{\\text{prod}}$ 的部分"
WHOLE = "$$Q_{\\text{prod}}(x) = \\left[Q_{\\text{mse}}(x), \\|x - Q_{\\text{mse}}^{-1}(Q_{\\text{mse}}(x))\\|_2\\right].$$"
PROBE = ("Q_{\\text{prod}}(x) = \\left[Q_{\\text{mse}}(x), \\|x - Q_{\\text{mse}}^{-1}"
         "(Q_{\\text{mse}}(x))\\|_2\\right].")

ZH_TEXT = "随机旋转在量化与低精度 transformer 文献中是一种有效的技术"
P2_ROW = "我们的量化器首先对 $d$ 维输入向量进行随机旋转。随机旋转在量化与低精度 " \
         "transformer 文献中是一种有效的技术，此前已被多次使用。"


@pytest.fixture
def capture(monkeypatch):
    grabbed: dict = {}

    def fake_create(payload, user_id=1):
        grabbed.update(payload)
        return {"id": "h-new", "block_id": payload["block_id"],
                "text": payload["selected_text"], "side": payload["side"],
                "scope": payload["scope"], "color": payload["color"],
                "start": payload["start_offset"], "end": payload["end_offset"]}

    monkeypatch.setattr(ann.svc, "create_highlight", fake_create)
    monkeypatch.setattr(ann, "spare_color_for", lambda pid: "yellow")
    return grabbed


def _install(monkeypatch, pages: list[dict]) -> None:
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: pages)


def test_chinese_text_never_stays_on_the_markdown_face(monkeypatch, capture) -> None:
    """中文引文 + 显式 `side="en"` → 仍然落到**译文行**（中文只存在于译文行）。"""
    payload = {"page": 2, "rows": [{"id": "p002-04", "original": "Random rotation ...",
                                   "translation": P2_ROW}],
               "original_md": "Random rotation ...", "translation_md": P2_ROW}
    _install(monkeypatch, [payload])
    out = ann.create(PAPER, anchor="md:p2", text=ZH_TEXT, side="en")
    assert out["ok"] is True
    assert capture["block_id"] == "p002-04", "要挪到正文行，不能留在 md:p2"
    assert capture["scope"] == "row" and capture["side"] == "zh"
    assert capture["selected_text"] == ZH_TEXT


def test_formula_lands_on_the_whole_formula_not_the_fragment(monkeypatch, capture) -> None:
    """调用方给了**碎片那一行**当锚点，也要按覆盖率落到整式所在行。"""
    payload = {"page": 6, "rows": [
        {"id": "p006-00", "original": FRAGMENT, "translation": FRAGMENT},
        {"id": "p006-09", "original": WHOLE, "translation": WHOLE}],
        "original_md": FRAGMENT, "translation_md": FRAGMENT}
    _install(monkeypatch, [payload])
    out = ann.create(PAPER, anchor="p006-00", text=PROBE)
    assert out["ok"] is True
    assert capture["block_id"] == "p006-09", "整式那一行（覆盖率 100%），不是碎片行"
    assert (capture["start_offset"], capture["end_offset"]) == (0, 1)
    assert capture["selected_text"] == WHOLE          # 存整条 LaTeX（含 $$），不是探针
    assert capture["side"] == "zh"


def test_formula_probe_without_a_whole_match_refuses_the_fragment(monkeypatch, capture) -> None:
    """整页只有碎片时**宁可报找不到**，也不拿碎片冒充整式（碎片上的高亮=标错地方）。"""
    payload = {"page": 6, "rows": [
        {"id": "p006-00", "original": FRAGMENT, "translation": FRAGMENT}],
        "original_md": FRAGMENT, "translation_md": FRAGMENT}
    _install(monkeypatch, [payload])
    out = ann.create(PAPER, anchor="md:p6", text=PROBE)
    assert out["ok"] is False and "找不到" in out["error"]
    assert capture == {}                              # 没落库


def test_bad_anchor_says_the_format_not_a_missing_text(capture) -> None:
    """`anchor="row"` 是**参数格式错**，不能说成"这段文字找不到"（实测误导过一轮）。"""
    out = ann.create(PAPER, anchor="row", text="随便一句中文")
    assert out["ok"] is False
    err = out["error"]
    assert "md:p4" in err and "格式" in err
    assert "重译" not in err, "别让人以为是内容/行号变了"
    assert capture == {}


def test_markdown_anchor_without_page_number(capture) -> None:
    out = ann.create(PAPER, anchor="md:p", text="随便一句中文")
    assert out["ok"] is False and "页号" in out["error"]


def test_near_miss_tolerates_a_few_wrong_characters() -> None:
    """错几个字（连开头都不一样）也要给出候选，不能回空数组。"""
    block = ("我们证明，MSE 优化的量化器在内积估计上是有偏的，因此需要另一种方案。"
             "下一句是别的内容。")
    near = ann.near_misses(block, "对估计内积是有偏的")
    assert near, "同一块里只差几个字，必须给候选"
    assert "内积" in near[0]


def test_near_miss_empty_when_nothing_is_close() -> None:
    assert ann.near_misses("完全无关的一句话。", "zzz unrelated english probe") == []

def test_short_probe_still_matches_a_longer_formula(monkeypatch) -> None:
    """短板探针（用户只引了一段）仍要认：覆盖率按**探针长度**算，短探针天然 100%。"""
    hit = ann.find_formula_span(WHOLE, r"\|_2\right].")
    assert hit is not None
    assert hit[2] == WHOLE


def test_long_probe_is_not_satisfied_by_a_fragment(monkeypatch) -> None:
    """长探针 vs 碎片：`find_formula_span` 自己就要拒（15/165 < 一半）。"""
    assert ann.find_formula_span(FRAGMENT, PROBE) is None
