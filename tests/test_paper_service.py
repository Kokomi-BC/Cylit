"""导入 / 删除 / 对账 的集成测试（需要 MySQL；不可用时自动跳过）。

覆盖规划 §21.1 导入、§21.12 删除顺序、§8 一致性机制，以及一个真实的
设计缺陷回归：**同一 PDF 重复导入必须复用论文**（否则会不断产生 -2/-3 副本）。
"""

from __future__ import annotations

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()
    return True


@pytest.fixture
def temp_pdf(tmp_path, sample_pdf):
    """每个测试用**独立的 PDF 副本**（内容加一个标记字节 → sha256 不同）。

    这一点很关键：``import_pdf`` 对同 sha256 的文件会**复用**已有论文，
    若多个测试共用同一个 PDF，第二个测试就会复用并最终删掉第一个测试
    （甚至演示论文）的记录。加标记字节即可让各测试互相隔离。
    """
    if sample_pdf is None:
        pytest.skip("未找到示例 PDF")
    target = tmp_path / "sample.pdf"
    data = bytearray(sample_pdf.read_bytes())
    # 末尾追加注释行标记（PDF 允许尾部空白/注释），保证 sha256 与其它测试不同
    data.extend(b"\n% isolated-test-copy\n")
    target.write_bytes(bytes(data))
    return target


def _safe_delete(paper_id: str) -> None:
    """删除测试自建论文；即使被复用成别的论文也不会误删（id 前缀校验）。"""
    from backend.services.paper import paper_service

    if paper_id.startswith(("it-", "e2e-")):
        paper_service.delete_paper(paper_id, hard=True)


def test_import_reuses_existing_paper(db_ready, temp_pdf, tmp_path):
    """同一 PDF（sha256 相同）重复导入必须复用同一篇论文。"""
    from backend.services.paper import paper_service

    first = paper_service.import_pdf(temp_pdf, "sample.pdf", title="复用测试",
                                     paper_id="it-reuse", auto_parse=False)
    second = paper_service.import_pdf(temp_pdf, "sample.pdf", title="复用测试",
                                      paper_id="it-reuse-again", auto_parse=False)
    assert second["id"] == first["id"], "同 hash 应复用，不应新建副本"
    assert second.get("reused") is True
    _safe_delete(first["id"])


def test_delete_order_and_reconcile(db_ready, temp_pdf):
    """删除顺序：先清索引，最后移文件；删除后对账应无问题。"""
    from backend.core.errors import NotFoundError
    from backend.services.paper import paper_service

    row = paper_service.import_pdf(temp_pdf, "sample.pdf", title="删除测试",
                                   paper_id="it-delete", auto_parse=False)
    pid = row["id"]
    paths = paper_service.paper_paths(pid)
    assert paths["pdf"].is_file()

    # 软删（默认）：对账（含已删）应看到"已软删且文件已移出、索引已清"
    soft = paper_service.delete_paper(pid, hard=False)
    steps = "；".join(soft["steps"])
    assert "lancedb" in steps
    assert steps.index("mysql: 标记 deleted_at") < steps.index("fs: 移入 trash")
    assert not paths["pdf"].is_file()
    report = paper_service.reconcile(pid, include_deleted=True)
    entry = report["papers"][0]
    assert entry["deleted_at"] is not None
    assert not entry["issues"], entry["issues"]

    # 已软删论文在默认对账中不可见
    with pytest.raises(NotFoundError):
        paper_service.reconcile(pid, include_deleted=False)

    # 彻底删除：MySQL 行与 trash 都被清掉
    _safe_delete(pid)
    with pytest.raises(NotFoundError):
        paper_service.reconcile(pid, include_deleted=True)


def test_delete_paper_purges_history(db_ready, temp_pdf):
    """删论文必须把**属于它的用户内容**一起清掉（用户 2026-10-03："历史记录能否被正确清除"）。

    回归：会话是按 `paper_id` 挂的，删论文那条链**原来完全不碰它们** ——
    论文删了、会话还在，UI 看不见、也永远不会被清；而论文 id 是标题派生的 slug，
    同一篇重新导入会拿到同一个 id，那些旧对话就"复活"到新论文上（引用还指着旧行号）。
    """
    import base64

    from backend.repositories.mysql.models import (Annotation, Conversation,
                                                   ConversationMessage, Highlight, Note,
                                                   ReadingProgress)
    from backend.repositories.mysql.session import session_scope
    from backend.services.agent import attachments as attachment_store
    from backend.services.agent import conversations
    from backend.services.paper import paper_service

    row = paper_service.import_pdf(temp_pdf, "sample.pdf", title="清理测试",
                                   paper_id="it-purge", auto_parse=False)
    pid = row["id"]
    # 走真上传口存一张真 PNG：附件文件也必须跟着会话一起删
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAALElEQVR4nGO8IyfHQApgIkk1"
        "Az00sCBzLOT2YVV04pETHZ3ENBI1MA6DtAQA8VYFUbO5ASoAAAAASUVORK5CYII=")
    att = attachment_store.save(png, filename="purge-check.png", mime="image/png")
    cid = conversations.ensure_conversation(None, pid, title="会被清掉的对话")
    conversations.append_message(cid, role="user", content="> 〔第 1 页〕测试",
                                 attachments=[att])
    conversations.append_message(cid, role="assistant", content="测试回答")
    with session_scope() as sess:
        sess.add(Highlight(id="it-hl-purge", paper_id=pid, block_id="it-x"))
        sess.add(Note(id="it-nt-purge", paper_id=pid, content_md="笔记"))
        sess.add(Annotation(id="it-an-purge", paper_id=pid, content_md="解释",
                            source="selection"))
        sess.add(ReadingProgress(user_id=1, paper_id=pid, last_page=3))

    # 先确认这些行**真的建出来了**（否则下面"删干净了"是空断言）
    with session_scope() as sess:
        assert sess.query(ConversationMessage).filter(
            ConversationMessage.conversation_id == cid).count() == 2
        for model in (Conversation, Highlight, Note, Annotation, ReadingProgress):
            assert sess.query(model).filter(model.paper_id == pid).count() == 1

    soft = paper_service.delete_paper(pid, hard=False)
    steps = "；".join(soft["steps"])
    assert "清用户内容" in steps, steps
    with session_scope() as sess:
        for model in (Conversation, ConversationMessage, Highlight, Note, Annotation,
                      ReadingProgress):
            left = sess.query(model).filter(model.paper_id == pid).count() \
                if model is not ConversationMessage else \
                sess.query(model).filter(model.conversation_id == cid).count()
            assert left == 0, f"{model.__tablename__} 还有 {left} 行没清"
    assert attachment_store.locate(att["attachment_id"]) is None, "附件图应一起删掉"

    _safe_delete(pid)                       # 彻底删除（幂等：再清一次也不该报错）


def test_block_ids_unique_across_papers(db_ready, temp_pdf):
    """跨论文块 ID 必须互不冲突（回归：曾因 ID 无前缀导致 MySQL 主键冲突）。"""
    from backend.parsing.pipeline import PaperBuilder
    from backend.services.paper import paper_service

    ids = []
    for pid in ("it-a", "it-b"):
        row = paper_service.import_pdf(temp_pdf, "sample.pdf", title="前缀测试",
                                       paper_id=pid, auto_parse=False)
        pid = row["id"]
        paths = paper_service.paper_paths(pid)
        result = PaperBuilder(paths["pdf"], pid, paths["root"], max_pages=2).build()
        ids.append({b.id for b in result.blocks})
        _safe_delete(pid)

    assert ids[0] and ids[1]
    assert not (ids[0] & ids[1]), "不同论文的块 ID 不应有交集"
    assert all(bid.startswith(("ita-", "itb-")) for bid in ids[0] | ids[1])


def test_task_queue_code_roundtrip(db_ready):
    """队列长名 ↔ 短码映射（数据库 VARCHAR(8) 限制）。"""
    from backend.services.task.task_service import queue_code, queue_name

    for long_name, code in (("system", "sys"), ("md", "md"), ("pdf", "pdf"),
                            ("knowledge", "kb")):
        assert queue_code(long_name) == code
        assert queue_name(code) == long_name
    assert len(queue_code("knowledge")) <= 8
