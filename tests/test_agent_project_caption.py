"""图注给模型看的长度上限（纯离线，不碰真库）。

为什么单独一条：图注已改成"读时拼接"，拼出来的完整图注实测最长 713 字符；`project.figures`
以前按 400 截断，把 14/138 条截在**半句**中间（用户看到的是"图 3：…其中"这种断尾）。
现在上限 800 —— 既让 713 这种长度整句保留，也仍然给超长图注留一道闸。
"""

from __future__ import annotations

import pytest

from backend.services.agent import project
from backend.services.paper import figure_regions


@pytest.fixture
def fake_media(monkeypatch):
    """把 `media_of_page` 与取论文都换掉：只测拼接/裁剪这一层。"""
    def install(captions: list[str]) -> None:
        monkeypatch.setattr(
            figure_regions, "media_of_page",
            lambda paper_id, page, figures_only=True: [
                {"kind": "figure", "number": f"Figure {i + 1}", "caption": text,
                 "url": f"/api/v1/papers/{paper_id}/figures/region/{page}/{i}?v=abc",
                 "card_url": "", "part": None, "parts": None}
                for i, text in enumerate(captions)])
        monkeypatch.setattr(project.paper_service, "get_paper",
                            lambda paper_id: {"id": paper_id, "page_count": 12})
    return install


def test_caption_under_the_limit_is_not_clipped(fake_media) -> None:
    """400~800 之间的图注必须**原样**给出去（以前 401 就会被截）。"""
    fake_media(["图" * 400, "注" * 713])
    captions = [item["caption"] for item in project.figures("p1", 3)["items"]]
    assert [len(text) for text in captions] == [400, 713]
    assert captions[1] == "注" * 713


def test_overlong_caption_is_still_capped_at_800(fake_media) -> None:
    fake_media(["超" * 1200])
    caption = project.figures("p1", 3)["items"][0]["caption"]
    assert len(caption) == project.CAPTION_CHARS == 800
    assert caption.endswith("…"), "超长仍然截断，且要有截断标记"
