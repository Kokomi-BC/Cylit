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


def test_near_miss_uses_the_preferred_face_not_the_last_one(capture, monkeypatch) -> None:
    """中文探针找不到时，候选要从**译文面**里挑（以前拿最后那一面=英文页去挑，必然为空）。"""
    payload = {"page": 4,
               "rows": [{"id": "p004-04", "original": "As our lower bounds show, the MSE distortion ...",
                         "translation": "如我们的下界所示，TURBOQUANT 的 MSE 失真可证明至多处于"
                                        "信息论下界的因子 2.7 之内。"}],
               "original_md": "As our lower bounds show, the MSE distortion is bounded ...",
               "translation_md": "如我们的下界所示，TURBOQUANT 的 MSE 失真可证明至多处于"
                                 "信息论下界的因子 2.7 之内。"}
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: [payload])
    out = ann.create(PAPER, anchor="md:p4", text="TURBOQUANT 的失真可证明处于信息论下界之内")
    assert out["ok"] is False
    assert out["near_miss"], "译文面里那句就在，必须给候选"
    assert "下界" in out["near_miss"][0]

# --------------------------------------------------------------------------
# 体验：公式占位符 / 显式面 / 跨块跨页
# --------------------------------------------------------------------------


def test_probe_may_omit_the_formula_space(capture, monkeypatch) -> None:
    """用户从界面复制中文时，公式那一处是**空位** —— 这种情况也要能落上。"""
    raw = "我们的目标是在将 $x$ 量化为每个坐标 $b$ 比特的同时，尽量减少失真。"
    payload = {"page": 5, "rows": [{"id": "p005-01", "original": "We quantize x ...",
                                   "translation": raw}],
               "original_md": "We quantize x ...", "translation_md": raw}
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: [payload])
    out = ann.create(PAPER, anchor="md:p5", text="我们的目标是在将 量化为每个坐标 比特的同时，尽量减少失真。")
    assert out["ok"] is True, out.get("error")
    assert capture["block_id"] == "p005-01" and capture["scope"] == "row"
    assert ann.ATOM not in capture["selected_text"], "存下来的引用里不能留方块占位符"
    assert "量化为每个坐标" in capture["selected_text"]


def test_explicit_en_side_is_respected_for_formulas(capture, monkeypatch) -> None:
    """显式 `side="en"` + 英文面有这条公式 → **照办**（以前被静默改成译文面）。"""
    en = "$$D_{\\text{prod}} \\leq \\frac{\\sqrt{3}}{2} \\cdot \\frac{1}{4^b}$$"
    zh = "$$D_{\\text{prod}} \\leq \\frac{\\sqrt{3}}{2} \\cdot \\frac{1}{4^b}$$"
    payload = {"page": 4, "rows": [{"id": "p004-09", "original": en, "translation": zh}],
               "original_md": en, "translation_md": zh}
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: [payload])
    probe = "D_{\\text{prod}} \\leq \\frac{\\sqrt{3}}{2} \\cdot \\frac{1}{4^b}"
    out = ann.create(PAPER, anchor="md:p4", text=probe, side="en")
    assert out["ok"] is True
    assert capture["side"] == "en"
    assert "side_note" not in out, "照办了就不该再解释"


def test_formula_falls_back_to_zh_with_a_note(capture, monkeypatch) -> None:
    """显式 `en` 但英文面没有这条 → 落到译文面，并**明说**（不静默）。"""
    zh = "$$Q_{\\text{prod}}(x) = \\left[Q_{\\text{mse}}(x)\\right]$$"
    payload = {"page": 6, "rows": [{"id": "p006-09", "original": "unrelated english",
                                   "translation": zh}],
               "original_md": "unrelated english", "translation_md": zh}
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: [payload])
    probe = "Q_{\\text{prod}}(x) = \\left[Q_{\\text{mse}}(x)\\right]"
    out = ann.create(PAPER, anchor="md:p6", text=probe, side="en")
    assert out["ok"] is True and capture["side"] == "zh"
    assert "side_note" in out and "en" in out["side_note"]


# --------------------------------------------------------------------------
# 工具层：宿主的 warning / side_note 必须原样带出去
# --------------------------------------------------------------------------


class _Host:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def highlight_create(self, paper_id, payload):
        self.calls += 1
        return self.payload


class _DepsStub:
    def __init__(self, host) -> None:
        self.host = host
        self.paper_id = PAPER
        self.edit_calls = 0

    def count_edit(self) -> None:
        self.edit_calls += 1

    def note_tool(self, *args, **kwargs) -> None:   # `_guarded` 记账用
        pass


def _tool_ctx(host):
    from types import SimpleNamespace

    return SimpleNamespace(deps=_DepsStub(host))


def test_warning_reaches_the_model() -> None:
    """宿主说"这条落在 Markdown 面、看不见"，工具不能把它吞掉（白名单字典丢过字段）。"""
    import asyncio

    from backend.agent.tools import highlight_manage

    host = _Host({"ok": True, "scope": "markdown", "page": 5,
                 "highlight": {"id": "h1", "text": "跨段落的句子", "color": "#0d9488", "side": "zh"},
                 "warning": "这条标在 Markdown 视图 —— 阅读视图里看不到它。"})
    out = asyncio.run(highlight_manage(_tool_ctx(host), op="create", anchor="md:p5",
                                       text="跨段落的句子"))
    assert out["ok"] is True and out["scope"] == "markdown"
    assert "warning" in out and "看不到" in out["warning"]


def test_side_note_reaches_the_model() -> None:
    import asyncio

    from backend.agent.tools import highlight_manage

    host = _Host({"ok": True, "scope": "row", "page": 5,
                 "highlight": {"id": "h1", "text": "$x$", "color": "yellow", "side": "zh"},
                 "side_note": "你要的是 `en` 面，这条却落到了 `zh` 面"})
    out = asyncio.run(highlight_manage(_tool_ctx(host), op="create", anchor="md:p5",
                                       text="$x$", side="en"))
    assert "side_note" in out and "en" in out["side_note"]





def test_edit_and_undo_drop_cached_searches() -> None:
    """改完正文后同一条检索必须**重新查**（缓存键不含内容版本，只能在写入时清）。"""
    from backend.agent.tools import _forget_searches, _cached, _remember

    from types import SimpleNamespace

    deps = SimpleNamespace(query_cache={})
    ctx = SimpleNamespace(deps=deps)
    _remember(ctx, "paper_search", "自检标记", 24, {"returned": 1})
    assert _cached(ctx, "paper_search", "自检标记", 24) is not None
    _forget_searches(ctx, "测试")
    assert _cached(ctx, "paper_search", "自检标记", 24) is None, "改完之后必须重新查"

def test_snippet_centers_on_the_match() -> None:
    """块很长、命中在末尾时，片段要截到命中处（否则回执看着像"没搜到"）。"""
    from backend.agent.tools import _snippet_around

    body = "注意式 (3) 中的 $\\mathcal{C}(f_X, b)$ 给出了码本。 " * 8 + "伪代码见算法 1〔自检甲〕。"
    out = _snippet_around(body, "〔自检甲〕")
    assert "〔自检甲〕" in out
    assert out.startswith("…"), "前面被截掉了就要有省略号"
    assert len(out) <= 200

def test_near_miss_drops_the_nested_short_candidate() -> None:
    """长句与它的子串只会被切成两个候选 —— 只留长的那条（短的不提供新信息）。"""
    block = "该上界可以表述为内积误差的上界，且与比特宽度无关。"
    near = ann.near_misses(block, "该上界可以被看成内积误差的上界")
    assert near
    for one in near:
        assert not any(one != other and one in other for other in near), near

def test_near_miss_is_deduplicated() -> None:
    block = "我们证明，MSE 优化的量化器在内积估计上是有偏的。"
    near = ann.near_misses(block, "对估计内积是有偏的")
    assert len(near) == len(set(near))


def test_message_says_the_text_is_on_another_page(capture, monkeypatch) -> None:
    """把第 3 页的文字传到 md:p4 → 报错要点明"在第 3 页"，而不是"没有相近的句子"。"""
    p3 = {"page": 3, "rows": [{"id": "p003-05", "original": "Proof of Theorem 2 ...",
                               "translation": "定理 2 的证明如下：先做正交变换。"}],
          "original_md": "Proof ...", "translation_md": "定理 2 的证明如下：先做正交变换。"}
    p4 = {"page": 4, "rows": [{"id": "p004-04", "original": "Lower bound ...",
                               "translation": "如我们的下界所示，失真至多处于下界附近。"}],
          "original_md": "Lower bound ...", "translation_md": "如我们的下界所示，失真至多处于下界附近。"}
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: [p3, p4])
    out = ann.create(PAPER, anchor="md:p4", text="定理 2 的证明如下：先做正交变换。")
    assert out["ok"] is False
    assert "第 3 页" in out["error"]


def test_no_contradictory_tail_when_the_text_is_on_another_page(capture, monkeypatch) -> None:
    """已经查出"在第 3 页"时，不能再补一句"这一段里没有相近的句子"。"""
    p3 = {"page": 3, "rows": [{"id": "p003-08", "original": "Online quantization ...",
                               "translation": "在线（数据无关的）量化方法无需针对特定数据调参，即可即时应用。"}],
          "original_md": "Online ...", "translation_md": "在线（数据无关的）量化方法无需针对特定数据调参，即可即时应用。"}
    p4 = {"page": 4, "rows": [{"id": "p004-04", "original": "Lower ...",
                               "translation": "如我们的下界所示。"}],
          "original_md": "Lower ...", "translation_md": "如我们的下界所示。"}
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: [p3, p4])
    out = ann.create(PAPER, anchor="md:p4",
                     text="在线（数据无关的）量化方法无需针对特定数据调参，即可即时应用。")
    assert out["ok"] is False
    assert "第 3 页" in out["error"]
    assert "没有相近的句子" not in out["error"]

def test_message_says_the_text_spans_two_rows(capture, monkeypatch) -> None:
    """把两段拼成一句 → 报错要点明"跨了两行"。"""
    rows = [{"id": "p005-07", "original": "a", "translation": "我们先做 QR 分解来生成"},
            {"id": "p005-08", "original": "b", "translation": "所得旋转向量再逐坐标量化。"}]
    payload = {"page": 5, "rows": rows,
               "original_md": "a\nb", "translation_md": "我们先做 QR 分解来生成\n所得旋转向量再逐坐标量化。"}
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: [payload])
    out = ann.create(PAPER, anchor="md:p5", text="我们先做 QR 分解来生成所得旋转向量再逐坐标量化。")
    # 跨块的句子只能落在 Markdown 面（看不见），所以不报错、但必须**明说**为什么
    assert out["ok"] is True and out["scope"] == "markdown"
    assert "跨了两个段落" in out["warning"], out.get("warning")
    assert "Markdown 视图" in out["warning"] and "看不到" in out["warning"]

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
