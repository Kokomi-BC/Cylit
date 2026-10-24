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
        # 笔记也扫一遍：删高亮会级联（见下面的用例），但早先留下的孤儿笔记要能清掉
        for note in svc.list_notes(FAKE_PAPER)["items"]:
            svc.delete_note(note["id"])

    wipe()
    yield
    wipe()


def test_deleting_a_highlight_takes_its_notes_away() -> None:
    """**笔记挂在高亮上**：删高亮要把挂在它上面的笔记一起删。

    不级联的话留下的是"没有原文可看的一条笔记"，而前端 `deleteHighlightIds` 是按
    "后端会级联"写的（本地把 `highlight_id` 已删的笔记过滤掉）—— 两边不一致的结果就是
    库里攒出一批谁也看不见的孤儿笔记，隔一阵冒出来就是用户说的"莫名其妙的笔记"。
    """
    from backend.services.annotation import annotation_service as svc

    hl = svc.create_highlight({"paper_id": FAKE_PAPER, "block_id": "p003-02",
                               "selected_text": "先有高亮", "start_offset": 0, "end_offset": 5})
    note = svc.create_note({"paper_id": FAKE_PAPER, "block_id": "p003-02",
                            "highlight_id": hl["id"], "selected_text": "先有高亮",
                            "content_md": "这条笔记挂在那处高亮上"})
    assert [n["id"] for n in svc.list_notes(FAKE_PAPER)["items"]] == [note["id"]]
    # 没挂笔记的那条：删掉时 deleted_notes 是空的，不该报错
    other = svc.create_highlight({"paper_id": FAKE_PAPER, "block_id": "p003-03",
                                  "selected_text": "另一处", "start_offset": 0, "end_offset": 3})
    assert svc.delete_highlight(other["id"])["deleted_notes"] == []
    out = svc.delete_highlight(hl["id"])
    assert out["count"] == 1 and out["deleted_notes"] == [note["id"]]
    assert svc.list_notes(FAKE_PAPER)["items"] == [], "高亮删了，挂在它上面的笔记也要走"
    # 幂等：再删一次不报错、也不再报有笔记
    again = svc.delete_highlight(hl["id"])
    assert again["missing"] is True and again["deleted_notes"] == []


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
    """脏值一律落到 en/row；但 `pdf` **不是**脏值（《修改计划》第三步起它是一面）。"""
    from backend.services.annotation import annotation_service as svc

    # 偏移量要给**真区间**：0/0 现在一律按文字重新定位，定位不到就 400（见
    # `test_highlight_span_localize`）—— 伪论文没有页产物可查，给真区间才存得下。
    created = svc.create_highlight({
        "paper_id": FAKE_PAPER, "block_id": "p001-03", "selected_text": "x",
        "start_offset": 0, "end_offset": 1,
        "side": "jp", "scope": "weird",
    })
    assert created["side"] == "en" and created["scope"] == "row"

    pdf = svc.create_highlight({
        "paper_id": FAKE_PAPER, "block_id": "pdf:p1", "selected_text": "Attention Is All You Need",
        "start_offset": 0, "end_offset": 37, "side": "en", "scope": "pdf",
    })
    assert pdf["scope"] == "pdf" and pdf["block_id"] == "pdf:p1"
    svc.delete_highlight(pdf["id"])


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
                              "selected_text": "x", "start_offset": 0, "end_offset": 1,
                              "color": "yellow"})
    svc.create_highlight({"paper_id": FAKE_PAPER, "block_id": "p002-01",
                          "selected_text": "y", "start_offset": 0, "end_offset": 1,
                          "color": "green"})
    out = svc.delete_highlight(a["id"])
    assert out["count"] == 1
    assert svc.list_highlights(FAKE_PAPER)["count"] == 1


def test_delete_highlight_is_idempotent() -> None:
    """重复删同一条**不该报错**：跨行高亮是"整组删"，前端（或多标签页同开一篇）天然会重复请求。

    以前再删一次会 404，日志里看着像删除失败（用户报过）。现在返回空列表 + `missing` 标记。
    """
    from backend.services.annotation import annotation_service as svc

    a = svc.create_highlight({"paper_id": FAKE_PAPER, "block_id": "p003-00",
                              "selected_text": "x", "start_offset": 0, "end_offset": 1,
                              "color": "yellow"})
    assert svc.delete_highlight(a["id"])["count"] == 1
    again = svc.delete_highlight(a["id"])
    assert again["count"] == 0
    assert again["deleted_ids"] == []
    assert again["missing"] is True
