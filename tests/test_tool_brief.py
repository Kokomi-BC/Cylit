r"""过程行 `·` 后面那截参数人话：19 个工具都要让人看出「这次到底传了什么」。

为什么单独钉一遍：digest 是**用户唯一能看到的调用参数**，可它以前有一半是空的或只说半句 ——
`reader_state` / `paper_archive_get` 返回空串，`note_manage` 只写「新增笔记」，
`highlight_manage` 写「新增高亮 · 第 5 页」却不带被标的原文，`paper_meta_write` 只写 venue，
`formula_get` 丢掉 query 与 include_inline。侧栏里那串「调了一下」看不出在干什么。

所以每个用例都同时过几道公共门禁（`_checked`）：单行、不超过 120 字
（`trace.DIGEST_CHARS`）、不露 `k=v` 键名、不出现 `None`、
不以「、」开头或结尾 —— 那一行只有一行的宽度，越界或露键名就退回成垃圾串。
"""
from __future__ import annotations

import pytest

from backend.agent.tools import tool_brief


def _checked(tool: str, args: object) -> str:
    """跑一次 tool_brief，并压上**所有**工具都该守的那几道门禁。"""
    brief = tool_brief(tool, args)
    assert len(brief) <= 120, brief
    assert "\n" not in brief and "\r" not in brief, brief
    assert "None" not in brief, brief
    assert "=" not in brief, f"过程行不该露 k=v 键名：{brief}"
    assert not brief.startswith("、") and not brief.endswith("、"), brief
    return brief


@pytest.mark.parametrize("tool", ["reader_state", "paper_archive_get"])
def test_tools_without_arguments_stay_empty(tool: str) -> None:
    """这两个工具没有入参：空串让前端固定短语兜底，「None」或「、」都不是人话。"""
    assert tool_brief(tool, {}) == ""


@pytest.mark.parametrize("tool,args,want", [
    ("page_markdown_get", {"pages": "5"}, "第 5 页"),
    ("page_markdown_get", {"pages": [3, 4, 5]}, "第 3-5 页"),
    ("page_markdown_get", {"pages": "第 4 页"}, "第 4 页"),
    ("translate_get", {"pages": "all"}, "整篇"),
    ("page_image_get", {"pages": "4,7"}, "第 4,7 页"),
    ("page_image_get", {"pages": "3-8"}, "第 3-8 页"),
])
def test_page_tools_show_which_pages(tool: str, args, want: str) -> None:
    """「第 [3, 4, 5] 页」这种原始 repr 看不出读了哪几页；连着的多页要压成区间。"""
    assert _checked(tool, args) == want


def test_block_get_shows_the_refs() -> None:
    """ref 是引用原文的唯一把手：单条与一批都要看得出是几号。"""
    assert _checked("block_get", {"ref": 7}) == "引用 [7]"
    assert _checked("block_get", {"refs": [3, 5, 9]}) == "引用 [3、5、9]"


def test_figure_tools_show_page_and_index() -> None:
    """看一张图要知道「哪一页第几张」；批量时给页与区域数，别只说调了 figure_image_get。"""
    assert _checked("figure_list", {"pages": [4, 7]}) == "第 4、7 页"
    assert _checked("figure_list", {"page": 4}) == "第 4 页"
    assert _checked("figure_image_get", {"page": 4, "index": 2}) == "第 4 页 · 第 2 张"
    brief = _checked("figure_image_get",
                     {"items": [{"page": 4, "index": 1}, {"page": 7, "index": 2}]})
    assert "第 4、7 页" in brief and "2 个区域" in brief
    assert _checked("figure_image_get", {}) == ""


@pytest.mark.parametrize("args,wants", [
    ({"page": 3, "query": "softmax", "include_inline": True}, ["第 3 页", "「softmax」", "含行内"]),
    ({"page": 3}, ["第 3 页"]),
    ({"query": "梯度"}, ["「梯度」"]),
])
def test_formula_get_keeps_query_and_inline(args, wants) -> None:
    """以前只写页码：既看不出在找哪个式子，也看不出要不要行内符号。"""
    brief = _checked("formula_get", args)
    for want in wants:
        assert want in brief, brief


def test_highlight_manage_shows_the_marked_text() -> None:
    """高亮的意义是「标了哪句话」：只写页码等于没说标了什么。"""
    brief = _checked("highlight_manage",
                     {"op": "create", "anchor": "md:p5", "text": "归一化指数函数的定义"})
    assert brief.startswith("新增高亮") and "第 5 页" in brief and "归一化指数函数" in brief
    # 行 id 当锚点（figure_list 回执里的 p008-02）：要翻成「第 8 页第 2 行」
    brief = _checked("highlight_manage",
                     {"op": "create", "anchor": "p008-02", "text": "softmax"})
    assert "第 8 页第 2 行" in brief


def test_highlight_manage_covers_every_op() -> None:
    """增 / 改 / 挪 / 删 / 查各说一句：用户靠这一行回看每一步动了哪条高亮。"""
    batch = [{"anchor": "md:p5", "text": "a"}, {"anchor": "md:p6", "text": "b"}]
    assert _checked("highlight_manage", {"op": "create", "items": batch}) == "新增 2 处高亮"
    assert _checked("highlight_manage",
                    {"op": "update", "highlight_id": "h-1", "color": "#0d9488"}) == "改高亮"
    assert "第 6 页" in _checked("highlight_manage",
                                 {"op": "update", "highlight_id": "h-1", "anchor": "md:p6"})
    assert _checked("highlight_manage",
                    {"op": "delete", "highlight_id": "h-1"}) == "删掉一条高亮"
    brief = _checked("highlight_manage", {"op": "list", "query": "softmax"})
    assert "高亮" in brief and "「softmax」" in brief
    assert "高亮" in _checked("highlight_manage", {"op": "locate", "highlight_id": "h-1"})


def test_note_manage_shows_where_it_hangs_and_what_it_says() -> None:
    """笔记必须挂在一条高亮上：只写「新增笔记」既看不出挂哪儿，也看不出写了什么。"""
    brief = _checked("note_manage",
                     {"op": "create", "highlight_id": "h-3", "content": "关键是把内积拆成两项"})
    assert brief.startswith("新增笔记") and "h-3" in brief and "把内积拆成两项" in brief
    batch = [{"highlight_id": "h-1", "content": "x"}, {"highlight_id": "h-2", "content": "y"}]
    assert _checked("note_manage", {"op": "create", "items": batch}) == "新增 2 条笔记"
    brief = _checked("note_manage",
                     {"op": "update", "note_id": "n-1", "content": "改写后的正文"})
    assert "「改写后的正文」" in brief
    assert _checked("note_manage", {"op": "delete", "note_id": "n-1"}) == "删掉一条笔记"
    assert "「量化误差」" in _checked("note_manage", {"op": "list", "query": "量化误差"})


def test_paper_meta_write_lists_fields_venue_and_year() -> None:
    """元数据一次写十几项：过程行要给「写了几项 + 前几项（+ 会刊年份）」，否则只剩一个 venue 词。"""
    assert _checked("paper_meta_write",
                    {"fields": ["venue", "year", "tags"]}) == "3 项 · venue、year、tags"
    brief = _checked("paper_meta_write", {"venue": "NeurIPS", "year": 2024,
                                          "tags": ["quantization"], "one_liner": "一句话"})
    assert "4 项" in brief and "venue" in brief
    assert "NeurIPS" in brief and "2024 年" in brief
    # 入参只有 fields 汇总、且是空表：什么都不该写出来
    assert _checked("paper_meta_write", {"fields": []}) == ""


@pytest.mark.parametrize("args,wants", [
    ({"op": "view", "page": 4}, ["第 4 页"]),
    ({"op": "view", "pages": "3-5"}, ["第 3-5 页"]),
    ({"op": "undo", "page": 4}, ["第 4 页", "撤回上一处"]),
    ({"op": "replace", "page": 4, "find": "旧的识别结果", "replace": "新的识别结果"},
     ["第 4 页", "「旧的识别结果」→「新的识别结果」"]),
    ({"op": "replace", "page": 4, "find": "多余的逗号"}, ["删掉「多余的逗号」"]),
])
def test_page_markdown_edit_spells_out_every_op(args, wants) -> None:
    """改页级产物是「先读后写」：三个分支都要看得出在哪一页、把什么换成了什么。"""
    brief = _checked("page_markdown_edit", args)
    for want in wants:
        assert want in brief, brief


def test_library_list_shows_paging_and_filters() -> None:
    """翻到第几页 / 每页几条 / 按哪个分类 / 只看已入库与否：这一行都要看得出来。

    以前只剩一个 query，用户从过程行看不出"这次翻页了没有、筛的是哪一档"。
    """
    brief = _checked("library_list", {"query": "量化", "page": 2, "page_size": 20,
                                      "folder": "none", "in_kb": True})
    assert brief == "「量化」 · 第 2 页 · 每页 20 · 分类 未分类 · 仅已入库"
    assert _checked("library_list", {"page": 3, "folder": "f-7",
                                     "in_kb": False}) == "第 3 页 · 分类 f-7 · 仅未入库"
    # 没传的键一个字都不占：第 1 页是噪音，不传 in_kb 就是默认口径
    assert _checked("library_list", {"query": "量化", "page": 1}) == "「量化」"


def test_list_tools_show_scope_and_limit() -> None:
    """只写 query 看不出「翻了多大范围」：条数与库范围要跟着。"""
    assert "50 条" in _checked("library_list", {"query": "量化", "limit": 50})
    brief = _checked("notes_search", {"query": "量化误差", "scope": "all", "kind": "highlights"})
    assert "全库" in brief and "高亮" in brief
    # 默认值（这一篇、笔记与高亮都要）不写，否则每一行都拖着两截没信息量的词
    assert _checked("notes_search", {"query": "量化误差"}) == "「量化误差」"
    assert _checked("notes_search",
                    {"queries": ["量化误差", "KL 散度"]}) == "「量化误差」「KL 散度」"
    assert _checked("library_list", {"query": "量化"}) == "「量化」"


def test_search_tools_show_top_k_and_count() -> None:
    """要几条是可以改的参数：改过就该看见；默认 8 条不占地方。"""
    assert "5 条" in _checked("paper_search", {"query": "随机旋转", "top_k": 5})
    assert _checked("paper_search", {"query": "随机旋转", "top_k": 8}) == "「随机旋转」"
    assert "「QJL」" in _checked("kb_search", {"queries": ["random rotation", "QJL"]})
    assert "6 条" in _checked("web_search", {"query": "TurboQuant 实现", "count": 6})


def test_web_fetch_shows_the_url() -> None:
    """抓网页要看得出抓的是哪一篇：url 太长要截，批量时给「等 N 篇」。"""
    brief = _checked("web_fetch", {"url": "https://example.com/paper"})
    assert brief.startswith("https://example.com/paper")
    brief = _checked("web_fetch", {"urls": ["https://a.example/x", "https://b.example/y"]})
    assert brief.startswith("https://a.example/x") and "等 2 篇" in brief
    # 超长 url 截断后仍要落在 120 字以内（前端只有一行）
    assert _checked("web_fetch", {"url": "https://example.com/" + "x" * 300}).endswith("…")


def test_arguments_arriving_as_json_string_still_brief_well() -> None:
    """框架实测把入参给成 JSON **串**：digest 不能因此退回成 {"pages": "3-5"} 这种原文。"""
    assert _checked("page_markdown_get", '{"pages": "3-5"}') == "第 3-5 页"
