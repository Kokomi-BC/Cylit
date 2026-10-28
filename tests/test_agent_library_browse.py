"""论文目录浏览（library_list）的真链路回归：工具 → HostClient.library → 主后端 → 库。

钉住的约定：

1. 分页在**数据层**：每页由 GET /papers 按 page/page_size 切好再下发，不是"先取全库再切片"；
2. 回执向后兼容：items / total 还在，新增 page / page_size / total_pages / has_next /
   has_prev / categories / page_info；
3. 分类与计数走 folder_service（一篇论文只属于一个分类），"未分类"也看得见；
4. 非法 / 越界页码钳到合法值并**如实**回 page，不 500、不回空页；
5. 排序稳定：last_read_at 全都相同的 5 篇伪论文也要有确定次序（tie-breaker = paper.id），
   逐页取完不重不漏；
6. 条目只给元数据，摘要最多 200 字（没有正文）；
7. 默认只列**已入库**（in_knowledge_base=1）的论文，被挡掉的篇数如实回在 hidden_not_in_kb；
   显式 in_kb=false 才列未入库的那批；in_kb 不传（None）= 旧行为，响应逐字段不变；
   分类计数是全库口径（categories_scope="all_papers"），不跟着 in_kb 变。

数据隔离：论文用 __test_lib_p*__ 伪 id、分类用带随机后缀的名字，跑前跑后直接删行，
不碰真实论文与真实分类。链路是真的：HostClient 用 httpx.ASGITransport 接到
backend.main.app（与 TestClient 同一个 ASGI 引擎，见 tests/test_agent_meta_write.py）。
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from backend.agent import tools as agent_tools
from backend.agent.citations import RefRegistry
from backend.agent.deps import Deps
from backend.agent.host import HostClient
from backend.agent.tools import LIBRARY_PAGE_DEFAULT, LIBRARY_PAGE_MAX, library_list

pytestmark = pytest.mark.e2e

#: 本文件全部伪论文标题都带它，查询时用 query= 把断言范围收在这 5 篇上
MARK = "zzlibt"
FOLDER_A = "__test_lib_folder_a__"
FOLDER_B = "__test_lib_folder_b__"
PAPERS = [f"__test_lib_p{i}__" for i in range(1, 6)]


# --------------------------------------------------------------------------
# 数据
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_ready():
    from backend.repositories.mysql.session import healthcheck

    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()
    return True


@pytest.fixture
def library(db_ready):
    """5 篇伪论文（分类 A 两篇、分类 B 一篇、未分类两篇）+ 两个伪分类，跑完清掉。

    last_read_at **故意全部相同**：默认排序（last_read_at desc）对它分不出先后，
    次序只能来自 tie-breaker（paper.id 升序）—— 跨页不重不漏正是靠它。

    这 5 篇**全部标成已入库**：本夹具钉的是分页 / 排序 / 分类计数，工具默认口径
    （只列已入库）不该在这里掺一脚；in_kb 过滤本身由下面的 mixed 造数覆盖。
    """
    from backend.repositories.mysql.models import Paper, PaperFolder
    from backend.repositories.mysql.session import session_scope

    def wipe() -> None:
        with session_scope() as sess:
            sess.query(Paper).filter(Paper.id.in_(PAPERS)).delete(synchronize_session=False)
            sess.query(PaperFolder).filter(
                PaperFolder.id.in_((FOLDER_A, FOLDER_B))).delete(synchronize_session=False)

    wipe()
    stamp = datetime(2026, 1, 1, 12, 0, 0)
    names = {FOLDER_A: f"zz-library-test-a-{uuid.uuid4().hex[:8]}",
             FOLDER_B: f"zz-library-test-b-{uuid.uuid4().hex[:8]}"}

    def row(i: int, title: str, folder_id, status: str):
        return Paper(id=PAPERS[i - 1], title=title, authors="作者甲, 作者乙",
                     venue="ICLR", year=2026, page_count=10, status=status,
                     folder_id=folder_id, last_read_at=stamp, created_at=stamp,
                     in_knowledge_base=True)

    rows = [row(1, f"{MARK} 量化误差 与下界", FOLDER_A, "parsed"),
            row(2, f"{MARK} 随机旋转", FOLDER_A, "parsed"),
            row(3, f"{MARK} 量化误差 复现", FOLDER_B, "parsed"),
            row(4, f"{MARK} 无关标题", None, "imported"),
            row(5, f"{MARK} 另一篇", None, "imported")]
    rows[1].abstract = "摘" * 600        # 回执里必须被截断（<= 200 字）
    with session_scope() as sess:
        sess.add(PaperFolder(id=FOLDER_A, name=names[FOLDER_A], sort=901))
        sess.add(PaperFolder(id=FOLDER_B, name=names[FOLDER_B], sort=902))
        for one in rows:
            sess.add(one)
    try:
        yield {"names": names, "ids": list(PAPERS)}
    finally:
        wipe()


#: 已入库 / 未入库两种口径的造数（11 篇未删里只有 3 篇 in_knowledge_base=1 —— 真库的形状）
MIX_MARK = "zzlibmix"
FOLDER_C = "__test_lib_folder_c__"
MIX_PAPERS = [f"__test_lib_mix{i:02d}__" for i in range(1, 12)]
MIX_IN_KB = MIX_PAPERS[:3]


@pytest.fixture
def mixed(db_ready):
    """11 篇伪论文：3 篇已入库、8 篇未入库；分类 C 装了其中 4 篇（3 入库 + 1 未入库）。

    这就是真库的形状（11 篇未删里只有 3 篇满足 in_knowledge_base=1）：用来钉住
    "默认只列已入库"与"把被挡掉的篇数如实回出来"，以及分类计数不跟着口径变。
    """
    from backend.repositories.mysql.models import Paper, PaperFolder
    from backend.repositories.mysql.session import session_scope

    def wipe() -> None:
        with session_scope() as sess:
            sess.query(Paper).filter(Paper.id.in_(MIX_PAPERS)).delete(synchronize_session=False)
            sess.query(PaperFolder).filter(
                PaperFolder.id == FOLDER_C).delete(synchronize_session=False)

    wipe()
    stamp = datetime(2026, 2, 2, 12, 0, 0)
    name_c = f"zz-library-test-c-{uuid.uuid4().hex[:8]}"
    rows = [Paper(id=paper_id, title=f"{MIX_MARK} 论文 {i:02d}", authors="作者丙",
                  venue="NeurIPS", year=2026, page_count=8, status="parsed",
                  last_read_at=stamp, created_at=stamp,
                  folder_id=FOLDER_C if i <= 4 else None,
                  in_knowledge_base=paper_id in MIX_IN_KB)
            for i, paper_id in enumerate(MIX_PAPERS, start=1)]
    with session_scope() as sess:
        sess.add(PaperFolder(id=FOLDER_C, name=name_c, sort=903))
        for one in rows:
            sess.add(one)
    try:
        yield {"ids": list(MIX_PAPERS), "in_kb": list(MIX_IN_KB), "folder_c": name_c}
    finally:
        wipe()


# --------------------------------------------------------------------------
# 调用器：一次调用 = 一个事件循环 + 一条真 ASGI 连接（用完就关）
# --------------------------------------------------------------------------


class _Tool:
    """httpx 的连接池与事件循环绑定，所以一条用例里每个调用各起一个循环。"""

    def __call__(self, **kwargs):
        async def run():
            from backend import main

            cfg = SimpleNamespace(host_api="http://test/api/v1", token="test-token",
                                  host_timeout=30.0)
            host = HostClient(cfg)                     # type: ignore[arg-type]
            host._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url=cfg.host_api,
                headers={"X-Internal-Token": "test-token"}, timeout=30.0)
            deps = Deps(cfg=SimpleNamespace(evidence_limit=12, evidence_chars=100000),
                        host=host, paper_id=PAPERS[0], registry=RefRegistry(PAPERS[0]))
            try:
                out = await library_list(SimpleNamespace(deps=deps), **kwargs)
            finally:
                await host.aclose()
            return out, deps

        return asyncio.run(run())


def _ids(out: dict) -> list[str]:
    return [str(one["paper_id"]) for one in out["items"]]


def _host_library(**kwargs) -> dict:
    """绕过工具直接问主后端（GET /papers）：这是**旧调用方**看到的那份响应。

    与 _Tool 同一条真链路（ASGITransport → backend.main.app），只是不经过工具层。
    """
    async def run() -> dict:
        from backend import main

        cfg = SimpleNamespace(host_api="http://test/api/v1", token="test-token",
                              host_timeout=30.0)
        host = HostClient(cfg)                     # type: ignore[arg-type]
        host._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url=cfg.host_api,
            headers={"X-Internal-Token": "test-token"}, timeout=30.0)
        try:
            return await host.library(**kwargs)
        finally:
            await host.aclose()

    return asyncio.run(run())


# --------------------------------------------------------------------------
# 1. 正常列论文 + 条目只给元数据
# --------------------------------------------------------------------------


def test_first_page_carries_metadata_and_paging(library) -> None:
    out, _ = _Tool()(query=MARK)
    assert out["total"] == 5 and out["returned"] == 5, out
    assert (out["page"], out["page_size"], out["total_pages"]) == (1, LIBRARY_PAGE_DEFAULT, 1)
    assert out["has_next"] is False and out["has_prev"] is False
    assert "第 1/1 页" in out["page_info"], out["page_info"]
    # 默认口径就是"只列已入库"；这批全在库里，所以被挡掉的是 0（0 也要给出来）
    assert out["scope"] == "in_knowledge_base" and out["hidden_not_in_kb"] == 0, out
    assert out["categories_scope"] == "all_papers"

    first = next(one for one in out["items"] if one["paper_id"] == PAPERS[0])
    for key in ("ref", "paper_id", "title", "authors", "year", "folder_id",
                "folder_name", "status", "in_knowledge_base"):
        assert key in first, (key, first)
    assert first["folder_name"] == library["names"][FOLDER_A]
    assert first["in_knowledge_base"] is True
    # status 会被"以磁盘为准"校正（这批伪论文没有产物，所以是 imported），这里只要求给出来
    assert isinstance(first["status"], str) and first["status"]


def test_items_never_carry_full_text(library) -> None:
    out, _ = _Tool()(query=MARK)
    row = next(one for one in out["items"] if one["paper_id"] == PAPERS[1])
    assert 0 < len(row["abstract"]) <= 200, "摘要只能是截断版（不超过 200 字）"
    assert "content" not in row and "text" not in row
    # 没写摘要的那几篇不凭空给一个字段
    assert "abstract" not in next(one for one in out["items"]
                                  if one["paper_id"] == PAPERS[0])


# --------------------------------------------------------------------------
# 2. 分类与数量（走 folder_service 的真实关系）
# --------------------------------------------------------------------------


def test_categories_carry_real_counts(library) -> None:
    out, _ = _Tool()(query=MARK)
    cats = {one["id"]: one for one in out["categories"]}
    assert cats[FOLDER_A]["paper_count"] == 2, cats          # 一篇只算一个分类
    assert cats[FOLDER_B]["paper_count"] == 1
    assert cats[FOLDER_A]["name"] == library["names"][FOLDER_A]
    assert cats["none"]["name"] == "未分类"
    # 未分类是全库口径：本用例至少贡献了 2 篇
    assert cats["none"]["paper_count"] >= 2


# --------------------------------------------------------------------------
# 3. 关键词搜索 / 4. 分类筛选
# --------------------------------------------------------------------------


def test_keyword_search_narrows_the_total(library) -> None:
    out, _ = _Tool()(query=f"{MARK} 量化误差")
    assert out["total"] == 2, out
    assert set(_ids(out)) == {PAPERS[0], PAPERS[2]}


def test_folder_filter_and_unclassified(library) -> None:
    in_a, _ = _Tool()(folder=FOLDER_A)
    assert in_a["total"] == 2
    assert {one["folder_id"] for one in in_a["items"]} == {FOLDER_A}
    assert {one["folder_name"] for one in in_a["items"]} == {library["names"][FOLDER_A]}

    loose, _ = _Tool()(folder="none", query=MARK)
    assert set(_ids(loose)) == {PAPERS[3], PAPERS[4]}
    assert all(one["folder_id"] is None for one in loose["items"])
    assert loose["items"][0]["folder_name"] == "未分类"


# --------------------------------------------------------------------------
# 5. 分页元数据 / 6. 下一页 · 上一页 · 指定页
# --------------------------------------------------------------------------


def test_total_pages_and_flags(library) -> None:
    out, _ = _Tool()(query=MARK, page_size=2)
    assert (out["total"], out["page_size"], out["total_pages"]) == (5, 2, 3)
    assert out["returned"] == 2 and out["page"] == 1
    assert out["has_next"] is True and out["has_prev"] is False


def test_next_prev_and_jump(library) -> None:
    page1, _ = _Tool()(query=MARK, page_size=2)
    page2, _ = _Tool()(query=MARK, page_size=2, page=2)
    page3, _ = _Tool()(query=MARK, page_size=2, page=3)
    assert [page1["page"], page2["page"], page3["page"]] == [1, 2, 3]
    assert page2["has_prev"] is True and page2["has_next"] is True
    assert page3["returned"] == 1 and page3["has_next"] is False

    jumped, _ = _Tool()(query=MARK, page_size=2, page=99)     # 越界：钳到最后一页
    assert jumped["page"] == 3 and jumped["returned"] == 1
    assert _ids(jumped) == _ids(page3)


# --------------------------------------------------------------------------
# 7. 空结果 + 非法页码
# --------------------------------------------------------------------------


def test_empty_result_is_empty_not_an_error(library) -> None:
    out, _ = _Tool()(query="zzlibt-库里绝对没有这个词-9f2c")
    assert out["total"] == 0 and out["items"] == []
    assert (out["page"], out["total_pages"]) == (1, 0)
    assert out["has_next"] is False and out["has_prev"] is False
    assert out.get("hint")


def test_illegal_page_and_page_size_are_clamped(library) -> None:
    for bad in ({"page": 0}, {"page": -7}, {"page_size": 0}, {"page_size": -3},
                {"page_size": LIBRARY_PAGE_MAX + 999}, {"page": "x", "page_size": "y"}):
        out, _ = _Tool()(query=MARK, **bad)
        assert out.get("ok", True) is not False, (bad, out)
        assert out["page"] >= 1 and 1 <= out["page_size"] <= LIBRARY_PAGE_MAX, (bad, out)
        assert out["total"] == 5, (bad, out)
    capped, _ = _Tool()(query=MARK, page_size=LIBRARY_PAGE_MAX + 999)
    assert capped["page_size"] == LIBRARY_PAGE_MAX


# --------------------------------------------------------------------------
# 8. 稳定排序：逐页取完不重不漏
# --------------------------------------------------------------------------


def test_pages_walked_one_by_one_match_the_whole_list(library) -> None:
    whole, _ = _Tool()(query=MARK, page_size=LIBRARY_PAGE_MAX)
    walked: list[str] = []
    for page in range(1, 6):
        out, _ = _Tool()(query=MARK, page_size=1, page=page)
        assert out["returned"] == 1, (page, out)
        walked.extend(_ids(out))
    assert walked == _ids(whole), "翻页次序必须与一次取全一致（tie-breaker = paper.id）"
    assert len(set(walked)) == 5, "不许重复"
    assert walked == sorted(walked), walked


# --------------------------------------------------------------------------
# 9. 契约：默认值只有一处口径 + 宿主桥真的收得到
# --------------------------------------------------------------------------


def test_page_defaults_agree_between_tool_and_bridge(library) -> None:
    bridge = inspect.signature(HostClient.library).parameters
    assert bridge["page"].default == 1
    assert bridge["page_size"].default == LIBRARY_PAGE_DEFAULT == 20
    assert LIBRARY_PAGE_MAX == 50
    tool = inspect.signature(agent_tools.library_list).parameters
    assert tool["page"].default == 1 and tool["page_size"].default == LIBRARY_PAGE_DEFAULT
    # 默认值只有一处口径：工具默认只列已入库，桥接层不替它决定（None = 不过滤）
    assert tool["in_kb"].default is True and bridge["in_kb"].default is None
    assert {"query", "status", "folder", "page", "page_size"} <= set(tool),         "旧参数（query / status / limit）之外还要收得到 folder / page / page_size"


def test_trace_digest_carries_page_and_folder(library) -> None:
    out, deps = _Tool()(query=MARK, folder=FOLDER_A, page=2, page_size=2)
    last = deps.trace[-1]
    assert last["tool"] == "library_list" and last["status"] == "ok", last
    assert last["args"]["page"] == 2 and last["args"]["folder"] == FOLDER_A, last
    assert out["page"] == 1, "只有 2 篇、每页 2 篇时，请求的第 2 页要被钳回第 1 页"


# --------------------------------------------------------------------------
# 10. 只列已入库（默认）：被挡掉多少必须看得出来
# --------------------------------------------------------------------------


def test_default_scope_lists_only_knowledge_base(mixed) -> None:
    """① 默认只列已入库的 3 篇；另外 8 篇未入库的篇数如实回在 hidden_not_in_kb。"""
    out, _ = _Tool()(query=MIX_MARK)
    assert out["scope"] == "in_knowledge_base", out
    assert out["total"] == 3 and out["returned"] == 3, out
    assert out["hidden_not_in_kb"] == 8, out
    assert out["categories_scope"] == "all_papers"
    assert set(_ids(out)) == set(mixed["in_kb"])
    assert all(one["in_knowledge_base"] is True for one in out["items"])
    # 分页只在过滤后的集合上算：8 篇未入库不该把页数撑成 3 页
    assert (out["page"], out["total_pages"], out["has_next"]) == (1, 1, False)
    assert "共 3 篇" in out["page_info"], out["page_info"]


def test_in_kb_false_lists_papers_not_in_knowledge_base(mixed) -> None:
    """② 显式 in_kb=false 才列未入库的那批（还在书架，只是不在知识库里）。"""
    out, _ = _Tool()(query=MIX_MARK, in_kb=False)
    assert out["scope"] == "not_in_knowledge_base", out
    assert out["total"] == 8 and out["returned"] == 8, out
    assert out["hidden_not_in_kb"] == 3, out
    assert set(_ids(out)) == set(mixed["ids"]) - set(mixed["in_kb"])
    assert all(one["in_knowledge_base"] is False for one in out["items"])


def test_in_kb_none_keeps_the_old_response(mixed) -> None:
    """③ 不传 in_kb（None）= 旧行为：响应逐字段不变，也不多出 total_unfiltered / hidden。"""
    from backend.services.paper import paper_service

    old = paper_service.list_papers(q=MIX_MARK)
    assert set(old) == {"items", "total", "page", "page_size"}, old.keys()
    assert old["total"] == 11
    # 显式传 None 与根本不传完全一致（同一份响应，逐字段）
    assert paper_service.list_papers(q=MIX_MARK, in_kb=None) == old
    # 旧调用方看到的 HTTP 响应也没多出 in_kb / total_unfiltered
    bridge = _host_library(q=MIX_MARK)
    assert set(bridge) == {"items", "total", "page", "page_size", "categories"}, bridge.keys()
    assert bridge["total"] == 11
    # 只有真过滤时，同一个接口才带上 total_unfiltered
    filtered = _host_library(q=MIX_MARK, in_kb=True)
    assert (filtered["total"], filtered["total_unfiltered"]) == (3, 11), filtered
    # 工具层 in_kb=None → scope=all，且拿不到 total_unfiltered 就不编这个键
    out, _ = _Tool()(query=MIX_MARK, in_kb=None)
    assert out["scope"] == "all", out
    assert "hidden_not_in_kb" not in out, out
    assert out["total"] == 11 and out["returned"] == 11
    assert (out["total_pages"], out["has_next"]) == (1, False)


def test_category_counts_do_not_follow_in_kb(mixed) -> None:
    """④ 分类计数是全库口径：同一个 folder 在两种口径下计数相同，变的是 total。"""
    def counts(out: dict) -> dict:
        return {one["id"]: (one["name"], one["paper_count"]) for one in out["categories"]}

    only_kb, _ = _Tool()(folder=FOLDER_C)
    not_kb, _ = _Tool()(folder=FOLDER_C, in_kb=False)
    every, _ = _Tool()(folder=FOLDER_C, in_kb=None)
    assert (only_kb["total"], not_kb["total"], every["total"]) == (3, 1, 4)
    assert counts(only_kb)[FOLDER_C] == counts(not_kb)[FOLDER_C] == counts(every)[FOLDER_C]
    assert counts(only_kb)[FOLDER_C] == (mixed["folder_c"], 4), counts(only_kb)


def test_empty_after_filtering_says_so_instead_of_no_papers(mixed) -> None:
    """被 in_kb 全部挡掉时，hint 要说"未入库"而不是"库里没有"。"""
    out, _ = _Tool()(query=f"{MIX_MARK} 论文 09")      # 只剩一篇未入库的
    assert out["total"] == 0 and out["items"] == [], out
    assert out["hidden_not_in_kb"] == 1, out
    assert "入库" in out["hint"] and "in_kb=false" in out["hint"], out["hint"]
