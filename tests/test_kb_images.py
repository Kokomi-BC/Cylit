"""知识条目的图片与「展开全文」（**全部离线**）。

用户口径：**图片也是知识库的一类内容** —— 检索命中插图/表格/公式要能出缩略图，
浏览"插图"要能当图库翻，点 [原文] 回阅读器。

这里只测纯逻辑：
1. 插图/表格 → 「按图注锚定的裁图」（与阅读器显示的是同一张），按 **同页 + 同类 + 图号** 匹配；
2. 匹配不到 → 退到解析阶段落盘的裁图（`data/papers/` 静态挂载）；
3. 公式 → 解析阶段的公式裁图；
4. 正文条目**没有图**（返回 None，不塞错图）；
5. `chunk_detail`（展开全文）只读那一条向量行，不下发对侧语种。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.errors import NotFoundError, ValidationError
from backend.services.knowledge import knowledge_service
from backend.services.knowledge.knowledge_service import (
    _decorate_items, chunk_detail, chunk_image_url, public_item,
)

PAPER = "demo-paper"


@pytest.fixture()
def fake_settings(tmp_path: Path, monkeypatch):
    """把 `papers_dir` 指到临时目录（真实 data/papers 一行不动）。"""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    monkeypatch.setattr(knowledge_service, "get_settings",
                        lambda: SimpleNamespace(papers_dir=papers_dir,
                                                api_prefix="/api/v1"))
    return papers_dir


def _crop(papers_dir: Path, rel: str) -> str:
    path = papers_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")
    return str(path)


# --------------------------------------------------------------------------
# 图片 URL
# --------------------------------------------------------------------------


def test_text_chunk_has_no_image(fake_settings) -> None:
    assert chunk_image_url(PAPER, "text", 3, {"block_ids": ["p03-b01"]}) is None


def test_formula_chunk_uses_parse_stage_crop(fake_settings) -> None:
    crop = _crop(fake_settings, f"{PAPER}/formula_crops/p03-b07@4x.png")
    url = chunk_image_url(PAPER, "formula", 3, {"crop": crop, "latex": "x"})
    assert url == f"/papers/{PAPER}/formula_crops/p03-b07@4x.png"


def test_crop_outside_papers_dir_is_refused(fake_settings, tmp_path) -> None:
    """裁图路径不在 data/papers 下 → 不给 URL（静态挂载也取不到，宁可不显示）。"""
    stray = tmp_path / "elsewhere" / "p.png"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"png")
    assert chunk_image_url(PAPER, "formula", 1, {"crop": str(stray)}) is None


def test_missing_crop_file_is_refused(fake_settings) -> None:
    assert chunk_image_url(PAPER, "formula", 1, {"crop": "no/such/file.png"}) is None


def test_figure_matches_anchored_region_by_number(fake_settings, monkeypatch) -> None:
    """同页同类同图号 → 用 figure_regions 那张（与阅读器一致）。"""
    regions = [
        {"kind": "figure", "number": "1", "caption": "Figure 1: overview"},
        {"kind": "table", "number": "1", "caption": "Table 1: results"},
        {"kind": "figure", "number": "2", "caption": "Figure 2: error vs bits"},
    ]
    monkeypatch.setattr("backend.services.paper.figure_regions.page_regions",
                        lambda pid, page: regions)
    assert chunk_image_url(PAPER, "figure", 4, {"caption": "Figure 2: error vs bits"}) == \
        f"/api/v1/papers/{PAPER}/figures/region/4/2"
    # 表格走同一页的 table 区域（kind 必须对上，不能拿到同号的图）
    assert chunk_image_url(PAPER, "table", 4, {"caption": "Table 1: results"}) == \
        f"/api/v1/papers/{PAPER}/figures/region/4/1"


def test_figure_without_number_matches_caption_head(fake_settings, monkeypatch) -> None:
    regions = [{"kind": "figure", "number": "", "caption": "A schematic of the pipeline"}]
    monkeypatch.setattr("backend.services.paper.figure_regions.page_regions",
                        lambda pid, page: regions)
    assert chunk_image_url(PAPER, "figure", 2, {"caption": "A schematic of the pipeline"}) == \
        f"/api/v1/papers/{PAPER}/figures/region/2/0"


def test_figure_falls_back_to_parse_crop_when_unmatched(fake_settings, monkeypatch) -> None:
    """图号对不上（或该页没有区域）→ 退到解析阶段的裁图，而不是给一张错的。"""
    monkeypatch.setattr("backend.services.paper.figure_regions.page_regions",
                        lambda pid, page: [{"kind": "figure", "number": "9",
                                            "caption": "Figure 9: something else"}])
    crop = _crop(fake_settings, f"{PAPER}/figures/fig-p05-01.png")
    assert chunk_image_url(PAPER, "figure", 5, {"caption": "Figure 3: ours",
                                                "image_path": crop}) == \
        f"/papers/{PAPER}/figures/fig-p05-01.png"
    # 连退路都没有 → None
    assert chunk_image_url(PAPER, "figure", 5, {"caption": "Figure 3: ours"}) is None


def test_region_lookup_failure_does_not_break_search(fake_settings, monkeypatch) -> None:
    """区域计算抛异常（缺 IR / 读盘失败）时只丢图，不能让整条检索失败。"""
    def boom(pid, page):
        raise RuntimeError("IR 缺失")

    monkeypatch.setattr("backend.services.paper.figure_regions.page_regions", boom)
    assert chunk_image_url(PAPER, "figure", 1, {"caption": "Figure 1: x"}) is None


def test_decorated_items_expose_image_url(fake_settings, monkeypatch) -> None:
    """检索结果条目要带 `image_url`（知识库页靠它出缩略图），且能穿过 public_item 的白名单。"""
    monkeypatch.setattr(knowledge_service, "_paper_titles", lambda ids: {PAPER: "示例论文"})
    monkeypatch.setattr("backend.services.paper.figure_regions.page_regions",
                        lambda pid, page: [{"kind": "figure", "number": "1",
                                            "caption": "Figure 1: overview"}])
    items = _decorate_items([
        {"chunk_id": "c1", "paper_id": PAPER, "chunk_type": "figure", "page_no": 6,
         "block_id": "p06-b02", "section": "3 METHOD", "lang": "en",
         "content": "插图 p06-b02：Figure 1: overview", "rrf": 0.03,
         "metadata": {"caption": "Figure 1: overview"}},
        {"chunk_id": "c2", "paper_id": PAPER, "chunk_type": "text", "page_no": 6,
         "content": "正文", "metadata": {}},
    ])
    assert items[0]["image_url"] == f"/api/v1/papers/{PAPER}/figures/region/6/0"
    assert items[1]["image_url"] is None
    assert public_item(items[0])["image_url"] == items[0]["image_url"]


# --------------------------------------------------------------------------
# 展开全文（/knowledge/chunk）
# --------------------------------------------------------------------------


def _row(**over) -> dict:
    row = {"chunk_id": f"{PAPER}:text:p03-b01:en", "paper_id": PAPER,
           "chunk_type": "text", "page_no": 3, "block_id": "p03-b01",
           "section": "2 METHOD", "lang": "en",
           "content": "这是一段足够长的正文内容，用来验证展开全文拿到的是完整正文。" * 12,
           "metadata": {"lang": "en", "pair_id": f"{PAPER}:text:p03-b01"}}
    row.update(over)
    return row


@pytest.fixture()
def fake_store(monkeypatch):
    from backend.repositories.vector import lancedb_store as store_mod

    monkeypatch.setattr(knowledge_service, "_chunk_row_meta",
                        lambda cid: {"tokens": 42, "index_version": 7})
    monkeypatch.setattr(knowledge_service, "_paper_titles",
                        lambda ids: {PAPER: "示例论文"})

    def install(rows: list[dict]):
        monkeypatch.setattr(store_mod.LanceDBStore, "get_rows",
                            lambda self, ids: [r for r in rows if r["chunk_id"] in ids])

    return install


def test_chunk_detail_returns_full_text(fake_store) -> None:
    fake_store([_row()])
    data = chunk_detail(f"{PAPER}:text:p03-b01:en")
    assert len(data["content"]) > 200 and data["chars"] == len(data["content"])
    assert data["preview"].endswith("…")            # preview 仍然是截断的那份
    assert data["paper_title"] == "示例论文"
    assert data["block_id"] == "p03-b01" and data["page"] == 3
    assert data["tokens"] == 42 and data["index_version"] == 7
    assert data["pair_id"] == f"{PAPER}:text:p03-b01"
    assert data["image_url"] is None


def test_chunk_detail_adds_image_for_figure(fake_settings, fake_store, monkeypatch) -> None:
    monkeypatch.setattr("backend.services.paper.figure_regions.page_regions",
                        lambda pid, page: [{"kind": "figure", "number": "2",
                                            "caption": "Figure 2: x"}])
    fake_store([_row(chunk_id=f"{PAPER}:figure:p07-b03:en", chunk_type="figure",
                     page_no=7, content="插图 p07-b03：Figure 2: x",
                     metadata={"lang": "en", "pair_id": f"{PAPER}:figure:p07-b03",
                               "caption": "Figure 2: x"})])
    data = chunk_detail(f"{PAPER}:figure:p07-b03:en")
    assert data["image_url"] == f"/api/v1/papers/{PAPER}/figures/region/7/0"


def test_chunk_detail_rejects_unknown_and_empty(fake_store) -> None:
    fake_store([])
    with pytest.raises(NotFoundError):
        chunk_detail(f"{PAPER}:text:missing:en")
    with pytest.raises(ValidationError):
        chunk_detail("   ")
