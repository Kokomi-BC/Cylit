"""笔记正文拿得到：`list_highlights` 收 `q`，`notes_search` 说清"高亮 ≠ 笔记"。

用户的投诉是"笔记正文永远拿不到"。拆开看是两件事：

1. `annotation_service.list_highlights` **没有** `q` —— 库级检索带关键词时，高亮那一支把
   查询静默丢掉，于是 `kind="all"` + `query` 回来 9 条与关键词无关的高亮、把真正命中的
   笔记挤下去（实测：`query="重号"` → 9 条 highlight、0 条 note）。
   这里用"一条高亮 + 挂一条笔记"的真实形状把两侧的过滤钉住。
2. `notes_search` 的回执在"全是高亮、note 全空"时不说人话，模型会以为检索坏了 ——
   现在给一条 hint 说明"这些是高亮，没写过笔记"。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agent.citations import RefRegistry

#: 独立伪论文 id：跑完即删，不碰真实论文的数据
FAKE_PAPER = "__test_notes_search_notes__"


# --------------------------------------------------------------------------
# ① `list_highlights` 的 `q`（需要 MySQL；不可用时自动跳过）
# --------------------------------------------------------------------------


@pytest.fixture
def db_ready():
    from backend.repositories.mysql.session import healthcheck

    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    return True


@pytest.fixture
def one_highlight_one_note(db_ready):
    """造"一条高亮 + 挂在它上面的一条笔记"（外加一条**不该命中**的高亮），跑完清干净。"""
    from backend.services.annotation import annotation_service as svc

    def wipe() -> None:
        for row in svc.list_highlights(FAKE_PAPER)["items"]:
            svc.delete_highlight(row["id"])   # 连带级联删掉它挂的笔记
        for note in svc.list_notes(FAKE_PAPER)["items"]:
            svc.delete_note(note["id"])

    wipe()
    hit = svc.create_highlight({"paper_id": FAKE_PAPER, "block_id": "p008-03",
                                "selected_text": "向量量化存在重号问题",
                                "start_offset": 0, "end_offset": 9})
    miss = svc.create_highlight({"paper_id": FAKE_PAPER, "block_id": "p008-04",
                                 "selected_text": "the baseline is PyTorch einsum",
                                 "start_offset": 0, "end_offset": 20})
    note = svc.create_note({"paper_id": FAKE_PAPER, "block_id": "p008-03",
                            "highlight_id": hit["id"], "selected_text": "向量量化存在重号问题",
                            "content_md": "笔记正文：重号这里我没看懂"})
    try:
        yield {"hit": hit, "miss": miss, "note": note}
    finally:
        wipe()


def test_list_highlights_filters_by_selected_text(one_highlight_one_note) -> None:
    """`q` 命中**选中文本**：只回那一条高亮，而不是整页 9 条一起回来。"""
    from backend.services.annotation import annotation_service as svc

    data = one_highlight_one_note
    items = svc.list_highlights(FAKE_PAPER, q="重号")["items"]
    assert [row["id"] for row in items] == [data["hit"]["id"]]
    assert items[0]["text"] == "向量量化存在重号问题"
    assert svc.list_highlights(FAKE_PAPER, q="重号")["count"] == 1


def test_list_highlights_returns_nothing_when_query_misses(one_highlight_one_note) -> None:
    """**不匹配的高亮不能出现** —— 这才是"高亮挤掉笔记"的根：以前查询被静默忽略。"""
    from backend.services.annotation import annotation_service as svc

    assert svc.list_highlights(FAKE_PAPER, q="完全不相干的关键词")["items"] == []


def test_list_highlights_query_is_case_insensitive_and_optional(one_highlight_one_note) -> None:
    """大小写不敏感（与 `list_notes` 的 `q` 一致）；不传 `q` 仍是全量（老调用方不变）。"""
    from backend.services.annotation import annotation_service as svc

    data = one_highlight_one_note
    assert [row["id"] for row in svc.list_highlights(FAKE_PAPER, q="pytorch")["items"]] \
        == [data["miss"]["id"]]
    assert svc.list_highlights(FAKE_PAPER)["count"] == 2
    assert svc.list_highlights(FAKE_PAPER, q=None)["count"] == 2


def test_the_note_body_lives_on_the_note_not_on_the_highlight(one_highlight_one_note) -> None:
    """笔记正文在 `note` 那一行上（`content_md`）；高亮行**只有被选中的原文**。

    这条把"一条笔记挂在一条高亮上"的数据形状钉住：`project.notes` 要给的
    "高亮行顺带笔记正文"只能靠 `highlight_id` join，不能指望高亮表自己带。
    """
    from backend.services.annotation import annotation_service as svc

    data = one_highlight_one_note
    notes = svc.list_notes(FAKE_PAPER, q="重号")["items"]
    assert [n["id"] for n in notes] == [data["note"]["id"]]
    assert notes[0]["content_md"] == "笔记正文：重号这里我没看懂"
    assert notes[0]["highlight_id"] == data["hit"]["id"], "笔记挂在高亮上（join 的键）"
    # 高亮行没有正文字段：它给的是 selected_text
    assert "content_md" not in svc.list_highlights(FAKE_PAPER, q="重号")["items"][0]


# --------------------------------------------------------------------------
# ② `notes_search` 的回执：note_id + "这些是高亮"的 hint（纯离线）
# --------------------------------------------------------------------------


class _FakeHost:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.calls: list[dict] = []

    async def notes(self, *, query=None, paper_id=None, kind="all"):
        self.calls.append({"query": query, "paper_id": paper_id, "kind": kind})
        return {"items": self.items}


class _Deps:
    def __init__(self, host) -> None:
        self.host = host
        self.paper_id = "p1"
        self.registry = RefRegistry("p1")
        self.query_cache: dict = {}
        self.row_spans: dict = {}

    def count_search(self) -> None:
        return None

    def note_tool(self, *args, **kwargs) -> None:
        return None


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def _highlight_row(**kwargs) -> dict:
    row = {"kind": "highlight", "paper_id": "p1", "paper_title": "某篇",
           "selected_text": "向量量化存在重号问题", "note": "", "block_id": "p008-03", "tags": []}
    row.update(kwargs)
    return row


def _run(items: list[dict], **kwargs):
    from backend.agent import tools as agent_tools

    host = _FakeHost(items)
    ctx = _Ctx(_Deps(host))
    return host, asyncio.run(agent_tools.notes_search(ctx, **kwargs))


def test_hint_says_these_are_highlights_without_notes() -> None:
    """全是高亮、note 全空 → hint 必须说"这些是高亮，没写过笔记"（而不是让模型以为检索坏了）。"""
    _, out = _run([_highlight_row()], query="重号", kind="all")
    assert out["items"][0]["note"] == ""
    assert "这些是高亮，没有附笔记" in (out.get("hint") or "")


def test_no_such_hint_for_kind_notes() -> None:
    """`kind="notes"` 本来就只回笔记正文，别给它扣一顶"这些是高亮"的帽子。"""
    _, out = _run([{"kind": "note", "paper_id": "p1", "selected_text": "向量量化",
                    "note": "", "block_id": "p008-03", "tags": []}], query="向量", kind="notes")
    assert "这些是高亮" not in (out.get("hint") or "")


def test_no_hint_when_a_note_body_is_present() -> None:
    """有正文就不提示（`kind="all"` 里笔记与高亮混排是正常情况）。"""
    _, out = _run([_highlight_row(), {"kind": "note", "paper_id": "p1",
                                      "selected_text": "向量量化", "note": "我的正文",
                                      "block_id": "p008-03", "tags": []}], kind="all")
    assert out.get("hint") in (None, "")


# --------------------------------------------------------------------------
# ③ `list_annotations` 的 `q`（需要 MySQL；不可用时自动跳过）
# --------------------------------------------------------------------------


@pytest.fixture
def two_annotations(db_ready):
    """造两条 AI 批注：一条命中关键词、一条**不该命中**（跑完清干净）。"""
    from backend.repositories.mysql import models
    from backend.repositories.mysql.session import session_scope

    def wipe() -> None:
        with session_scope() as sess:
            (sess.query(models.Annotation)
             .filter(models.Annotation.paper_id == FAKE_PAPER)
             .delete(synchronize_session=False))

    wipe()
    from backend.services.annotation import annotation_service as svc

    hit = svc.create_annotation({"paper_id": FAKE_PAPER, "block_id": "p004-02",
                                 "type": "explanation",
                                 "content_md": "这条批注讲 KL 散度的近似"})
    miss = svc.create_annotation({"paper_id": FAKE_PAPER, "block_id": "p004-03",
                                  "type": "explanation",
                                  "content_md": "这条批注讲位置编码"})
    try:
        yield {"hit": hit, "miss": miss}
    finally:
        wipe()


def test_list_annotations_filters_by_content(two_annotations) -> None:
    """P0-2 回归：`q` 必须过滤批注正文。

    修前 `list_annotations` 没有 `q`，`project.notes(?, query="KL", kind="annotations")`
    回的是**全部批注** —— 模型把无关条目当命中引用（"我关于 KL 的批注"答成别的内容）。
    """
    from backend.services.annotation import annotation_service as svc

    data = two_annotations
    items = svc.list_annotations(FAKE_PAPER, q="KL")["items"]
    assert [row["id"] for row in items] == [data["hit"]["id"]]
    assert svc.list_annotations(FAKE_PAPER, q="KL")["count"] == 1
    # 不传 `q` 仍是全量（老调用方一个字不用改）
    assert svc.list_annotations(FAKE_PAPER)["count"] == 2
    assert svc.list_annotations(FAKE_PAPER, q=None)["count"] == 2
    # 不匹配就不该有东西回来
    assert svc.list_annotations(FAKE_PAPER, q="完全不相干的关键词")["items"] == []


def test_project_notes_forwards_the_query_to_annotations(two_annotations, monkeypatch) -> None:
    """关键词要**下传**到批注那一支：这是 P0-2 的接线点（`project.notes` 的批注分支）。"""
    from backend.services.agent import project

    # 伪论文只在批注表里有行（没有 paper 行）→ 让存在性检查放行，别为此造一篇真论文
    monkeypatch.setattr(project.paper_service, "get_paper",
                        lambda pid, include_deleted=False: {"id": pid, "page_count": 1})

    data = two_annotations
    out = project.notes(paper_id=FAKE_PAPER, query="KL", kind="annotations")
    assert out["returned"] == 1, [row["note"] for row in out["items"]]
    assert data["hit"]["content_md"] in out["items"][0]["note"]
