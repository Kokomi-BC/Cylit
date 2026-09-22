-- IR（Intermediate Representation）—— 每篇论文一个 SQLite（规划 §9）
-- 定位：构建期产物 / 调试与审计留痕 / 增量重跑与可复现；
--       **不是运行时依赖**（运行时真相源 = Markdown 正文 + MySQL 定位）。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 论文级元信息
CREATE TABLE IF NOT EXISTS paper (
    paper_id        TEXT PRIMARY KEY,
    title           TEXT,
    authors         TEXT,
    venue           TEXT,
    year            INTEGER,
    page_count      INTEGER,
    source_path     TEXT,
    source_hash     TEXT,
    page_width      REAL,
    page_height     REAL,
    is_two_column   INTEGER DEFAULT 0,
    parser_version  TEXT,
    schema_version  INTEGER DEFAULT 1,
    built_at        TEXT,
    stats_json      TEXT
);

-- 页
CREATE TABLE IF NOT EXISTS page (
    page_no         INTEGER PRIMARY KEY,
    width           REAL,
    height          REAL,
    rotation        INTEGER DEFAULT 0,
    columns         INTEGER DEFAULT 1,
    gutter_x        REAL,           -- 栏间隙中心（双栏）
    image_path      TEXT,           -- 150dpi 页面 PNG
    text_chars      INTEGER DEFAULT 0,
    block_count     INTEGER DEFAULT 0,
    has_text_layer  INTEGER DEFAULT 1
);

-- 块（★ 全系统锚点）
CREATE TABLE IF NOT EXISTS block (
    id              TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL,
    page_no         INTEGER NOT NULL,
    order_in_page   INTEGER NOT NULL,
    order_global    INTEGER NOT NULL,
    type            TEXT NOT NULL,      -- para|heading|caption|formula|figure|table|ref
    col             INTEGER DEFAULT 0,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    text            TEXT,               -- 文本层原样（含可能乱码）
    text_clean      TEXT,               -- 规范化后文本
    source_text     TEXT,               -- 未经处理的源证据
    source_kind     TEXT DEFAULT 'text',-- text|ocr|manual
    font_names      TEXT,               -- JSON：去重字体名
    font_size       REAL,
    line_count      INTEGER DEFAULT 0,
    char_count      INTEGER DEFAULT 0,
    content_hash    TEXT,               -- 增量与对账
    md_anchor       TEXT,               -- 指向 Markdown 锚点
    section_id      TEXT,               -- 章节归属（P1）
    chunk_id        TEXT,               -- 与 MySQL / LanceDB 对齐
    is_inline       INTEGER DEFAULT 0,
    meta_json       TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_block_order ON block(paper_id, order_global);
CREATE INDEX IF NOT EXISTS ix_block_page ON block(paper_id, page_no, order_in_page);

-- 公式（规划 §14.2 FormulaBlock）
CREATE TABLE IF NOT EXISTS formula (
    block_id        TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL,
    page_number     INTEGER,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    raw_text        TEXT,
    image_path      TEXT,               -- 高倍裁图（人工核对 + C 级兜底）
    vector_data_json TEXT,              -- 矢量层审计结果
    latex_candidate TEXT,
    latex_repaired  TEXT,
    latex_final     TEXT,
    confidence      REAL,
    grade           TEXT,               -- A|B|C
    source          TEXT,               -- text|vector|image|llm|human
    status          TEXT DEFAULT 'candidate',
    validation_json TEXT,
    review_note     TEXT,
    is_inline       INTEGER DEFAULT 0,
    FOREIGN KEY (block_id) REFERENCES block(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_formula_grade ON formula(paper_id, grade, status);

-- 插图
CREATE TABLE IF NOT EXISTS figure (
    block_id        TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL,
    page_number     INTEGER,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    image_path      TEXT,
    caption_block_id TEXT,
    caption         TEXT,
    width_px        INTEGER,
    height_px       INTEGER,
    effective_dpi   REAL,
    edge_ink        REAL,
    subfigure_count INTEGER DEFAULT 0,
    aspect_ratio    REAL,
    audit_json      TEXT,
    status          TEXT DEFAULT 'ok',
    FOREIGN KEY (block_id) REFERENCES block(id) ON DELETE CASCADE
);

-- 表格
CREATE TABLE IF NOT EXISTS tbl (
    block_id        TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL,
    page_number     INTEGER,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    caption_block_id TEXT,
    caption         TEXT,
    image_path      TEXT,
    row_count       INTEGER,
    col_count       INTEGER,
    has_borders     INTEGER DEFAULT 0,
    grid_json       TEXT,
    status          TEXT DEFAULT 'image_only',
    FOREIGN KEY (block_id) REFERENCES block(id) ON DELETE CASCADE
);

-- 术语（两级：paper 级 / global 级；规划 §4.2）
CREATE TABLE IF NOT EXISTS term (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        TEXT,
    scope           TEXT DEFAULT 'paper',   -- paper|global
    source_term     TEXT NOT NULL,
    target_term     TEXT NOT NULL,
    note            TEXT,
    hit_count       INTEGER DEFAULT 0,
    created_at      TEXT,
    UNIQUE(paper_id, source_term)
);

-- 一致性规则与常量表（规划 §13.3 C2）
CREATE TABLE IF NOT EXISTS rule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        TEXT,
    kind            TEXT,                   -- constant|power|theorem_appendix
    symbol          TEXT,
    value           TEXT,
    value_float     REAL,
    tolerance       REAL DEFAULT 1e-6,
    source_block    TEXT,
    note            TEXT,
    UNIQUE(paper_id, kind, symbol, value)
);

-- 构建记录（可复现）
CREATE TABLE IF NOT EXISTS build (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        TEXT,
    stage           TEXT,
    status          TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    duration_ms     REAL,
    counts_json     TEXT,
    message         TEXT
);

-- 复核项（review 表）已随"复核模式"移除：新建的 IR 库不再建这张表，
-- 旧库里的 review 表保留不动，代码不再读写。
