r"""`formula_get` 的**归一化去重**：`\boldsymbol{y}` 与 `y` 是同一条公式（纯离线）。

用户实测（TurboQuant 真实数据）：同一个内积界，正文第 7 页写成 `\|y\|_2^2`、
附录第 19 页写成 `\|\boldsymbol{y}\|_2^2`；期望内积式同样（p7 / p19 各一条）。
两条的 `also_on` 都是空数组 —— 没被认成"同一处重复陈述"，于是"4 个不同位置"里
有两处其实是重复的。

规则：算去重键之前先**归一化**（`_latex_norm_key`），剥掉 `\boldsymbol{…}` /
`\mathbf{…}` / `\bm{…}`（含不带花括号的写法）、`\left` / `\right`、细间距与空白，
但**保留内容本体**（`\boldsymbol{y}` → `y`）。不同的式子绝不合并。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agent import tools as agent_tools


def _item(page: int, row_id: str, latex: str, *, display: bool = True) -> dict:
    return {"page": page, "row_id": row_id, "latex": latex, "display": display,
            "grade": None, "partial": False, "source": "markdown",
            "context": "", "context_zh": ""}


class _FakeHost:
    """按真实回执的形状回公式清单（条目已按文档顺序排好）。"""

    def __init__(self, items: list[dict]) -> None:
        self.items = list(items)
        self.calls: list[dict] = []

    async def formulas(self, paper_id, *, page=None, query=None, include_inline=False):
        self.calls.append({"page": page, "include_inline": include_inline})
        kept = [one for one in self.items
                if not page or int(one["page"]) == int(page)]
        display = sum(1 for one in self.items if one["display"])
        return {"total": len(self.items), "returned": len(kept), "source": "markdown",
                "truncated": False, "display_total": display,
                "inline_total": len(self.items) - display, "omitted_inline": 0,
                "missing_pages": [], "items": [dict(one) for one in kept]}


class _Deps:
    def __init__(self, host) -> None:
        from backend.agent.citations import RefRegistry

        self.host = host
        self.paper_id = "p1"
        self.registry = RefRegistry("p1")
        self.query_cache: dict = {}
        self.row_spans: dict = {}

    def count_search(self) -> None:
        return None

    def note_tool(self, *args, **kwargs) -> None:
        return None


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def _call(host, **kwargs):
    return asyncio.run(agent_tools.formula_get(_Ctx(_Deps(host)), **kwargs))


#: 正文第 7 页与附录第 19 页的那一对（线上原文）
BOUND_PLAIN = r"\|y\|_2^2"
BOUND_BOLD = r"\|\boldsymbol{y}\|_2^2"
EXPECT_PLAIN = r"\mathbb{E}[\langle y, q \rangle]"
EXPECT_BOLD = r"\mathbb{E}[\langle \boldsymbol{y}, \boldsymbol{q} \rangle]"


# --------------------------------------------------------------------------
# ① 归一化本身：剥包装、留本体
# --------------------------------------------------------------------------


@pytest.mark.parametrize("wrapped", [
    r"\boldsymbol{y}",            # 带花括号
    r"\mathbf{y}",
    r"\bm{y}",
    r"\boldsymbol y",            # 不带花括号
    r"\boldsymbol{\mathbf{y}}",  # 套娃（手抄会出现）
    r"y \,",                     # 细间距
    r"\boldsymbol{y} \;",       # 字重 + 间距一起出现
    "  y  ",                      # 空白
])
def test_wrappers_are_stripped_but_the_body_survives(wrapped: str) -> None:
    r"""剥的是**包装**，本体（y）必须留下 —— 否则 y 与 `\boldsymbol{y}` 仍然不等。"""
    assert agent_tools._latex_norm_key(wrapped) == "y"


def test_left_right_only_change_bracket_size() -> None:
    r"""`\left(` / `\right)` 只调括号尺寸：剥掉后括号**还在**（它是内容）。"""
    assert (agent_tools._latex_norm_key(r"\left( \boldsymbol{y} \right)")
            == agent_tools._latex_norm_key(r"(y)"))


def test_structurally_different_formulas_stay_different() -> None:
    r"""上下标 / `\frac` 这类是**语义**，归一化不许把它们抹平。"""
    assert (agent_tools._latex_norm_key(r"\|y\|_2^2")
            != agent_tools._latex_norm_key(r"\|y\|_2"))
    assert (agent_tools._latex_norm_key(r"\frac{\|y\|_2^2}{d}")
            != agent_tools._latex_norm_key(r"\frac{1}{d}"))


# --------------------------------------------------------------------------
# ② 合并：一条条目 + also_on + 计数跟着减
# --------------------------------------------------------------------------


def test_bold_wrapper_pages_merge_into_one_item_with_also_on() -> None:
    r"""第 7 页的 `\|y\|_2^2` 与第 19 页的 `\|\boldsymbol{y}\|_2^2` 是同一条。"""
    host = _FakeHost([_item(7, "p007-02", BOUND_PLAIN),
                      _item(19, "p019-03", BOUND_BOLD)])
    out = _call(host)
    assert len(out["items"]) == 1, out["items"]
    one = out["items"][0]
    assert one["page"] == 7 and one["also_on"] == [19]
    # 计数跟着去重（全量口径）：两条合成一条
    assert (out["total"], out["display_total"], out["returned"]) == (1, 1, 1)


def test_two_inner_product_pairs_shrink_by_two() -> None:
    """实测那一批：内积界与期望内积式各合并一次 → 4 条变 2 条。"""
    host = _FakeHost([_item(7, "p007-02", BOUND_PLAIN),
                      _item(7, "p007-05", EXPECT_PLAIN),
                      _item(19, "p019-03", BOUND_BOLD),
                      _item(19, "p019-07", EXPECT_BOLD)])
    out = _call(host)
    assert [(one["page"], one["also_on"]) for one in out["items"]] == [(7, [19]), (7, [19])]
    assert (out["total"], out["display_total"]) == (2, 2)


def test_two_different_formulas_on_the_same_page_stay_two() -> None:
    """同页两条**不同的**式子绝不合并（各自的引用要分别给号）。"""
    host = _FakeHost([_item(4, "p004-01", r"x \leq \frac{\|y\|_2^2}{d}"),
                      _item(4, "p004-01", r"x \leq \frac{1}{d}")])
    out = _call(host)
    assert len(out["items"]) == 2 and out["total"] == 2
    assert len({one["ref"] for one in out["items"]}) == 2
