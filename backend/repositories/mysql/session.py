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
    # A 路（英文转录）的进度：与 translate_done/translate_total 分开记，两路并发时各自显示。
    # 默认值必须给：老库里的既有行要落到 0，而不是 NULL（读出来就是 None，前端显示成空白）。
    ("paper", "transcribe_done", "`transcribe_done` INT NOT NULL DEFAULT 0"),
    ("paper", "transcribe_total", "`transcribe_total` INT NOT NULL DEFAULT 0"),
    # 参考文献的中英对照（2026-10-07）：`title` 存中文标题、`title_en` 存英文标题、
    # `authors` 存作者段（侧栏默认收起它）。三者都由翻译产物对齐着拆出来，
    # 允许 NULL —— 没翻译的论文就只有 `raw`，前端显示整条原文。
    ("paper_reference", "title_en", "`title_en` VARCHAR(512) NULL"),
    # 参考文献的**页码**（2026-10-07）：侧栏每张卡片上的「定位」按钮靠它跳回正文那一页。
    # 允许 NULL —— 抽不到位置（没有页产物、条目来自 Markdown 兜底）时按钮就不显示。
    ("paper_reference", "page_no", "`page_no` INT NULL"),
    # 会话冻结的问答风格（2026-10-15）：**只对新对话生效**，老会话首次提问时判一次再写回。
    # 允许 NULL —— NULL 就是"升级前建的会话，还没判过"。
    ("conversation", "agent_style", "`agent_style` LONGTEXT NULL"),
    # 会话上选的问答模型与推理等级：**允许 NULL = 跟随默认**，所以不给默认值
    # （给了默认值就等于替用户选了一个模型，那个模型被删掉后全库会话一起失效）。
    ("conversation", "model_config_id", "`model_config_id` VARCHAR(36) NULL"),
    ("conversation", "reasoning_effort", "`reasoning_effort` VARCHAR(32) NULL"),
    # 参考文献里的 **GitHub 仓库地址**（2026-10-07）：既有论文链接又有代码链接的条目，
    # 侧栏并排显示"论文图标 + GitHub 图标"，分别跳不同的页面。
    ("paper_reference", "code_url", "`code_url` VARCHAR(512) NULL"),
    # 元数据扩展（2026-10-07）：中英文摘要/关键词、领域、创新点、前置知识、难度、
    # 推荐阅读时间、一句话概括、类型、DOI/开源地址、来源标记。
    # 一律允许 NULL —— NULL 表示"还不知道"（与 "" 表示"读过、确实没有"是两回事）。
    ("paper", "abstract_zh", "`abstract_zh` LONGTEXT NULL"),
    ("paper", "keywords_en", "`keywords_en` VARCHAR(512) NULL"),
    ("paper", "domain", "`domain` VARCHAR(128) NULL"),
    ("paper", "one_liner", "`one_liner` VARCHAR(512) NULL"),
    ("paper", "contributions_json", "`contributions_json` LONGTEXT NULL"),
    ("paper", "prereq_json", "`prereq_json` LONGTEXT NULL"),
    ("paper", "difficulty", "`difficulty` VARCHAR(16) NULL"),
    ("paper", "read_time_min", "`read_time_min` INT NULL"),
    ("paper", "paper_type", "`paper_type` VARCHAR(32) NULL"),
    ("paper", "doi", "`doi` VARCHAR(128) NULL"),
    ("paper", "code_url", "`code_url` VARCHAR(256) NULL"),
    ("paper", "meta_sources_json", "`meta_sources_json` LONGTEXT NULL"),
    ("paper", "meta_filled_at", "`meta_filled_at` DATETIME NULL"),
    # 对话里的图片附件，以及思考模式的思维链 / 思考耗时（都是"那一轮"的附属数据）：
    # 允许 NULL —— 老消息就是没有，读出来空列表 / 0 即可。
    ("conversation_message", "attachments_json", "`attachments_json` LONGTEXT NULL"),
    ("conversation_message", "thinking", "`thinking` LONGTEXT NULL"),
    ("conversation_message", "thinking_ms", "`thinking_ms` INT NULL"),
    # 多轮思考 + 工具调用的有序轨迹（`{steps, elapsed_ms, rounds}`）：
    # 与 `tool_trace_json` 分开 —— 那个是工具审计表，这份是"用户看得见的过程"，
    # 重开会话时前端照着它重画（老消息没有这一列，读出来空对象 → 前端回退到旧画法）。
    ("conversation_message", "run_json", "`run_json` LONGTEXT NULL"),
    # 论文的自定义分类（`paper_folder.id`）：**允许 NULL = 未分类**，所以不给默认值 ——
    # 给了默认值就变成三态（NULL / 空串 / 真 id），每次判未分类都要多写一个分支。
    # 新表 `paper_folder` 由 `create_all` 建，只有这个后加的列要走这里。
    ("paper", "folder_id", "`folder_id` VARCHAR(32) NULL"),
    # 「模型用哪把密钥」（改造计划 A 阶段）：`create_all` 不会给既有表补列，而
    # `model_config` 早就在线上库里了 —— 不补这两列，读模型配置会直接报
    # "Unknown column 'model_config.key_mode'"，整条模型链断掉。
    # 默认值必须给：老模型一律 `auto`（= 改造前的行为），provider_key_id 保持 NULL。
    ("model_config", "key_mode", "`key_mode` VARCHAR(8) NOT NULL DEFAULT 'auto'"),
    ("model_config", "provider_key_id", "`provider_key_id` VARCHAR(64) NULL"),
    # 提供方目录的展示顺序（用户可拖拽）：`model_provider` 早就在线上库里了，
    # `create_all` 不会补列 —— 不补这条，读提供方会报 Unknown column 'model_provider.sort'。
    # 默认 0 = "还没排过"，第一次启动由 provider_store 的默认回填写成正式顺序。
    ("model_provider", "sort", "`sort` INT NOT NULL DEFAULT 0"),
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
    # chunk.section = 章节标题。实测 3360-mmdfnd 有一处把整段 `CCS CONCEPTS • **Information
    # systems** → …` 当成标题（134 字符），撞上老的 varchar(128) → MySQL 1406
    # "Data too long for column 'section'"，**整个 INDEX 任务失败**（2026-09-27 用户报的）。
    ("chunk", "section", 255),
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


# ---------------------------------------------------------------- 索引补建
# 趋势 / 分桶类查询按 `created_at` 过滤（`knowledge_service.index_trend` 与
# `api/knowledge.latency_report` 的时间窗口都是），而这两列在模型里**没有** index=True。
#
# 为什么必须走这里、而不是"给模型那两列加 index=True 就完事"：
# `create_all` 只建**不存在的表** —— 加 index=True 只对**新库**生效，
# 线上/开发机的老 `paper` / `chunk` 表一个字节都不会变，索引永远不存在。
# 所以老库只能靠这里的 CREATE INDEX 补；新库由 metadata 负责，两条路径各自成立。
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_paper_created_at", "paper", "created_at"),
    ("ix_chunk_created_at", "chunk", "created_at"),
)


def _index_exists_error(exc: Exception) -> bool:
    """这个异常是不是"索引已经存在"（幂等的正常路径，不是错误）。

    MySQL 抛 1061 `Duplicate key name`；SQLite 抛
    `OperationalError: index ix_xxx already exists`。
    SQLAlchemy 把驱动的原始异常包在 `.orig` 里，所以两处都要看：

    - MySQL：错误码在 `orig.args[0]` 上（pymysql 的 `OperationalError`）；
    - SQLite：只有消息串，靠 "already exists" 认。
    """
    orig = getattr(exc, "orig", exc)
    args = getattr(orig, "args", ()) or ()
    if args and args[0] == 1061:          # MySQL: ER_DUP_KEYNAME
        return True
    message = str(orig).lower()
    return "already exist" in message or "duplicate key name" in message


def ensure_indexes(engine: Engine | None = None) -> list[str]:
    """补建 `created_at` 索引（**幂等**），返回这次真的新建出来的索引名。

    只服务**已有部署**：新库的这两张表由 `create_all` 按模型建，本函数对它们
    只会撞上"已存在"（见 `_index_exists_error`，捕获后忽略、并返回空列表）。

    为什么只捕获而不先 `inspect().get_indexes()` 查一遍再建：
    "已存在"本来就是幂等的正常路径，多一次往返换不到正确性；而**其它**错误
    （列不存在、没权限、库连不上）照旧上抛 —— 那是真的坏了，静默忽略只会让人
    在"索引到底建没建"上再猜一轮。
    """
    engine = engine or get_engine()
    created: list[str] = []
    for name, table, column in _INDEXES:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"CREATE INDEX `{name}` ON `{table}` (`{column}`)"))
        except Exception as exc:  # noqa: BLE001 —— 只吞"已存在"，其余照抛
            if _index_exists_error(exc):
                log.debug("索引已存在，跳过：%s", name)
                continue
            raise
        created.append(name)
    if created:
        log.info("已补建索引：%s", "；".join(created))
    return created


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
    # 建索引必须在 create_all 之后：表得先存在（新库是这一步建的，老库早已存在）
    indexed = ensure_indexes(engine)
    tables = sorted(inspect(engine).get_table_names())
    log.info("数据库就绪：%s（%d 张表）", s.db_name if s.db_backend == "mysql"
             else s.database_url, len(tables))
    return {"backend": s.db_backend, "tables": tables, "repaired": repaired,
            "added": added, "indexed": indexed}


def healthcheck() -> dict:
    s = get_settings()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "backend": s.db_backend, "database": s.db_name}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": s.db_backend, "error": str(exc)[:300]}
