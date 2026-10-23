"""批量读工具：一次给多条（用户口径 2026-12 —— 「能批量就别一次一条」）。

模型（也是用户）的实际用法是**一次问好几件事**：4 个检索词、6 个 ref、5 页图、3 个网址。
以前只能一条一来回，代价是回合被工具往返吃光。这里钉五件事：

1. **按条对应**：回执里每一条都带自己的 `ref` / `query` / `url` / 序号，顺序与入参一致；
2. **逐条失败**：一条坏了只在那一条上给原因，别的照常返回（不是整批失败）；
3. **硬上限**：超了截断，并在回执里写明「还有 N 条没处理」——静默丢等于骗模型；
4. **向后兼容**：老的单条调用（`ref` / `query` / `url` / `page+index`）回执结构一个字都不变；
5. **分组不串味**：多个检索词的命中按 query 分组，不混成一个数组。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from backend.agent import tools as agent_tools
from backend.agent.citations import RefRegistry
from backend.agent.deps import Deps
from backend.agent.host import HostError
from backend.agent.tools import (
    BATCH_FIGURE_MAX,
    BATCH_PAGE_MAX,
    BATCH_QUERY_MAX,
    BATCH_REF_MAX,
    BATCH_URL_MAX,
    LIBRARY_LIST_DEFAULT,
    block_get,
    figure_image_get,
    figure_list,
    kb_search,
    library_list,
    notes_search,
    paper_search,
    tool_brief,
    web_fetch,
    web_search,
)


# --------------------------------------------------------------------------
# 桩：一张能按"谁坏了"报警的假主机
# --------------------------------------------------------------------------


class _Break:
    """要坏的那几条（按 block_id / query / url / page 登记）。"""

    def __init__(self, *values: object) -> None:
        self.values = {str(v) for v in values}

    def __contains__(self, value: object) -> bool:
        return str(value) in self.values


class FakeHost:
    def __init__(self, *, broken: _Break | None = None, delay: float = 0.0) -> None:
        self.broken = broken or _Break()
        self.delay = delay
        self.calls: list[tuple] = []

    async def block(self, paper_id, block_id):
        self.calls.append(("block", block_id))
        if block_id in self.broken:
            raise HostError("主后端 HTTP 500", status=500, code="BOOM")
        return {"page": 4, "section": "2", "type": "text",
                "text": f"正文 {block_id}", "translation": None}

    async def search(self, paper_id, query, *, top_k, chunk_types=None, mode=None):
        self.calls.append(("search", query))
        if query in self.broken:
            raise HostError("检索炸了", status=500, code="BOOM")
        return {"page_total": 10,
                "items": [{"chunk_id": f"{query}-1", "block_id": f"blk-{query}",
                           "page_no": 4, "content": f"{query} 的正文",
                           "chunk_type": "text"}]}

    async def kb_search(self, query, *, top_k):
        self.calls.append(("kb", query))
        return {"items": [{"chunk_id": f"kb-{query}", "block_id": f"blk-{query}",
                           "paper_id": "p2", "paper_title": "另一篇",
                           "page_no": 3, "content": f"{query} 跨库命中"}]}

    async def notes(self, *, query=None, paper_id=None, kind="all"):
        self.calls.append(("notes", query))
        return {"items": [{"kind": "note", "paper_id": "p", "block_id": "b1",
                           "selected_text": "原文", "note": f"{query} 的笔记",
                           "note_id": "n1"}]}

    async def web_search(self, query, *, count=None, need_content=None, ori_question=""):
        self.calls.append(("web", query))
        return {"provider": "deepseek",
                "results": [{"title": f"{query} 的结果", "url": f"https://x/{query}",
                             "site_name": "Example", "summary": "摘要"}]}

    async def web_fetch(self, url, *, max_chars=None):
        self.calls.append(("fetch", url))
        if url in self.broken:
            raise HostError("抓不到", status=502, code="BOOM")
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"url": url, "title": f"{url} 的标题", "content": "正" * 10,
                "truncated": False}

    async def figures(self, paper_id, page=None):
        self.calls.append(("figures", page))
        if page in self.broken:
            raise HostError("图形区域取不到", status=500, code="BOOM")
        return {"items": [{"index": 0, "kind": "figure", "number": "1",
                           "caption": f"第 {page} 页的图注", "url": f"https://img/{page}"}]}

    async def figure_image(self, paper_id, page_no, index, **kw):
        self.calls.append(("figure_image", (page_no, index)))
        if (page_no, index) in {(int(p), int(i)) for p, i in
                                (one.split(":") for one in self.broken.values
                                 if ":" in one)}:
            return None, None
        return b"PNG", "image/png"

    async def library(self, *, q=None, status=None):
        self.calls.append(("library", q))
        return {"total": 120,
                "items": [{"paper_id": f"p{i}", "title": f"论文 {i}", "authors": "A",
                           "venue": "ICLR", "year": 2026, "page_count": 10}
                          for i in range(30)]}


def _deps(host: FakeHost) -> Deps:
    cfg = SimpleNamespace(evidence_limit=12, evidence_chars=100000,
                          image_max_pages=3, image_max_dpi=400)
    return Deps(cfg=cfg, host=host, paper_id="p", registry=RefRegistry("p"))


def _ctx(deps: Deps) -> SimpleNamespace:
    return SimpleNamespace(deps=deps)


def _cite(deps: Deps, block_id: str) -> int:
    return deps.registry.assign(key=f"k:{block_id}", kind="block", block_id=block_id,
                                page=4, snippet="片段").ref


# --------------------------------------------------------------------------
# 1. block_get(refs=[…])
# --------------------------------------------------------------------------


def test_block_get_batch_aligns_every_item_with_its_own_ref() -> None:
    host = FakeHost()
    deps = _deps(host)
    r1, r2, r3 = (_cite(deps, f"blk{n}") for n in (1, 2, 3))
    out = asyncio.run(block_get(_ctx(deps), refs=[r3, r1, r2]))
    assert [row["ref"] for row in out["items"]] == [r3, r1, r2], "回执顺序必须跟着入参"
    assert [row["text"] for row in out["items"]] == ["正文 blk3", "正文 blk1", "正文 blk2"]
    assert out["returned"] == 3 and out["failed"] == 0
    assert host.calls == [("block", "blk3"), ("block", "blk1"), ("block", "blk2")]


def test_block_get_merges_ref_and_refs_and_drops_repeats() -> None:
    deps = _deps(FakeHost())
    r1, r2 = _cite(deps, "blk1"), _cite(deps, "blk2")
    out = asyncio.run(block_get(_ctx(deps), ref=r2, refs=[r1, r2]))
    assert [row["ref"] for row in out["items"]] == [r2, r1], "ref 在前、refs 在后，重复的合并"
    assert "重复的 1 条已合并" in out["note"]


def test_block_get_single_ref_keeps_the_old_receipt() -> None:
    deps = _deps(FakeHost())
    ref = _cite(deps, "blk1")
    out = asyncio.run(block_get(_ctx(deps), ref=ref))
    assert out["text"] == "正文 blk1" and out["ref"] == ref
    assert "items" not in out, "单条调用不该被改成新结构（老调用点还在用）"


def test_block_get_batch_form_is_stable_even_for_one_ref() -> None:
    """给了 `refs` 就一律走批量形态：模型按 `refs` 下注时不该拿到另一种结构。"""
    deps = _deps(FakeHost())
    ref = _cite(deps, "blk1")
    out = asyncio.run(block_get(_ctx(deps), refs=[ref]))
    assert [row["ref"] for row in out["items"]] == [ref] and out["returned"] == 1


def test_block_get_one_broken_ref_does_not_sink_the_batch() -> None:
    deps = _deps(FakeHost(broken=_Break("blk2")))
    r1, r2, r3 = (_cite(deps, f"blk{n}") for n in (1, 2, 3))
    out = asyncio.run(block_get(_ctx(deps), refs=[r1, r2, r3]))
    assert out["returned"] == 2 and out["failed"] == 1
    bad = next(row for row in out["items"] if row.get("ok") is False)
    assert bad["ref"] == r2 and "取数失败" in bad["error"]
    assert [row["ref"] for row in out["items"] if row.get("ok") is not False] == [r1, r3]


def test_block_get_caps_the_batch_and_says_how_many_are_left() -> None:
    deps = _deps(FakeHost())
    refs = [_cite(deps, f"blk{n}") for n in range(BATCH_REF_MAX + 3)]
    out = asyncio.run(block_get(_ctx(deps), refs=refs))
    assert out["returned"] == BATCH_REF_MAX
    assert out["unprocessed"] == 3 and "还有 3 条没处理" in out["note"]


def test_block_get_without_any_ref_explains_the_batch_form() -> None:
    out = asyncio.run(block_get(_ctx(_deps(FakeHost()))))
    assert out["ok"] is False and "refs=" in out["error"]


# --------------------------------------------------------------------------
# 2. paper_search(queries=[…])：按 query 分组，不混成一锅
# --------------------------------------------------------------------------


def test_paper_search_groups_results_per_query() -> None:
    host = FakeHost()
    deps = _deps(host)
    out = asyncio.run(paper_search(_ctx(deps), queries=["旋转", "量化"]))
    assert len(out["groups"]) == 2, "分组数必须等于输入的词数"
    assert [g["query"] for g in out["groups"]] == ["旋转", "量化"]
    assert [g["items"][0]["snippet"] for g in out["groups"]] == ["旋转 的正文", "量化 的正文"]
    assert out["returned"] == 2 and out["failed"] == 0


def test_paper_search_single_query_keeps_the_old_receipt() -> None:
    out = asyncio.run(paper_search(_ctx(_deps(FakeHost())), query="旋转"))
    assert out["query"] == "旋转" and "groups" not in out
    assert out["items"][0]["snippet"] == "旋转 的正文"


def test_paper_search_one_broken_query_keeps_the_others() -> None:
    deps = _deps(FakeHost(broken=_Break("量化")))
    out = asyncio.run(paper_search(_ctx(deps), queries=["旋转", "量化", "残差"]))
    assert out["failed"] == 1 and out["returned"] == 2
    bad = next(g for g in out["groups"] if g.get("ok") is False)
    assert bad["query"] == "量化" and "取数失败" in bad["error"]


def test_paper_search_caps_queries_and_reports_the_rest() -> None:
    deps = _deps(FakeHost())
    words = [f"词{i}" for i in range(BATCH_QUERY_MAX + 2)]
    out = asyncio.run(paper_search(_ctx(deps), queries=words))
    assert len(out["groups"]) == BATCH_QUERY_MAX
    assert out["unprocessed"] == 2 and "还有 2 条没处理" in out["note"]


def test_kb_search_and_notes_search_group_the_same_way() -> None:
    deps = _deps(FakeHost())
    kb = asyncio.run(kb_search(_ctx(deps), queries=["旋转", "量化"]))
    assert [g["query"] for g in kb["groups"]] == ["旋转", "量化"]
    assert kb["groups"][0]["items"][0]["paper_id"] == "p2"
    notes = asyncio.run(notes_search(_ctx(deps), queries=["旋转", "量化"]))
    assert [g["query"] for g in notes["groups"]] == ["旋转", "量化"]
    assert notes["groups"][0]["items"][0]["note"] == "旋转 的笔记"


def test_notes_search_without_query_still_works() -> None:
    out = asyncio.run(notes_search(_ctx(_deps(FakeHost()))))
    assert out["items"], "不给关键词是合法用法：看最近的那些"


# --------------------------------------------------------------------------
# 3. web_search(queries=[…]) / web_fetch(urls=[…])
# --------------------------------------------------------------------------


def test_web_search_groups_per_query() -> None:
    out = asyncio.run(web_search(_ctx(_deps(FakeHost())),
                                 queries=["site:arxiv.org A", "B"]))
    assert [g["query"] for g in out["groups"]] == ["site:arxiv.org A", "B"]
    assert out["groups"][1]["items"][0]["title"] == "B 的结果"


def test_web_fetch_batch_runs_concurrently() -> None:
    host = FakeHost(delay=0.1)
    deps = _deps(host)
    started = time.perf_counter()
    out = asyncio.run(web_fetch(_ctx(deps), urls=[f"https://x/{n}" for n in range(3)]))
    spent = time.perf_counter() - started
    assert out["returned"] == 3
    assert spent < 0.28, f"3 篇应当并发（各 0.1s）而不是串行 0.3s+，实测 {spent:.2f}s"
    assert [row["url"] for row in out["items"]] == [f"https://x/{n}" for n in range(3)]


def test_web_fetch_one_broken_url_keeps_the_rest() -> None:
    deps = _deps(FakeHost(broken=_Break("https://x/1")))
    out = asyncio.run(web_fetch(_ctx(deps), urls=["https://x/0", "https://x/1"]))
    assert out["returned"] == 1 and out["failed"] == 1
    bad = out["items"][1]
    assert bad["url"] == "https://x/1" and "取数失败" in bad["error"]
    assert out["items"][0]["title"] == "https://x/0 的标题", "别的条照常返回"


def test_web_fetch_batch_times_out_one_url_without_blocking_the_batch(monkeypatch) -> None:
    monkeypatch.setattr(agent_tools, "WEB_FETCH_TIMEOUT", 0.05)
    deps = _deps(FakeHost(delay=0.3))
    out = asyncio.run(web_fetch(_ctx(deps), urls=["https://x/0", "https://x/1"]))
    assert out["failed"] == 2
    assert all("超时" in row["error"] for row in out["items"])


def test_web_fetch_caps_urls_and_reports_the_rest() -> None:
    deps = _deps(FakeHost())
    out = asyncio.run(web_fetch(_ctx(deps), urls=[f"https://x/{n}"
                                                  for n in range(BATCH_URL_MAX + 2)]))
    assert out["returned"] == BATCH_URL_MAX
    assert out["unprocessed"] == 2 and "还有 2 条没处理" in out["note"]


def test_web_fetch_single_url_keeps_the_old_receipt() -> None:
    out = asyncio.run(web_fetch(_ctx(_deps(FakeHost())), url="https://x/0"))
    assert out["title"] == "https://x/0 的标题" and "items" not in out


# --------------------------------------------------------------------------
# 4. figure_list(pages=[…]) / figure_image_get(items=[…])
# --------------------------------------------------------------------------


def test_figure_list_groups_by_page_and_isolates_a_broken_page() -> None:
    deps = _deps(FakeHost(broken=_Break(7)))
    out = asyncio.run(figure_list(_ctx(deps), pages=[4, 7]))
    assert [g["page"] for g in out["pages"]] == [4, 7]
    assert out["pages"][0]["items"][0]["caption"] == "第 4 页的图注"
    assert out["pages"][1]["ok"] is False and "取数失败" in out["pages"][1]["error"]
    assert out["returned"] == 1 and out["failed"] == 1


def test_figure_list_single_page_keeps_the_old_receipt() -> None:
    out = asyncio.run(figure_list(_ctx(_deps(FakeHost())), page=4))
    assert out["page"] == 4 and "pages" not in out and out["items"][0]["index"] == 0


def test_figure_list_caps_pages() -> None:
    out = asyncio.run(figure_list(_ctx(_deps(FakeHost())),
                                  pages=list(range(1, BATCH_PAGE_MAX + 3))))
    assert len(out["pages"]) == BATCH_PAGE_MAX and out["unprocessed"] == 2


def test_figure_image_get_batch_pairs_pages_and_indices() -> None:
    deps = _deps(FakeHost())
    out = asyncio.run(figure_image_get(_ctx(deps), items=[
        {"page": 4, "index": 0}, {"page": 7, "index": 2}]))
    note = out.return_value
    assert "已附上 2 个区域" in note
    assert "1. 第 4 页 · 图 1（ref=" in note and "2. 第 7 页 · 图 #2（ref=" in note
    assert len(out.content) == 3, "一条说明 + 两张图"


def test_figure_image_get_flags_the_missing_region_only() -> None:
    deps = _deps(FakeHost(broken=_Break("7:2")))
    out = asyncio.run(figure_image_get(_ctx(deps), items=[
        {"page": 4, "index": 0}, {"page": 7, "index": 2}]))
    assert "没取到：取不到这个区域的裁图" in out.return_value
    assert len(out.content) == 2, "只附上成功的那一张"


def test_figure_image_get_single_keeps_the_old_return() -> None:
    out = asyncio.run(figure_image_get(_ctx(_deps(FakeHost())), page=4, index=0))
    assert out.return_value.startswith("已附上第 4 页的图 1")
    assert len(out.content) == 2


def test_figure_image_get_caps_regions() -> None:
    items = [{"page": 1, "index": n} for n in range(BATCH_FIGURE_MAX + 2)]
    out = asyncio.run(figure_image_get(_ctx(_deps(FakeHost())), items=items))
    assert "还有 2 条没处理" in out.return_value


# --------------------------------------------------------------------------
# 5. library_list：条数上限 + 「还有 N 篇没列」
# --------------------------------------------------------------------------


def test_library_list_reports_what_it_could_not_list() -> None:
    out = asyncio.run(library_list(_ctx(_deps(FakeHost()))))
    assert out["returned"] == LIBRARY_LIST_DEFAULT and out["total"] == 120
    assert out["unprocessed"] == 120 - LIBRARY_LIST_DEFAULT
    assert f"还有 {120 - LIBRARY_LIST_DEFAULT} 篇没列" in out["note"]


def test_library_list_limit_narrows_the_list() -> None:
    out = asyncio.run(library_list(_ctx(_deps(FakeHost())), limit=2))
    assert out["returned"] == 2 and out["unprocessed"] == 118


# --------------------------------------------------------------------------
# 6. 过程行（tool_brief）也要看得出"这是批量"
# --------------------------------------------------------------------------


def test_tool_brief_shows_the_batch_arguments() -> None:
    assert tool_brief("block_get", {"refs": [3, 5, 9]}) == "引用 [3、5、9]"
    assert tool_brief("block_get", {"ref": 7}) == "引用 [7]"
    assert "等 2 篇" in tool_brief("web_fetch", {"urls": ["https://a", "https://b"]})
    assert "「A」「B」" in tool_brief("paper_search", {"queries": ["A", "B"]})
    assert "2 个区域" in tool_brief("figure_image_get",
                                    {"items": [{"page": 4, "index": 0},
                                               {"page": 7, "index": 1}]})
    assert tool_brief("figure_list", {"pages": [4, 7]}) == "第 4、7 页"


# --------------------------------------------------------------------------
# 7. `notes_search` 的两个参数是**闭集**：拼错必须报错，不能静默降级
# --------------------------------------------------------------------------


def test_notes_search_rejects_an_unknown_scope_and_echoes_the_effective_one() -> None:
    """`scope="library"` 以前被当成 `paper`（只有本篇），模型于是断言"你库里没有别的论文讲过"。"""
    host = FakeHost()
    out = asyncio.run(notes_search(_ctx(_deps(host)), query="旋转", scope="library"))
    assert out["ok"] is False
    assert "scope='library'" in out["error"]
    assert "paper/all" in out["error"], "要把允许值列出来，模型才知道该怎么写"
    assert out["effective_scope"] == "paper", "回显旧实现会当成什么（这句话就是错的来源）"
    assert out["effective_kind"] == "all"
    assert host.calls == [], "非法取值不许打后端：查了也是白查"


def test_notes_search_rejects_an_unknown_kind() -> None:
    """`kind="note"`（漏了 s）以前被当成 `all` —— 模型以为只要笔记，回执里全是高亮。"""
    out = asyncio.run(notes_search(_ctx(_deps(FakeHost())), query="旋转", kind="note"))
    assert out["ok"] is False
    assert "kind='note'" in out["error"]
    assert "all/notes/highlights/annotations" in out["error"]
    assert out["effective_kind"] == "all"


def test_notes_search_normalizes_case_and_still_works() -> None:
    """闭集校验不能把正常写法挡回去：大小写归一后照常查。"""
    host = FakeHost()
    out = asyncio.run(notes_search(_ctx(_deps(host)), query="旋转", scope="ALL",
                                   kind="Annotations"))
    assert "ok" not in out, out
    assert out["scope"] == "all" and out["kind"] == "annotations"
    assert host.calls[-1] == ("notes", "旋转"), "scope=all 必须真的打一次后端"
