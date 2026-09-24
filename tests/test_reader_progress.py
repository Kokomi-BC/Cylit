"""阅读进度的"打开状态"（视图 / 原文栏呈现 / 左右顺序）回归测试。

背景（用户口径）：**分别保存每篇论文的打开状态**，并且切标签 / 重开时还原它。
- `view_mode`（原文 / 译文 / 对照）与 `pair_order`（原文在左 / 译文在左）早就在进度里；
- `original_mode`（原文文本 / 原 PDF）是这一轮补上的 —— 以前切一下"原 PDF"，切个标签就丢了。

这里测两层：
1. **纯映射**：``_progress_payload`` 对新字段的默认值与旧值归一化（不需要数据库）；
2. **落库往返**（需要 MySQL，不可用就跳过）：写进去 → 读回来一致，非法值不覆盖已有状态；
   跑完把**原来那份进度原样写回**，绝不改用户真实的阅读状态。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.repositories.mysql.session import healthcheck
from backend.services.paper import reader_service


def _row(**kw):
    """伪造一行阅读进度（只用到 `_progress_payload` 读的那几个字段）。"""
    base = dict(last_block_id="p001-01", last_page=3, percent=12.5, view_mode="bilingual",
                pair_order="en_top", original_mode="page", scroll_ratio=0.5,
                aside_open=None, text_size=None, show_refs=None, zoom=None,
                page_side="zh", updated_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_payload_has_original_mode():
    """没有进度记录时也要给出默认的 `original_mode`（前端据此还原"原文文本"）。"""
    data = reader_service._progress_payload(None)  # noqa: SLF001 - 直接测映射
    assert data["original_mode"] == "text"
    assert data["view_mode"] == "bilingual"
    assert data["pair_order"] == "en_left"


def test_payload_reads_and_normalizes():
    """有记录时原样读出；历史的 `en_top/zh_top` 归一化成左右对照。"""
    data = reader_service._progress_payload(_row())  # noqa: SLF001
    assert data["original_mode"] == "page"
    assert data["pair_order"] == "en_left"          # en_top → en_left
    assert reader_service._progress_payload(_row(pair_order="zh_top"))["pair_order"] == "zh_left"  # noqa: SLF001
    # 旧记录里没有这个字段（NULL）时退回默认值，而不是塞个 None 给前端
    assert reader_service._progress_payload(_row(original_mode=None))["original_mode"] == "text"  # noqa: SLF001


def test_payload_defaults_for_ui_state():
    """界面状态（侧栏 / 字号 / 参考文献 / 缩放）没有记录时必须是 None（前端据此回退本地偏好）。"""
    empty = reader_service._progress_payload(None)  # noqa: SLF001
    for k in ("aside_open", "text_size", "show_refs", "zoom"):
        assert empty[k] is None
    data = reader_service._progress_payload(_row())  # noqa: SLF001
    assert data["aside_open"] is None and data["zoom"] is None   # 伪造行里也没给


def test_payload_page_side_defaults_to_zh():
    """对照 + 原 PDF 时文字列默认放译文（老记录里没这一列时不能变成"原文"）。"""
    assert reader_service._progress_payload(None)["page_side"] == "zh"        # noqa: SLF001
    assert reader_service._progress_payload(_row(page_side="en"))["page_side"] == "en"  # noqa: SLF001
    assert reader_service._progress_payload(_row(page_side=None))["page_side"] == "zh"  # noqa: SLF001


@pytest.mark.e2e
def test_progress_roundtrip_keeps_open_state():
    """写进去 → 读回来一致；非法值被忽略。跑完还原用户原来的打开状态。"""
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用")
    from backend.repositories.mysql.session import init_db
    from backend.services.paper import paper_service

    init_db()
    items = (paper_service.list_papers() or {}).get("items") or []
    if not items:
        pytest.skip("库里还没有论文")
    paper_id = items[0]["id"]
    keys = ("last_block_id", "last_page", "percent", "view_mode",
            "pair_order", "original_mode", "scroll_ratio",
            "aside_open", "text_size", "show_refs", "zoom", "page_side")
    before = reader_service.get_progress(paper_id)
    try:
        saved = reader_service.save_progress(paper_id, {
            "view_mode": "original", "pair_order": "zh_left", "original_mode": "page",
            "page_side": "en",
            "aside_open": False, "text_size": "lg", "show_refs": True, "zoom": 1.5})
        assert (saved["view_mode"], saved["pair_order"], saved["original_mode"]) \
            == ("original", "zh_left", "page")
        assert saved["page_side"] == "en"
        assert (saved["aside_open"], saved["text_size"], saved["show_refs"]) \
            == (False, "lg", True)
        assert abs(float(saved["zoom"]) - 1.5) < 1e-6
        again = reader_service.get_progress(paper_id)
        assert (again["view_mode"], again["pair_order"], again["original_mode"]) \
            == ("original", "zh_left", "page")
        assert again["page_side"] == "en"
        assert (again["aside_open"], again["text_size"], again["show_refs"]) \
            == (False, "lg", True)
        # 非法值不许覆盖已有状态（避免一个手滑的请求把用户的视图改坏）
        reader_service.save_progress(paper_id, {"original_mode": "bogus", "pair_order": "junk",
                                                "text_size": "huge", "zoom": 99,
                                                "aside_open": "yes", "page_side": "xx"})
        same = reader_service.get_progress(paper_id)
        assert same["original_mode"] == "page" and same["pair_order"] == "zh_left"
        assert same["page_side"] == "en"
        assert same["text_size"] == "lg" and same["aside_open"] is False
        assert abs(float(same["zoom"]) - 3.0) < 1e-6        # 超范围被夹到上限，而不是照单全收
    finally:
        reader_service.save_progress(paper_id, {k: before.get(k) for k in keys})
