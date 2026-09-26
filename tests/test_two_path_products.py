"""两路页产物的**合并读模型**（`reader_service.page_products`）与旧格式兼容。

## 为什么这些测试必须存在

页级翻译原本是**同一次调用**产出原文 + 译文 + 对齐好的行，产物一个文件（`md/pages.json`）。
改成两路并发之后：

1. 两路各写**一个文件**（`path_a.json` / `path_b.json`）—— 硬约束，因为
   `PageStore.flush()` 是整文件原子重写，两个并发写者共用一个文件会互相覆盖；
2. 两侧的对齐从"构造性成立"变成"靠 `pair_rows` 结构对齐"；
3. 存量论文的 `pages.json` 不能被作废 —— 所以读模型要能从它**合成** A/B 两路视图。

这三条里的任何一条错了，表现都是"阅读器突然少了一半内容"或者"行错位"，因此这里逐条钉：

- `legacy`：只有旧产物 → 与改造前的读时重算**逐行一致**，锚点归属 A 路、B 路为空；
- `paths`：两个路文件 → 结构对齐成行，单侧行被如实计数（不强求两侧块数相等）；
- `mixed`：只跑完一路（过渡态）→ 缺的那一侧从旧产物补，页面仍完整可读；
- 缺一侧时仍然出行（不能因为"少一半"就整页消失）；
- 两路锚点合成一次校验：英文锚点落原文侧、中文锚点落译文侧，**互不干扰**；
- 旧形态保留**行 id 保护判据**（块数变了也不动已有行 id，否则高亮会指到别的文字）。

全部离线：`paper_service.paper_paths` 被指到 `tmp_path`，产物用真实 `PageStore` 写。
"""
from __future__ import annotations

import pytest

from backend.services.paper import paper_service, reader_service
from backend.services.translation.page_translate import (
    PATH_VERSION, PAGES_VERSION, PageStore,
)

PAPER = "test-two-path"


@pytest.fixture
def paths(monkeypatch, tmp_path):
    """把 `paper_paths` 指到临时目录（产物布局与真实一致）。"""
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
    return fake


def write(path, pages: dict[int, dict], *, version: int) -> None:
    """用真实 `PageStore` 写产物（保证磁盘格式与产线一致）。"""
    store = PageStore(PAPER, path, version=version)
    for page, payload in pages.items():
        store.put(page, payload)


def page_of(products: list[dict], page: int) -> dict:
    return next(p for p in products if p["page"] == page)


# --------------------------------------------------------------------------
# 旧格式：零迁移必须逐行等价
# --------------------------------------------------------------------------


def test_legacy_only_paper_reads_as_legacy(paths) -> None:
    write(paths["page_products"], {1: {
        "page": 1, "page_total": 1,
        "original_md": "# 1 Method\n\nWe define the estimator.\n\nThe bound holds.",
        "translation_md": "# 1 方法\n\n我们定义这个估计量。\n\n这个界成立。",
        "rows": [{"id": "p001-00", "index": 0, "type": "heading", "level": 1,
                  "number": "1", "original": "# 1 Method", "translation": "# 1 方法"},
                 {"id": "p001-01", "index": 1, "type": "para", "level": 0, "number": "",
                  "original": "We define the estimator.",
                  "translation": "我们定义这个估计量。"},
                 {"id": "p001-02", "index": 2, "type": "para", "level": 0, "number": "",
                  "original": "The bound holds.", "translation": "这个界成立。"}],
        # 锚点必须落在"前一行真的收句了"的正文块上：标题的下一行不许切（`after_heading`）
        "breaks": ["The bound holds."],
        "align": "exact", "model": "m", "updated_at": "2026-01-01T00:00:00",
    }}, version=PAGES_VERSION)

    products, shape = reader_service.page_products(PAPER)
    assert shape == "legacy"
    assert [p["page"] for p in products] == [1]
    page = products[0]
    assert [r["id"] for r in page["rows"]] == ["p001-00", "p001-01", "p001-02"]
    assert page["align"] == "exact"
    # 旧格式的锚点全是英文的（同一次调用产出、只给原文锚点）→ 归 A 路，B 路为空。
    # 注意：这里是 v1 纯字符串数组（存量产物的真实形状）→ 读出来被归一成 `{"start", "type": ""}`。
    assert page["breaks_a"] == [{"start": "The bound holds.", "type": ""}]
    assert page["breaks_b"] == []
    assert page["breaks"] == page["breaks_a"]
    assert page["break_stats"]["accepted"] == 1
    assert page["rows"][2]["semantic_break"] is True
    assert page["sides"] == {"a": True, "b": True}


def test_legacy_keeps_stored_rows_when_counts_differ(paths) -> None:
    """行 id 保护判据：重算出的行数与存下来的不一致时，**用存下来的行**。

    行 id 是位置编号（`p{页}-{序号}`）；块数一变，已有的高亮 / 笔记就会指到别的文字上。
    """
    stored = [{"id": f"p001-{i:02d}", "index": i, "type": "para", "level": 0,
               "number": "", "original": f"stored {i}", "translation": f"存 {i}"}
              for i in range(5)]
    write(paths["page_products"], {1: {
        "page": 1, "page_total": 1,
        # 这两份 markdown 只配得出 1 行 —— 与存下来的 5 行不一致
        "original_md": "only one paragraph",
        "translation_md": "只有一段",
        "rows": stored, "breaks": [], "align": "exact", "model": "m",
    }}, version=PAGES_VERSION)

    products, shape = reader_service.page_products(PAPER)
    assert shape == "legacy"
    assert [r["id"] for r in products[0]["rows"]] == [f"p001-{i:02d}" for i in range(5)]
    assert products[0]["rows"][3]["original"] == "stored 3"


def test_legacy_structure_pages_take_recomputed_rows(paths) -> None:
    """`align="structure"` 的页**必须**用重算的行（老产物是按正文文本签名配的，会整体错位）。"""
    stored = [{"id": "p001-00", "index": 0, "type": "para", "level": 0, "number": "",
               "original": "错位的行", "translation": ""}]
    write(paths["page_products"], {1: {
        "page": 1, "page_total": 1,
        "original_md": "# 1 Method\n\nWe define it.",
        "translation_md": "# 1 方法\n\n我们定义它。",
        "rows": stored, "breaks": [], "align": "structure", "model": "m",
    }}, version=PAGES_VERSION)

    products, _ = reader_service.page_products(PAPER)
    rows = products[0]["rows"]
    assert [r["type"] for r in rows] == ["heading", "para"]
    assert rows[1]["translation"] == "我们定义它。"


# --------------------------------------------------------------------------
# 两路形态
# --------------------------------------------------------------------------


def test_two_path_files_merge_into_rows(paths) -> None:
    """两侧块数不同 → 结构对齐成行，再由 `absorb_one_sided_rows` 把**单侧行并进上一行**。

    为什么要吸收：两路各自分段，块数差 1~2 段时 `pair_rows` 会留下"只有一侧有内容"的行。
    实测这些差异绝大多数是"一侧把相邻两段并成了一块"，并进上一行的缺失侧之后两侧重新覆盖同一段
    文字（否则阅读器会出现"这一行英文栏是空的"）。
    """
    write(paths["path_a"], {1: {
        "page": 1, "page_total": 1,
        "original_md": "# 1 Method\n\nWe define the estimator.\n\nThe bound holds.",
        "breaks": ["We define the estimator."], "model": "a",
    }}, version=PATH_VERSION)
    write(paths["path_b"], {1: {
        "page": 1, "page_total": 1,
        "translation_md": "# 1 方法\n\n我们定义这个估计量。",
        "carry_md": "", "breaks": [], "model": "b",
    }}, version=PATH_VERSION)

    products, shape = reader_service.page_products(PAPER)
    assert shape == "paths"
    page = products[0]
    assert page["source"] == "paths"
    # A 侧 3 块 / B 侧 2 块 → 配成 3 行，最后一行的原文并进上一行 → 2 行、**没有空栏**
    assert len(page["rows"]) == 2
    assert page["one_sided"] == 0
    assert page["align"] == "structure"
    joined = page["rows"][-1]["original"]
    assert "We define the estimator." in joined and "The bound holds." in joined
    assert page["rows"][-1]["translation"] == "我们定义这个估计量。"
    assert page["sides"] == {"a": True, "b": True}


def test_missing_side_is_filled_from_legacy(paths) -> None:
    """过渡态：只跑完 A 路，但旧产物还在 → 译文从旧产物补，页面完整可读。"""
    write(paths["path_a"], {1: {
        "page": 1, "page_total": 1,
        "original_md": "# 1 Method\n\nWe define the estimator.",
        "breaks": [], "model": "a",
    }}, version=PATH_VERSION)
    write(paths["page_products"], {1: {
        "page": 1, "page_total": 1,
        "original_md": "# 1 Method\n\nWe define the estimator.",
        "translation_md": "# 1 方法\n\n我们定义这个估计量。",
        "rows": [], "breaks": [], "align": "exact", "model": "legacy",
    }}, version=PAGES_VERSION)

    products, shape = reader_service.page_products(PAPER)
    assert shape == "mixed"
    assert products[0]["translation_md"] == "# 1 方法\n\n我们定义这个估计量。"
    assert products[0]["rows"][1]["translation"] == "我们定义这个估计量。"
    assert products[0]["sides"] == {"a": True, "b": True}


def test_page_with_only_one_side_still_produces_rows(paths) -> None:
    """没有任何旧产物、只跑完 A 路：仍然要出行（只是译文全空），不能整页消失。

    只有一侧有内容时**不做吸收**（吸收的目标是"并进上一行的缺失侧"，而这里每行都缺同一侧）；
    页首的单侧行同理保留。
    """
    write(paths["path_a"], {1: {
        "page": 1, "page_total": 1,
        "original_md": "# 1 Method\n\nWe define the estimator.",
        "breaks": [], "model": "a",
    }}, version=PATH_VERSION)

    products, shape = reader_service.page_products(PAPER)
    assert shape == "mixed"
    page = products[0]
    assert page["sides"] == {"a": True, "b": False}
    assert [r["original"] for r in page["rows"]] == ["# 1 Method", "We define the estimator."]
    assert all(r["translation"] == "" for r in page["rows"])
    assert page["one_sided"] == len(page["rows"])


def test_no_products_at_all(paths) -> None:
    assert reader_service.page_products(PAPER) == ([], "none")


def test_merged_page_matches_page_products(paths) -> None:
    """`merged_page`（逐页 SSE 用）必须与 `page_products` 的同一页**逐字段一致**。

    SSE 的 `rows` 就是它 —— 不一致的话，"实时长出来的页"和"刷新后看到的页"会不一样。
    """
    write(paths["path_a"], {1: {
        "page": 1, "page_total": 2,
        "original_md": "# 1 Method\n\nWe define the estimator.\n\nThe bound holds.",
        "breaks": [{"start": "The bound holds.", "type": "method"}], "model": "a",
    }}, version=PATH_VERSION)
    write(paths["path_b"], {1: {
        "page": 1, "page_total": 2,
        "translation_md": "# 1 方法\n\n我们定义这个估计量。\n\n这个界成立。",
        "carry_md": "", "breaks": [], "model": "b",
    }}, version=PATH_VERSION)

    products, _ = reader_service.page_products(PAPER)
    assert reader_service.merged_page(PAPER, 1) == products[0]
    assert reader_service.merged_page(PAPER, 9) is None, "不存在的页要返回 None（别造空页）"


# --------------------------------------------------------------------------
# 两路锚点
# --------------------------------------------------------------------------


def test_anchors_from_both_paths_land_on_their_own_side(paths) -> None:
    """A 路给英文锚点、B 路给中文锚点：各自落在自己那一侧的行上，**同一行不重复计数**。

    两条锚点都指向第 2 行（一条落原文侧、一条落译文侧）→ `accepted` 只有 1、
    另一条记进 `duplicate`。这正是"两路标记合成一次校验"该有的行为。
    """
    write(paths["path_a"], {1: {
        "page": 1, "page_total": 1,
        "original_md": "# 1 Method\n\nWe define the estimator.\n\nThe bound holds.",
        "breaks": ["The bound holds."], "model": "a",
    }}, version=PATH_VERSION)
    write(paths["path_b"], {1: {
        "page": 1, "page_total": 1,
        # 中文锚点规范化后要 ≥12 个字符，否则只允许落标题行（`short_not_heading`）
        "translation_md": "# 1 方法\n\n我们定义这个估计量。\n\n这个界成立而且它不依赖维度。",
        "carry_md": "", "breaks": ["这个界成立而且它不依赖维度"], "model": "b",
    }}, version=PATH_VERSION)

    products, _ = reader_service.page_products(PAPER)
    page = products[0]
    assert [r["index"] for r in page["rows"] if r.get("semantic_break")] == [2]
    stats = page["break_stats"]
    assert stats["by_side"] == {"original": 1, "translation": 1}
    assert stats["accepted"] == 1
    assert stats["duplicate"] == 1


def test_anchors_on_different_rows_are_unioned(paths) -> None:
    """两路锚点落在**不同行**时，求并集：两行都被标上边界。"""
    write(paths["path_a"], {1: {
        "page": 1, "page_total": 1,
        "original_md": ("# 1 Method\n\nAlpha sentence.\n\nBeta sentence.\n\n"
                        "Gamma sentence."),
        "breaks": ["Gamma sentence."], "model": "a",
    }}, version=PATH_VERSION)
    write(paths["path_b"], {1: {
        "page": 1, "page_total": 1,
        "translation_md": "# 1 方法\n\n甲句。\n\n乙句，它足够长以便通过锚点长度下限。\n\n丙句。",
        "carry_md": "", "breaks": ["乙句，它足够长以便通过锚点长度下限"], "model": "b",
    }}, version=PATH_VERSION)

    products, _ = reader_service.page_products(PAPER)
    page = products[0]
    assert len(page["rows"]) == 4
    assert [r["index"] for r in page["rows"] if r.get("semantic_break")] == [2, 3]
    assert page["break_stats"]["by_side"] == {"original": 1, "translation": 1}
    assert page["break_stats"]["accepted"] == 2


def test_v2_anchor_types_reach_rows_from_both_paths(paths) -> None:
    """v2 锚点（对象 + `type`）：两路的类型标签都落到各自那一侧的行上。"""
    write(paths["path_a"], {1: {
        "page": 1, "page_total": 1,
        "original_md": "# 1 Method\n\nAlpha sentence.\n\nBeta sentence.",
        "breaks": [{"start": "Beta sentence.", "type": "method"}], "model": "a",
    }}, version=PATH_VERSION)
    write(paths["path_b"], {1: {
        "page": 1, "page_total": 1,
        "translation_md": "# 1 方法\n\n甲句。\n\n乙句，它足够长以便通过锚点长度下限。",
        "carry_md": "",
        "breaks": [{"start": "乙句，它足够长以便通过锚点长度下限", "type": "definition"}],
        "model": "b",
    }}, version=PATH_VERSION)

    products, _ = reader_service.page_products(PAPER)
    page = products[0]
    # 两条锚点都指向 idx 2（一条落原文侧、一条落译文侧）→ 先到的那条胜出，另一条记 duplicate
    assert page["breaks_a"] == [{"start": "Beta sentence.", "type": "method"}]
    assert page["breaks_b"] == [{"start": "乙句，它足够长以便通过锚点长度下限",
                                 "type": "definition"}]
    assert page["rows"][2]["semantic_type"] == "method"
    assert page["break_stats"]["by_type"] == {"method": 1}
    assert page["break_stats"]["typed"] == 1
    assert page["break_stats"]["duplicate"] == 1


# --------------------------------------------------------------------------
# 进度口径：B 路才是"翻译进度"
# --------------------------------------------------------------------------


def test_translation_stats_counts_the_b_path(paths) -> None:
    write(paths["path_a"], {1: {"page": 1, "page_total": 3,
                                "original_md": "only english", "breaks": []}},
          version=PATH_VERSION)
    write(paths["path_b"], {2: {"page": 2, "page_total": 3,
                                "translation_md": "只有中文", "breaks": []}},
          version=PATH_VERSION)
    stats = paper_service.translation_stats(PAPER)
    assert stats["done"] == 1, "A 路那一页不算翻译完成"
    assert stats["total"] == 3


def test_translation_stats_falls_back_to_legacy(paths) -> None:
    write(paths["page_products"], {1: {"page": 1, "page_total": 2,
                                       "original_md": "en", "translation_md": "中",
                                       "rows": [], "breaks": []}},
          version=PAGES_VERSION)
    assert paper_service.translation_stats(PAPER)["done"] == 1
