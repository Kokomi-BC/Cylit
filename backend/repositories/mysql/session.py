"""数据库连接与会话管理。

- 单进程应用，使用全局单例 Engine（pool_pre_ping 防 MySQL 断连）。
- ``session_scope()`` 提供事务上下文；异常自动回滚。
- ``init_db()`` 幂等：自动建库（MySQL）+ 建表（12 张业务表）+ 修补历史窄列 + 补加后加的列。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import get_settings
from backend.core.errors import StorageError
from backend.core.logging import get_logger
from backend.repositories.mysql.models import Base

log = get_logger(__name__)
_lock = threading.Lock()
_engine: Engine | None = None
_session_factory: sessionmaker | None = None

# 后加的列（表名, 列名, ALTER 用的列定义）：`create_all` 不会给既有表补列，见 `ensure_columns`。
# 默认值必须给，否则老库里的既有行会变成 NULL，读出来就是 None（而不是 'en'/'row'）。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("highlight", "side", "`side` VARCHAR(4) NOT NULL DEFAULT 'en'"),
    ("highlight", "scope", "`scope` VARCHAR(16) NOT NULL DEFAULT 'row'"),
    ("highlight", "group_id", "`group_id` VARCHAR(32) NULL"),
    # 阅读进度里记下"原文栏呈现"（`text` 原文文本 / `page` 原 PDF）：
    # 与 view_mode / pair_order 一起，切标签或重开这篇论文时还原成上次的样子
    ("reading_progress", "original_mode", "`original_mode` VARCHAR(8) NOT NULL DEFAULT 'text'"),
    # 界面状态（侧栏开合 / 正文字号 / 显示参考文献 / 原 PDF 缩放）：按论文记，切标签与重开时还原。
    # 允许 NULL = 还没记过（前端回退到本地偏好），所以不给 NOT NULL 默认值。
    ("reading_progress", "aside_open", "`aside_open` TINYINT(1) NULL"),
    ("reading_progress", "text_size", "`text_size` VARCHAR(4) NULL"),
    ("reading_progress", "show_refs", "`show_refs` TINYINT(1) NULL"),
    ("reading_progress", "zoom", "`zoom` FLOAT NULL"),
    # 对照 + 原 PDF 时右侧文字列放哪一侧（zh 译文 / en 原文）
    ("reading_progress", "page_side", "`page_side` VARCHAR(4) NOT NULL DEFAULT 'zh'"),
)


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        s = get_settings()
        kwargs: dict = {"echo": s.db_echo, "future": True}
        if s.db_backend == "sqlite":
            kwargs["connect_args"] = {"check_same_thread": False}
            _engine = create_engine(s.database_url, **kwargs)
        else:
            kwargs.update(pool_pre_ping=True, pool_recycle=1800,
                          pool_size=s.db_pool_size, max_overflow=s.db_max_overflow)
            _engine = create_engine(s.database_url, **kwargs)
        return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False,
            expire_on_commit=False, class_=Session,
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务上下文：正常提交，异常回滚。"""
    sess = get_session_factory()()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def new_session() -> Session:
    """长生命周期会话（任务内部用，需自行 close）。"""
    return get_session_factory()()


# --------------------------------------------------------------------------
# 初始化 / 迁移
# --------------------------------------------------------------------------


def ensure_database() -> None:
    """MySQL：库不存在则创建（utf8mb4）。SQLite：确保目录存在。"""
    s = get_settings()
    if s.db_backend == "sqlite":
        s.data_dir.mkdir(parents=True, exist_ok=True)
        return
    import pymysql

    try:
        conn = pymysql.connect(
            host=s.db_host, port=s.db_port, user=s.db_user,
            password=s.db_password, charset="utf8mb4", connect_timeout=6,
        )
    except Exception as exc:  # noqa: BLE001
        raise StorageError(
            f"无法连接 MySQL {s.db_host}:{s.db_port}：{exc}。"
            f"请检查服务是否启动，或在 .env 中把 PR_DB_BACKEND 改为 sqlite。"
        ) from exc
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{s.db_name}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()


# 需要保证最小宽度的文本列：(表, 列, 最小长度)
# 只在实际窄于要求时才 ALTER（幂等，可反复启动）。
_MIN_COLUMN_WIDTHS: tuple[tuple[str, str, int], ...] = (
    # chunk.id = "{paper_id}:{chunk_type}:{首块 id}[-{末块 id}]"，长论文 id 下约 105 字符
    ("chunk", "id", 128),
    ("block", "chunk_id", 128),  # 指向 Chunk.id，同步预留
)


def repair_column_widths(engine: Engine | None = None) -> list[str]:
    """把历史库里偏窄的列加宽（MySQL 的 ``create_all`` 不会 ALTER 既有列）。

    背景：``chunk.id`` 由论文 id + 类型 + 块 id 范围拼成，长论文 id
    （如 ``turboquant-online-vector-iclr-26``）会超过早期的 ``varchar(64)``，
    插入时报 ``1406 Data too long for column 'id'``。
    返回被修改的列描述列表（空表示无需修补）。
    """
    if get_settings().db_backend == "sqlite":
        return []  # SQLite 不强制字符串长度
    engine = engine or get_engine()
    repaired: list[str] = []
    with engine.begin() as conn:
        for table, column, want in _MIN_COLUMN_WIDTHS:
            row = conn.execute(text(
                "SELECT CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = :t AND COLUMN_NAME = :c"
            ), {"t": table, "c": column}).first()
            if row is None or row[0] is None or int(row[0]) >= want:
                continue
            nullable = "NULL" if str(row[1]).upper() == "YES" else "NOT NULL"
            conn.execute(text(
                f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` "
                f"VARCHAR({want}) {nullable}"
            ))
            repaired.append(f"{table}.{column}: varchar({int(row[0])}) -> varchar({want})")
    if repaired:
        log.info("已加宽历史窄列：%s", "；".join(repaired))
    return repaired


def ensure_columns(engine: Engine | None = None) -> list[str]:
    """给历史库**补上后加的列**（MySQL/SQLite 的 ``create_all`` 都不会 ALTER 既有表）。

    目前只有一个：``highlight.side`` / ``highlight.scope``（原文/译文分开存高亮，
    以及区分"正文行"与"Markdown 视图"两种锚点）。老数据一律按 ``en`` / ``row`` 处理 ——
    它们的偏移量本来就是照着某一侧的文本记的，只是当时没存这一列。
    """
    engine = engine or get_engine()
    added: list[str] = []
    inspector = inspect(engine)
    for table, column, ddl in _ADDED_COLUMNS:
        if table not in inspector.get_table_names():
            continue
        if any(c["name"] == column for c in inspector.get_columns(table)):
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN {ddl}"))
        added.append(f"{table}.{column}")
    if added:
        log.info("已补加历史列：%s", "；".join(added))
    return added


def init_db(drop: bool = False) -> dict:
    """建库建表，可重复执行（规划阶段 05 验收）。"""
    s = get_settings()
    ensure_database()
    engine = get_engine()
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    repaired = repair_column_widths(engine)
    added = ensure_columns(engine)
    tables = sorted(inspect(engine).get_table_names())
    log.info("数据库就绪：%s（%d 张表）", s.db_name if s.db_backend == "mysql"
             else s.database_url, len(tables))
    return {"backend": s.db_backend, "tables": tables, "repaired": repaired, "added": added}


def healthcheck() -> dict:
    s = get_settings()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "backend": s.db_backend, "database": s.db_name}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": s.db_backend, "error": str(exc)[:300]}


def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
