"""别篇引用浮窗的 ``GET /blocks/{block_id}``：**块 id 与行 id 两种引用都要能解析**。

背景（用户口径）：agent 的 ``page_markdown_get`` 在读**页级转录行**时，引用里带的是
``row_id``（``p002-00``）、``block_id`` 为空；而 ``block_detail`` 以前只按 ``Block.id``
查库 —— 于是"按行引用"的别篇引用一律 404（浮窗读不到），块式引用却正常，
一半能用一半不能用。

这里钉住六件事：

1. 块 id 命中：返回与改造前**逐字段相同**（字段顺序也钉住）；
2. 行 id 命中：``p<页码>-<序号>`` 解析出正确 page，正文/译文就是**阅读器那一行**；
3. 行 id 的页码必须当真：不能"猜"到别的页去（那一页没有这行就是 404）；
4. 两种 id 都不存在 → 仍抛 ``NotFoundError``（**不许**退化成空内容）；
5. ``paper_id`` 不匹配（块存在但属于别篇）→ 仍抛 ``NotFoundError``（归属校验不绕过）；
6. 只认"两种 id"，``fig-p002-00`` 这类不是行 id 的形状**不许**蒙对；
7. 插图 ``figure`` 只在**精确**对上 ``figures_payload`` 的 ``block_id`` 时给出（块分支与行
   回退分支都要带），对不上就是 ``None`` —— 不许按"这一页只有一张图"去猜。

全部离线：``paper_service.paper_paths`` 与 ``row_identity.registry_path`` 都指到
``tmp_path``（绝不碰真实 ``data/``），产物用真实 ``PageStore`` 写；数据库那一层换成
一个只有 ``get`` 的最小假 session。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.errors import NotFoundError
from backend.repositories.mysql.models import Block, Formula
from backend.services.paper import paper_service, reader_service, row_identity
from backend.services.translation.page_translate import PATH_VERSION, PageStore

PAPER = "test-block-detail"
OTHER = "test-block-detail-other"

#: 块的 Markdown 文本（`load_markdown_map` 那一份，测试里换成固定值）
BLOCK_ID = "attentioncle-p01-b01"
BLOCK_MD = {"text": "# 1 Method", "translation": "# 1 方法"}


# --------------------------------------------------------------------------
# 夹具：产物目录落在 tmp_path；数据库换成最小假 session
# --------------------------------------------------------------------------


@pytest.fixture
def paths(monkeypatch, tmp_path):
    """``paper_id -> 假 paper_paths``；每一篇一个独立目录（互不串数据）。"""
    def fake(paper_id: str) -> dict:
        root = tmp_path / str(paper_id)
        md = root / "md"
        md.mkdir(parents=True, exist_ok=True)
        return {
            "root": root, "pdf": root / "source.pdf", "ir": root / "ir.sqlite",
            "pages": root / "pages", "figures": root / "figures",
            "crops": root / "formula_crops", "md": md, "reports": root / "reports",
            "markdown": md / "full.md", "blocks_index": md / "blocks.json",
            "path_a": md / "path_a.json", "path_b": md / "path_b.json",
            "page_products": md / "pages.json",
        }

    monkeypatch.setattr(paper_service, "paper_paths", fake)
    monkeypatch.setattr(
        row_identity, "registry_path",
        lambda paper_id, *, root=None: tmp_path / str(paper_id) / "md" / "row_ids.json")
    return fake


def write(path, pages: dict[int, dict]) -> None:
    """用真实 ``PageStore`` 写产物（磁盘格式与产线一致）。"""
    store = PageStore(PAPER, path, version=PATH_VERSION)
    for page, payload in pages.items():
        store.put(page, payload)


@pytest.fixture
def two_pages(paths):
    """两页、每页两段的真实两路产物（行 id 就是产线那种 ``p001-00``）。"""
    d = paths(PAPER)
    write(d["path_a"], {
        1: {"page": 1, "page_total": 2, "model": "a", "breaks": [],
            "original_md": "Alpha sentence one.\n\nBeta sentence two."},
        2: {"page": 2, "page_total": 2, "model": "a", "breaks": [],
            "original_md": "Gamma sentence three.\n\nDelta sentence four."},
    })
    write(d["path_b"], {
        1: {"page": 1, "page_total": 2, "model": "b", "breaks": [],
            "translation_md": "第一句甲。\n\n第二句乙。"},
        2: {"page": 2, "page_total": 2, "model": "b", "breaks": [],
            "translation_md": "第三句丙。\n\n第四句丁。"},
    })
    return d


class FakeSession:
    """只有 ``get`` 的最小 session（``block_detail`` 用到的那一个方法）。"""

    def __init__(self, blocks=None, formulas=None):
        self.blocks = dict(blocks or {})
        self.formulas = dict(formulas or {})

    def get(self, model, key):
        if model is Block:
            return self.blocks.get(str(key))
        if model is Formula:
            return self.formulas.get(str(key))
        raise AssertionError(f"意外的模型：{model!r}")


class _Ctx:
    def __init__(self, sess):
        self.sess = sess

    def __enter__(self):
        return self.sess

    def __exit__(self, *exc):
        return False


def use_session(monkeypatch, sess: FakeSession) -> FakeSession:
    monkeypatch.setattr(reader_service, "session_scope", lambda: _Ctx(sess))
    return sess


def block(block_id: str, paper_id: str = PAPER, **kw):
    """一个真 ``Block`` 行的替身（只用到 ``block_detail`` 读的那些字段）。"""
    base = dict(id=block_id, paper_id=paper_id, page_no=1, type="heading",
                col=0, order_global=3, x0=10.0, y0=20.0, x1=110.0, y1=40.0,
                preview="block preview", content_hash="hash-1", section_id="s-1")
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------
# a) 块 id 命中：与改造前逐字段相同
# --------------------------------------------------------------------------


def test_block_id_returns_the_unchanged_branch(monkeypatch, paths):
    """块分支一个字都没动：字段、取值、顺序都钉死。"""
    use_session(monkeypatch, FakeSession(blocks={BLOCK_ID: block(BLOCK_ID)}))
    monkeypatch.setattr(reader_service, "load_markdown_map",
                        lambda pid: {BLOCK_ID: dict(BLOCK_MD)})

    got = reader_service.block_detail(PAPER, BLOCK_ID)

    assert got == {
        "id": BLOCK_ID, "paper_id": PAPER, "page": 1, "type": "heading",
        "col": 0, "order": 3, "bbox": [10.0, 20.0, 110.0, 40.0],
        "text": BLOCK_MD["text"], "translation": BLOCK_MD["translation"],
        "content_hash": "hash-1", "section": "s-1", "formula": None,
        # 唯一的增量：末尾多一个 figure（这篇没有插图 -> None）
        "figure": None,
    }
    assert list(got) == ["id", "paper_id", "page", "type", "col", "order", "bbox",
                         "text", "translation", "content_hash", "section", "formula",
                         "figure"]


def test_block_preview_is_the_text_fallback(monkeypatch, paths):
    """Markdown 里没有这一块 → 仍退 ``preview``（与改造前同一兜底）。"""
    use_session(monkeypatch, FakeSession(blocks={BLOCK_ID: block(BLOCK_ID)}))
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    got = reader_service.block_detail(PAPER, BLOCK_ID)
    assert got["text"] == "block preview"
    assert got["translation"] == ""


# --------------------------------------------------------------------------
# b) 行 id 命中：page / 正文 / 译文就是阅读器那一行
# --------------------------------------------------------------------------


def test_row_id_resolves_to_the_reader_row(monkeypatch, two_pages):
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    rows = reader_service.merged_page(PAPER, 2)["rows"]
    row = next(r for r in rows if r["id"] == "p002-01")

    got = reader_service.block_detail(PAPER, "p002-01")

    assert got["id"] == "p002-01"
    assert got["paper_id"] == PAPER
    assert got["page"] == 2
    assert got["type"] == (row.get("type") or "para")
    assert got["text"] == row["original"] == "Delta sentence four."
    assert got["translation"] == row["translation"] == "第四句丁。"
    assert got["formula"] is None
    assert got["section"] == ""


def test_row_id_keeps_positional_row_number(monkeypatch, two_pages):
    """页内序号必须原样对上（``p001-00`` 是这一页第一行，不是第二行）。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    first = reader_service.block_detail(PAPER, "p001-00")
    last = reader_service.block_detail(PAPER, "p001-01")

    assert (first["page"], first["text"]) == (1, "Alpha sentence one.")
    assert (last["page"], last["text"]) == (1, "Beta sentence two.")


# --------------------------------------------------------------------------
# c) 行 id 自带页码：不许被"猜"到别的页去
# --------------------------------------------------------------------------


def test_row_id_is_scoped_to_the_page_in_its_id(monkeypatch, two_pages):
    """页码说第 1 页，就只能看第 1 页：第 2 页的行不许顶上来。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    got = reader_service.block_detail(PAPER, "p001-01")
    assert got["page"] == 1 and got["text"] == "Beta sentence two."

    # 第 2 页没有第 3 行 -> 第 1 页的行绝不能被拿来凑数
    with pytest.raises(NotFoundError):
        reader_service.block_detail(PAPER, "p002-02")
    # 整篇都没有第 9 页
    with pytest.raises(NotFoundError):
        reader_service.block_detail(PAPER, "p009-00")


# --------------------------------------------------------------------------
# d) 两种 id 都不存在 → 仍抛 NotFoundError
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["attentioncle-p99-b99", "p002-99", "p001-00-extra"])
def test_unknown_ids_still_raise_not_found(monkeypatch, two_pages, missing):
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    with pytest.raises(NotFoundError) as exc:
        reader_service.block_detail(PAPER, missing)
    assert missing in str(exc.value)


# --------------------------------------------------------------------------
# e) paper_id 不匹配 → 仍抛 NotFoundError（归属校验不因回退被绕过）
# --------------------------------------------------------------------------


def test_foreign_block_is_still_not_found(monkeypatch, paths):
    """块存在，但属于别篇：不许因为"加了行回退"就把别人的块交出去。"""
    use_session(monkeypatch,
                FakeSession(blocks={BLOCK_ID: block(BLOCK_ID, paper_id=OTHER)}))
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    with pytest.raises(NotFoundError) as exc:
        reader_service.block_detail(PAPER, BLOCK_ID)
    assert BLOCK_ID in str(exc.value)


def test_foreign_block_does_not_leak_through_the_row_fallback(monkeypatch, two_pages):
    """块属于别篇 + 本页恰好有同名行 id：仍按"块不存在"处理（归属优先）。"""
    rid = "p002-00"
    use_session(monkeypatch, FakeSession(blocks={rid: block(rid, paper_id=OTHER)}))
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    with pytest.raises(NotFoundError):
        reader_service.block_detail(PAPER, rid)


# --------------------------------------------------------------------------
# 只认两种 id：不是行 id 的形状不许蒙对
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["fig-p002-00", "row-p002-00", "p2-0", "p002-00-x"])
def test_non_row_id_shapes_are_rejected(monkeypatch, two_pages, shape):
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    with pytest.raises(NotFoundError):
        reader_service.block_detail(PAPER, shape)


# --------------------------------------------------------------------------
# 插图：只有"精确对上 figures_payload 的 block_id"才带 figure（缺了就说缺）
# --------------------------------------------------------------------------

#: figures_payload 的替身（真实形状：image_url 里的 /region/{page}/{index} 与 index 同源）
def figures_stub(*items):
    return lambda paper_id: {"items": [dict(it) for it in items], "count": len(items)}


CAPTION_ITEM = {
    "block_id": "p001-00", "index": 2, "row_id": "p001-00", "page": 1,
    "number": "1", "caption": "Figure 1: The pipeline.",
    "caption_zh": "图 1：流水线。",
    "image_url": f"/api/v1/papers/{PAPER}/figures/region/1/2?card=1&v=deadbeef",
    "card_url": f"/api/v1/papers/{PAPER}/figures/region/1/2?card=1&v=deadbeef",
}


@pytest.fixture(autouse=True)
def fresh_figure_cache():
    """这一层是**进程内**缓存（TTL 30s）：每个用例前后都清掉，别让替身互相串。"""
    reader_service._figure_cache.clear()
    yield
    reader_service._figure_cache.clear()


def test_block_branch_carries_the_figure(monkeypatch, paths):
    """块分支：这块 id 就是 figures 的 block_id -> 原样带出 index / image_url / caption。"""
    bid = "attentioncle-p01-fig01"
    use_session(monkeypatch, FakeSession(blocks={bid: block(bid, type="caption")}))
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})
    item = dict(CAPTION_ITEM, block_id=bid)
    monkeypatch.setattr(reader_service, "figures_payload", figures_stub(item))

    got = reader_service.block_detail(PAPER, bid)

    assert got["figure"] == {
        "index": 2,
        "image_url": item["image_url"],
        "caption": item["caption"],
        "caption_zh": item["caption_zh"],
    }
    # 只加在末尾：原有字段与顺序一个都没动
    assert list(got)[-1] == "figure"
    assert got["formula"] is None and got["type"] == "caption"


def test_paragraph_block_gets_no_figure_even_when_the_page_has_one(monkeypatch, two_pages):
    """同页有且只有一张图，也**不许**塞给普通段落（用户口径：缺了就说缺，不要猜）。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})
    monkeypatch.setattr(reader_service, "figures_payload", figures_stub(CAPTION_ITEM))

    got = reader_service.block_detail(PAPER, "p001-01")     # 第 1 页第 2 行，普通段落

    assert got["text"] == "Beta sentence two."
    assert got["figure"] is None
    assert list(got)[-1] == "figure"          # 字段在，值是 None（不是"字段消失"）


def test_row_fallback_carries_the_figure(monkeypatch, two_pages):
    """行回退那条支（块表里没有这个 id）也必须带 figure：图注行的行 id 就是插图锚点。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})
    monkeypatch.setattr(reader_service, "figures_payload", figures_stub(CAPTION_ITEM))

    got = reader_service.block_detail(PAPER, "p001-00")

    assert (got["id"], got["page"]) == ("p001-00", 1)
    assert got["figure"] == {
        "index": 2,
        "image_url": CAPTION_ITEM["image_url"],
        "caption": CAPTION_ITEM["caption"],
        "caption_zh": CAPTION_ITEM["caption_zh"],
    }
    assert list(got)[-1] == "figure"


def test_missing_or_broken_figures_yield_none_and_never_raise(monkeypatch, two_pages):
    """对不上就 None，而且插图层出任何问题都不许把块详情弄成 500。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    # ① 这篇一张图都没有
    monkeypatch.setattr(reader_service, "figures_payload", figures_stub())
    got = reader_service.block_detail(PAPER, "p001-00")
    assert got["figure"] is None and got["text"] == "Alpha sentence one."

    # ② 有图，但锚在另一个 id 上（fig-p001-00 是"图号没被结构化"时的兜底 id）-> 不许按页猜
    reader_service._figure_cache.clear()
    monkeypatch.setattr(reader_service, "figures_payload",
                        figures_stub(dict(CAPTION_ITEM, block_id="fig-p001-00")))
    assert reader_service.block_detail(PAPER, "p001-00")["figure"] is None

    # ③ figures_payload 自己炸了（产物坏 / 正在重写）-> 只丢这一个字段
    reader_service._figure_cache.clear()

    def boom(paper_id):
        raise RuntimeError("产物坏了")

    monkeypatch.setattr(reader_service, "figures_payload", boom)
    got = reader_service.block_detail(PAPER, "p001-00")
    assert got["figure"] is None and got["text"] == "Alpha sentence one."


def test_figure_index_is_computed_once_and_dropped_by_invalidate_cache(monkeypatch, two_pages):
    """性能：同一篇连开多个块详情只算一次 figures；invalidate_cache 之后重算。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})
    calls: list[str] = []

    def counting(paper_id):
        calls.append(paper_id)
        return {"items": [dict(CAPTION_ITEM)], "count": 1}

    monkeypatch.setattr(reader_service, "figures_payload", counting)

    reader_service.block_detail(PAPER, "p001-00")
    reader_service.block_detail(PAPER, "p001-01")
    reader_service.block_detail(PAPER, "p002-00")
    assert calls == [PAPER]

    reader_service.invalidate_cache(PAPER)
    reader_service.block_detail(PAPER, "p001-00")
    assert calls == [PAPER, PAPER]
