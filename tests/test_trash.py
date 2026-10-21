"""回收站（列表 / 恢复 / 清空）的接口测试（需要 MySQL；不可用时自动跳过）。

钉住的约定：`GET /papers/trash` 只列已软删的论文且按 `deleted_at` 倒序、`has_files`
跟着 `data/trash` 里有没有目录走；`restore` 把目录搬回 `data/papers/<id>` 并**不**
把 `in_knowledge_base` 说成 True（索引已删，说在库里就是"检索永远查不到"）；
`clear` 逐篇彻底删除、一篇失败不中断其余。

数据隔离：论文用 `__test_trash_*` 伪 id、`data_dir` 整份换成 `tmp_path`
（`paper_service.get_settings` 被 patch），不碰真实论文与真实 trash。
`clear` 会扫**全库**回收站，所以用例里把 `list_trash` 收窄到本文件造的这几篇。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e

API = "/api/v1"
P1 = "__test_trash_p1__"
P2 = "__test_trash_p2__"
P3 = "__test_trash_p3__"
MISSING = "__test_trash_missing__"
IDS = (P1, P2, P3)


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()
    return True


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """把 `paper_service` 看的 `data_dir` 换成临时目录（真实 data 一个字都不动）。"""
    from backend.core.config import get_settings
    from backend.services.paper import paper_service

    root = tmp_path / "data"
    (root / "papers").mkdir(parents=True)
    (root / "trash").mkdir(parents=True)
    fake = get_settings().model_copy(update={"data_dir": root})
    monkeypatch.setattr(paper_service, "get_settings", lambda: fake)
    return fake


@pytest.fixture
def client(db_ready, data_dir):
    from fastapi.testclient import TestClient

    import backend.main as main

    return TestClient(main.app)


def _wipe() -> None:
    from backend.repositories.mysql.models import Paper, Task
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        sess.query(Task).filter(Task.paper_id.in_(IDS)).delete(synchronize_session=False)
        sess.query(Paper).filter(Paper.id.in_(IDS)).delete(synchronize_session=False)


@pytest.fixture(autouse=True)
def clean_rows(db_ready, data_dir):
    _wipe()
    yield
    _wipe()


@pytest.fixture
def seeded(db_ready):
    """三篇伪论文：P1/P2 带文件、P3 没有。"""
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        sess.add(Paper(id=P1, title="回收站测试件一", status="parsed", page_count=3,
                       in_knowledge_base=True, folder_id="fld-test"))
        sess.add(Paper(id=P2, title="回收站测试件二", status="translated", page_count=7))
        sess.add(Paper(id=P3, title="回收站测试件三", status="imported", page_count=0))
    return IDS


def _make_files(s, *paper_ids: str) -> None:
    """给这些论文造一份 `data/papers/<id>`（有文件才谈得上 `has_files`）。"""
    for pid in paper_ids:
        root = s.paper_dir(pid)
        (root / "md").mkdir(parents=True, exist_ok=True)
        (root / "source.pdf").write_bytes(b"%PDF-1.4 trash test")
        (root / "md" / "full.md").write_text("# title", encoding="utf-8")


def _set_deleted_at(pid: str, moment: datetime) -> None:
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Paper, pid)
        row.deleted_at = moment


def _trash(client) -> dict:
    got = client.get(f"{API}/papers/trash")
    assert got.status_code == 200, got.text
    return got.json()["data"]


def _soft_delete(client, pid: str) -> None:
    got = client.delete(f"{API}/papers/{pid}")
    assert got.status_code == 200, got.text


def test_trash_list_only_deleted_ordered_and_has_files(client, seeded, data_dir):
    """列表口径：只有已软删的、`deleted_at` 倒序、`has_files` 跟磁盘走。"""
    s = data_dir
    _make_files(s, P1, P2)
    _soft_delete(client, P1)
    _soft_delete(client, P2)
    _set_deleted_at(P3, datetime(2019, 1, 1, 0, 0, 0))   # 行软删、磁盘没文件
    _set_deleted_at(P1, datetime(2020, 1, 1, 0, 0, 0))
    _set_deleted_at(P2, datetime(2021, 1, 1, 0, 0, 0))

    body = _trash(client)
    mine = [item for item in body["items"] if item["id"] in IDS]
    assert [item["id"] for item in mine] == [P2, P1, P3], "最后删的排最前"
    assert body["total"] == len(body["items"])
    assert all(item["deleted_at"] for item in body["items"]), "未删的论文不许出现在回收站"
    by_id = {item["id"]: item for item in mine}
    assert by_id[P1]["has_files"] is True and by_id[P2]["has_files"] is True
    assert by_id[P3]["has_files"] is False, "trash 里没目录就是 has_files=false"
    assert by_id[P1]["page_count"] == 3 and by_id[P1]["folder_id"] == "fld-test"
    assert by_id[P2]["status"] == "translated"
    assert set(by_id[P1]) == {"id", "title", "status", "page_count", "deleted_at",
                              "folder_id", "has_files"}


def test_restore_brings_files_back_and_leaves_knowledge_flag_false(client, seeded, data_dir):
    """恢复：目录搬回、从 trash 消失、回到 GET /papers、`in_knowledge_base` 保持 false。"""
    s = data_dir
    _make_files(s, P1)
    _soft_delete(client, P1)
    assert s.paper_dir(P1).exists() is False
    assert [item["id"] for item in _trash(client)["items"]].count(P1) == 1

    got = client.post(f"{API}/papers/{P1}/restore")
    assert got.status_code == 200, got.text
    data = got.json()["data"]
    assert data["paper_id"] == P1 and data["restored"] is True and data["files"] is True
    assert data["steps"], "步骤要能说出做了什么"
    assert (s.paper_dir(P1) / "source.pdf").is_file(), "PDF 必须搬回 data/papers/<id>"
    assert (s.paper_dir(P1) / "md" / "full.md").is_file(), "页级产物一起回来"
    assert not any(path.name.startswith(f"{P1}-") for path in s.trash_dir.iterdir()), "trash 里不许留"
    assert P1 not in [item["id"] for item in _trash(client)["items"]]

    listed = client.get(f"{API}/papers", params={"q": "回收站测试件一", "page_size": 200})
    assert listed.status_code == 200, listed.text
    assert P1 in [item["id"] for item in listed.json()["data"]["items"]], "恢复后要回到论文列表"

    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Paper, P1)
        assert row.deleted_at is None
        assert bool(row.in_knowledge_base) is False, "索引已删，不许说在知识库"


def test_restore_reopens_index_task_for_parsed_paper(client, seeded, data_dir):
    """已解析 / 已翻译的论文恢复后要重开 INDEX 任务（否则永远不进检索）。"""
    from backend.services.task.task_service import task_service

    _make_files(data_dir, P1)
    _soft_delete(client, P1)
    got = client.post(f"{API}/papers/{P1}/restore")
    assert got.status_code == 200, got.text
    assert got.json()["data"]["indexed"] is True
    active = task_service.find_active("INDEX", P1, "knowledge")
    assert active is not None and active["type"] == "INDEX"

    # imported 状态（还没解析）没有可索引的东西，不许凭空开任务
    _soft_delete(client, P3)
    got = client.post(f"{API}/papers/{P3}/restore")
    assert got.status_code == 200, got.text
    assert got.json()["data"]["indexed"] is False
    assert got.json()["data"]["files"] is False, "trash 里没目录 → files=false，元数据照常恢复"


def test_restore_rejects_paper_not_in_trash(client, seeded):
    """没软删的论文不是回收站里的东西 → 400（P2 在 `seeded` 里就是在库的）。"""
    got = client.post(f"{API}/papers/{P2}/restore")
    assert got.status_code == 400, got.text
    assert "不在回收站" in got.json()["error"]["message"]


def test_restore_unknown_id_is_404(client, seeded):
    got = client.post(f"{API}/papers/{MISSING}/restore")
    assert got.status_code == 404, got.text


def test_clear_purges_every_trashed_paper(client, seeded, data_dir, monkeypatch):
    """清空回收站：逐篇彻底删（行 + trash 目录），一篇失败不中断其余。"""
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope
    from backend.services.paper import paper_service

    s = data_dir
    _make_files(s, P1, P2)
    _soft_delete(client, P1)
    _soft_delete(client, P2)

    real = {item["id"]: item for item in paper_service.list_trash()["items"]}
    scoped = [real[P1], real[P2], {"id": MISSING, "has_files": False}]
    monkeypatch.setattr(paper_service, "list_trash",
                        lambda: {"items": scoped, "total": len(scoped)})

    got = client.post(f"{API}/papers/trash/clear")
    assert got.status_code == 200, got.text
    data = got.json()["data"]
    assert data["purged"] == 2, data
    assert [item["paper_id"] for item in data["failed"]] == [MISSING], "一篇失败不影响其余"
    assert data["failed"][0]["error"]

    with session_scope() as sess:
        assert sess.get(Paper, P1) is None and sess.get(Paper, P2) is None, "彻底删要连行一起删"
    leftover = [p.name for p in s.trash_dir.iterdir() if p.name.split("-")[0] in IDS]
    assert leftover == [], f"trash 目录也要清掉：{leftover}"
    assert not s.paper_dir(P1).exists()

    monkeypatch.undo()
    assert [item["id"] for item in _trash(client)["items"] if item["id"] in IDS] == []
