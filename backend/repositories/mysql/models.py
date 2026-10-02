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

# ⚠️ 下面这些状态/类型元组是**声明式契约**（给人和文档看的"合法取值全集"），
# 代码路径（`task_runner.register("IMPORT", …)` 等）用裸字符串、不读它们 ——
# P9 的死代码审计把它们标成"零引用"，这里是**有意保留**的决定：删掉就没人能一眼看出合法取值。
# 论文状态机（规划 §10 ①）
PAPER_STATUSES = (
    "imported", "parsed", "translating", "translated", "indexed", "frozen", "failed",
)
# 任务状态机（规划 §10 ⑨）
TASK_STATUSES = (
    "pending", "processing", "paused", "completed", "failed", "cancelled",
)
# 任务类型
# `TRANSCRIBE_EN` = A 路英文转录、`TRANSLATE_ZH` = B 路中文翻译（两路各一个任务类型，
# 幂等与任务面板才分得清是哪一路在跑）；`TRANSLATE_MD` 是融合模式（对照臂 / 回滚）。
TASK_TYPES = (
    "IMPORT", "PARSE", "TRANSLATE_MD", "TRANSCRIBE_EN", "TRANSLATE_ZH",
    "TRANSLATE_PDF", "EMBED", "INDEX", "ANALYZE", "AI_READING", "RECONCILE",
    # 第二阶段「论文理解」：两路各一个任务（A 技术解剖 / B 阅读地图与相关概念）
    "UNDERSTAND_A", "UNDERSTAND_B",
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
    # A 路（英文转录）的进度。与 translate_* 分开：两路并发，进度条要能各自显示，
    # 而 `translate_done` 的口径已经是"中文译文已产出的页数"（见 paper_service.translation_stats）。
    transcribe_done: Mapped[int] = mapped_column(Integer, default=0)
    transcribe_total: Mapped[int] = mapped_column(Integer, default=0)
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
    # 指向 Chunk.id（见上），同样按 128 预留。
    chunk_id: Mapped[str | None] = mapped_column(String(128))
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
HIGHLIGHT_SIDES = ("en", "zh")
# 高亮出现的"面"：正文行（锚点 = 行 id） / Markdown 视图（锚点 = `md:p<页号>`）
HIGHLIGHT_SCOPES = ("row", "markdown")


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
    # 偏移量是相对**哪一侧**的文本：原文 en / 译文 zh。
    # 为什么必须存：一行对照里两侧共用同一个 `block_id`，光有偏移量根本分不清
    # 标的是原文还是译文 —— 原文的高亮会按同样的偏移量画到译文上（实测就是这个 bug）。
    side: Mapped[str] = mapped_column(String(4), default="en")
    # 哪个视图里标的：正文行（`block_id` = 行 id）/ Markdown 视图（`block_id` = `md:p<页号>`）
    scope: Mapped[str] = mapped_column(String(16), default="row")
    # 一次划词**跨了多行**时会拆成多条记录（每行一条，偏移量各自相对本行），这些记录共用
    # 同一个 `group_id`：删除时整组一起删，用户眼里就是"一条高亮"。单行高亮为 NULL（老数据同）。
    group_id: Mapped[str | None] = mapped_column(String(32))
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
    # 原文栏呈现：`text` = 转录的原文文本；`page` = 原 PDF 页图。
    # 与 view_mode / pair_order 一起构成"这篇论文上次是怎么看的"（前端切标签/重开时还原）。
    original_mode: Mapped[str] = mapped_column(String(8), default="text")
    scroll_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    # 阅读时的**界面状态**（用户口径："记录侧栏展开状态、字号等信息"）。
    # 一律允许为空：NULL = 这篇论文还没记过 → 前端回退到本地偏好/默认值
    # （不能给 `default`，否则"没记过"和"记成了默认值"就分不出来了）。
    aside_open: Mapped[bool | None] = mapped_column(Boolean)
    text_size: Mapped[str | None] = mapped_column(String(4))
    show_refs: Mapped[bool | None] = mapped_column(Boolean)
    zoom: Mapped[float | None] = mapped_column(Float)
    # 对照 + 原 PDF 时，右侧文字列放哪一侧：`zh` 译文（默认）/ `en` 原文
    page_side: Mapped[str] = mapped_column(String(4), default="zh")
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

    # chunk.id = ``{paper_id}:{chunk_type}:{首块 id}[-{末块 id}]``，
    # 最长约 48 + 1 + 16 + 1 + 19 + 1 + 19 = 105 字符（长论文 id 会直奔上限），
    # 所以给到 128；老库若仍是 64，启动时由 session._repair_column_widths 自动加宽。
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    block_id: Mapped[str | None] = mapped_column(String(32))
    chunk_type: Mapped[str] = mapped_column(String(16), default="text")
    page_no: Mapped[int] = mapped_column(Integer, default=1)
    order_global: Mapped[int] = mapped_column(Integer, default=0)
    # 章节标题。**给到 255**：实测有论文把整段 `CCS CONCEPTS • **Information systems** → …`
    # 当成标题（134 字符），老库 varchar(128) 会直接 1406 让整个 INDEX 任务失败；
    # 老库启动时由 session.repair_column_widths 自动加宽（幂等）。
    section: Mapped[str | None] = mapped_column(String(255))
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


# ---------------------------------------------------------------- ⑭ 论文理解（第二阶段）
class Understanding(Base, TimestampMixin):
    """第二阶段「论文理解」的结果：一篇论文 × 一路 = 一条。

    为什么**不做成页级**：理解是"读完全篇再讲一遍"，页级切开就没有全局视角了
    （方法链条、相关概念、推荐阅读顺序都是跨页的）。所以一篇论文两路各一次调用。

    为什么结果是 ``Text`` 里的**自由 Markdown**：用户口径 ⑦ —— 理解结果不是固定 schema，
    模型可以增删合并小节，所以不能按字段拆表；结构化只留在 ``refs_json``
    （引用校验与"跳转到原文"要用）与 ``inventory``（短码 → chunk id 的映射）。
    """

    __tablename__ = "understanding"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)   # ``{paper_id}:{path}``
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(String(4), default="a")        # a / b
    markdown: Mapped[str | None] = mapped_column(Text)
    #: ``{"codes":[...], "resolved":{code: chunk_id}, "invalid":[...], "cross_road":[...]}``
    refs_json: Mapped[str | None] = mapped_column(Text)
    #: ``[{"code","chunk_id","chunk_type","page"}]``：短码映射**与结果一起存**，
    #: 这样重新分块导致短码变化时，已生成的理解仍然是自洽的
    inventory: Mapped[str | None] = mapped_column(Text)
    refs_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    #: B 路硬要求：相关概念必须引用本路 chunk（用户 ⑧）
    missing_concept_refs: Mapped[bool] = mapped_column(Boolean, default=False)
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- ⑮ 问答会话（Agent）
class Conversation(Base, TimestampMixin):
    """一个论文问答会话。

    ``head_id`` 是**可见分支的末端**：消息存成树（``parent_id``），从头沿父指针回溯
    再反转就是当前可见的对话。为什么不上来就存成线性数组：``regenerate`` 与
    "编辑我这条提问"本质都是在同一父节点下挂**兄弟**，线性数组表达不了，
    等需要时再改数据模型会很痛（历史数据没有分叉信息，补不回来）。
    """

    __tablename__ = "conversation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: 会话所属论文；NULL = 跨文献问答（P1 的 `kb_search` 用得上）
    paper_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    #: 当前可见分支的最后一条消息 id（见类文档）
    head_id: Mapped[str | None] = mapped_column(String(36))
    user_id: Mapped[int] = mapped_column(Integer, default=1)


class ConversationMessage(Base):
    """会话里的一条消息（树节点）。

    ``role`` 多出 ``summary`` 与 ``tool`` 两种：``summary`` 是压缩后的历史摘要
    （抽取式，不调模型），``tool`` 记工具轨迹 —— 两者都要能**重放**，
    否则"重新生成"和"接着问"看到的历史会不一样。
    """

    __tablename__ = "conversation_message"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    #: 同一父节点下的兄弟顺序；线性浏览（无分叉时）也按它排
    parent_id: Mapped[str | None] = mapped_column(String(36))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16))            # user/assistant/summary/tool
    content: Mapped[str | None] = mapped_column(Text)
    #: ``Citation`` 列表（给前端渲染引用表与跳转；内部 id 在这一层是**该留的**）
    citations_json: Mapped[str | None] = mapped_column(Text)
    #: 每轮的工具轨迹（名/轮次/参数摘要/耗时/状态），审计与自检读它
    tool_trace_json: Mapped[str | None] = mapped_column(Text)
    #: stop / rounds_exhausted / cancelled / error（终态口径，见规划 §11）
    finish_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
