"""论文列表「按分类排序」（`GET /papers?sort=folder`）的接口测试（需要 MySQL；不可用时自动跳过）。

钉住的约定：
- 分类之间按 `PaperFolder.sort` 升序、再按 `name` 升序；
- **未分类（`folder_id IS NULL`）永远排在最后** —— `order=desc` 只反转「分类之间」的顺序，
  未分类仍在末尾（它不是一个分类，不该因为降序跑到最前面）；
- 组内仍按 `last_read_at` 倒序（每组里最近读的在前）；
- 其它 sort 值的行为不变（不认识的值仍退化成 `created_at`）。

数据隔离：论文用 `__test_sort_*` 伪 id、分类用带随机后缀的名字，跑前跑后直接删行，
不碰真实论文与真实分类。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e

API = "/api/v1"
#: 论文标题里带它，跑用例时用 `q=` 把断言范围收到这 6 篇伪论文上（真实论文库不动）
MARK = "zz-testsort"
#: 本文件建的分类名都以此开头，清理时按前缀删
FOLDER_PREFIX = "zz-testsort-"

#: (分类标签, sort 值)。b 与 c 的 sort 相同 —— 靠 name 决定先后（两个键都要被钉住）
FOLDER_SPECS = (("a", 1), ("b", 2), ("c", 2))

#: (论文标签, 分类标签 / None=未分类, created_at 的秒, last_read_at 的秒)。
#: created_at 的次序**故意与期望的展示顺序不同**：否则"退化成 created_at"也能歪打正着通过。
PAPER_SPECS = (
    ("c1", "c", 1, 30),
    ("u1", None, 2, 40),
    ("a1", "a", 3, 90),
    ("b1", "b", 4, 50),
    ("a2", "a", 5, 60),
    ("u2", None, 6, 80),
)

#: sort=folder&order=asc：a1/a2（分类 a，组内 last_read 倒序）→ b1（b）→ c1（c）→ 未分类 u2/u1
EXPECT_ASC = ("a1", "a2", "b1", "c1", "u2", "u1")
#: order=desc 只把**分类之间**反过来（c 在 b 前、a 最后），未分类仍在末尾、组内仍是 last_read 倒序
EXPECT_DESC = ("c1", "b1", "a1", "a2", "u2", "u1")


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
def seeded(db_ready):
    """三个伪分类 + 六篇伪论文（四篇已分类、两篇未分类），跑完清掉。"""
    from backend.repositories.mysql.models import Paper, PaperFolder
    from backend.repositories.mysql.session import session_scope

    suffix = uuid.uuid4().hex[:8]
    paper_ids = {tag: f"__test_sort_{tag}__" for tag, *_ in PAPER_SPECS}
    folder_ids = {label: f"folder-ts-{suffix}{label}" for label, _ in FOLDER_SPECS}

    def wipe() -> None:
        with session_scope() as sess:
            sess.query(Paper).filter(Paper.id.in_(paper_ids.values())).delete(
                synchronize_session=False)
            sess.query(PaperFolder).filter(
                PaperFolder.name.like(f"{FOLDER_PREFIX}%")).delete(synchronize_session=False)

    wipe()
    with session_scope() as sess:
        for label, sort in FOLDER_SPECS:
            sess.add(PaperFolder(id=folder_ids[label],
                                 name=f"{FOLDER_PREFIX}{label}-{suffix}", sort=sort))
    with session_scope() as sess:
        for tag, folder, created_s, read_s in PAPER_SPECS:
            sess.add(Paper(id=paper_ids[tag], title=f"{MARK}-{tag.upper()}",
                           status="parsed", page_count=1,
                           folder_id=folder_ids[folder] if folder else None,
                           created_at=datetime(2020, 1, 1) + timedelta(seconds=created_s),
                           last_read_at=datetime(2020, 1, 1) + timedelta(minutes=read_s)))
    yield {"ids": paper_ids, "folders": folder_ids}
    wipe()


def _ids(papers: dict, tags) -> list[str]:
    return [papers["ids"][t] for t in tags]


def _list(client, **params) -> dict:
    params.setdefault("page_size", 200)
    body = client.get(f"{API}/papers", params=params)
    assert body.status_code == 200, body.text
    return body.json()["data"]


def test_folder_sort_asc_groups_by_folder_sort_then_name(client, seeded):
    data = _list(client, q=MARK, sort="folder", order="asc")

    assert data["total"] == len(PAPER_SPECS)
    assert len(data["items"]) == len(PAPER_SPECS), "outer join 不该把行数放大"
    assert [x["id"] for x in data["items"]] == _ids(seeded, EXPECT_ASC)

    folders = seeded["folders"]
    assert [x["folder_id"] for x in data["items"]] == [
        folders["a"], folders["a"], folders["b"], folders["c"], None, None,
    ]
    # 组内按 last_read_at 倒序：a1 读得比 a2 晚，所以 a1 在前
    assert data["items"][0]["last_read_at"] > data["items"][1]["last_read_at"]


def test_folder_sort_desc_flips_groups_but_unclassified_stays_last(client, seeded):
    data = _list(client, q=MARK, sort="folder", order="desc")

    assert data["total"] == len(PAPER_SPECS)
    assert [x["id"] for x in data["items"]] == _ids(seeded, EXPECT_DESC)

    # 降序只反转"分类之间"：c(2) 在 b(2) 前、a(1) 最后
    folders = seeded["folders"]
    assert [x["folder_id"] for x in data["items"]][:4] == [
        folders["c"], folders["b"], folders["a"], folders["a"],
    ]
    # 而未分类仍钉在末尾（不是"按 folder_id 降序"就能得到的位置）
    assert [x["folder_id"] for x in data["items"]][-2:] == [None, None]
    assert data["items"][-1]["folder_id"] is None
    # 组内不被 order 影响，仍是 last_read_at 倒序
    assert data["items"][-2]["last_read_at"] > data["items"][-1]["last_read_at"]


def test_folder_sort_is_case_insensitive(client, seeded):
    upper = _list(client, q=MARK, sort="FOLDER", order="asc")
    assert [x["id"] for x in upper["items"]] == _ids(seeded, EXPECT_ASC)


def test_folder_sort_keeps_total_and_pagination(client, seeded):
    first = _list(client, q=MARK, sort="folder", order="asc", page=1, page_size=2)
    assert first["total"] == len(PAPER_SPECS)
    assert [x["id"] for x in first["items"]] == _ids(seeded, EXPECT_ASC)[:2]

    last = _list(client, q=MARK, sort="folder", order="asc", page=3, page_size=2)
    assert last["total"] == len(PAPER_SPECS)
    assert [x["id"] for x in last["items"]] == _ids(seeded, EXPECT_ASC)[4:]


def test_other_sorts_are_unchanged(client, seeded):
    # 认识的列照旧
    by_title = _list(client, q=MARK, sort="title", order="asc")
    assert [x["id"] for x in by_title["items"]] == _ids(
        seeded, ("a1", "a2", "b1", "c1", "u1", "u2"))

    by_read = _list(client, q=MARK)          # 默认 sort=last_read_at&order=desc
    assert [x["id"] for x in by_read["items"]] == _ids(
        seeded, ("a1", "u2", "a2", "b1", "u1", "c1"))

    # 不认识的列仍退化成 created_at（降序 / 升序各一次）
    by_created_desc = _list(client, q=MARK, sort="created_at", order="desc")
    bogus_desc = _list(client, q=MARK, sort="zz-not-a-column", order="desc")
    assert [x["id"] for x in bogus_desc["items"]] == [x["id"] for x in by_created_desc["items"]]
    assert [x["id"] for x in bogus_desc["items"]] == _ids(
        seeded, ("u2", "a2", "b1", "a1", "u1", "c1"))

    by_created_asc = _list(client, q=MARK, sort="created_at", order="asc")
    bogus_asc = _list(client, q=MARK, sort="zz-not-a-column", order="asc")
    assert [x["id"] for x in bogus_asc["items"]] == [x["id"] for x in by_created_asc["items"]]


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_unclassified_is_always_a_suffix_of_the_real_list(client, seeded, order):
    """真实全库口径（不按 q 收窄）：不管升降序，未分类只出现在列表尾部，绝不夹在分类之间。"""
    data = _list(client, sort="folder", order=order)
    folders = [x["folder_id"] for x in data["items"]]
    assert any(f is not None for f in folders), "本文件的伪论文里就有 4 篇已分类的，不该全是未分类"

    first_unclassified = folders.index(None) if None in folders else len(folders)
    assert all(f is None for f in folders[first_unclassified:]), \
        f"order={order} 时未分类后面还有已分类论文：{folders}"

    # 分类之间的先后就是 (sort, name) 的升/降序 —— 用 /folders 里的真实分类行核对
    body = client.get(f"{API}/folders")
    assert body.status_code == 200, body.text
    keys = {x["id"]: (x["sort"], x["name"]) for x in body.json()["data"]["items"]}
    seq = [keys[f] for f in folders[:first_unclassified] if f in keys]
    assert seq == sorted(seq, reverse=(order == "desc")), f"分类之间的顺序不对：{seq}"
