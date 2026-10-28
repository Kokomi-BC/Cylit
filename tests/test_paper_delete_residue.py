"""删论文的**磁盘残留**（需要 MySQL；不可用时自动跳过）。

钉住两件实测出过的事：

1. `DELETE /papers/{id}?hard=true` 对**已经软删过**的论文是直接可达的，而那时
   `data/papers/<id>` 早就不在了 → 硬删只删"本次刚移入的那份"= 什么都不删，
   早先软删搬进 `data/trash` 的目录永远留着（论文行一没，`list_trash()` 就列不到它，
   没有任何界面能再碰到它）。现在硬删清该 id 的**全部** trash 目录。
2. `sweep_orphans` 原先只管 MySQL 行与向量，磁盘完全没覆盖。现在多了 `report["disk"]`：
   `apply=False` **一个字节都不许动**（trash 目录是软删论文的文件本体，
   `restore_paper` 要搬回去），`apply=True` 只删"论文行已经没了"的那批，
   `uploads/` 永远只报告不删。

数据隔离：论文用 `__test_residue_*` 伪 id、`data_dir` 整份换成 `tmp_path`
（`paper_service.get_settings` 被 patch），不碰真实论文目录与真实 trash。
"""

from __future__ import annotations

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e

SOFT = "__test_residue_soft__"          # 软删一次 → 硬删
TWICE = "__test_residue_twice__"        # 软删两次 → 硬删
LIVE = "__test_residue_live__"          # 在库论文：清扫一根毫毛都不许动
GONE = "__test_residue_gone__"          # 只有 trash 目录、没有 paper 行 → 孤儿
NO_ROW = "__test_residue_norow__"       # 只有 papers/<id> 目录、没有 paper 行 → 孤儿
IDS = (SOFT, TWICE, LIVE, GONE, NO_ROW)


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
    for sub in ("papers", "trash", "uploads"):
        (root / sub).mkdir(parents=True)
    fake = get_settings().model_copy(update={"data_dir": root})
    monkeypatch.setattr(paper_service, "get_settings", lambda: fake)
    return fake


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


def _seed_row(pid: str) -> None:
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        sess.add(Paper(id=pid, title=f"磁盘残留测试件 {pid}", status="parsed", page_count=1))


def _row(pid: str):
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        return sess.get(Paper, pid)


def _make_files(s, pid: str, *, size: int = 64) -> None:
    root = s.paper_dir(pid)
    (root / "md").mkdir(parents=True, exist_ok=True)
    (root / "source.pdf").write_bytes(b"x" * size)
    (root / "md" / "full.md").write_text("# title", encoding="utf-8")


def _trash_of(s, pid: str) -> list[str]:
    return sorted(p.name for p in s.trash_dir.iterdir() if p.name.startswith(f"{pid}-"))


def _snapshot(*roots) -> list[tuple[str, int]]:
    out = []
    for root in roots:
        if root.exists():
            out.extend((str(p), p.stat().st_size) for p in root.rglob("*"))
    return sorted(out)


def test_hard_delete_purges_the_dir_moved_by_the_earlier_soft_delete(data_dir):
    """软删 → 目录进 trash；再硬删 → 该 id 的 trash 目录全消失、paper 行也没了。"""
    from backend.services.paper import paper_service

    s = data_dir
    _seed_row(SOFT)
    _make_files(s, SOFT)

    paper_service.delete_paper(SOFT, hard=False)
    assert len(_trash_of(s, SOFT)) == 1, "软删把论文目录搬进 trash"
    assert not s.paper_dir(SOFT).exists()
    row = _row(SOFT)
    assert row is not None and row.deleted_at is not None, "软删只标记，行还在"

    out = paper_service.delete_paper(SOFT, hard=True)
    assert any("trash" in step for step in out["steps"]), out["steps"]
    assert _trash_of(s, SOFT) == [], "硬删要把早先软删搬进 trash 的那份也删掉（残留 bug）"
    assert _row(SOFT) is None


def test_hard_delete_purges_every_trash_dir_of_the_same_paper(data_dir, monkeypatch):
    """同一篇软删两次（两次都搬了目录）→ 硬删后两份都不剩。"""
    from backend.services.paper import paper_service

    s = data_dir
    # 两次软删落在同一秒里会撞同一个目录名，这里钉死两个不同的时间戳
    stamps = iter(["20240101-000001", "20240101-000002"])
    monkeypatch.setattr(paper_service, "ts_compact", lambda: next(stamps))

    _seed_row(TWICE)
    _make_files(s, TWICE, size=32)
    paper_service.delete_paper(TWICE, hard=False)
    _make_files(s, TWICE, size=48)      # 重新落盘了文件，第二次软删才又有一份会搬进 trash
    paper_service.delete_paper(TWICE, hard=False)
    assert len(_trash_of(s, TWICE)) == 2, _trash_of(s, TWICE)

    paper_service.delete_paper(TWICE, hard=True)
    assert _trash_of(s, TWICE) == [], "同一个 paper_id 的每一份都要清"
    assert _row(TWICE) is None


def test_sweep_disk_reports_orphans_and_touches_nothing(data_dir):
    """apply=False：如实报 dirs/bytes，盘上一个字节都不许动（硬约束）。"""
    from backend.services.paper import paper_service

    s = data_dir
    _seed_row(LIVE)
    _make_files(s, LIVE)                       # 在库论文的目录：不是孤儿

    (s.trash_dir / f"{GONE}-20240101-000001").mkdir()
    (s.trash_dir / f"{GONE}-20240101-000001" / "source.pdf").write_bytes(b"y" * 100)
    (s.trash_dir / f"{GONE}-20240101-000002" / "md").mkdir(parents=True)
    (s.trash_dir / f"{GONE}-20240101-000002" / "md" / "full.md").write_bytes(b"z" * 300)
    (s.trash_dir / f"{LIVE}-20240101-000003").mkdir()
    (s.trash_dir / f"{LIVE}-20240101-000003" / "source.pdf").write_bytes(b"w" * 7)
    (s.papers_dir / NO_ROW / "md").mkdir(parents=True)
    (s.papers_dir / NO_ROW / "md" / "full.md").write_bytes(b"q" * 40)
    (s.uploads_dir / "up1.pdf").write_bytes(b"u" * 11)
    (s.uploads_dir / "up2.pdf").write_bytes(b"u" * 22)

    before = _snapshot(s.trash_dir, s.papers_dir, s.uploads_dir)
    disk = paper_service.sweep_orphans(apply=False)["disk"]
    assert _snapshot(s.trash_dir, s.papers_dir, s.uploads_dir) == before, "只报告时一个字节都不许动"

    assert disk["trash"] == {"papers": 1, "dirs": 2, "bytes": 400, "sample": [GONE],
                             "deleted": 0}, disk["trash"]
    assert disk["papers"] == {"papers": 1, "dirs": 1, "bytes": 40, "sample": [NO_ROW],
                              "deleted": 0}, disk["papers"]
    assert disk["uploads"] == {"files": 2, "bytes": 33, "deleted": 0}, disk["uploads"]


def test_sweep_disk_apply_deletes_only_orphans(data_dir):
    """apply=True：孤儿目录消失；在库论文的 trash 目录 / papers 目录、uploads 一根不动。"""
    from backend.services.paper import paper_service

    s = data_dir
    _seed_row(LIVE)
    _make_files(s, LIVE)

    (s.trash_dir / f"{LIVE}-20240101-000001").mkdir()
    (s.trash_dir / f"{LIVE}-20240101-000001" / "source.pdf").write_bytes(b"w" * 7)
    (s.trash_dir / f"{GONE}-20240101-000002").mkdir()
    (s.trash_dir / f"{GONE}-20240101-000002" / "source.pdf").write_bytes(b"y" * 100)
    (s.papers_dir / NO_ROW / "md").mkdir(parents=True)
    (s.papers_dir / NO_ROW / "md" / "full.md").write_bytes(b"q" * 40)
    (s.uploads_dir / "up1.pdf").write_bytes(b"u" * 11)

    disk = paper_service.sweep_orphans(apply=True)["disk"]
    assert disk["trash"]["deleted"] == 1, disk["trash"]
    assert disk["papers"]["deleted"] == 1, disk["papers"]
    assert not (s.trash_dir / f"{GONE}-20240101-000002").exists(), "孤儿 trash 目录要删"
    assert not (s.papers_dir / NO_ROW).exists(), "孤儿 papers 目录要删"
    assert (s.trash_dir / f"{LIVE}-20240101-000001" / "source.pdf").is_file(), "在库论文的 trash 不动"
    assert (s.paper_dir(LIVE) / "source.pdf").is_file(), "在库论文的目录不动"
    assert (s.uploads_dir / "up1.pdf").is_file(), "uploads 永不删"
    assert _row(LIVE) is not None
