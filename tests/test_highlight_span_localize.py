"""**写入口**（REST `POST /highlights`）也要按文字定位、按文字判面（用户 2026-12 复报）。

背景（生产库实测）：一条 `selected_text="Yao 极小极大原理"` 的高亮，`side=en`、
`start_offset=end_offset=0` —— 中文引文落在原文面，偏移量又是 0/0，于是
`utils/domMarks.markRanges`（只画 `end > start`）永远画不出来。两个问题都在
`annotation_service.create_highlight` 里：它以前把请求里的 `side` / 偏移量**原样照抄**。

这里锁住四条规则：

1. 面按**文字语言**判（含 CJK → `zh`），与 agent 那条路（`agent.annotations.create`）同一口径；
2. 偏移量按**文字**在当前页的行文本里定位（`locate_in_rows`）；
3. 定位不到 → `ValidationError`（API 400，消息可直接给用户看），**绝不存 0/0**；
4. `scope="markdown"` 的偏移量语义不动（它是那一面 Markdown 的坐标系，与行文本无关）。

用伪论文 + 伪页产物（`_page_payloads` 打桩），不碰真实论文的数据。
"""

from __future__ import annotations

import pytest

from backend.repositories.mysql.session import healthcheck
from backend.services.agent import annotations as ann
from backend.services.annotation import annotation_service as svc

pytestmark = pytest.mark.e2e

FAKE_PAPER = "__test_highlight_span__"
ZH_TEXT = "我们依赖 Yao 极小极大原理来证明下界。"
EN_TEXT = "We rely on Yao's minimax principle."
ROWS = [
    {"id": "p003-00", "original": EN_TEXT, "translation": ZH_TEXT},
    {"id": "p003-01", "original": "Second row of the proof.",
     "translation": "证明的第二行。"},
]


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()
    return True


@pytest.fixture(autouse=True)
def fake_page(db_ready, monkeypatch):
    """伪页产物：`locate_in_rows` / `anchor_text` 都走它（真实定位代码不绕过）。"""
    monkeypatch.setattr(ann, "_page_payloads", lambda pid: [{"page": 3, "rows": ROWS}])
    for row in svc.list_highlights(FAKE_PAPER)["items"]:
        svc.delete_highlight(row["id"])
    yield
    for row in svc.list_highlights(FAKE_PAPER)["items"]:
        svc.delete_highlight(row["id"])


def _make(**payload) -> dict:
    base = {"paper_id": FAKE_PAPER, "block_id": "p003-00"}
    base.update(payload)
    return svc.create_highlight(base)


def test_side_follows_the_text_language() -> None:
    """没给 `side` 时按文字判：引中文 → 译文面，引英文 → 原文面（旧代码一律 `en`）。"""
    zh = _make(selected_text="Yao 极小极大原理")
    assert zh["side"] == "zh"
    assert zh["start"] != zh["end"], "定位不到就 400，能存下来的必须是真的区间"

    en = _make(selected_text="minimax principle")
    assert en["side"] == "en"
    assert EN_TEXT[en["start"]:en["end"]] == "minimax principle"


def test_cjk_text_cannot_land_on_the_english_side() -> None:
    """显式给了 `en` 也听文字：中文引文存进原文面，用户看译文时**根本画不出来**。"""
    made = _make(selected_text="极小极大原理", side="en")
    assert made["side"] == "zh"
    assert ZH_TEXT[made["start"]:made["end"]] == "极小极大原理"


def test_side_aliases_are_accepted() -> None:
    """页产物那套词（`original` / `translation`）也认（agent 路径同口径）。"""
    assert _make(selected_text="Yao 极小极大原理", side="translation")["side"] == "zh"
    assert _make(selected_text="minimax principle", side="original")["side"] == "en"


def test_zero_span_is_located_by_text() -> None:
    """0/0（用户报的那条）→ 按文字定位出真实区间。"""
    made = _make(selected_text="Yao 极小极大原理", start_offset=0, end_offset=0)
    assert (made["start"], made["end"]) == (ZH_TEXT.index("Yao"), ZH_TEXT.index("Yao") + 10)
    assert made["side"] == "zh"


def test_wrong_span_is_relocated_by_text() -> None:
    """给定的区间上取出来的字对不上（换了译本 / 换了坐标系）→ 按文字重定位。"""
    made = _make(selected_text="minimax principle", start_offset=0, end_offset=3, side="en")
    assert made["start"] == EN_TEXT.index("minimax")
    assert made["end"] == made["start"] + len("minimax principle")
    assert made["side"] == "en"


def test_matching_span_is_left_alone() -> None:
    """区间与文字对得上就一个字节都不重算（老译本 / 跨行高亮都别被"纠正"）。"""
    start = EN_TEXT.index("minimax")
    made = _make(selected_text="minimax", start_offset=start, end_offset=start + 7,
                 side="en")
    assert (made["start"], made["end"]) == (start, start + 7)


def test_unfindable_text_is_rejected_not_stored_as_zero() -> None:
    """定位不到 → `ValidationError`（API 400），消息能直接给用户看；库里不许留 0/0。"""
    from backend.core.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        _make(selected_text="这句在论文里根本不存在的话")
    message = str(exc.value)
    assert "这段文字在当前页找不到" in message
    assert "Yao" not in message.split("「")[0]
    assert svc.list_highlights(FAKE_PAPER)["items"] == []


def test_text_found_on_another_row_is_reported_not_moved() -> None:
    """找到了、但在**别的行**上：报错说清是哪一行，不偷偷把锚点搬过去。"""
    from backend.core.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        _make(selected_text="Second row of the proof.", block_id="p003-00", side="en")
    assert "p003-01" in str(exc.value)


def test_markdown_scope_offsets_are_untouched() -> None:
    """`md:pN` 的偏移量是那一面渲染的 Markdown 的坐标系：语义不动，原样存。"""
    made = _make(block_id="md:p3", scope="markdown", selected_text="向量量化",
                 start_offset=0, end_offset=0, side="zh")
    assert (made["start"], made["end"], made["side"]) == (0, 0, "zh")
