r"""检索片段：窗口要围着**命中处**，块尾命中要夹紧到块尾而不是退回块首。

线上实测 2026-12-07：搜 `QR decomposition` / `i.i.d Normal entries`，命中的是第 5 页那段**结尾**
的句子，可下发片段是块首「## 1.1 MSE OPTIMAL TURBOQUANT Let $x \in …$」—— 400 字符的窗口从块首
起算，命中处不在窗口里，回执看着像"没搜到"。
"""
from __future__ import annotations

from backend.services.agent import project

LONG = r"## 1.1 MSE OPTIMAL TURBOQUANT Let $x \in \mathbb{S}^{d-1}$ be a worst-case vector. " * 6
TAIL = r"We can generate $\boldsymbol{\Pi}$ by applying QR decomposition on a random matrix."


def test_clip_around_keeps_the_hit_near_the_block_end() -> None:
    body = LONG + TAIL
    out = project.clip_around(body, "QR decomposition")
    assert "QR decomposition" in out
    assert out.startswith("…"), "前面被截掉了要有省略号"
    assert len(out) <= project.SNIPPET_CHARS


def test_clip_around_falls_back_to_the_head_when_the_hit_is_absent() -> None:
    out = project.clip_around(LONG, "完全不相干的词")
    assert out.startswith("## 1.1 MSE OPTIMAL TURBOQUANT")


def test_search_returns_content_centered_on_the_query(monkeypatch) -> None:
    """投影层下发的内容必须是**围着命中处**的那一段（工具层再夹一次也无害）。"""
    row = {"chunk_id": "c1", "paper_id": "p1", "page_no": 5, "section": "1.1",
           "chunk_type": "para", "lang": "en", "block_id": "p005-01",
           "content": LONG + TAIL, "metadata": {}, "rerank_score": 0.9}
    monkeypatch.setattr(project.retrieval, "search",
                        lambda *a, **k: {"items": [row], "retrieval": {}})
    monkeypatch.setattr(project.paper_service, "get_paper",
                        lambda pid: {"id": pid, "page_count": 22})
    monkeypatch.setattr(project, "_paired_zh_previews", lambda items: {})
    out = project.search("p1", "QR decomposition")
    content = out["items"][0]["content"]
    assert "QR decomposition" in content
    assert len(content) <= project.SNIPPET_CHARS

