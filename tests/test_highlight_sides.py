"""高亮**原文 / 译文分开存**的回归测试（需要 MySQL；不可用时自动跳过）。

背景：一行对照里两侧共用同一个 `block_id`，只存偏移量的话，原文上标的那段会按同样的偏移量
画到译文上（中英文字数完全对不齐）。所以 `highlight` 表加了 `side`（en/zh）与
`scope`（row/markdown）两列，这里锁住：默认值、校验、读写往返、以及"老数据补列"的迁移。

测试用**独立的伪论文 id**，跑完即删，不碰真实论文的数据。
"""

from __future__ import annotations

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e

FAKE_PAPER = "__test_highlight_side__"


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()          # 顺带跑一遍补列（`ensure_columns`），缺列时这里就会失败
    return True


@pytest.fixture(autouse=True)
def clean_rows(db_ready):
    """跑前跑后都清掉本文件造的伪数据。"""
    from backend.services.annotation import annotation_service as svc

    def wipe() -> None:
        for row in svc.list_highlights(FAKE_PAPER)["items"]:
            svc.delete_highlight(row["id"])

    wipe()
    yield
    wipe()


def test_side_and_scope_round_trip() -> None:
    from backend.services.annotation import annotation_service as svc

    created = svc.create_highlight({
        "paper_id": FAKE_PAPER, "block_id": "md:p3", "selected_text": "向量量化",
        "start_offset": 5, "end_offset": 9, "color": "green",
        "side": "zh", "scope": "markdown",
    })
    assert created["side"] == "zh" and created["scope"] == "markdown"
    assert created["start"] == 5 and created["end"] == 9 and created["color"] == "green"

    items = svc.list_highlights(FAKE_PAPER)["items"]
    assert len(items) == 1
    assert items[0]["side"] == "zh" and items[0]["scope"] == "markdown"
    assert items[0]["block_id"] == "md:p3"


def test_defaults_are_en_and_row() -> None:
    """老前端（不带 side/scope）创建的记录必须落到 en/row —— 与补列时的默认值一致。"""
    from backend.services.annotation import annotation_service as svc

    created = svc.create_highlight({
        "paper_id": FAKE_PAPER, "block_id": "p001-03", "selected_text": "attention",
        "start_offset": 0, "end_offset": 9, "color": "yellow",
    })
    assert created["side"] == "en" and created["scope"] == "row"


def test_invalid_side_and_scope_fall_back() -> None:
    from backend.services.annotation import annotation_service as svc

    created = svc.create_highlight({
        "paper_id": FAKE_PAPER, "block_id": "p001-03", "selected_text": "x",
        "side": "jp", "scope": "pdf",
    })
    assert created["side"] == "en" and created["scope"] == "row"


def test_side_column_exists() -> None:
    """补列迁移的结果：`highlight` 表真的有三列（列宽/默认值按模型定义）。"""
    from sqlalchemy import inspect

    from backend.repositories.mysql.session import get_engine

    cols = {c["name"]: c for c in inspect(get_engine()).get_columns("highlight")}
    for name in ("side", "scope", "group_id"):
        assert name in cols, f"highlight.{name} 缺列"


def test_group_delete_removes_whole_cross_row_highlight() -> None:
    """跨行高亮 = 同组多条记录：删任意一条，**整组一起删**（用户眼里那是一条高亮）。"""
    from backend.services.annotation import annotation_service as svc

    group = "g-test-cross-row"
    for row_id in ("p001-00", "p001-01", "p001-02"):
        svc.create_highlight({
            "paper_id": FAKE_PAPER, "block_id": row_id, "selected_text": "跨行的一段",
            "start_offset": 0, "end_offset": 4, "color": "blue",
            "side": "zh", "scope": "row", "group_id": group,
        })
    items = svc.list_highlights(FAKE_PAPER)["items"]
    assert len(items) == 3
    assert {r["group_id"] for r in items} == {group}

    out = svc.delete_highlight(items[0]["id"])
    assert out["count"] == 3 and len(out["deleted_ids"]) == 3
    assert svc.list_highlights(FAKE_PAPER)["count"] == 0


def test_delete_single_highlight_keeps_others() -> None:
    """没有分组的普通高亮：删一条只影响它自己。"""
    from backend.services.annotation import annotation_service as svc

    a = svc.create_highlight({"paper_id": FAKE_PAPER, "block_id": "p002-00",
                              "selected_text": "x", "color": "yellow"})
    svc.create_highlight({"paper_id": FAKE_PAPER, "block_id": "p002-01",
                          "selected_text": "y", "color": "green"})
    out = svc.delete_highlight(a["id"])
    assert out["count"] == 1
    assert svc.list_highlights(FAKE_PAPER)["count"] == 1
