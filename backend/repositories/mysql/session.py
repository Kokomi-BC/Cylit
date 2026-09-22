"""数据库连接与会话管理。

- 单进程应用，使用全局单例 Engine（pool_pre_ping 防 MySQL 断连）。
- ``session_scope()`` 提供事务上下文；异常自动回滚。
- ``init_db()`` 幂等：自动建库（MySQL）+ 建表（12 张业务表）。
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


def init_db(drop: bool = False) -> dict:
    """建库建表，可重复执行（规划阶段 05 验收）。"""
    s = get_settings()
    ensure_database()
    engine = get_engine()
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    tables = sorted(inspect(engine).get_table_names())
    log.info("数据库就绪：%s（%d 张表）", s.db_name if s.db_backend == "mysql"
             else s.database_url, len(tables))
    return {"backend": s.db_backend, "tables": tables}


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
