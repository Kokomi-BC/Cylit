"""页级产物纠错（`page_edit`）——**离线**：`paper_paths` 指到 `tmp_path`，产物用真实 `PageStore`。

为什么这些断言必须存在：这是整个工具面里**唯一会改用户数据**的能力，而它改的东西正是
阅读器正文、双语导出与检索索引的共同上游。所以每条拒绝路径都要有测试：

- 字面替换必须**唯一命中**（找不到 → 给"最像的几行"；命中多处 → 要求带上前后文）；
- **行结构不许变**：行 id 是位置编号，行数一变，用户已有的高亮 / 笔记与历史引用整体错位
  → 检测到就**回滚**（磁盘上必须逐字节回到改动前）；
- 改完能**原样撤回**（改前那一份整页文本在日志里）；
- 存量论文（只有 `md/pages.json`）改在**原文件**里，不顺手把它拆成两路；
- 论文上有活着的任务时拒绝（那些任务也在写同一个文件）；
- 工具层的"先读后写"与改动预算。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.agent.deps import Deps
from backend.agent.tools import page_markdown_edit
from backend.services.paper import paper_service
from backend.services.translation import page_edit
from backend.services.translation import page_translate
from backend.services.translation.page_translate import PATH_VERSION, PAGES_VERSION, PageStore

PAPER = "test-page-edit"

MD_A = "# Attention\n\nThe scale factor is 1/sqrt(dk) here.\n\nSecond paragraph stays.\n"
MD_B = "# 注意力\n\n缩放因子是 1/sqrt(dk)。\n\n第二段不动。\n"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """真实产物布局 + 真实 `PageStore`，只把"要数据库 / 要模型"的几处换成空实现。"""
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
    monkeypatch.setattr(paper_service, "get_paper", lambda pid: {"id": pid})
    monkeypatch.setattr(page_edit, "_blocking_tasks", lambda pid: [])
    monkeypatch.setattr(page_edit, "_publish", lambda *a, **k: None)
    monkeypatch.setattr(page_edit, "_reindex", lambda pid, enabled: {"skipped": True})
    # 页首/页尾续句判定要读解析索引：对这里要测的东西没有影响，固定成"不续"
    monkeypatch.setattr(page_translate, "page_continuity",
                        lambda pid, page: {"head": False, "tail": False})
    return fake


def write_paths(paths, a_pages: dict, b_pages: dict) -> None:
    store_a = PageStore(PAPER, paths["path_a"], version=PATH_VERSION)
    store_b = PageStore(PAPER, paths["path_b"], version=PATH_VERSION)
    for page, text in a_pages.items():
        store_a.put(page, {"page": page, "path": "a", "page_total": 2,
                           "original_md": text, "breaks": [], "model": "m"})
    for page, text in b_pages.items():
        store_b.put(page, {"page": page, "path": "b", "page_total": 2,
                           "translation_md": text, "carry_md": "", "breaks": [],
                           "model": "m"})


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------


def test_side_aliases_and_rejects_garbage() -> None:
    assert page_edit.normalize_side("original") == "original"
    assert page_edit.normalize_side("en") == "original"
    assert page_edit.normalize_side("OCR") == "original"
    assert page_edit.normalize_side("英文") == "original"
    assert page_edit.normalize_side("translation") == "translation"
    assert page_edit.normalize_side("译文") == "translation"
    with pytest.raises(Exception):
        page_edit.normalize_side("both")


def test_count_line_and_replace_once() -> None:
    text = "a\nb\ntarget\nb\ntarget\n"
    assert page_edit.count_occurrences(text, "target") == 2
    assert page_edit.line_of(text, "target") == 3
    assert page_edit.replace_once(text, "target", "X").count("X") == 1


def test_near_misses_point_at_the_closest_line() -> None:
    """OCR 的错字常常只差一两个字符：找不到时要回"最像的那一行"，省掉模型重读一轮。"""
    text = "# Title\n\n1/sqrt(d_k) scaling\n\nlast line\n"
    hits = page_edit.near_misses(text, "1/sqrt(dk) scaling")
    assert hits and hits[0]["line"] == 3
    assert "d_k" in hits[0]["text"]


def test_numbered_prefixes_lines_and_truncates() -> None:
    body, truncated = page_edit.numbered("one\ntwo")
    assert body == "   1| one\n   2| two" and not truncated
    _, long = page_edit.numbered("x" * (page_edit.MAX_VIEW + 10))
    assert long


# --------------------------------------------------------------------------
# 拒绝路径（都不许落盘）
# --------------------------------------------------------------------------


def test_missing_find_is_refused_with_suggestion(env) -> None:
    write_paths(env, {1: MD_A}, {1: MD_B})
    before = env["path_a"].read_text(encoding="utf-8")
    out = page_edit.replace(PAPER, 1, "original", "1/sqrt(dx)", "1/sqrt(d_x)")
    assert out["ok"] is False and out["near_miss"]
    assert env["path_a"].read_text(encoding="utf-8") == before


def test_ambiguous_find_is_refused(env) -> None:
    write_paths(env, {1: "alpha beta\n\nalpha gamma\n"}, {1: MD_B})
    before = env["path_a"].read_text(encoding="utf-8")
    out = page_edit.replace(PAPER, 1, "original", "alpha", "ALPHA")
    assert out["ok"] is False and out["occurrences"] == 2
    assert "唯一" in out["error"]
    assert env["path_a"].read_text(encoding="utf-8") == before


def test_active_task_blocks_the_edit(env, monkeypatch) -> None:
    write_paths(env, {1: MD_A}, {1: MD_B})
    monkeypatch.setattr(page_edit, "_blocking_tasks", lambda pid: ["translate_zh(processing)"])
    out = page_edit.replace(PAPER, 1, "original", "dk", "d_k")
    assert out["ok"] is False and "translate_zh" in out["error"]


def test_page_without_product_is_refused(env) -> None:
    write_paths(env, {1: MD_A}, {1: MD_B})
    out = page_edit.replace(PAPER, 7, "original", "x", "y")
    assert out["ok"] is False and "7" in out["error"]


def test_bad_side_returns_refusal_not_exception(env) -> None:
    write_paths(env, {1: MD_A}, {1: MD_B})
    out = page_edit.replace(PAPER, 1, "both", "dk", "d_k")
    assert out["ok"] is False and out["side"] == "both"


# --------------------------------------------------------------------------
# 真改：落盘 / 位置 / 撤回
# --------------------------------------------------------------------------


def test_replace_writes_and_reports_position(env) -> None:
    write_paths(env, {1: MD_A}, {1: MD_B})
    out = page_edit.replace(PAPER, 1, "original", "1/sqrt(dk) here.",
                            "1/sqrt(d_k) here.", reason="下标被识别丢了")
    assert out["ok"] is True and out["side"] == "original" and out["source"] == "path"
    assert out["line"] == 3 and out["before"] == "1/sqrt(dk) here."
    stored = PageStore(PAPER, env["path_a"], version=PATH_VERSION).page(1)
    assert "1/sqrt(d_k) here." in stored["original_md"]
    assert stored["edited_by"] == "agent" and stored["edit_count"] == 1
    # 未改动的那一侧逐字节不变
    other = PageStore(PAPER, env["path_b"], version=PATH_VERSION).page(1)
    assert other["translation_md"] == MD_B


def test_undo_restores_previous_text(env) -> None:
    write_paths(env, {1: MD_A}, {1: MD_B})
    page_edit.replace(PAPER, 1, "translation", "缩放因子", "尺度因子")
    mid = PageStore(PAPER, env["path_b"], version=PATH_VERSION).page(1)
    assert "尺度因子" in mid["translation_md"]
    out = page_edit.undo(PAPER, 1, "translation")
    assert out["ok"] is True and out["reverted"]["find"] == "缩放因子"
    back = PageStore(PAPER, env["path_b"], version=PATH_VERSION).page(1)
    assert back["translation_md"] == MD_B
    # 撤回要连"改过"的痕迹一起擦掉：这一页本来没有 edited_* 字段，撤回后就该没有
    assert "edited_by" not in back and "edit_count" not in back
    # 撤回是"用掉"了那条日志：再来一次就没有可撤的了
    assert page_edit.undo(PAPER, 1, "translation")["ok"] is False


def test_view_returns_numbered_product(env) -> None:
    write_paths(env, {1: MD_A}, {1: MD_B})
    out = page_edit.view(PAPER, 1, "en")
    assert out["side"] == "original" and out["rows"] >= 3
    assert out["lines"].startswith("   1| # Attention")
    assert out["chars"] == len(MD_A)


def test_legacy_page_is_edited_in_place(env) -> None:
    """存量论文只有 `md/pages.json`：改在**原文件**里，不顺手拆成两路。"""
    store = PageStore(PAPER, env["page_products"], version=PAGES_VERSION)
    store.put(1, {"page": 1, "page_total": 1, "original_md": MD_A,
                  "translation_md": MD_B, "breaks": [], "model": "m"})
    out = page_edit.replace(PAPER, 1, "original", "Second paragraph stays.",
                            "Second paragraph remains.")
    assert out["ok"] is True and out["source"] == "legacy"
    assert not env["path_a"].exists() and not env["path_b"].exists()
    again = PageStore(PAPER, env["page_products"], version=PAGES_VERSION).page(1)
    assert "Second paragraph remains." in again["original_md"]
    assert again["translation_md"] == MD_B


# --------------------------------------------------------------------------
# 行结构不变量：变了一律回滚
# --------------------------------------------------------------------------


def test_restructure_is_rolled_back(env) -> None:
    """把两段并成一段（跨块编辑）必须被拒且**逐字节回滚** —— 行号漂移看不出来。"""
    write_paths(env, {1: "para one\n\npara two\n"}, {1: MD_B})
    before = env["path_a"].read_text(encoding="utf-8")
    out = page_edit.replace(PAPER, 1, "original", "one\n\npara two", "one and two")
    assert out["ok"] is False and "结构" in out["error"]
    assert out["blocks_before"] == 2 and out["blocks_after"] == 1
    assert env["path_a"].read_text(encoding="utf-8") == before


def test_deleting_the_whole_side_is_rolled_back(env) -> None:
    write_paths(env, {1: "only one block\n"}, {1: MD_B})
    before = env["path_a"].read_text(encoding="utf-8")
    out = page_edit.replace(PAPER, 1, "original", "only one block\n", "")
    assert out["ok"] is False
    assert env["path_a"].read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# 工具层：先读后写 + 改动预算
# --------------------------------------------------------------------------


class _FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def page_product(self, paper_id, page, side):
        self.calls.append(("view", page, side))
        return {"page": page, "side": side, "lines": "   1| text", "chars": 4}

    async def page_edit(self, paper_id, page, side, find, replace, *, reason="", reindex=True):
        self.calls.append(("edit", page, side, find, replace))
        return {"ok": True, "page": page, "side": side, "line": 1,
                "before": find, "after": replace}

    async def page_undo(self, paper_id, page, side, *, reindex=True):
        self.calls.append(("undo", page, side))
        return {"ok": True, "page": page, "side": side}


def _ctx(host: _FakeHost, *, budget: int = 2):
    deps = Deps(cfg=SimpleNamespace(edit_budget=budget, search_budget=5),
                host=host, paper_id="p", registry=None)
    return SimpleNamespace(deps=deps), deps


def test_tool_requires_reading_the_page_first() -> None:
    host = _FakeHost()
    ctx, deps = _ctx(host)
    out = asyncio.run(page_markdown_edit(ctx, op="replace", page=3, side="original",
                                         find="a", replace="b"))
    assert out["ok"] is False and "先读后写" in out["error"]
    assert host.calls == [] and deps.edit_calls == 0


def test_tool_view_then_edit_and_budget() -> None:
    host = _FakeHost()
    ctx, deps = _ctx(host, budget=2)
    asyncio.run(page_markdown_edit(ctx, op="view", page=3, side="en"))
    first = asyncio.run(page_markdown_edit(ctx, op="replace", page=3, side="original",
                                          find="a", replace="b", reason="改个字"))
    assert first["ok"] is True and deps.edits[0]["reason"] == "改个字"
    # 侧别叫法不同也要认得出"同一侧"（view 用 en、edit 用 original）
    assert host.calls[1][2] == "original"
    asyncio.run(page_markdown_edit(ctx, op="replace", page=3, side="original",
                                   find="c", replace="d"))
    over = asyncio.run(page_markdown_edit(ctx, op="replace", page=3, side="original",
                                          find="e", replace="f"))
    assert over["ok"] is False and "用尽" in over["error"]
    assert deps.edit_calls == 2


def test_tool_rejects_unknown_op() -> None:
    host = _FakeHost()
    ctx, _ = _ctx(host)
    out = asyncio.run(page_markdown_edit(ctx, op="delete", page=1, side="original"))
    assert out["ok"] is False and host.calls == []
