r"""`formula_get` 的跨页合并：**结构等价**与 `also_on` 的收口（纯离线）。

用户实测 C：`$D_{\text{mse}} \approx 0.36, 0.117, 0.03, 0.009$` 在第 3 / 6 / 18 页各出现一次，
结果列表里却剩两条 —— 第 6 页那条 `also_on=[18]`、没提第 3 页，第 3 页那条 `also_on` 是空的。
两处成因（都在真实回执里核过）：

1. 第 3 页那一版**带参数**（`D_{\text{mse}}(Q_{\text{mse}}) \approx 0.36, …`），而归一化只认
   "逐字相等"，于是它永远合不进去 —— 逐字相等不能当唯一判据（那会破掉 `\boldsymbol{y}` 与 `y`
   的合并），所以多一档**结构等价**：只在**左端**允许"带参数 / 不带参数"，右端逐字相同；
2. 宿主**先**合并过一次：被合并掉的那条（第 18 页）工具层再也看不到，`also_on_detail` 只能是空的。
   所以并进来那条自己带的页码必须**一起并上去**（否则三页反而并丢一页）。

D 的先判定：第 4 页的单行下界 `D_{\text{mse}}(Q) := … \geq \frac{1}{4^b}` 真正的同伴是第 20 页
**同一行**的复制（只差一个句号）；第 20 页那条 `\begin{aligned} … \end{aligned}` 是另一条式子，
**不许**合并（用户口径：不同的式子绝不合并）。
"""

from __future__ import annotations

import asyncio

from backend.agent import tools as agent_tools
from backend.agent.tools import _latex_head_args, _latex_norm_key, _latex_struct_key


def _item(page: int, row_id: str, latex: str, **extra) -> dict:
    item = {"page": page, "row_id": row_id, "latex": latex, "display": True,
            "grade": None, "partial": False, "source": "markdown",
            "context": "", "context_zh": ""}
    item.update(extra)
    return item


class _FakeHost:
    """按真实回执的形状回公式清单（条目已按文档顺序排好）。"""

    def __init__(self, items: list[dict]) -> None:
        self.items = list(items)

    async def formulas(self, paper_id, *, page=None, query=None, include_inline=False,
                       offset=None, limit=None):
        kept = [one for one in self.items if not page or int(one["page"]) == int(page)]
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


def _call(host):
    return asyncio.run(agent_tools.formula_get(_Ctx(_Deps(host))))


#: 线上三处（第 3 页那一版带参数，第 6 / 18 页不带）
VALUES_ARG = r"D_{\text{mse}}(Q_{\text{mse}}) \approx 0.36, 0.117, 0.03, 0.009"
VALUES_PLAIN = r"D_{\text{mse}} \approx 0.36, 0.117, 0.03, 0.009"
#: 第 4 页的单行下界 vs 第 20 页的 aligned 推导（D：不是同一条）
ONE_LINE = (r"D_{\text{mse}}(Q) := \mathbb{E}\left[\left\|\boldsymbol{x} - Q^{-1}"
            r"\left(Q(\boldsymbol{x})\right)\right\|_2^2\right] \geq \frac{1}{4^b}")
ONE_LINE_P20 = ONE_LINE + "."
ALIGNED_P20 = (r"\begin{aligned} D_{\text{mse}} &= \sum_{j=1}^{d} \mathbb{E}\left[\left|"
               r"\boldsymbol{x}_j - [Q^{-1}(Q(\boldsymbol{x}))]_j\right|^2\right] \\ "
               r"&\geq \frac{1}{4^b}, \end{aligned}")


# --------------------------------------------------------------------------
# ① 结构等价本身
# --------------------------------------------------------------------------


def test_structural_key_drops_only_the_head_argument() -> None:
    r"""左端参数表不算身份；右端（关系符之后）一个字都不许抹。"""
    assert _latex_struct_key(VALUES_ARG) == _latex_struct_key(VALUES_PLAIN)
    assert _latex_head_args(VALUES_ARG) == r"(Q_{\text{mse}})"
    assert _latex_norm_key(VALUES_ARG) != _latex_norm_key(VALUES_PLAIN), \
        "逐字相等在这条上本来就不成立 —— 这正是需要结构等价的原因"
    # 右端不同 = 不同式子（第 4 页 vs 第 20 页那个只差因子的下界）
    left = r"D_{\text{prod}}(Q) \geq \frac{\|y\|_2^2}{d} \cdot \frac{1}{4^b}"
    right = r"D_{\text{prod}}(Q) \geq \frac{1}{d} \cdot \frac{1}{4^b}"
    assert _latex_struct_key(left) != _latex_struct_key(right)


def test_two_different_arguments_are_not_equivalent() -> None:
    r"""两边都有参数且不同（`f(x)` vs `f(y)`）→ 不是同一个左端。

    键本身**相等**（参数不参与键），区别由参数表单独判 —— 所以 `_struct_mergeable` 那道
    护栏必须存在：参数表都在、却不相同，就不许合并。
    """
    assert _latex_struct_key(r"f(x) = x^2") == _latex_struct_key(r"f(y) = x^2")
    assert _latex_head_args(r"f(x) = x^2") != _latex_head_args(r"f(y) = x^2")
    assert not agent_tools._struct_mergeable("x", "y")
    assert agent_tools._struct_mergeable("", "Q") and agent_tools._struct_mergeable("Q", "")
    out = _call(_FakeHost([_item(3, "p003-01", r"f(x) = x^2"),
                           _item(9, "p009-01", r"f(y) = x^2")]))
    assert [one["page"] for one in out["items"]] == [3, 9], "参数不同的两条绝不合并"


# --------------------------------------------------------------------------
# ② C：三页各一次 → 一条 + also_on 两页（升序、去重、无自指）
# --------------------------------------------------------------------------


def test_three_pages_with_argument_variant_merge_into_one() -> None:
    out = _call(_FakeHost([_item(3, "p003-13", VALUES_ARG),
                           _item(6, "p006-03", VALUES_PLAIN),
                           _item(18, "p018-04", VALUES_PLAIN)]))
    assert len(out["items"]) == 1, out["items"]
    one = out["items"][0]
    assert one["page"] == 3
    assert one["also_on"] == [6, 18], "另外两页，升序"
    assert [d["page"] for d in one["also_on_detail"]] == [6, 18], "详情的页码与 also_on 一一对应"
    assert one["also_on_detail"][0]["latex"] == VALUES_PLAIN


def test_pages_the_host_already_merged_are_not_lost() -> None:
    r"""宿主先合并过一次（第 6 页那条带着 `also_on=[18]`）：再合并时那些页必须并上去。"""
    host_item = _item(6, "p006-03", VALUES_PLAIN, also_on=[18],
                      also_on_detail=[{"page": 18, "latex": VALUES_PLAIN}])
    out = _call(_FakeHost([_item(3, "p003-13", VALUES_ARG), host_item]))
    assert len(out["items"]) == 1, out["items"]
    one = out["items"][0]
    assert one["also_on"] == [6, 18], "三页各一次，合并后必须还剩三页的信息"
    assert [d["page"] for d in one["also_on_detail"]] == [6, 18]


def test_also_on_never_points_at_its_own_page() -> None:
    """宿主回执里万一出现自指（`page` 在 `also_on` 里）→ 收口时剔掉，不是原样转发。"""
    out = _call(_FakeHost([_item(6, "p006-03", VALUES_PLAIN, also_on=[6, 18],
                                 also_on_detail=[{"page": 6, "latex": VALUES_PLAIN},
                                                 {"page": 18, "latex": VALUES_PLAIN}])]))
    one = out["items"][0]
    assert one["page"] == 6 and one["also_on"] == [18], one
    assert [d["page"] for d in one["also_on_detail"]] == [18]


# --------------------------------------------------------------------------
# ③ D：不同的式子绝不合并（先判定，再钉住）
# --------------------------------------------------------------------------


def test_aligned_derivation_is_not_the_one_line_lower_bound() -> None:
    r"""第 4 页单行下界 vs 第 20 页 `\begin{aligned}` 推导 → 两条（误并会毁掉引用）。"""
    out = _call(_FakeHost([_item(4, "p004-03", ONE_LINE),
                           _item(20, "p020-09", ALIGNED_P20)]))
    assert [one["page"] for one in out["items"]] == [4, 20], out["items"]
    assert all(one["also_on"] == [] for one in out["items"])


def test_the_real_partner_of_the_page4_bound_is_the_page20_one_line_copy() -> None:
    """第 4 页那条真正的同伴是第 20 页**同一行**的复制（只差一个句号）—— 合，且带上它的原文。"""
    out = _call(_FakeHost([_item(4, "p004-03", ONE_LINE),
                           _item(20, "p020-02", ONE_LINE_P20)]))
    assert len(out["items"]) == 1, out["items"]
    one = out["items"][0]
    assert one["page"] == 4 and one["also_on"] == [20]
    assert one["also_on_detail"] == [{"page": 20, "latex": ONE_LINE_P20}], \
        "另一版逐字不同（末尾句号），正是要一起给的理由"


def test_bold_wrapper_merge_survives_the_structural_rule() -> None:
    r"""用户上一轮明确要的那条不能被新规则破掉：`\|y\|_2^2` 与 `\|\boldsymbol{y}\|_2^2` 合并。"""
    assert _latex_struct_key(r"\|y\|_2^2") == _latex_struct_key(r"\|\boldsymbol{y}\|_2^2")
    out = _call(_FakeHost([_item(7, "p007-02", r"\|y\|_2^2"),
                           _item(19, "p019-03", r"\|\boldsymbol{y}\|_2^2")]))
    assert len(out["items"]) == 1 and out["items"][0]["also_on"] == [19]
