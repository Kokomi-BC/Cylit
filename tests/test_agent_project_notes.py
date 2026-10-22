"""`project.notes`：一条高亮 + 挂着的笔记 → 用户眼里是**一条**标注，两个字段都要在。

线上实测（TurboQuant，9 条高亮 + 9 条笔记）：`kind="all"` 以前把笔记行与它的高亮行各列一遍，
而高亮行的 `note` 恒为空 —— 带关键词检索时 `list_highlights` 又没有 `q`，于是回来一批与关键词
无关的高亮、命中的笔记被挤掉（用户看到的就是"笔记正文永远拿不到"）。
"""
from __future__ import annotations

import pytest

from backend.services.agent import project

PAPER = "demo-paper"
HL = {"id": "h-1", "paper_id": PAPER, "block_id": "p003-05",
      "text": "向量量化存在重号问题", "side": "zh"}
NOTE = {"id": "n-1", "paper_id": PAPER, "highlight_id": "h-1", "block_id": "p003-05",
        "selected_text": None, "content_md": "笔记正文：重号这里我没看懂",
        "tags": ["公式"]}


@pytest.fixture
def svc(monkeypatch):
    from backend.services.annotation import annotation_service as real

    calls: list[tuple[str, str | None]] = []

    def list_notes(paper_id=None, user_id=1, q=None):
        calls.append(("notes", q))
        rows = [NOTE] if not q or q in (NOTE["content_md"] or "") else []
        return {"items": [dict(r) for r in rows], "count": len(rows)}

    def list_highlights(paper_id=None, user_id=1, q=None):
        calls.append(("highlights", q))
        rows = [HL] if not q or q in HL["text"] else []
        return {"items": [dict(r) for r in rows], "count": len(rows)}

    monkeypatch.setattr(real, "list_notes", list_notes)
    monkeypatch.setattr(real, "list_highlights", list_highlights)
    monkeypatch.setattr(real, "list_annotations", lambda paper_id=None, user_id=1:
                        {"items": [], "count": 0})
    monkeypatch.setattr(project.paper_service, "get_paper",
                        lambda pid: {"id": pid, "page_count": 22})
    return calls


def test_kind_all_lists_the_annotation_once(svc) -> None:
    """同一条标注不要列两遍：笔记行已带 `selected_text`，高亮行就不再出现。"""
    out = project.notes(paper_id=PAPER, kind="all")
    assert out["returned"] == 1, [row["kind"] for row in out["items"]]
    row = out["items"][0]
    assert row["kind"] == "note" and row["note_id"] == "n-1"
    assert row["note"] == "笔记正文：重号这里我没看懂"
    # 笔记自己没存 selected_text → 用它挂的那条高亮的原文补上（用户眼里这就是一条标注）
    assert row["selected_text"] == "向量量化存在重号问题"
    assert row["tags"] == ["公式"]


def test_kind_highlights_carries_the_attached_note(svc) -> None:
    """只看高亮时，挂在上面的笔记正文也要带出来（`note` 空 = 真的没写过笔记）。"""
    out = project.notes(paper_id=PAPER, kind="highlights")
    row = out["items"][0]
    assert row["kind"] == "highlight" and row["note_id"] == "n-1"
    assert row["note"] == "笔记正文：重号这里我没看懂"
    assert row["selected_text"] == "向量量化存在重号问题"


def test_query_is_forwarded_to_both_branches(svc) -> None:
    """关键词必须下传 `list_highlights` —— 以前那一支没有 `q`，查询被静默丢掉。"""
    project.notes(paper_id=PAPER, query="重号")
    assert ("notes", "重号") in svc
    assert ("highlights", "重号") in svc


def test_query_that_misses_everything_returns_nothing(svc) -> None:
    """不匹配就不该有东西回来（而不是回来一批无关高亮）。"""
    out = project.notes(paper_id=PAPER, query="完全不相干的关键词")
    assert out["returned"] == 0 and out["items"] == []
