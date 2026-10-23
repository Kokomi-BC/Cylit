"""P0-1 回归：`paper_meta_write` 真的能把字段写进库（不是"方法存在"就够了）。

2026-12 的故障形状：`backend/agent/host.py` 里 `# ---- 写：论文元数据 ----` 这行标题把
紧随其后的 `async def meta_write(...)` **吞成了注释的一部分**。于是 `HostClient` 没有这个方法，
而 `tools.paper_meta_write` 照旧调它 → 每次「AI 补论文元数据」都 AttributeError，一个字段也写不进去。
源码里签名看得见、`api/agent.py` 接口是好的、`prompts` 还要求模型"最后调一次 paper_meta_write"——
三处都在正常工作，所以没人发现写口根本不存在。

这条用例走**真链路**：`paper_meta_write` → `HostClient.meta_write` → `POST /api/v1/agent/papers/{id}/metadata`
（FastAPI 的 ASGI 传输，与 TestClient 同一个引擎）→ `metadata_fill.apply_fills` → MySQL，
然后直接读库确认字段真的变了。跑完把测试论文删掉。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from backend.agent import tools as agent_tools
from backend.agent.citations import RefRegistry
from backend.agent.deps import Deps
from backend.agent.host import HostClient

#: 独立伪论文 id：跑完即删，不碰真实论文
PAPER = "__test_agent_meta_write__"


@pytest.fixture
def db_ready():
    from backend.repositories.mysql.session import healthcheck

    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    return True


@pytest.fixture
def scratch_paper(db_ready):
    """建一篇"元数据全空"的临时论文（元数据写口只**填空字段**，所以必须空着）。"""
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope

    def wipe() -> None:
        with session_scope() as sess:
            row = sess.get(Paper, PAPER)
            if row is not None:
                sess.delete(row)

    wipe()
    with session_scope() as sess:
        sess.add(Paper(id=PAPER, title="契约测试：元数据写口", page_count=1,
                       status="imported"))
    try:
        yield PAPER
    finally:
        wipe()


def _read_row() -> dict:
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Paper, PAPER)
        return {"venue": row.venue, "year": row.year,
                "one_liner": row.one_liner, "domain": row.domain}


def _host_through_the_real_app() -> HostClient:
    """把 `HostClient` 接到 FastAPI 应用上（与 TestClient 同一个 ASGI 引擎）。"""
    from backend import main
    from backend.core.config import get_settings

    settings = get_settings()
    token = settings.agent_token()
    if not token:
        pytest.skip("没有内部令牌（data/secrets/agent.token），跳过真链路用例")
    cfg = SimpleNamespace(host_api=f"http://test{settings.api_prefix}",
                          token=token, host_timeout=30.0)
    client = HostClient(cfg)   # type: ignore[arg-type]
    client._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url=cfg.host_api, headers={"X-Internal-Token": token}, timeout=30.0)
    return client


def _deps(host: HostClient) -> Deps:
    return Deps(cfg=SimpleNamespace(max_rounds=10, deadline_seconds=60.0),   # type: ignore[arg-type]
                host=host, paper_id=PAPER, registry=RefRegistry(PAPER),
                page_total=1, title="契约测试：元数据写口")


def test_meta_write_lands_in_the_database(scratch_paper) -> None:
    """真打一次写口：字段必须**落到库里**（不是"返回 ok 就算数"）。"""
    host = _host_through_the_real_app()
    deps = _deps(host)
    before = _read_row()
    assert before["venue"] is None and before["one_liner"] is None, "临时论文一开始必须是空的"

    try:
        out = asyncio.run(agent_tools.paper_meta_write(
            SimpleNamespace(deps=deps), venue="契约测试会议 ICLR", year=2026,
            one_liner="一句话：这是元数据写口的回归用例"))
    finally:
        asyncio.run(host.aclose())

    assert out.get("ok") is True, out
    assert "venue" in (out.get("applied") or {}), out
    after = _read_row()
    assert after["venue"] == "契约测试会议 ICLR", f"库里没写进去：{after}"
    assert after["year"] == 2026, after
    assert after["one_liner"] == "一句话：这是元数据写口的回归用例", after
    assert deps.meta_submitted is True


def test_second_submission_is_refused_with_the_truth(scratch_paper) -> None:
    """P1-1：提交过之后**不能再改**，而且文案必须与实现一致（旧文案叫人重发，重发必被拒）。"""
    host = _host_through_the_real_app()
    deps = _deps(host)
    ctx = SimpleNamespace(deps=deps)
    try:
        first = asyncio.run(agent_tools.paper_meta_write(
            ctx, venue="契约测试会议 ICLR", year=2026))
        assert first.get("ok") is True, first
        again = asyncio.run(agent_tools.paper_meta_write(ctx, venue="另一个会议", year=2027))
    finally:
        asyncio.run(host.aclose())

    assert again.get("ok") is False, again
    assert "不能再改" in str(again.get("error") or ""), again
    assert "重发" not in str(again.get("error") or ""), "文案不能再说「重发一次」：重发必被拒"
    # 已经生效的字段要回给模型：它得据实交代，而不是假装改了
    assert "venue" in (again.get("applied") or {}), again
    assert _read_row()["venue"] == "契约测试会议 ICLR", "第二次提交不能改库里的值"