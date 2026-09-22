"""12 张业务表的 SQLAlchemy 2.0 映射（规划 §10）。

字段命名与规划文档逐条对齐；额外新增一张 ``setting`` 表存放
「并发上限 / RAG 参数 / 允许通用知识开关 / 数据目录」（规划 §20.7 settings）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.core.utils import now

# 论文状态机（规划 §10 ①）
PAPER_STATUSES = (
    "imported", "parsed", "translating", "translated", "indexed", "frozen", "failed",
)
# 任务状态机（规划 §10 ⑨）
TASK_STATUSES = (
    "pending", "processing", "paused", "completed", "failed", "cancelled",
)
# 任务类型
TASK_TYPES = (
    "IMPORT", "PARSE", "TRANSLATE_MD", "TRANSLATE_PDF", "EMBED", "INDEX",
    "ANALYZE", "AI_READING", "RECONCILE",
)
# 队列（三条产线，规划 §3）
TASK_QUEUES = ("md", "pdf", "knowledge", "system")

BLOCK_TYPES = ("para", "heading", "caption", "formula", "figure", "table", "ref")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


# ---------------------------------------------------------------- ① 论文
class Paper(Base, TimestampMixin):
    __tablename__ = "paper"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(512))
    authors: Mapped[str | None] = mapped_column(String(1024))
    venue: Mapped[str | None] = mapped_column(String(128))
    year: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    source_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="imported")

    translate_done: Mapped[int] = mapped_column(Integer, default=0)
    translate_total: Mapped[int] = mapped_column(Integer, default=0)
    analyze_done: Mapped[int] = mapped_column(Integer, default=0)
    analyze_total: Mapped[int] = mapped_column(Integer, default=0)
    pdf_translate_done: Mapped[int] = mapped_column(Integer, default=0)
    pdf_translate_total: Mapped[int] = mapped_column(Integer, default=0)

    tags: Mapped[str | None] = mapped_column(String(256))
    in_knowledge_base: Mapped[bool] = mapped_column(Boolean, default=False)
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    # 解析摘要（便于论文库列表直接展示，不存正文）
    abstract: Mapped[str | None] = mapped_column(String(2000))
    parse_summary_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_paper_status", "status"),
        Index("ix_paper_last_read_at", "last_read_at"),
    )


# ---------------------------------------------------------------- ② 块索引
class Block(Base):
    """只存定位与类型，正文在 Markdown（规划 §8）。"""

    __tablename__ = "block"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(16), default="para")
    col: Mapped[int] = mapped_column(Integer, default=0)
    order_global: Mapped[int] = mapped_column(Integer, default=0)
    x0: Mapped[float] = mapped_column(Float, default=0.0)
    y0: Mapped[float] = mapped_column(Float, default=0.0)
    x1: Mapped[float] = mapped_column(Float, default=0.0)
    y1: Mapped[float] = mapped_column(Float, default=0.0)
    md_anchor: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    has_translation: Mapped[bool] = mapped_column(Boolean, default=False)

    # 规划 §9.3 扩展字段
    section_id: Mapped[str | None] = mapped_column(String(64))
    chunk_id: Mapped[str | None] = mapped_column(String(64))
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    preview: Mapped[str | None] = mapped_column(String(200))  # ≤200 字预览，非正文
    meta_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_block_paper_order", "paper_id", "order_global"),)


# ---------------------------------------------------------------- ③ 翻译状态
class Translation(Base):
    __tablename__ = "translation"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    block_id: Mapped[str] = mapped_column(String(32), index=True)
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    lang: Mapped[str] = mapped_column(String(8), default="zh")
    status: Mapped[str] = mapped_column(String(12), default="draft")
    confidence: Mapped[float | None] = mapped_column(Float)
    produced_by: Mapped[str | None] = mapped_column(String(64))
    prompt_hash: Mapped[str | None] = mapped_column(String(16))
    md_pointer: Mapped[str | None] = mapped_column(String(256))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    uncertain_json: Mapped[str | None] = mapped_column(Text)
    translated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (
        UniqueConstraint("block_id", "version", "revision", name="uq_tr_block_ver_rev"),
    )


# ---------------------------------------------------------------- ④ 公式
class Formula(Base):
    __tablename__ = "formula"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # = block_id
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    bbox_json: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(String(256))
    vector_data_json: Mapped[str | None] = mapped_column(Text)
    latex_candidate: Mapped[str | None] = mapped_column(Text)
    latex_repaired: Mapped[str | None] = mapped_column(Text)
    latex_final: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    grade: Mapped[str | None] = mapped_column(String(1))
    source: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    validation_json: Mapped[str | None] = mapped_column(Text)
    review_note: Mapped[str | None] = mapped_column(Text)
    is_inline: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    __table_args__ = (Index("ix_formula_paper_grade_status", "paper_id", "grade", "status"),)


# ---------------------------------------------------------------- ⑤ 高亮
class Highlight(Base):
    __tablename__ = "highlight"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, default=1)
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    block_id: Mapped[str] = mapped_column(String(32))
    selected_text: Mapped[str | None] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer, default=0)
    end_offset: Mapped[int] = mapped_column(Integer, default=0)
    color: Mapped[str] = mapped_column(String(16), default="yellow")
    anchor_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (Index("ix_highlight_paper_block", "paper_id", "block_id"),)


# ---------------------------------------------------------------- ⑥ 笔记
class Note(Base, TimestampMixin):
    __tablename__ = "note"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, default=1)
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    block_id: Mapped[str | None] = mapped_column(String(32))
    highlight_id: Mapped[str | None] = mapped_column(String(32))
    selected_text: Mapped[str | None] = mapped_column(Text)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    content_md: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(String(256))


# ---------------------------------------------------------------- ⑦ AI 批注
class Annotation(Base):
    __tablename__ = "annotation"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, default=1)
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    block_id: Mapped[str | None] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(16), default="explanation")
    content_md: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="analysis")
    sources_json: Mapped[str | None] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(String(64))
    prompt_hash: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    task_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(12), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (Index("ix_annotation_paper_block", "paper_id", "block_id"),)


# ---------------------------------------------------------------- ⑧ 阅读进度
class ReadingProgress(Base):
    __tablename__ = "reading_progress"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_block_id: Mapped[str | None] = mapped_column(String(32))
    last_page: Mapped[int] = mapped_column(Integer, default=1)
    percent: Mapped[float] = mapped_column(Float, default=0.0)
    view_mode: Mapped[str] = mapped_column(String(16), default="bilingual")
    pair_order: Mapped[str] = mapped_column(String(16), default="en_top")
    scroll_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


# ---------------------------------------------------------------- ⑨ 任务
class Task(Base, TimestampMixin):
    __tablename__ = "task"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    queue: Mapped[str] = mapped_column(String(8), default="system")
    paper_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    done_items: Mapped[int] = mapped_column(Integer, default=0)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    cursor_json: Mapped[str | None] = mapped_column(Text)
    params_json: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str | None] = mapped_column(String(512))
    error: Mapped[str | None] = mapped_column(Text)
    log_tail: Mapped[str | None] = mapped_column(Text)
    stats_json: Mapped[str | None] = mapped_column(Text)   # token/耗时统计（实验用）
    lease_expire_at: Mapped[datetime | None] = mapped_column(DateTime)
    worker: Mapped[str | None] = mapped_column(String(64))
    retries: Mapped[int] = mapped_column(Integer, default=0)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_task_status_type", "status", "type"),
        Index("ix_task_queue", "queue"),
    )


# ---------------------------------------------------------------- ⑩ 模型配置
class ModelConfig(Base):
    __tablename__ = "model_config"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32), default="openai_compatible")
    model_type: Mapped[str] = mapped_column(String(16), default="generation")
    model_name: Mapped[str | None] = mapped_column(String(128))
    base_url: Mapped[str | None] = mapped_column(String(256))
    device: Mapped[str | None] = mapped_column(String(16))
    parameters_json: Mapped[str | None] = mapped_column(Text)
    secret_ref: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    status_detail: Mapped[str | None] = mapped_column(String(512))
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


# ---------------------------------------------------------------- ⑫ chunk 元数据
class Chunk(Base):
    __tablename__ = "chunk"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    block_id: Mapped[str | None] = mapped_column(String(32))
    chunk_type: Mapped[str] = mapped_column(String(16), default="text")
    page_no: Mapped[int] = mapped_column(Integer, default=1)
    order_global: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str | None] = mapped_column(String(128))
    content_preview: Mapped[str | None] = mapped_column(String(200))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    index_version: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model_id: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (Index("ix_chunk_paper_type", "paper_id", "chunk_type"),)


# ---------------------------------------------------------------- ⑬ 设置（键值）
class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
