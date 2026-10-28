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
6. 只认"两种 id"，``fig-p002-00`` 这类不是行 id 的形状**不许**蒙对。

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
    }
    assert list(got) == ["id", "paper_id", "page", "type", "col", "order", "bbox",
                         "text", "translation", "content_hash", "section", "formula"]


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
