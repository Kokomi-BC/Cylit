"""自定义分类（文件夹）的接口测试（需要 MySQL；不可用时自动跳过）。

钉住的约定：分类名唯一（重名 409）、改名 / 删除的语义（删分类 ≠ 删论文，
论文回到未分类）、批量归类与 ``GET /papers?folder=`` 的过滤口径（none = 未分类），
以及 ``GET /folders`` 里 ``paper_count`` / ``unclassified`` 两个计数。

数据隔离：论文用 ``__test_folder_*`` 伪 id（跑前跑后直接删行），分类用带随机后缀的名字，
不碰真实论文与真实分类。``unclassified`` 是**全库**口径，所以只断言"相对变化 2"，
不断言绝对值（真实论文库里本来就有一堆未分类论文）。
"""

from __future__ import annotations

import uuid

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e

P1 = "__test_folder_p1__"
P2 = "__test_folder_p2__"
API = "/api/v1"


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()          # 顺带跑一遍补列（`ensure_columns` 里新加的 paper.folder_id）
    return True


@pytest.fixture
def client(db_ready):
    from fastapi.testclient import TestClient

    import backend.main as main

    return TestClient(main.app)


@pytest.fixture
def papers(db_ready):
    """两篇伪论文（未分类）+ 跑完清掉，返回它们的 id。"""
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope

    ids = (P1, P2)

    def wipe() -> None:
        from backend.repositories.mysql.models import PaperFolder

        with session_scope() as sess:
            sess.query(Paper).filter(Paper.id.in_(ids)).delete(synchronize_session=False)
            # 本文件建的分类（名字都带这个前缀）
            sess.query(PaperFolder).filter(
                PaperFolder.name.like("zz-folder-test-%")).delete(synchronize_session=False)

    wipe()
    with session_scope() as sess:
        sess.add(Paper(id=P1, title="分类测试件一", status="parsed", page_count=1))
        sess.add(Paper(id=P2, title="分类测试件二", status="parsed", page_count=1))
    yield ids
    wipe()


@pytest.fixture
def new_folder(client):
    """建一个带唯一后缀的分类（避免和库里真实分类重名），用完删掉。"""
    def make(name: str | None = None) -> dict:
        unique = name or f"zz-folder-test-{uuid.uuid4().hex[:8]}"
        got = client.post(f"{API}/folders", json={"name": unique})
        assert got.status_code == 200, got.text
        return got.json()["data"]

    return make


def _folders(client) -> dict:
    body = client.get(f"{API}/folders")
    assert body.status_code == 200, body.text
    return body.json()["data"]


def _papers(client, **params) -> dict:
    params.setdefault("page_size", 200)
    body = client.get(f"{API}/papers", params=params)
    assert body.status_code == 200, body.text
    return body.json()["data"]


def test_create_folder_shows_up_with_zero_count(client, papers, new_folder):
    folder = new_folder()
    assert folder["id"].startswith("folder-")
    assert isinstance(folder["sort"], int)

    data = _folders(client)
    row = next(x for x in data["items"] if x["id"] == folder["id"])
    assert row["name"] == folder["name"]
    assert row["paper_count"] == 0, "新分类里一篇论文都没有"
    assert data["total"] == len(data["items"])


def test_duplicate_name_is_409(client, papers, new_folder):
    folder = new_folder()
    again = client.post(f"{API}/folders", json={"name": folder["name"]})
    assert again.status_code == 409, again.text
    assert "已存在" in again.json()["error"]["message"]

    # 改名撞到别人也要 409（前面只判"新建重名"的话，这里就会冒出 500）
    other = new_folder()
    clash = client.patch(f"{API}/folders/{other['id']}", json={"name": folder["name"]})
    assert clash.status_code == 409, clash.text


def test_name_validation_is_400(client, papers, new_folder):
    assert client.post(f"{API}/folders", json={"name": "   "}).status_code == 400
    assert client.post(f"{API}/folders", json={"name": "x" * 33}).status_code == 400
    folder = new_folder()
    assert client.patch(f"{API}/folders/{folder['id']}",
                        json={"name": " "}).status_code == 400
    assert client.patch(f"{API}/folders/nope-not-a-folder",
                        json={"name": "随便"}).status_code == 404
    assert client.delete(f"{API}/folders/nope-not-a-folder").status_code == 404


def test_rename_folder(client, papers, new_folder):
    folder = new_folder()
    new_name = f"zz-folder-test-{uuid.uuid4().hex[:8]}"
    got = client.patch(f"{API}/folders/{folder['id']}", json={"name": new_name})
    assert got.status_code == 200, got.text
    assert got.json()["data"]["name"] == new_name
    assert any(x["name"] == new_name for x in _folders(client)["items"])


def test_assign_two_papers_and_filter_by_folder(client, papers, new_folder):
    folder = new_folder()
    unclassified_before = _folders(client)["unclassified"]

    got = client.post(f"{API}/papers/folder",
                      json={"paper_ids": list(papers), "folder_id": folder["id"]})
    assert got.status_code == 200, got.text
    assert got.json()["data"]["updated"] == 2

    inside = _papers(client, folder=folder["id"])
    assert inside["total"] == 2
    assert {x["id"] for x in inside["items"]} == set(papers)
    assert all(x["folder_id"] == folder["id"] for x in inside["items"])

    data = _folders(client)
    row = next(x for x in data["items"] if x["id"] == folder["id"])
    assert row["paper_count"] == 2
    assert data["unclassified"] == unclassified_before - 2, "这两篇不在未分类里了"
    # 与 /papers?folder=none 的口径必须一致（两处各数一遍迟早漂）
    assert _papers(client, folder="none")["total"] == data["unclassified"]

    # folder_id=null = 移回未分类（批量归类的另一半语义）
    back = client.post(f"{API}/papers/folder",
                       json={"paper_ids": list(papers), "folder_id": None})
    assert back.json()["data"]["updated"] == 2
    assert _papers(client, folder=folder["id"])["total"] == 0
    assert _papers(client, folder="none")["total"] == unclassified_before


def test_assign_unknown_paper_or_folder(client, papers, new_folder):
    folder = new_folder()
    assert client.post(f"{API}/papers/folder", json={"paper_ids": [], "folder_id": None}
                       ).status_code == 400
    missing = client.post(f"{API}/papers/folder",
                          json={"paper_ids": list(papers), "folder_id": "nope-not-a-folder"})
    assert missing.status_code == 404, missing.text


def test_delete_folder_keeps_papers_as_unclassified(client, papers, new_folder):
    folder = new_folder()
    unclassified_before = _folders(client)["unclassified"]
    client.post(f"{API}/papers/folder",
                json={"paper_ids": list(papers), "folder_id": folder["id"]})

    gone = client.delete(f"{API}/folders/{folder['id']}")
    assert gone.status_code == 200, gone.text
    assert gone.json()["data"]["moved"] == 2

    # 论文没被删：还在列表里、folder_id 回到 NULL
    listed = _papers(client, q="分类测试件")
    assert {x["id"] for x in listed["items"]} >= set(papers)
    assert all(x["folder_id"] is None for x in listed["items"])
    assert client.get(f"{API}/papers/{P1}").json()["data"]["folder_id"] is None

    data = _folders(client)
    assert all(x["id"] != folder["id"] for x in data["items"])
    assert _papers(client, folder="none")["total"] == unclassified_before
    assert data["unclassified"] == unclassified_before
