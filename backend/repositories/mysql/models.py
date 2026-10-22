"""12 张业务表的 SQLAlchemy 2.0 映射（规划 §10）。

字段命名与规划文档逐条对齐；额外新增一张 ``setting`` 表存放
「并发上限 / RAG 参数 / 允许通用知识开关 / 数据目录」（规划 §20.7 settings）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String,
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
    "UNDERSTAND_A", "META_FILL",
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
    # 自定义分类（`paper_folder.id`）。允许 NULL = 未分类；一篇论文最多属于一个分类、只有一层。
    # 不做外键：删分类时把这一列置 NULL 即可，论文要留下（见 folder_service.delete_folder）。
    folder_id: Mapped[str | None] = mapped_column(String(32))

    # 解析摘要（便于论文库列表直接展示，不存正文）
    abstract: Mapped[str | None] = mapped_column(String(2000))
    parse_summary_json: Mapped[str | None] = mapped_column(Text)

    # ---- 元数据扩展（2026-10-07）----
    # 用户口径：元数据要「中英文摘要、中英文关键词、参考文献、所属领域、核心创新点、
    # 前置知识与难度、推荐阅读时间、一句话概括」，另外"可以加上其他有用的数据"。
    # 分工：**离线**能读出来的（原文摘要/作者/会议/年份/参考文献/阅读时间）不花钱先填；
    # 其余的由模型（或 agent）读全文后填。字段一律允许为空 = "还不知道"，不写默认值。
    abstract_zh: Mapped[str | None] = mapped_column(Text)              # 中文摘要（译文，不是概括）
    keywords_en: Mapped[str | None] = mapped_column(String(512))       # 英文关键词，逗号分隔
    # `tags` 就是**中文关键词**（老字段名保留：论文库按 tag 过滤、前端都在用）
    domain: Mapped[str | None] = mapped_column(String(128))            # 所属领域 / 研究方向
    one_liner: Mapped[str | None] = mapped_column(String(512))         # 一句话概括
    contributions_json: Mapped[str | None] = mapped_column(Text)       # 核心创新点 JSON 数组
    prereq_json: Mapped[str | None] = mapped_column(Text)              # 前置知识 JSON 数组
    difficulty: Mapped[str | None] = mapped_column(String(16))         # 入门 / 进阶 / 专家
    read_time_min: Mapped[int | None] = mapped_column(Integer)         # 推荐阅读时间（分钟）
    paper_type: Mapped[str | None] = mapped_column(String(32))         # 方法 / 综述 / 数据集 / 系统 / 理论
    doi: Mapped[str | None] = mapped_column(String(128))
    code_url: Mapped[str | None] = mapped_column(String(256))          # 开源 / 项目地址
    # 每个字段的来源：`{"abstract": "pdf", "tags": "agent", ...}`。
    # 为什么要它：以前**没有来源标记**，所以"补全"只能一律"只填空字段" —— 分不清
    # "空是因为论文里没有"还是"用户故意清空了"，也就没法让模型重填某一项。
    meta_sources_json: Mapped[str | None] = mapped_column(Text)
    meta_filled_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_paper_status", "status"),
        Index("ix_paper_last_read_at", "last_read_at"),
    )


# ------------------------------------------------- ①.2 自定义分类（文件夹）
class PaperFolder(Base, TimestampMixin):
    """论文库的自定义分类（单层文件夹）。

    为什么不是"给论文加一个 category 字符串"：分类要能改名、能删、要能算"这一类有几篇" ——
    字符串列做不到"改名一次全体生效"，也数不出空分类。单用户系统，所以分类全局唯一、不带 owner 列。
    """

    __tablename__ = "paper_folder"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 分类名。唯一索引是**兜底**：业务层先按名查重给 409，并发窗口里才靠它报冲突。
    name: Mapped[str] = mapped_column(String(64), unique=True)
    #: 排序号，小的在前
    sort: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------- ①.1 参考文献（元数据的一部分）
class PaperReference(Base):
    """参考文献一条。

    为什么单独一张表而不是塞进 `paper.abstract` 那种字符串列：一篇论文几十上百条，
    要看得到、数得清、还要能被后续检索（"哪篇论文引了 X"），所以按行存。
    离线从解析出的 `ref` 块抽（`backend/parsing` 已经把参考文献切成了 `ref` 类型的块），
    `title/authors/venue/year` 这些结构化字段留给模型或后续解析补。

    2026-10-07 起 `title` / `authors` / `title_en` 由**翻译产物**对齐着填
    （`metadata_fill.split_ref_text`）：译文里只有标题被换成了《中文标题》，于是
    `title` = 中文标题、`title_en` = 原文里的英文标题、`authors` = 作者那一段 ——
    侧栏据此中英对照显示，并把作者那一段收起来（`raw` 永远是完整原文，不会丢东西）。
    """

    __tablename__ = "paper_reference"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    order_no: Mapped[int] = mapped_column(Integer, default=0)
    #: 原始条目（去掉换行与连字问题之后的一整条），永远保留 —— 结构化字段可能解析错
    raw: Mapped[str | None] = mapped_column(Text)
    #: 中文标题（译文里《…》那一段）
    title: Mapped[str | None] = mapped_column(String(512))
    #: 英文标题（原文里切掉作者段与会议/年份/链接之后剩下的那一段）
    title_en: Mapped[str | None] = mapped_column(String(512))
    #: 作者段（与原文逐字相同的那一段；侧栏默认**不显示**，只在 tooltip 里给）
    authors: Mapped[str | None] = mapped_column(String(512))
    #: 这一条出现在**第几页**（侧栏每张卡片的「定位」按钮跳的就是它）。
    #: 只存页码不存行号：行号是位置编号（`p011-05`），重译后块数一变就指到别的文字上了，
    #: 页码在"这篇论文的排版没变"这个前提下才是稳的。
    page_no: Mapped[int | None] = mapped_column(Integer)
    venue: Mapped[str | None] = mapped_column(String(256))
    year: Mapped[int | None] = mapped_column(Integer)
    doi: Mapped[str | None] = mapped_column(String(128))
    #: 这一条里的 **GitHub 仓库地址**（用户口径 2026-10-07：既有论文链接又有代码链接的，
    #: 侧栏并排给两枚图标 —— 论文图标跳论文页、GitHub 图标跳仓库，互不干扰）。
    #: 从原文里抠出来（`metadata_fill.ref_code_url`），没有就是 NULL（那一枚图标不显示）。
    code_url: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(String(256))
    #: 这一条是哪来的：现在只有 `pdf`（离线抽取）
    source: Mapped[str] = mapped_column(String(16), default="pdf")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (Index("ix_paper_reference_order", "paper_id", "order_no"),)


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
# 高亮出现的"面"：正文行（锚点 = 行 id） / Markdown 视图（锚点 = `md:p<页号>`） /
# **原 PDF 页图**（锚点 = `pdf:p<页号>`，偏移量相对该页 pdf.js 文字层的渲染文本）。
# 三套锚点刻意**不统一**：PDF 与 Markdown 是两种页面存储，硬套同一个位置模型必然错位。
HIGHLIGHT_SCOPES = ("row", "markdown", "pdf")


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


# ---------------------------------------------------------------- ⑦b 图片批注文档
class ImageMark(Base, TimestampMixin):
    """大图查看器里的一份「图片批注文档」（手写笔迹 + 图上的笔记）。

    为什么不塞进 `highlight` / `note`：这里的坐标系是**图片自身的归一化坐标**（0..1），
    跟正文行的字符偏移是两套东西；而且一次查看里的笔迹与笔记要**原子地一起存**
    （撤销 / 橡皮 / 加笔记都是整份文档的变化），所以按 `(paper_id, key)` 存一份 JSON，
    前端防抖覆盖写。

    `key` 是**图片锚点**：`media:<sha1(url)>`（插图 / 图表卡片）或 `page:<页号>`（整页页图）。
    用锚点而不是 URL 本身 —— URL 带签名/时间戳时换个会话就换 URL，笔迹会全部丢。
    """

    __tablename__ = "image_mark"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, default=1)
    paper_id: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(160))
    #: 文档本体（`{"v":1,"strokes":[…],"notes":[…]}`），见 `services/annotation/image_marks.py`
    doc_json: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("user_id", "paper_id", "key", name="uq_image_mark_owner"),
    )


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


# ---------------------------------------------------------------- ⑨½ 模型提供方
class ModelProvider(Base):
    """模型提供方（厂商身份 + 连接方式 + 凭据引用）。

    用户口径（改造计划 §5）：Provider 是**一级实体** —— 一家厂商一条记录，
    模型挂在这条记录上；官方 / 自定义由 `kind` 区分，`kind="official"` 只能由后端
    按 `presets.py` 里真有的厂商写，前端字段管不到它。

    ⚠️ 历史数据里「提供方」只是 `model_config.provider` 一个字符串，所以本表允许
    **先有模型、后有 provider 行**：读取时会把模型行上的字符串回填成 provider 记录
    （`provider_store.ensure_providers`，幂等）。表由启动时的 `create_all` 建。
    """

    __tablename__ = "model_provider"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # official = 后端预设里真有的厂商；custom = 用户自己填的端点
    kind: Mapped[str] = mapped_column(String(16), default="custom")
    label: Mapped[str] = mapped_column(String(128))
    # 图标键（与前端 assets/model-icons 同一套），空 = 用统一占位图标
    icon: Mapped[str] = mapped_column(String(32), default="")
    base_url: Mapped[str | None] = mapped_column(String(256))
    auth: Mapped[str] = mapped_column(String(16), default="bearer")   # bearer | none
    secret_ref: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


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
    path: Mapped[str] = mapped_column(String(4), default="a")        # a（老库里可能还有 b）
    markdown: Mapped[str | None] = mapped_column(Text)
    #: ``{"codes":[...], "resolved":{code: chunk_id}, "invalid":[...], "cross_road":[...]}``
    refs_json: Mapped[str | None] = mapped_column(Text)
    #: ``[{"code","chunk_id","chunk_type","page"}]``：短码映射**与结果一起存**，
    #: 这样重新分块导致短码变化时，已生成的理解仍然是自洽的
    inventory: Mapped[str | None] = mapped_column(Text)
    refs_ok: Mapped[bool] = mapped_column(Boolean, default=False)
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
    #: **建会话那一刻冻结的问答风格**（提示词里可改的那一段，见 `services/agent/style.py`）。
    #: 用户口径 2026-10-15："风格只对新对话生效" —— 所以它记在会话行上，之后设置页怎么改
    #: 都追不到已有会话：一来行为可预期，二来 system 前缀不会因为一次改动把**所有**会话的
    #: `system + 全部历史` 缓存同时打掉（前缀缓存是从第 0 个 token 起比的）。
    #: NULL = 升级前建的老会话（还没有这一列），首次提问时按"谁更晚"判一次再冻结。
    agent_style: Mapped[str | None] = mapped_column(Text)
    #: 这一条会话选的问答模型（`model_config.id`，只认 agent 类型）与推理等级。
    #: **NULL = 跟随默认**（agent 默认 → generation 默认，见 services/agent/model_config.py）：
    #: 升级前的老会话与"没在对话里换过模型"的会话都落在这一档，不给默认值就等于替用户选。
    model_config_id: Mapped[str | None] = mapped_column(String(36))
    reasoning_effort: Mapped[str | None] = mapped_column(String(32))
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
    #: 用户上传/粘贴的图片（``[{attachment_id, name, mime, bytes}]``）。
    #: 存**消息上**而不是单独一张表：图片是这一轮的输入，跟着消息一起重放
    #: （"重新生成"要能拿到当时那几张图，见 `conversations.branch`）。
    attachments_json: Mapped[str | None] = mapped_column(Text)
    #: 思考模式的思维链（**只给前端折叠展示**：不进正文、不进下一轮历史、不参与引用清洗）
    thinking: Mapped[str | None] = mapped_column(Text)
    #: 思考耗时（毫秒，模型调用 → 第一段正文）；没有思考模式时为空
    thinking_ms: Mapped[int | None] = mapped_column(Integer)
    #: 这一轮的**过程轨迹**（`{"steps": [...], "elapsed_ms": N, "rounds": N}`，见 `agent/trace.py`）：
    #: 多轮思考与工具调用**按发生顺序**排成一条流水，重开会话时照着它重画。
    #: 与 `tool_trace_json`（工具审计表）分开：那个只记工具，这份记"用户看得见的过程"。
    run_json: Mapped[str | None] = mapped_column(Text)
    #: stop / rounds_exhausted / cancelled / error（终态口径，见规划 §11）
    finish_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- ⑯ 模型用量
class ModelUsage(Base):
    """**一次**模型调用的 token 用量（模型设置页的消耗统计数据源）。

    为什么单独一张表：翻译 / 理解 / 元数据 / 问答四条链路的入参口径完全不同，
    唯一共同的只有 provider 返回的 usage —— 塞进 ``task.stats_json`` 就没法按天折线、
    按模型排行地查（任务会被清理、会被重跑覆盖）。

    ``model_name`` / ``provider`` / ``model_type`` 是**反规范化**存的一份：模型配置被删掉之后，
    那条配置历史上的消耗仍然要能在排行榜上显示出来（否则"总消耗"会随删除凭空缩水）。
    显示名同理，缺失时前端回落到 ``model_name``。

    ``day`` 是按天聚合用的 **本地日**（口径说明见 ``services/model/usage_stats.py``），
    ``created_at`` 沿用全库约定 = UTC naive。
    """

    __tablename__ = "model_usage"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    #: 发起这次调用的模型配置（可空：配置被删 / 历史行 / agent 侧只拿到 config_id）
    model_config_id: Mapped[str | None] = mapped_column(String(64))
    #: 反规范化的模型标识（配置删了统计仍要显示）
    model_name: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(32))
    model_type: Mapped[str | None] = mapped_column(String(16))
    #: 哪条链路花的钱：translate / transcribe / understand / formula / vision / ask / generation
    source: Mapped[str] = mapped_column(String(32), default="generation")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    #: 冗余存一份：厂商的 total 有时含缓存计费口径，不等于两项之和
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    #: 聚合键（DATE）。取**本地日** —— 用户看到的「今日消耗」才是他那一整天
    day: Mapped[date] = mapped_column(Date, default=lambda: datetime.now().date())

    __table_args__ = (Index("ix_model_usage_day", "day"),)
