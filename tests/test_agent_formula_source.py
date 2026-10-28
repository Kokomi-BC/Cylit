r"""`formula_get` 的**数据来源**：默认读页级 Markdown 里的块级公式（完整），解析卡只做兜底。

口径来自用户实测：阅读器「公式」栏里复制得到的都是**完整公式**，而那批正是
`formulas_payload(source="markdown")` 里 `display=true` 的条目（全篇 346 条抽取中 24 条）。
解析卡（`formula` 表 97 条）的 latex 多数只是 `latex_candidate[0]`，是**碎片**，不能当默认。

这里钉五件事：
1. 默认只回 `display=true`（行内公式不出现）；
2. `include_inline=True` 时行内也列出来；
3. 某页在 markdown 源里**没有块级公式**时，用该页解析卡补上（`source="parse"`）；
4. 超上限 → `truncated=true`、`total` 全量、条数 = 上限；
5. 排序是文档顺序（页 → 块序号）。
"""

from __future__ import annotations

import pytest

from backend.services.agent import project


def _md(page: int, row: int, latex: str, display: bool = True) -> dict:
    return {"id": f"f{page:03d}-{row:04d}", "page": page, "row_id": f"p{page:03d}-{row:02d}",
            "latex": latex, "display": display, "context": "上下文",
            "context_zh": "", "source": "markdown"}


def _card(page: int, block: int, latex: str, grade: str = "A",
          partial: bool = False) -> dict:
    return {"id": f"paper-p{page:03d}-b{block:02d}",
            "row_id": f"paper-p{page:03d}-b{block:02d}", "page": page, "latex": latex,
            "display": True, "grade": grade, "partial": partial,
            "context": "", "context_zh": "", "source": "parse"}


@pytest.fixture
def fake_sources(monkeypatch):
    def install(md: list[dict], cards: list[dict] | None = None) -> None:
        monkeypatch.setattr(project.reader_service, "formulas_payload",
                            lambda paper_id, source="markdown": {
                                "items": md if source == "markdown" else (cards or []),
                                "source": source})
        monkeypatch.setattr(project.paper_service, "get_paper",
                            lambda paper_id: {"id": paper_id, "page_count": 22})
    return install


def test_default_returns_only_block_level_formulas(fake_sources) -> None:
    """① 3 条行内 + 2 条块级 → 只回那 2 条（行内碎片默认不出现）。"""
    fake_sources([
        _md(5, 1, "b - 1", display=False),
        _md(5, 2, "\\qquad", display=False),
        _md(5, 3, "j \\in [d]", display=False),
        _md(5, 4, "D_{\\text{mse}} := \\mathbb{E}_Q[…]"),
        _md(5, 7, "\\mathcal{C}(f_X, b) := \\min …"),
    ])
    out = project.formulas("p1")
    assert [i["latex"] for i in out["items"]] == [
        "D_{\\text{mse}} := \\mathbb{E}_Q[…]",
        "\\mathcal{C}(f_X, b) := \\min …"]
    # `total` 是"去重后、筛选范围内的全部条目"（行内也算），`returned` 才是这次给了几条：
    # 两个数一起看才知道"还有多少没给"，这是 2026-12 统一口径后定下的语义。
    assert out["returned"] == 2 and out["total"] == 5
    assert (out["display_total"], out["inline_total"]) == (2, 3)
    assert out["omitted_inline"] == 3 and out["source"] == "markdown"


def test_include_inline_lists_the_inline_ones_too(fake_sources) -> None:
    """② include_inline=True 时 5 条都在（语义就是"把行内也列出来"）。"""
    fake_sources([_md(5, 1, "b - 1", display=False), _md(5, 2, "\\qquad", display=False),
                  _md(5, 3, "j \\in [d]", display=False),
                  _md(5, 4, "D_{\\text{mse}} := E[…]"),
                  _md(5, 7, "\\mathcal{C}(f_X, b) := \\min …")])
    out = project.formulas("p1", include_inline=True)
    assert out["returned"] == 5 and out["total"] == 5




def test_parse_fallback_skips_a_fragment_of_a_listed_formula(fake_sources) -> None:
    """卡片只是转录里那条公式的**一段** → 不再单独列（第 22 页 `n\\times d` 那种重复）。"""
    fake_sources([_md(22, 1, "\\boldsymbol{K}, \\boldsymbol{V} \\in \\mathbb{R}^{n \\times d}",
                       display=False)],
                 [_card(22, 1, "n\\times d")])
    out = project.formulas("p1", page=22)
    assert out["returned"] == 0, [i["latex"] for i in out["items"]]
    assert out["display_total"] == 0 and out["source"] == "markdown"


def test_parse_fallback_still_lists_a_card_that_is_not_in_the_text(fake_sources) -> None:
    """对不上的卡片照旧兜底（别把整条兜底路径堵死）。"""
    fake_sources([_md(22, 1, "\\boldsymbol{q} \\in \\mathbb{R}^d", display=False)],
                 [_card(22, 1, "Q_{\\text{qjl}}(x) := \\text{sign}(S x)")])
    out = project.formulas("p1", page=22)
    assert out["returned"] == 1 and out["items"][0]["source"] == "parse"

def test_page_without_block_formula_falls_back_to_the_parse_card(fake_sources) -> None:
    """③ 某页 markdown 里连一条块级公式都没有 → 用该页解析卡补，并标 source=parse。"""
    fake_sources([_md(5, 4, "D_{\\text{mse}} := E[…]"),
                  _md(9, 1, "b - 1", display=False)],      # p9 只有行内
                 [_card(9, 3, "\\Gamma (d/2)", grade="A", partial=True),
                  _card(5, 9, "不该用这张卡", grade="B")])   # p5 已有块级公式
    out = project.formulas("p1")
    assert [(i["page"], i["latex"], i["source"], i["grade"], i["partial"])
            for i in out["items"]] == [
        (5, "D_{\\text{mse}} := E[…]", "markdown", None, False),
        (9, "\\Gamma (d/2)", "parse", "A", True)]
    assert out["source"] == "mixed"


def test_limit_sets_truncated_and_total_is_the_full_count(fake_sources) -> None:
    """④ 超上限：returned == limit、truncated=True、total 是全量。"""
    fake_sources([_md(5, i, f"f_{{{i}}}") for i in range(1, 8)])
    out = project.formulas("p1", limit=3)
    assert out["returned"] == 3 and out["truncated"] is True and out["total"] == 7


def test_items_come_out_in_document_order(fake_sources) -> None:
    """⑤ 乱序输入（含两种来源）→ 输出按（页, 块序号）排好。"""
    fake_sources([_md(9, 2, "nine-two"), _md(5, 25, "five-twenty-five"),
                  _md(5, 9, "five-nine")],
                 [_card(3, 4, "three-four"), _card(3, 1, "three-one")])
    out = project.formulas("p1")
    assert [i["latex"] for i in out["items"]] == [
        "three-one", "three-four", "five-nine", "five-twenty-five", "nine-two"]


# --------------------------------------------------------------------------
# `partial` / `grade` 一路带到工具回执（模型据此决定要不要看原版面）
# --------------------------------------------------------------------------


class _FakeHost:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        #: 想换一份回执就设它（默认是下面那份"两来源"的固定数据）
        self.payload: dict | None = None

    # offset / limit 是宿主 `project.formulas` 的分页口径（契约变了就跟着改）：
    # 本文件的回执是**老形状**（没有 next_offset / has_more），正好钉住工具的老宿主兼容路径。
    async def formulas(self, paper_id, *, page=None, query=None, include_inline=False,
                       offset=None, limit=None):
        self.calls.append({"page": page, "include_inline": include_inline,
                           "offset": offset, "limit": limit})
        if self.payload is not None:
            return self.payload
        return {"total": 2, "returned": 2, "source": "mixed", "truncated": False,
                "display_total": 2, "inline_total": 3,
                "items": [
                    {"page": 5, "row_id": "p005-09", "latex": "D_{\\text{mse}} := E[…]",
                     "display": True, "grade": None, "partial": False, "source": "markdown",
                     "context": "", "context_zh": ""},
                    {"page": 9, "row_id": "c03", "latex": "\\Gamma (d/2)",
                     "display": True, "grade": "A", "partial": True, "source": "parse",
                     "context": "", "context_zh": ""},
                ]}


class _Deps:
    def __init__(self, host) -> None:
        from backend.agent.citations import RefRegistry

        self.host = host
        self.paper_id = "p1"
        self.registry = RefRegistry("p1")
        # Deps 契约里有它：`_read_target` 连「不跨篇」的早退分支都要读 page_total，
        # 桩少这一个字段，回执就变成「工具内部错误：AttributeError」
        self.page_total = 22
        self.query_cache: dict = {}
        self.row_spans: dict = {}

    def count_search(self) -> None:
        return None

    def note_tool(self, *args, **kwargs) -> None:
        return None


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def test_formula_get_returns_partial_grade_and_a_usable_hint() -> None:
    import asyncio

    from backend.agent import tools as agent_tools

    host = _FakeHost()
    out = asyncio.run(agent_tools.formula_get(_Ctx(_Deps(host))))
    assert [(i["grade"], i["partial"]) for i in out["items"]] == [(None, False), ("A", True)]
    assert "page_image_get" in (out.get("hint") or ""), "partial 的条目要引导去看原版面"
    assert host.calls[0]["include_inline"] is False

# --------------------------------------------------------------------------
# 可见性：默认只给块级那批，行内另计一个数（让模型知道"还有多少没给"）
# --------------------------------------------------------------------------


def test_counts_split_block_level_and_inline(fake_sources) -> None:
    """① display_total / inline_total 各自数对（含 page 过滤之后的口径）。"""
    fake_sources([_md(5, 1, "a", display=False), _md(5, 2, "b", display=False),
                  _md(5, 3, "c", display=True), _md(7, 1, "d", display=True)])
    whole = project.formulas("p1")
    assert (whole["display_total"], whole["inline_total"]) == (2, 2)
    page5 = project.formulas("p1", page=5)
    assert (page5["display_total"], page5["inline_total"]) == (1, 2)


def test_inline_hint_when_not_requested() -> None:
    """② include_inline=False 且行内 > 0 → hint 里说清"另有 N 条"。"""
    import asyncio

    from backend.agent import tools as agent_tools

    out = asyncio.run(agent_tools.formula_get(_Ctx(_Deps(_FakeHost()))))
    assert "另有 3 条行内公式" in (out.get("hint") or "")
    assert out["inline_total"] == 3 and out["display_total"] == 2




def test_two_formulas_sharing_a_prefix_get_distinct_refs() -> None:
    """两条公式前 24 字符相同（第 4 页那对）→ 必须是**两个** ref，不能共用一个号。"""
    import asyncio

    from backend.agent import tools as agent_tools

    prefix = "D_{\\text{prod}}(Q_{\\text{prod}})"
    # 用例本身要保证"前 24 字符完全相同"这个前提成立（否则测不到撞键）
    assert len(prefix) >= 24
    host = _FakeHost()
    host.payload = {"total": 2, "returned": 2, "source": "markdown", "truncated": False,
                    "display_total": 2, "inline_total": 0, "omitted_inline": 0,
                    "items": [
                        {"page": 4, "row_id": "p004-01", "latex": prefix + " := E[…] \\leq b^{-1}",
                         "display": True, "grade": None, "partial": False,
                         "source": "markdown", "context": "", "context_zh": ""},
                        {"page": 4, "row_id": "p004-01", "latex": prefix + " \\approx 1.57/d",
                         "display": True, "grade": None, "partial": False,
                         "source": "markdown", "context": "", "context_zh": ""},
                    ]}
    out = asyncio.run(agent_tools.formula_get(_Ctx(_Deps(host)), page=4))
    refs = [item["ref"] for item in out["items"]]
    assert len(set(refs)) == 2, f"两条不同公式不能共用一个 ref：{refs}"

def test_no_inline_hint_when_include_inline_is_on() -> None:
    """③ 已经把行内要来了就不再提示（否则模型会以为还有没取的）。"""
    import asyncio

    from backend.agent import tools as agent_tools

    out = asyncio.run(agent_tools.formula_get(_Ctx(_Deps(_FakeHost())), include_inline=True))
    assert "另有" not in (out.get("hint") or "")


def test_include_inline_hint_points_at_the_complete_ones() -> None:
    """④ 传了 include_inline 要提醒：完整那几条带 display=true，按文档顺序夹在碎片中间。"""
    import asyncio

    from backend.agent import tools as agent_tools

    out = asyncio.run(agent_tools.formula_get(_Ctx(_Deps(_FakeHost())), include_inline=True))
    hint = out.get("hint") or ""
    assert "display: true" in hint
    assert "另有" not in hint, "行内已经要来了，不该再说「还有 N 条没给」"



def test_truncated_receipt_names_the_pages_that_did_not_fit() -> None:
    """截断时回执直接给「还差哪几页」（p1-1：以前只有 truncated + 条数，得自己按页数）。"""
    import asyncio

    from backend.agent import tools as agent_tools

    host = _FakeHost()
    host.payload = {"total": 319, "returned": 1, "source": "markdown", "truncated": True,
                    "display_total": 47, "inline_total": 272, "omitted_inline": 272,
                    "missing_pages": [20, 21],
                    "items": [{"page": 3, "row_id": "p003-01", "latex": "x",
                               "display": True, "grade": None, "partial": False,
                               "source": "markdown", "context": "", "context_zh": ""}]}
    out = asyncio.run(agent_tools.formula_get(_Ctx(_Deps(host))))
    assert out["missing_pages"] == [20, 21]
    hint = out.get("hint") or ""
    assert "20、21" in hint, hint
    assert "formula_get(page=" in hint


def test_also_on_reaches_the_model() -> None:
    """跨页合并的那条要把别的页码带出来（第 6 页那条 also_on=[18]）。"""
    import asyncio

    from backend.agent import tools as agent_tools

    host = _FakeHost()
    host.payload = {"total": 1, "returned": 1, "source": "markdown", "truncated": False,
                    "display_total": 1, "inline_total": 0, "omitted_inline": 0,
                    "items": [{"page": 6, "row_id": "p006-02", "latex": "x", "display": True,
                               "grade": None, "partial": False, "source": "markdown",
                               "also_on": [18], "context": "", "context_zh": ""}]}
    out = asyncio.run(agent_tools.formula_get(_Ctx(_Deps(host))))
    assert out["items"][0]["also_on"] == [18]
