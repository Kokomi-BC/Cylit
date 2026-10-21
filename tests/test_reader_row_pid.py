"""正文行的稳定身份 pid 接线（阶段 1b：**只加字段，行为零变化**）。

`row_identity`（注册表 + 继承规则）在阶段 1a 就已经写完并有单测，但当时**没有接线**。
这一层把它挂到阅读器读模型的**唯一挂钩点** `reader_service._finalize_page` 上，并钉住五件事：

1. **只多一个键**：每行都有 pid、页内唯一，而 `id` / `index` / 正文 / 顺序一个字没动
   （拿"把 assign 关掉"的那一版当基线逐字段比）；
2. **幂等**：同一页连着读两次，pid 相同且第二次**不写盘**（注册表字节不变）；
3. **整篇一次**：`page_products` 只读一次注册表、至多写一次（不是逐页 flush）；
4. **两个出口一致**：`page_products`（整篇批量）与 `merged_page`（单页 / SSE）给同一页的
   pid 必须相同 —— 否则"实时长出来的行"和"刷新后拿到的行"身份对不上，引用会断；
5. **空页不动注册表**；页内插一行时其余行继承原 pid。

全部离线：`paper_service.paper_paths` 与 `row_identity.registry_path` 都指到 tmp_path
（**绝不往真实 data/ 写**），产物用真实 `PageStore` 写。
"""
from __future__ import annotations

import pytest

from backend.services.paper import paper_service, reader_service, row_identity
from backend.services.translation.page_translate import PATH_VERSION, PageStore

PAPER = "test-row-pid"


# --------------------------------------------------------------------------
# 夹具：产物目录与 pid 注册表都落在 tmp_path
# --------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    """`(paper_paths 假字典, 注册表路径)`；注册表**不落真实 data/**。"""
    md = tmp_path / "md"
    md.mkdir(parents=True, exist_ok=True)
    fake = {
        "root": tmp_path, "pdf": tmp_path / "source.pdf", "ir": tmp_path / "ir.sqlite",
        "pages": tmp_path / "pages", "figures": tmp_path / "figures",
        "crops": tmp_path / "formula_crops", "md": md, "reports": tmp_path / "reports",
        "markdown": md / "full.md", "blocks_index": md / "blocks.json",
        "path_a": md / "path_a.json", "path_b": md / "path_b.json",
        "page_products": md / "pages.json",
    }
    monkeypatch.setattr(paper_service, "paper_paths", lambda pid: fake)
    reg = md / "row_ids.json"
    monkeypatch.setattr(row_identity, "registry_path",
                        lambda paper_id, *, root=None: reg)
    return fake, reg


@pytest.fixture
def tally(monkeypatch):
    """数 `row_identity.save` / `load` 的调用次数（包装，不改行为）。"""
    counts = {"save": 0, "load": 0}
    real_save, real_load = row_identity.save, row_identity.load

    def counting_save(*args, **kwargs):
        counts["save"] += 1
        return real_save(*args, **kwargs)

    def counting_load(*args, **kwargs):
        counts["load"] += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(row_identity, "save", counting_save)
    monkeypatch.setattr(row_identity, "load", counting_load)
    return counts


def write(path, pages: dict[int, dict]) -> None:
    """用真实 `PageStore` 写产物（磁盘格式与产线一致）。"""
    store = PageStore(PAPER, path, version=PATH_VERSION)
    for page, payload in pages.items():
        store.put(page, payload)


def page_of(products: list[dict], page: int) -> dict:
    return next(p for p in products if int(p["page"]) == page)


def pids_by_row(page: dict) -> dict[str, str]:
    return {str(r["id"]): str(r["pid"]) for r in page["rows"]}


def _rows(*texts: str) -> list[dict]:
    return [{"id": f"p001-{i:02d}", "original": t, "translation": ""}
            for i, t in enumerate(texts)]


CARRIED = "tlenecks between HBM and SRAM."


def write_carry_paper(paths) -> None:
    """两页、页尾半句被上一页收完（`_rejoin_carried_rows` 会真的动行）。"""
    write(paths["path_a"], {
        1: {"page": 1, "page_total": 2, "model": "a", "breaks": [],
            "original_md": "# 1 Method\n\nWe end with a bot-"},
        2: {"page": 2, "page_total": 2, "model": "a", "breaks": [],
            "original_md": f"{CARRIED}\n\nBy compressing weights we cut latency."},
    })
    write(paths["path_b"], {
        1: {"page": 1, "page_total": 2, "model": "b", "breaks": [],
            "translation_md": "# 1 方法\n\n我们以通信瓶", "carry_md": CARRIED},
        2: {"page": 2, "page_total": 2, "model": "b", "breaks": [],
            "translation_md": "通过压缩权重我们降低了延迟。", "carry_md": ""},
    })


# --------------------------------------------------------------------------
# ① 只加字段：pid 每行都有、页内唯一，其余一个字没动
# --------------------------------------------------------------------------


def test_finalize_page_only_adds_pid(env, monkeypatch) -> None:
    paths, _reg = env
    write(paths["path_a"], {2: {
        "page": 2, "page_total": 3, "breaks": [], "model": "a",
        "original_md": "# 2 Results\n\nWe report three numbers.\n\nThe gain is real.",
    }})

    # 基线 = "把 pid 那一层关掉"的同一份读模型（阶段 1b 承诺：只多一个键）
    real_assign = row_identity.assign
    monkeypatch.setattr(row_identity, "assign", lambda *a, **kw: [])
    baseline = reader_service.merged_page(PAPER, 2)
    monkeypatch.setattr(row_identity, "assign", real_assign)

    page = reader_service.merged_page(PAPER, 2)
    rows = page["rows"]
    assert len(rows) == 3
    pids = [str(r["pid"]) for r in rows]
    assert all(p.startswith("r-") for p in pids)
    assert len(set(pids)) == len(pids), "页内 pid 必须唯一"
    assert [r["id"] for r in rows] == ["p002-00", "p002-01", "p002-02"]
    stripped = [{k: v for k, v in r.items() if k != "pid"} for r in rows]
    assert stripped == baseline["rows"], "除了多一个 pid 键，整页逐字段必须与基线一致"


# --------------------------------------------------------------------------
# ② 幂等：同页连读两次 → pid 相同，第二次不写盘
# --------------------------------------------------------------------------


def test_same_page_twice_keeps_pids_and_writes_once(env, tally) -> None:
    paths, reg = env
    write(paths["path_a"], {3: {
        "page": 3, "page_total": 3, "breaks": [], "model": "a",
        "original_md": "Alpha sentence.\n\nBeta sentence.",
    }})

    first = reader_service.merged_page(PAPER, 3)
    assert tally["save"] == 1
    written = reg.read_bytes()

    second = reader_service.merged_page(PAPER, 3)
    assert [r["pid"] for r in second["rows"]] == [r["pid"] for r in first["rows"]]
    assert tally["save"] == 1, "同一批行第二次不该再写注册表"
    assert reg.read_bytes() == written


# --------------------------------------------------------------------------
# ③ 整篇一次：page_products 只读一次、至多写一次
# --------------------------------------------------------------------------


def test_page_products_reads_once_and_writes_once(env, tally) -> None:
    paths, _reg = env
    write(paths["path_a"], {p: {
        "page": p, "page_total": 4, "breaks": [], "model": "a",
        "original_md": f"Page {p} first sentence.\n\nPage {p} second sentence.",
    } for p in (1, 2, 3)})

    products, shape = reader_service.page_products(PAPER)
    assert shape == "mixed"
    assert tally["load"] == 1, "整篇批量入口只该读一次注册表"
    assert tally["save"] == 1, "整篇批量入口至多写一次"
    pids = [str(r["pid"]) for page in products for r in page["rows"]]
    assert len(pids) == 6
    assert len(set(pids)) == 6, "跨页也不该撞 pid"


# --------------------------------------------------------------------------
# ④ 两个出口一致：page_products 的 pid == merged_page 的 pid（两种顺序）
# --------------------------------------------------------------------------


def test_pids_agree_when_single_page_runs_first(env) -> None:
    paths, _reg = env
    write_carry_paper(paths)

    single = reader_service.merged_page(PAPER, 2)
    products, _shape = reader_service.page_products(PAPER)

    assert pids_by_row(page_of(products, 2)) == pids_by_row(single)
    assert page_of(products, 2) == single, "整页逐字段一致（pid 也一致）"


def test_pids_agree_when_whole_paper_runs_first(env) -> None:
    paths, _reg = env
    write_carry_paper(paths)

    products, _shape = reader_service.page_products(PAPER)
    single = reader_service.merged_page(PAPER, 2)

    assert pids_by_row(single) == pids_by_row(page_of(products, 2))
    # 第 2 页的第 1 块已被第 1 页收走，两页都只剩自己那几行（pid 对得上才算接线成功）
    assert [r["original"] for r in page_of(products, 1)["rows"]][-1].endswith(CARRIED)
    assert len(page_of(products, 2)["rows"]) == 1


# --------------------------------------------------------------------------
# ⑤ 空页不碰注册表
# --------------------------------------------------------------------------


def test_empty_page_does_not_touch_registry(env, tally) -> None:
    _paths, reg = env
    page = {"page": 7, "rows": []}

    assert reader_service._finalize_page(PAPER, page) is page
    assert page["rows"] == []
    assert tally["load"] == 0 and tally["save"] == 0, "空页连读都不该读"
    assert not reg.exists()
    assert row_identity.assign(PAPER, 7, []) == []
    assert row_identity.assign_pages(PAPER, {7: [], 8: []}) == {7: [], 8: []}
    assert row_identity.assign_pages(PAPER, {}) == {}
    assert tally["save"] == 0 and not reg.exists()


def test_page_with_no_rows_gets_no_pid_and_no_registry(env, tally) -> None:
    paths, reg = env
    write(paths["path_a"], {1: {"page": 1, "page_total": 1, "model": "a",
                                "breaks": [], "original_md": "   "}})

    products, _shape = reader_service.page_products(PAPER)
    assert products[0]["rows"] == []
    assert "pid" not in (products[0].get("rows") or [{}])[0]
    assert tally["save"] == 0 and not reg.exists()


# --------------------------------------------------------------------------
# ⑥ 页内插一行：其余行继承原 pid，只有新行是新的
# --------------------------------------------------------------------------


def test_insert_row_keeps_other_pids(env) -> None:
    paths, _reg = env
    write(paths["path_a"], {1: {
        "page": 1, "page_total": 1, "breaks": [], "model": "a",
        "original_md": "# 1 Method\n\nAlpha sentence one.\n\nBeta sentence two.",
    }})
    before = reader_service.merged_page(PAPER, 1)
    was = {(r["original"]): r["pid"] for r in before["rows"]}

    write(paths["path_a"], {1: {
        "page": 1, "page_total": 1, "breaks": [], "model": "a",
        "original_md": ("# 1 Method\n\nAlpha sentence one.\n\n"
                        "Gamma paragraph inserted in the middle here.\n\n"
                        "Beta sentence two."),
    }})
    after = reader_service.merged_page(PAPER, 1)
    now = {r["original"]: r["pid"] for r in after["rows"]}

    assert len(after["rows"]) == 4
    for text, pid in was.items():
        assert now[text] == pid, f"插入一行不该改已有行的 pid：{text!r}"
    inserted = "Gamma paragraph inserted in the middle here."
    assert now[inserted] not in set(was.values()), "新行必须是新 pid"
    assert len(set(now.values())) == 4


# --------------------------------------------------------------------------
# ⑦ 批量入口与逐页入口结果相同（同一份注册表）
# --------------------------------------------------------------------------


def _seed_copy(tmp_path, seed_root, name: str):
    src = row_identity.registry_path("p1", root=seed_root)
    dst = row_identity.registry_path("p1", root=tmp_path / name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return dst


def _norm(reg: dict) -> dict:
    return {"rows": [(e.get("page"), e.get("order"), e.get("pid"), e.get("fp"))
                     for e in reg["rows"]],
            "retired": [(e.get("pid"), e.get("page"), e.get("reason"))
                        for e in reg["retired"]]}


def test_assign_pages_matches_per_page_assign(tmp_path) -> None:
    original = {1: _rows("alpha sentence", "beta sentence"),
                2: _rows("gamma sentence"),
                3: _rows("delta sentence", "epsilon sentence")}
    row_identity.assign_pages("p1", original, root=tmp_path / "seed")
    _seed_copy(tmp_path, tmp_path / "seed", "batch")
    _seed_copy(tmp_path, tmp_path / "seed", "single")

    # 只改字、不换段落：全部行都应**继承** pid（这样两次运行的 pid 可以严格比较）
    edited = {1: _rows("alpha sentence", "beta sentence!"),
              2: _rows("gamma sentence?"),
              3: _rows("delta sentence", "epsilon sentence.")}

    batch = row_identity.assign_pages("p1", edited, root=tmp_path / "batch")
    single = {p: row_identity.assign("p1", p, rows, root=tmp_path / "single")
              for p, rows in edited.items()}

    assert batch == single
    assert _norm(row_identity.load("p1", root=tmp_path / "batch")) == \
        _norm(row_identity.load("p1", root=tmp_path / "single"))


def test_assign_pages_second_call_is_a_noop(tmp_path, monkeypatch) -> None:
    pages = {1: _rows("alpha sentence", "beta sentence"), 2: _rows("gamma sentence")}
    first = row_identity.assign_pages("p1", pages, root=tmp_path)
    path = row_identity.registry_path("p1", root=tmp_path)
    written = path.read_bytes()

    writes = []
    real_save = row_identity.save
    monkeypatch.setattr(row_identity, "save",
                        lambda *a, **kw: (writes.append(1), real_save(*a, **kw))[1])

    again = row_identity.assign_pages("p1", pages, root=tmp_path)
    assert again == first
    assert writes == [], "同一批行第二次不该写盘"
    assert path.read_bytes() == written

# --------------------------------------------------------------------------
# 失败必须**开放**：身份这层不许让"打开论文"失败
# --------------------------------------------------------------------------


def test_registry_write_failure_does_not_break_reads(env, monkeypatch) -> None:
    """磁盘满 / 只读挂载：注册表写不进去，pid 照旧返回（本次不落盘，日志有痕迹）。"""
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(row_identity, "save", boom)
    pids = row_identity.assign(PAPER, 1, [{"id": "p001-00", "original": "a b c",
                                           "translation": ""}])
    assert len(pids) == 1 and pids[0].startswith("r-")


def test_finalize_page_swallows_identity_errors(env, monkeypatch) -> None:
    """分配身份本身炸了：页面照旧返回（只是这一轮没有 pid），绝不 500。"""
    page = {"page": 2, "rows": [{"id": "p002-00", "original": "x", "translation": ""}]}

    def boom(*args, **kwargs):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(row_identity, "assign", boom)
    out = reader_service._finalize_page(PAPER, page)
    assert out is page and "pid" not in out["rows"][0]


def test_page_products_survives_batch_identity_error(env, monkeypatch) -> None:
    """整篇批量分配炸了：page_products 仍旧返回页面（不抛），行上没有 pid。"""
    def boom(*args, **kwargs):
        raise RuntimeError("batch exploded")

    monkeypatch.setattr(row_identity, "assign_pages", boom)
    out = reader_service._finalize_page(PAPER, {"page": 3, "rows": [
        {"id": "p003-00", "original": "y", "translation": ""}]}, pids=[])
    assert "pid" not in out["rows"][0]

