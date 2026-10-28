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
        # 增量只在末尾：figure（这篇没有插图 -> None）、table（这篇没有表 -> None）
        "figure": None,
        "table": None,
    }
    assert list(got) == ["id", "paper_id", "page", "type", "col", "order", "bbox",
                         "text", "translation", "content_hash", "section", "formula",
                         "figure", "table"]


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
    reader_service._table_cache.clear()
    yield
    reader_service._figure_cache.clear()
    reader_service._table_cache.clear()


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
    assert list(got)[-1] == "table"          # 只加在末尾（figure 之后）
    assert got["formula"] is None and got["type"] == "caption"


def test_paragraph_block_gets_no_figure_even_when_the_page_has_one(monkeypatch, two_pages):
    """同页有且只有一张图，也**不许**塞给普通段落（用户口径：缺了就说缺，不要猜）。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})
    monkeypatch.setattr(reader_service, "figures_payload", figures_stub(CAPTION_ITEM))

    got = reader_service.block_detail(PAPER, "p001-01")     # 第 1 页第 2 行，普通段落

    assert got["text"] == "Beta sentence two."
    assert got["figure"] is None
    assert list(got)[-2:] == ["figure", "table"]   # 字段在，值是 None（不是"字段消失"）


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
    assert list(got)[-1] == "table"          # 只加在末尾（figure 之后）


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

# --------------------------------------------------------------------------
# 表格：**表注行与表体行在页产物里是两行**（实测 attention 第 6 页：p006-00 表注、p006-01 表体），
# agent 引用表格时给的是表注那一行 —— 块详情必须把表带上，否则浮窗里只剩一句「表 1：…」。
# 口径与 figure 一致：**同页 + 表号相同且唯一**才给，否则 None（不按页猜）。
# --------------------------------------------------------------------------

#: 一张 2 列表的 Markdown（与产线 markdown_tables 里那一份同形）
TABLE_MD = "| 层类型 | 每层复杂度 |\n|---|---|\n| 自注意力 | $O(n^2 d)$ |"


def tables_stub(*items) -> None:
    return lambda paper_id: {"items": [dict(it) for it in items], "count": len(items)}


def table_item(**kw) -> dict:
    base = dict(id="t001-0000", page=1, number="1", markdown=TABLE_MD, rows=1, cols=2,
                caption="表 1：不同层类型的最大路径长度。",
                caption_original="Table 1: Maximum path lengths.", source="translation",
                row_id="p001-01")
    base.update(kw)
    return base


@pytest.fixture
def caption_page(paths):
    """第 1、2 页各放一句**表注**（`Table 1: …` / `表 2：…`）：用来验「同页 + 表号」这条判据。"""
    d = paths(PAPER)
    write(d["path_a"], {
        1: {"page": 1, "page_total": 2, "model": "a", "breaks": [],
            "original_md": "Table 1: Maximum path lengths."},
        2: {"page": 2, "page_total": 2, "model": "a", "breaks": [],
            "original_md": "Something on page two."},
    })
    write(d["path_b"], {
        1: {"page": 1, "page_total": 2, "model": "b", "breaks": [],
            "translation_md": "表 1：最大路径长度。"},
        2: {"page": 2, "page_total": 2, "model": "b", "breaks": [],
            "translation_md": "第二页的一句话。"},
    })
    return d


def test_caption_row_carries_the_table(monkeypatch, caption_page):
    """表注行 -> 带上同页那张 1 号表的 Markdown（这就是「只有表注」的修复点）。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})
    monkeypatch.setattr(reader_service, "markdown_tables",
                        tables_stub(table_item()))

    got = reader_service.block_detail(PAPER, "p001-00")

    assert got["type"] == "caption" or got["text"].startswith("Table 1")
    assert got["table"] == {
        "id": "t001-0000", "page": 1, "number": "1",
        # 表体所在行：浮窗「定位到原文」要闪**这一行**，不是表注那行（用户 2026-12-28）
        "row_id": "p001-01", "row_ids": None,
        "caption": "表 1：不同层类型的最大路径长度。",
        "caption_original": "Table 1: Maximum path lengths.",
        "markdown": TABLE_MD, "rows": 1, "cols": 2, "source": "translation",
    }
    assert got["figure"] is None


def test_table_lookup_is_scoped_to_the_page(monkeypatch, caption_page):
    """同一个表号出现在**另一页**：不许跨页配上来（缺了就说缺）。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})
    monkeypatch.setattr(reader_service, "markdown_tables", tables_stub(table_item(page=2)))

    assert reader_service.block_detail(PAPER, "p001-00")["table"] is None


def test_two_tables_with_the_same_number_on_one_page_yield_none(monkeypatch, caption_page):
    """同页两张同号表（跨页拆表那种）：候选不唯一 -> 两个都不给，绝不猜。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})
    monkeypatch.setattr(reader_service, "markdown_tables",
                        tables_stub(table_item(), table_item(id="t001-0001", row_id="p001-02")))

    assert reader_service.block_detail(PAPER, "p001-00")["table"] is None


def test_block_branch_matches_a_table_whose_caption_lives_inside_the_block(monkeypatch, paths):
    """解析把表注写在 table 块里时（attention 那篇就是这样）：块分支也要认出来。"""
    bid = "attentioncle-p01-b01"
    use_session(monkeypatch, FakeSession(blocks={bid: block(bid, type="table")}))
    monkeypatch.setattr(reader_service, "load_markdown_map",
                        lambda pid: {bid: {"text": "Table 1: Maximum path lengths.", "translation": "表 1：最大路径长度。"}})
    monkeypatch.setattr(reader_service, "markdown_tables", tables_stub(table_item()))

    got = reader_service.block_detail(PAPER, bid)
    assert got["table"]["markdown"] == TABLE_MD
    assert list(got)[-1] == "table"


def test_broken_tables_payload_only_drops_the_field(monkeypatch, caption_page):
    """表格清单自己炸了 -> 只丢 table，块详情照旧（绝不 500）。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})

    def boom(paper_id):
        raise RuntimeError("产物坏了")

    monkeypatch.setattr(reader_service, "markdown_tables", boom)
    got = reader_service.block_detail(PAPER, "p001-00")
    assert got["table"] is None and got["text"] == "Table 1: Maximum path lengths."


def test_table_lookup_is_computed_once_and_dropped_by_invalidate_cache(monkeypatch, caption_page):
    """性能：同一篇连开多个块详情只算一次表格；invalidate_cache 之后重算。"""
    use_session(monkeypatch, FakeSession())
    monkeypatch.setattr(reader_service, "load_markdown_map", lambda pid: {})
    calls: list[str] = []

    def counting(paper_id):
        calls.append(paper_id)
        return {"items": [table_item()], "count": 1}

    monkeypatch.setattr(reader_service, "markdown_tables", counting)
    reader_service.block_detail(PAPER, "p001-00")
    reader_service.block_detail(PAPER, "p001-00")
    assert calls == [PAPER]

    reader_service.invalidate_cache(PAPER)
    reader_service.block_detail(PAPER, "p001-00")
    assert calls == [PAPER, PAPER]

# --------------------------------------------------------------------------
# 跨页拆表：同一张表被页边界切成两半（两页各有一份「表头 + 若干行」）时，
# 表格清单要并回**一张**——表头取第一页，行按顺序接上（用户口径 2026-12-28）。
# --------------------------------------------------------------------------


@pytest.fixture
def split_pages(paths) -> dict:
    """第 1、2 页各半张 1 号表（列数相同），第 3 页是另一张 2 号表。"""
    d = paths(PAPER)
    half1 = "| 方法 | KV |\n|---|---|\n| Full | 16 |\n| KIVI | 3 |"
    half2 = "| 方法 | KV |\n|---|---|\n| Polar | 3.9 |\n| Ours | 2.5 |"
    other = "| 指标 | 值 |\n|---|---|\n| BLEU | 28.4 |"
    write(d["path_a"], {
        1: {"page": 1, "page_total": 3, "model": "a", "breaks": [],
            "original_md": half1 + "\n\nTable 1: LongBench results."},
        2: {"page": 2, "page_total": 3, "model": "a", "breaks": [], "original_md": half2},
        3: {"page": 3, "page_total": 3, "model": "a", "breaks": [],
            "original_md": other + "\n\nTable 2: BLEU."},
    })
    write(d["path_b"], {
        1: {"page": 1, "page_total": 3, "model": "b", "breaks": [], "translation_md": ""},
        2: {"page": 2, "page_total": 3, "model": "b", "breaks": [], "translation_md": ""},
        3: {"page": 3, "page_total": 3, "model": "b", "breaks": [], "translation_md": ""},
    })
    return d


def test_split_table_across_pages_is_merged(monkeypatch, split_pages):
    """同号 + 相邻页 + 列数相同 -> 并成一张，行数相加，pages 记两页。"""
    got = reader_service.markdown_tables(PAPER)
    merged = [it for it in got["items"] if it.get("pages") == [1, 2]]

    assert got["count"] == 2                 # 三张半表 -> 一张合并 + 第 3 页那张
    assert len(merged) == 1
    item = merged[0]
    assert item["row_id"] == "p001-00" and item["page"] == 1
    assert item["rows"] == 4                 # 2 + 2 行
    assert item["cols"] == 2
    md = item["markdown"]
    assert md.count("| 方法 | KV |") == 1    # 表头只留一份
    assert "Ours" in md and "KIVI" in md    # 两页的行都在
    assert item["row_ids"] == ["p001-00", "p002-00"]   # 两半各自的行 id（正文要用）


def test_tables_with_different_numbers_are_not_merged(monkeypatch, split_pages):
    """号不同 / 页不相邻一律不并：第 3 页那张 2 号表必须单独在。"""
    got = reader_service.markdown_tables(PAPER)
    alone = [it for it in got["items"] if str(it.get("number")) == "2"]
    assert len(alone) == 1 and alone[0]["page"] == 3
    assert "pages" not in alone[0]

def test_same_number_but_different_header_is_not_merged(monkeypatch, paths):
    """同号但表头不同（列名不一样）-> 是两张表，不许并。"""
    d = paths(PAPER)
    a = "| 方法 | KV |\n|---|---|\n| Full | 16 |"
    b = "| 数据集 | 值 |\n|---|---|\n| GloVe | 0.9 |"
    write(d["path_a"], {
        1: {"page": 1, "page_total": 2, "model": "a", "breaks": [],
            "original_md": a + "\n\nTable 1: first."},
        2: {"page": 2, "page_total": 2, "model": "a", "breaks": [],
            "original_md": b + "\n\nTable 1: second."},
    })
    write(d["path_b"], {1: {"page": 1, "page_total": 2, "model": "b", "breaks": [], "translation_md": ""},
                        2: {"page": 2, "page_total": 2, "model": "b", "breaks": [], "translation_md": ""}})

    got = reader_service.markdown_tables(PAPER)
    assert got["count"] == 2
    assert all("pages" not in it for it in got["items"])


def test_uncaptioned_table_on_the_next_page_is_not_merged(monkeypatch, paths):
    """第一页那张**没有表号**（认不出是哪张表）-> 一律不并。"""
    d = paths(PAPER)
    a = "| 方法 | KV |\n|---|---|\n| Full | 16 |"
    b = "| 方法 | KV |\n|---|---|\n| Polar | 3.9 |"
    write(d["path_a"], {
        1: {"page": 1, "page_total": 2, "model": "a", "breaks": [], "original_md": a},
        2: {"page": 2, "page_total": 2, "model": "a", "breaks": [], "original_md": b},
    })
    write(d["path_b"], {1: {"page": 1, "page_total": 2, "model": "b", "breaks": [], "translation_md": ""},
                        2: {"page": 2, "page_total": 2, "model": "b", "breaks": [], "translation_md": ""}})

    got = reader_service.markdown_tables(PAPER)
    assert got["count"] == 2 and all("pages" not in it for it in got["items"])
