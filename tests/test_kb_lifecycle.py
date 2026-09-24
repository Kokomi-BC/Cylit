"""知识库生命周期（需要 MySQL；不可用时跳过）：对账 / 移除 / 任务记录删除。

覆盖本轮用户报的三件事：
1. `scanned-demo` 那 10 条"向量已被重建丢掉"的旧 chunk 元数据 → `reconcile` 能报出来并清掉；
2. 「建索引 = 纳入知识库」之后，退出知识库要用 `remove_from_kb`（清向量 + 清元数据 + 取消标记）；
3. 任务记录可以单条删除、也可以一键清空已完成（运行中的不许删）。

用**独立伪论文 / 伪任务 id**，跑完即删；向量库指向 `tmp_path`，绝不碰真实索引目录。
"""

from __future__ import annotations

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e

FAKE = "__test_kb_lifecycle__"


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()
    return True


@pytest.fixture()
def fake_paper(db_ready):
    from backend.repositories.mysql.models import Chunk, Paper, Task
    from backend.repositories.mysql.session import session_scope

    def wipe() -> None:
        with session_scope() as sess:
            sess.query(Chunk).filter(Chunk.paper_id == FAKE).delete()
            sess.query(Task).filter(Task.paper_id == FAKE).delete()
            row = sess.get(Paper, FAKE)
            if row is not None:
                sess.delete(row)

    wipe()
    with session_scope() as sess:
        sess.add(Paper(id=FAKE, title="KB 生命周期测试件", status="parsed",
                       in_knowledge_base=False, page_count=1))
    yield FAKE
    wipe()


@pytest.fixture()
def temp_store(tmp_path, monkeypatch):
    """把向量库单例指到临时目录（真实索引一行不动）。"""
    from backend.repositories.vector.lancedb_store import LanceDBStore, vector_store

    store = LanceDBStore(tmp_path / "lancedb")
    monkeypatch.setattr(vector_store, "_path", store.path)
    monkeypatch.setattr(vector_store, "_db", None)
    yield store
    monkeypatch.undo()


def _chunk(cid: str, lang: str = "en") -> dict:
    return {"id": cid, "paper_id": FAKE, "chunk_type": "text", "page_no": 1,
            "order_global": 1, "block_id": "b1", "section": "S", "lang": lang,
            "content": f"content of {cid}", "content_hash": f"h-{cid}",
            "metadata": {"lang": lang, "pair_id": cid.rsplit(":", 1)[0]}}


def _insert_chunks(ids: list[str]) -> None:
    from backend.repositories.mysql.models import Chunk
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        for cid in ids:
            lang = "zh" if cid.endswith(":zh") else "en"
            sess.add(Chunk(id=cid, paper_id=FAKE, block_id="b1", chunk_type="text",
                           page_no=1, order_global=1, section="S",
                           content_preview="x", content_hash=f"h-{cid}",
                           token_count=3, index_version=1,
                           embedding_model_id="model-a",
                           metadata_json='{"lang": "%s"}' % lang))


# --------------------------------------------------------------------------
# 对账
# --------------------------------------------------------------------------


def test_reconcile_reports_orphans_and_missing(fake_paper, temp_store) -> None:
    from backend.services.knowledge import knowledge_service as ks

    # MySQL 有 c1/c2；向量库只有 c2（模拟"向量表被重建，旧元数据留下"）
    _insert_chunks(["c1", "c2"])
    temp_store.upsert_chunks([_chunk("c2")], [[0.1, 0.2]], 1)

    report = ks.reconcile(paper_id=FAKE)
    assert report["mysql_chunks"] == 2 and report["vector_chunks"] == 1
    assert report["orphans"] == 1 and report["orphan_sample"] == ["c1"]
    assert report["missing"] == 0 and report["consistent"] is False
    assert report["fixed"] is False

    # fix=True：孤儿元数据被清掉，两侧一致
    _, _ = None, None
    fixed = ks.reconcile(paper_id=FAKE, fix=True)
    assert fixed["removed_metadata"] == 1 and fixed["fixed"] is True
    after = ks.reconcile(paper_id=FAKE)
    assert after["consistent"] is True and after["orphans"] == 0


def test_reconcile_removes_vector_without_metadata(fake_paper, temp_store) -> None:
    from backend.services.knowledge import knowledge_service as ks

    _insert_chunks(["c1"])
    temp_store.upsert_chunks([_chunk("c1"), _chunk("ghost:en")],
                             [[0.1, 0.2], [0.3, 0.4]], 1)

    report = ks.reconcile(paper_id=FAKE)
    assert report["missing"] == 1 and report["missing_sample"] == ["ghost:en"]

    fixed = ks.reconcile(paper_id=FAKE, fix=True)
    assert fixed["removed_vectors"] == 1
    assert temp_store.count(paper_id=FAKE) == 1


# --------------------------------------------------------------------------
# 从知识库移除
# --------------------------------------------------------------------------


def test_remove_from_kb_clears_chunks_vectors_and_flag(fake_paper, temp_store) -> None:
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope
    from backend.services.knowledge import knowledge_service as ks

    _insert_chunks(["c1:zh", "c1:en"])
    temp_store.upsert_chunks([_chunk("c1:zh", "zh"), _chunk("c1:en")],
                             [[0.1, 0.2], [0.2, 0.1]], 1)
    with session_scope() as sess:
        sess.get(Paper, FAKE).in_knowledge_base = True

    result = ks.remove_from_kb(FAKE)

    assert result["removed_chunks"] == 2 and result["removed_vectors"] == 2
    assert result["in_knowledge_base"] is False and result["vector_error"] is None
    assert temp_store.count(paper_id=FAKE) == 0
    assert ks.reconcile(paper_id=FAKE)["consistent"] is True
    with session_scope() as sess:
        assert sess.get(Paper, FAKE).in_knowledge_base is False


def test_remove_from_kb_is_idempotent(fake_paper, temp_store) -> None:
    from backend.services.knowledge import knowledge_service as ks

    first = ks.remove_from_kb(FAKE)
    second = ks.remove_from_kb(FAKE)
    assert first["removed_chunks"] == 0 and second["removed_chunks"] == 0
    assert second["in_knowledge_base"] is False


def test_remove_from_kb_unknown_paper(fake_paper, temp_store) -> None:
    from backend.core.errors import NotFoundError
    from backend.services.knowledge import knowledge_service as ks

    with pytest.raises(NotFoundError):
        ks.remove_from_kb("__no_such_paper__")


# --------------------------------------------------------------------------
# 索引状态里的对账字段
# --------------------------------------------------------------------------


def test_index_status_exposes_consistency(fake_paper, temp_store) -> None:
    from backend.services.knowledge import knowledge_service as ks

    _insert_chunks(["c1", "c2"])
    temp_store.upsert_chunks([_chunk("c2")], [[0.1, 0.2]], 1)

    status = ks.index_status()          # 全库视角才有对账字段
    assert status["consistent"] is False
    assert status["orphan_chunks"] >= 1
    assert ks.index_status(FAKE)["consistent"] is None      # 单篇视角不判定


def test_overview_resolves_embedding_model_labels(fake_paper, temp_store) -> None:
    from backend.services.knowledge import knowledge_service as ks

    _insert_chunks(["c1"])
    models = ks.overview()["embedding_models"]
    assert models and all(isinstance(m, dict) and m.get("label") for m in models)
    assert any(m["id"] == "model-a" for m in models)        # 认不出的 id 原样回退


# --------------------------------------------------------------------------
# 任务记录删除
# --------------------------------------------------------------------------


def test_task_delete_requires_terminal_and_cleanup(fake_paper) -> None:
    from backend.core.errors import ConflictError
    from backend.services.task.task_service import task_service

    done = task_service.create("INDEX", "knowledge", FAKE, total_items=1)
    running = task_service.create("INDEX", "knowledge", FAKE, total_items=1)

    with pytest.raises(ConflictError):
        task_service.delete(running["id"])                  # 运行中的不许删

    task_service.finish(done["id"], "completed", message="ok")
    assert task_service.delete(done["id"])["deleted"] == done["id"]
    with pytest.raises(Exception):
        task_service.get(done["id"])                        # 记录真的没了

    # ⚠️ 清理**必须带 paper_id**：裸调 cleanup_finished() 是全库清理，
    # 我在这一轮真的把用户的任务记录（含 stats_json 实验数据）清空了。
    assert task_service.cleanup_finished(paper_id=FAKE)["deleted"] == 0   # running 还在跑
    task_service.finish(running["id"], "cancelled", message="测试收尾")
    cleaned = task_service.cleanup_finished(paper_id=FAKE)
    assert cleaned["deleted"] == 1 and cleaned["paper_id"] == FAKE
    with pytest.raises(Exception):
        task_service.get(running["id"])


def test_cleanup_with_bogus_status_does_not_wipe_everything(fake_paper) -> None:
    """显式给了非法状态 → 清 0 条，**不能**回落到"全清"（否则前端传错就把历史清光）。"""
    from backend.services.task.task_service import task_service

    t = task_service.create("INDEX", "knowledge", FAKE, total_items=1)
    task_service.finish(t["id"], "completed", message="ok")

    result = task_service.cleanup_finished(statuses=["done", "finished"], paper_id=FAKE)
    assert result["deleted"] == 0 and "note" in result
    assert task_service.get(t["id"])["status"] == "completed"   # 记录还在
    task_service.delete(t["id"])
