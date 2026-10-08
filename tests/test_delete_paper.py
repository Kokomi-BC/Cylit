"""删论文的清理面（需要 MySQL；不可用时跳过）。

为什么单独锁住：删除是**逐表**列的（`paper_service._purge_paper_content`）——
漏一张表（`paper_reference` 就真的漏过）就会攒下谁也读不到的孤儿行；
而论文 id 是标题派生的 slug，"重新导入同一篇"常常拿到同一个 id，旧数据会跟着复活。
这里同时钉住"清理覆盖到新表"和"孤儿行清扫能用"。
"""

from __future__ import annotations

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e

FAKE = "__test_delete_paper__"
#: 故意**不建** Paper 行：这些行本身就是孤儿
ORPHAN = "__test_delete_orphan__"


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()
    return True


def _tables():
    from backend.repositories.mysql.models import (Annotation, Block, Chunk, Formula, Highlight,
                                                   Note, Paper, PaperReference, ReadingProgress,
                                                   Task, Translation, Understanding)

    return (Annotation, Block, Chunk, Formula, Highlight, Note, PaperReference, ReadingProgress,
            Task, Translation, Understanding), Paper


def _wipe() -> None:
    from backend.repositories.mysql.session import session_scope

    models, Paper = _tables()
    with session_scope() as sess:
        for pid in (FAKE, ORPHAN):
            for model in models:
                sess.query(model).filter(model.paper_id == pid).delete(synchronize_session=False)
            row = sess.get(Paper, pid)
            if row is not None:
                sess.delete(row)


@pytest.fixture(autouse=True)
def clean_rows(db_ready):
    _wipe()
    yield
    _wipe()


def _seed(pid: str, *, with_paper: bool) -> None:
    """造一行"挂在 paper_id 上的每一类用户内容"（少一类，这个测试就少锁一列）。"""
    from backend.repositories.mysql.models import (Formula, Note, Paper, PaperReference,
                                                   ReadingProgress)
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        if with_paper:
            sess.add(Paper(id=pid, title="删除清理测试件（临时）", status="parsed", page_count=1))
        sess.add(PaperReference(id=f"{pid}-ref", paper_id=pid, order_no=1,
                                source="selfcheck", raw="[1] Demo Reference"))
        sess.add(Note(id=f"{pid}-note", paper_id=pid, user_id=1, content_md="笔记正文"))
        sess.add(ReadingProgress(user_id=1, paper_id=pid, last_page=1))
        sess.add(Formula(id=f"{pid}-f", paper_id=pid, page_number=1, status="candidate"))


def _count(pid: str, model) -> int:
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        return int(sess.query(model).filter(model.paper_id == pid).count())


def test_soft_delete_purges_every_paper_scoped_table() -> None:
    """软删就要清干净**用户内容**（含本次新加的参考文献表），不是等硬删。"""
    from backend.repositories.mysql.models import Formula, Note, Paper, PaperReference, ReadingProgress
    from backend.repositories.mysql.session import session_scope
    from backend.services.paper import paper_service

    _seed(FAKE, with_paper=True)
    assert _count(FAKE, PaperReference) == 1 and _count(FAKE, Note) == 1

    out = paper_service.delete_paper(FAKE, hard=False)
    assert out["paper_id"] == FAKE
    assert any("参考文献" in step for step in out["steps"]), out["steps"]
    assert _count(FAKE, PaperReference) == 0, "参考文献行必须跟着删（这张表漏过一次）"
    assert _count(FAKE, Note) == 0 and _count(FAKE, ReadingProgress) == 0
    assert _count(FAKE, Formula) == 0
    with session_scope() as sess:
        row = sess.get(Paper, FAKE)
        assert row is not None and row.deleted_at is not None, "软删只标记，Paper 行还在"
        assert row.in_knowledge_base is False

    # 硬删再把 Paper 行也抹掉（软删过的那篇允许再来一次 hard：幂等清理）
    paper_service.delete_paper(FAKE, hard=True)
    with session_scope() as sess:
        assert sess.get(Paper, FAKE) is None
    # 已经彻底删掉的 id 再删就是 404（`get_paper` 找不到）—— 这是接口口径，不是缺陷
    from backend.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        paper_service.delete_paper(FAKE, hard=True)


def test_paper_scoped_table_list_covers_references() -> None:
    """清理清单必须列全"挂在 paper_id 上的表" —— 这个断言就是防"又漏一张"。"""
    from backend.repositories.mysql.models import PaperReference
    from backend.services.paper import paper_service

    models = [model for _, model in paper_service._PAPER_SCOPED_TABLES]
    assert PaperReference in models
    keys = {key for key, _ in paper_service._PAPER_SCOPED_TABLES}
    assert keys <= set(paper_service._PURGE_LABELS) | {"blocks", "translations", "chunks", "tasks"}


def test_sweep_orphans_reports_then_deletes() -> None:
    """孤儿行清扫：默认只报告，`apply=True` 才删；**在库论文的行一根都不碰**。"""
    from backend.repositories.mysql.models import Note, PaperReference
    from backend.services.paper import paper_service

    _seed(ORPHAN, with_paper=False)          # 没有 Paper 行 → 全是孤儿
    _seed(FAKE, with_paper=True)             # 在库论文，不许动

    dry = paper_service.sweep_orphans(apply=False)
    assert dry["tables"]["references"]["rows"] == 1
    assert ORPHAN in dry["tables"]["references"]["sample"]
    assert _count(ORPHAN, PaperReference) == 1, "只报告时不许删"

    done = paper_service.sweep_orphans(apply=True)
    assert done["tables"]["references"]["deleted"] == 1
    assert _count(ORPHAN, PaperReference) == 0 and _count(ORPHAN, Note) == 0
    assert _count(FAKE, PaperReference) == 1, "在库论文的行不许被清扫碰到"
    assert paper_service.sweep_orphans(apply=False)["tables"]["references"]["rows"] == 0
