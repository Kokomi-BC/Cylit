"""一轮问答的运行时依赖（pydantic-ai 的 deps 注入点）。

工具是**模块级函数**（pydantic-ai 靠函数签名生成工具 schema），一轮问答的可变状态
（当前论文、引用表、工具轨迹）因此只能挂在 deps 上。这样同一个 agent 实例可以并发
服务多个请求，不会互相串状态 —— 把 paper_id 存成模块全局是这类服务最典型的越权来源。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from backend.agent.citations import RefRegistry
from backend.agent.config import AgentConfig
from backend.agent.host import HostClient

#: 阅读器「看哪一面」/「左右顺序」/「视图」三组**闭集**值：认不出来的一律当没传。
#: 为什么不猜一个最像的：落错面会让高亮在读者的视图里**根本画不出来**，
#: 而"不知道"至少还能退回去问用户。
READER_PAGE_SIDES = ("en", "zh", "both")
READER_ORDERS = ("en-first", "zh-first")
READER_VIEWS = ("rows", "markdown", "pdf")
#: `visible_pages` 最多记几页（读者眼前就那么几页，给多了只是撑长上下文）
READER_VISIBLE_MAX = 16
#: `side_tab` 的长度上限（页签 id 是 insight / highlight 这种短词）
READER_TAB_CHARS = 40


@dataclass(frozen=True)
class ReaderState:
    """读者**这一刻**在阅读器里的位置与版面（`/agent/ask` 请求体里的 `reader`）。

    为什么需要它：`highlight_manage` 的 `side` 以前按**文字语言**猜（含 CJK → zh），
    于是"中文读者高亮一段英文"会被标到 `en` 面 —— 而读者在译文视图里看不到那一面。
    知道"读者正在看哪一面"才能按**实际在看的那一面**落笔。

    **未知就是未知**：没带 `reader` 时 `available=False`，其余字段全空。编一个
    "大概在第 1 页"比不回答更糟 —— 模型会照着它去标高亮，用户却看不到。
    """

    available: bool = False
    #: 当前页（0 = 读者没报 / 报的值认不出来）
    page: int = 0
    #: 对照方式：`en` 只看原文 / `zh` 只看译文 / `both` 两侧对照（空 = 不知道）
    page_side: str = ""
    #: 左右顺序：`en-first` 原文在左 / `zh-first` 译文在左
    order: str = ""
    #: 视图：`rows` 正文行 / `markdown` / `pdf`
    view: str = ""
    #: 左栏当前页签 id（不认识的 id 也照收：那份列表归前端所有）
    side_tab: str = ""
    #: 读者眼前能看到的页（可选；`page` 认不出来时的旁证）
    visible_pages: tuple[int, ...] = ()

    @property
    def page_known(self) -> bool:
        return self.page > 0

    @property
    def note_side(self) -> str | None:
        """默认落笔的那一面。

        `both`（对照）时**不给**建议：读者眼前同时有两侧，标哪一面取决于要标的
        那段文字属于哪一面 —— 这个判断只能由模型（或用户）做，宿主不替它选。
        """
        return self.page_side if self.page_side in ("en", "zh") else None

    def as_body(self) -> dict:
        """回写成请求体的形状（只含**认得的**字段；全空时返回 `{}`）。"""
        out: dict = {}
        if self.page_known:
            out["page"] = self.page
        for key in ("page_side", "order", "view", "side_tab"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.visible_pages:
            out["visible_pages"] = list(self.visible_pages)
        return out


def _reader_choice(raw: dict, key: str, allowed: tuple[str, ...]) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if value in allowed else ""


def _reader_pages(raw: object) -> tuple[int, ...]:
    """`visible_pages`：只留正整数页码（`True` 这种 bool 不算数）。"""
    if not isinstance(raw, (list, tuple)):
        return ()
    pages = [one for one in raw
             if isinstance(one, int) and not isinstance(one, bool) and one > 0]
    return tuple(pages[:READER_VISIBLE_MAX])


def normalize_reader(raw: object) -> ReaderState:
    """把请求体里的 `reader` 洗成 `ReaderState`（**全仓唯一的解析口**）。

    非法字段**丢弃而不是报错**：阅读状态只是"读者在哪"的旁证，一个拼错的页签 id
    不该让整轮问答 500 —— 认不出来就当没传，剩下的字段照常用。

    全都认不出来时 `available=False`（与"这次请求没带"等价）：否则工具会拿着一个
    空壳状态告诉模型"读者在第 0 页、看空的一面"，那正是要禁掉的"编造阅读状态"。

    幂等：传进来已经是 `ReaderState` 就原样返回，所以上层可以放心多处调用。
    """
    if isinstance(raw, ReaderState):
        return raw
    if not isinstance(raw, dict):
        return ReaderState()

    page = raw.get("page")
    page_no = page if isinstance(page, int) and not isinstance(page, bool) and page > 0 else 0
    tab = raw.get("side_tab")
    side_tab = tab.strip()[:READER_TAB_CHARS] if isinstance(tab, str) else ""
    reader = ReaderState(
        page=page_no,
        page_side=_reader_choice(raw, "page_side", READER_PAGE_SIDES),
        order=_reader_choice(raw, "order", READER_ORDERS),
        view=_reader_choice(raw, "view", READER_VIEWS),
        side_tab=side_tab,
        visible_pages=_reader_pages(raw.get("visible_pages")),
    )
    known = bool(page_no or reader.page_side or reader.order or reader.view
                 or reader.side_tab or reader.visible_pages)
    return replace(reader, available=known)


@dataclass
class Deps:
    cfg: AgentConfig
    host: HostClient
    paper_id: str
    registry: RefRegistry
    page_total: int = 0
    title: str = ""
    #: 这条会话**冻结**的风格（主后端按 `conversation.agent_style` 取好带过来）。
    #: 设置页的改动只对新对话生效 —— 已开着的会话继续用它出生时那一份，
    #: 免得一次改动把历史会话的 `system + 全部历史` 前缀缓存全打掉。空 = 用 `prompts.DEFAULT_STYLE`。
    style: str = ""
    #: 这条会话冻结的**口吻预设**（default / rigorous / concise / plain / custom）
    #: 与**回复长度**（short / normal / detailed）：和 `style` 一样由主后端按会话行取好带过来。
    #: 三项一起进 system（见 `prompts.build_instructions`），所以都必须冻结在会话行上。
    style_preset: str = ""
    reply_length: str = ""
    #: 是否**本次对话的第一条消息**（判据 = 主后端没给历史）。只有首轮才附论文摘要。
    first_turn: bool = False
    #: 论文摘要原文 / 中文译文：**首轮**拼进用户消息当背景（`prompts.abstract_block`）。
    #: 它不是可引用的材料（没有 [ref]），规则常驻在 system 的 `ABSTRACT_RULES` 里。
    abstract_en: str = ""
    abstract_zh: str = ""
    #: 划词解释：用户选中的原文 + 它对应的 ref（ref 用来让模型自己展开上下文）
    selection: str = ""
    #: 本轮用户上传的图片张数 / 其中真正随消息发出去的那几张（见 `prompts.IMAGE_NOTE`）
    attachments: int = 0
    attachments_shown: int = 0
    selection_ref: int | None = None
    selection_page: int | None = None
    #: 用户对选区的提问（划词追问）；为空时是"解释这段内容"
    selection_question: str = ""
    #: 读者**此刻**在阅读器里的位置与版面（`normalize_reader` 洗好的那一份）。
    #: 为什么放 deps 而不是每轮拼进 system：它每翻一页就变，混进 system 会把
    #: "system + 全部历史"的前缀缓存整段打掉；而"没带就是不知道"的口径由
    #: `reader_state` 工具当场说明，也比一句常驻的提示词更省。
    reader: ReaderState = field(default_factory=ReaderState)
    #: 工具轨迹（审计 + 自检 A5/A13/A14 读它）
    trace: list[dict] = field(default_factory=list)
    #: 模型轮次（含工具轮）：超上限就停，见 §11 的 `rounds_exhausted`
    model_calls: int = 0
    #: 这一轮读过的页（自检用：验证"先检索定位、再读页"的顺序）
    pages_read: list[int] = field(default_factory=list)
    #: 归一化检索词 → 上一次的返回。模型会把同一句话问两遍（实测跨库问题连发 9 次
    #: `kb_search`，最后把轮数用满），重复的查询不必再打一次后端。
    query_cache: dict[str, dict] = field(default_factory=dict)
    #: 检索命中的**行跨度**（首行 id → 这个 chunk 覆盖的整串行 id）：`block_get` 展开跨多行的
    #: chunk 时要拼整段，而 `Citation` 里只存了首行（前端契约不动，别塞进引用表）。
    row_spans: dict[str, list[str]] = field(default_factory=dict)
    #: 本轮检索次数（**纯记账**：检索预算 2026-11-26 已取消，只剩统计与自检在读它）
    search_calls: int = 0
    #: 本轮改动次数（高亮 / 笔记 / 页级产物都算，同样只是记账 —— 改动预算已取消）
    edit_calls: int = 0
    #: 本轮改过的产物（`done` 事件带给前端做提示，也是自检的断言口）
    edits: list[dict] = field(default_factory=list)
    #: 本轮**读过**其产物原文的 ``(页, 侧)``：`page_markdown_edit` 的"先读后写"判据
    viewed_sides: set[tuple[int, str]] = field(default_factory=set)
    #: 元数据填写（`paper_meta_write`）真正写进去的字段。一次会话只允许写一次：
    #: 元数据是"整篇的结论"，边读边改只会让前面写的几条与后面的判断互相矛盾。
    meta_applied: dict = field(default_factory=dict)
    #: 元数据**提交过**没有（与 `meta_applied` 不同：提交成功但所有字段都已经有值时，
    #: `applied` 是空的 —— 那也是一次成功的会话，不该报"没有提交"）。
    meta_submitted: bool = False

    def count_search(self) -> None:
        """记一次检索（**只记账，不拦截**）。

        检索预算 2026-11-26 已取消（长程任务需要）：那道"用尽后不打后端"的闸早就成了
        恒 False 的死分支，留着只会让人以为还有上限。`search_calls` 保留 —— 自检与统计在读它。
        """
        self.search_calls += 1

    def count_edit(self) -> None:
        """记一次改动（同样是纯记账，改动预算已取消）。"""
        self.edit_calls += 1

    # ------------------------------------------------------------------
    def note_tool(self, name: str, args: dict, status: str,
                  started: float, detail: str = "") -> None:
        self.trace.append({
            "tool": name, "args": args, "status": status,
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "detail": detail[:200],
        })

    def note_model_call(self) -> int:
        self.model_calls += 1
        return self.model_calls

    @property
    def rounds_exhausted(self) -> bool:
        return False        # 轮数也不再设限（长程任务）
