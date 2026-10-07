/** 后端数据模型（对齐 docs/API.md 与 backend/repositories/mysql/models.py 的序列化结果）。 */

export type PaperStatus =
  | 'imported' | 'parsing' | 'parsed' | 'translating' | 'translated'
  | 'analyzing' | 'indexed' | 'failed' | 'error';

export interface Paper {
  id: string;
  title: string;
  authors: string | null;
  venue: string | null;
  year: number | null;
  page_count: number | null;
  status: PaperStatus | string;
  tags: string[];
  translate_done: number | null;
  translate_total: number | null;
  analyze_done: number | null;
  analyze_total: number | null;
  pdf_translate_done: number | null;
  pdf_translate_total: number | null;
  in_knowledge_base: boolean;
  source_hash: string | null;
  abstract: string | null;
  parse_summary: Record<string, unknown>;
  last_read_at: string | null;
  created_at: string | null;
  deleted_at: string | null;

  /* ---- 元数据扩展（2026-10-04）：中英文摘要/关键词、领域、创新点、前置知识、
     难度、推荐阅读时间、一句话概括、类型、DOI/开源地址、字段来源 ---- */
  abstract_zh?: string | null;
  /** 英文关键词（中文关键词就是 `tags`） */
  keywords_en?: string[];
  /** 所属领域 / 研究方向 */
  domain?: string | null;
  /** 一句话概括 */
  one_liner?: string | null;
  /** 核心创新点（2~5 条） */
  contributions?: string[];
  /** 前置知识 */
  prereq?: string[];
  /** 入门 / 进阶 / 专家 */
  difficulty?: string | null;
  /** 推荐阅读时间（分钟） */
  read_time_min?: number | null;
  /** 方法 / 综述 / 数据集 / 系统 / 理论 / 其他 */
  paper_type?: string | null;
  doi?: string | null;
  /** 开源 / 项目地址 */
  code_url?: string | null;
  /** 字段 → 来源（`pdf` / `model` / `agent` / `manual`） */
  meta_sources?: Record<string, string>;
  meta_filled_at?: string | null;
  /** 参考文献（只有详情接口 `GET /papers/{id}` 带；列表接口不带） */
  references?: PaperReference[];
  reference_count?: number;
}

/** 参考文献一条（离线从页产物的 `ref` 行 / Markdown 的 References 节抽出来）。 */
export interface PaperReference {
  id: string;
  order_no: number;
  /** 原始条目（结构化字段可能解析错，所以原文永远保留） */
  raw: string;
  title?: string | null;
  authors?: string | null;
  venue?: string | null;
  year?: number | null;
  doi?: string | null;
  url?: string | null;
  source?: string;
}

export interface FormulaValidation {
  syntax_ok?: boolean;
  symbols_ok?: boolean;
  constants_ok?: boolean;
  rerender?: string;
  rerender_score?: number | null;
  issues?: string[];
  symbol_diff?: { unknown_symbols?: string[]; unknown_commands?: string[] };
  constants_checked?: unknown[];
  all_passed?: boolean;
  llm_changed_structure?: boolean;
  [key: string]: unknown;
}

export interface FormulaCard {
  id: string;
  page: number;
  bbox: number[] | null;
  raw_text: string | null;
  latex_candidate: string[];
  latex_repaired: string | null;
  latex_final: string | null;
  grade: 'A' | 'B' | 'C' | string;
  confidence: number | null;
  source: string | null;
  status: string | null;
  vector_audit: Record<string, any>;
  validation: FormulaValidation;
  review_note: string | null;
  is_inline: boolean;
  crop_url: string | null;
  crop_path?: string | null;
}

export type BlockType =
  | 'para' | 'heading' | 'formula' | 'caption' | 'figure' | 'table' | 'ref';

export interface Block {
  id: string;
  page: number;
  type: BlockType | string;
  col: number;
  order: number;
  bbox: number[];
  anchor: string;
  text: string;
  heading_level: number | null;
  /** 块级译文：页级时代只有历史产物里才有（新论文一律空串），用于未翻译页退回解析块时的占位 */
  translation: string;
  content_hash: string | null;
  section: string | null;
  formula?: FormulaCard;
  /** 前端本地字段：LiveUpdater 刚更新时的时间戳（用于闪一下的动画） */
  flash?: number;
}

export interface PageInfo {
  page: number;
  width: number;
  height: number;
  columns: number;
  gutter_x: number | null;
  blocks: number;
  has_text_layer: boolean;
  /** 该页是否已有页级翻译产物 */
  translated?: boolean;
  row_count?: number;
  /** 行对齐质量：exact（块数相同，逐块对应）/ structure（块数不同，结构对齐） */
  align?: string | null;
  /** 该页的插图（按图注锚定的裁图；`url` 指向按需裁图接口） */
  media?: Array<{
    url: string;
    /** 图 + 图注合成的「图表卡片」（放大看大图 / 导出用） */
    card_url?: string;
    caption: string;
    status?: string | null;
    /** figure | table */
    kind?: string;
    /** 图号（用于把图挂到对应的图注行） */
    number?: string;
    /** 同一张图被切成多段时的段号（从 0 开始） */
    part?: number;
    /** 总段数 */
    parts?: number;
    /** 段标签：上段 / 下段 / 跨页续图 */
    part_label?: string;
    /** 区域 bbox（PDF pt，[x0,y0,x1,y1]）：用来判断"这张图在一页里占多大"——
     *  占比大的图允许显示得更大（见 `ReaderPageRows.isLargeFigure`）。 */
    bbox?: number[];
  }>;
}

/** 页级翻译的一行：**原文与译文成对**（模型同一次调用产出，天然对齐）。 */
export interface PageRow {
  id: string;
  /** 全局序号（跨页递增，前端排序用） */
  order?: number;
  page: number;
  index: number;
  type: 'heading' | 'para' | 'formula' | 'caption' | 'list' | 'table' | 'code' | 'ref' | string;
  level: number;
  number: string;
  original: string;
  translation: string;
  /** 本行句子延续到下一页（后端按原文判定；前端只加「续下页」提示，不隐藏内容） */
  continues?: boolean;
  /** 本行句子从上一页接过来（后端已清掉「……」/「承接上页」这类占位噪声） */
  continued_from_prev?: boolean;
}

/** 一页的页级翻译产物（两份逐块对应的 Markdown + 对齐后的行）。 */
export interface PageTranslation {
  page: number;
  page_total: number;
  original_md: string;
  translation_md: string;
  rows: PageRow[];
  align: string | null;
  model: string | null;
  updated_at: string | null;
}

/** **带插图**的双语 Markdown（后端把插图行插到图注上方后才返回）。 */
export interface TranslatedMarkdownPage {
  page: number;
  page_total: number;
  original_md: string;
  translation_md: string;
  align: string | null;
  model: string | null;
}

/**
 * 公式条目：**译文优先，未翻译的页用原文（解析产物）兜底** —— 与正文里那一页显示的同源。
 */
export interface MarkdownFormula {
  id: string;
  page: number;
  /** 公式所在行的行 id（定位用）。解析产物兜底时就是**解析块的 id**（正文那一行的锚点） */
  row_id: string | null;
  latex: string;
  /** true = 行间公式（`$$…$$`），false = 行内（`$…$`） */
  display: boolean;
  /** 原文上下文片段（译文来源的条目才有） */
  context: string;
  /** 译文上下文片段 */
  context_zh: string;
  model?: string | null;
  /** `markdown` = 从译文抽的；`parse` = 这一页还没翻译，用解析产物兜底 */
  source?: 'markdown' | 'parse' | string;
  /** 解析产物来源才有：公式分级（A/B/C） */
  grade?: string | null;
  /** 解析产物来源才有：高倍裁图地址（C 级公式只有它可用） */
  crop_url?: string | null;
}

export type MarkdownViewMode = 'bilingual' | 'original' | 'translation';

export interface ReadingProgress {
  last_block_id: string | null;
  last_page: number;
  percent: number;
  view_mode: ViewMode;
  pair_order: PairOrder;
  /** 原文栏呈现（`text` 原文文本 / `page` 原 PDF）—— 与 `view_mode`/`pair_order` 一起按论文保存 */
  original_mode: OriginalMode;
  /** 界面状态：侧栏展开 / 正文字号 / 显示参考文献 / 原 PDF 缩放。
   *  `null` = 这篇论文还没记过 → 回退到本地偏好 / 默认值。 */
  aside_open: boolean | null;
  text_size: 'sm' | 'md' | 'lg' | null;
  show_refs: boolean | null;
  zoom: number | null;
  /** 对照 + 原 PDF 时右侧文字列放哪一侧（`zh` 译文 / `en` 原文） */
  page_side: 'en' | 'zh';
  scroll_ratio: number;
  updated_at?: string;
}

export type ViewMode = 'original' | 'translation' | 'bilingual';
/** 对照方向：`en_left` = 原文在左（当前默认，左右对照）；`en_top/zh_top` 为历史取值，后端会归一化。 */
export type PairOrder = 'en_left' | 'zh_left' | 'en_top' | 'zh_top';
/** 原文栏的呈现：`text` = 模型转录的原文文本（逐段对齐）；`page` = 原 PDF 页图（按页对齐） */
export type OriginalMode = 'text' | 'page';

/** 目录项（**中英双语**：`text` = 英文原文，`text_zh` = 中文译文）。 */
export interface OutlineItem {
  block_id: string;
  page: number;
  level: number;
  number: string;
  text: string;
  text_zh?: string;
  children: OutlineItem[];
}

export interface Outline {
  items: OutlineItem[];
  tree: OutlineItem[];
  count: number;
}

export interface Annotation {
  id: string;
  paper_id?: string;
  block_id: string | null;
  /** 锚点所在页（行 id 失效时按页定位用） */
  page?: number | null;
  type: string;
  content_md: string;
  source: string;
  sources?: Array<Record<string, any>>;
  model_id?: string | null;
  confidence?: number | null;
  created_at: string | null;
}

export interface Highlight {
  id: string;
  paper_id?: string;
  /** 锚点：正文行 = 行 id；Markdown 视图 = `md:p<页号>` */
  block_id: string;
  text: string | null;
  start: number;
  end: number;
  color: 'yellow' | 'green' | 'blue' | 'pink' | 'purple' | string;
  /** 偏移量相对哪一侧的文字：**原文 en / 译文 zh 分开存**（同一行两侧共用一个 block_id） */
  side?: 'en' | 'zh';
  /** 在哪个视图标的：正文行 row / Markdown 视图 markdown */
  scope?: 'row' | 'markdown';
  /** 跨行高亮的分组：同一次划词切出来的几条记录共用它，删除时整组一起删 */
  group_id?: string | null;
  page?: number | null;
  created_at?: string | null;
}

export interface Note {
  id: string;
  paper_id: string;
  block_id: string | null;
  page?: number | null;
  selected_text: string | null;
  content_md: string;
  tags?: string[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TaskInfo {
  id: string;
  type: string;
  queue: string;
  queue_code?: number;
  paper_id: string | null;
  status: string;
  progress: number;
  done_items: number;
  total_items: number;
  cursor?: Record<string, any>;
  params?: Record<string, any>;
  cancel_requested?: boolean;
  pause_requested?: boolean;
  message: string | null;
  error?: string | null;
  stats?: Record<string, any>;
  created_at: string | null;
  updated_at: string | null;
  finished_at?: string | null;
  log_tail?: string[];
}

export interface ReaderStats {
  blocks: number;
  by_type: Record<string, number>;
  /** 页级进度：`pages_done / pages`（当前唯一粒度） */
  pages?: number;
  pages_done?: number;
  rows?: number;
  align?: Record<string, number>;
  model?: string;
  translated: number;
  translate_total: number;
  translate_percent: number;
}

export interface ReaderPayload {
  paper: Paper;
  blocks: Block[];
  pages: PageInfo[];
  /** 页级翻译产物（原文/译文两份 Markdown + 对齐后的行） */
  page_translations?: PageTranslation[];
  /**
   * 解析块 id → 正文行 id（如 `attentionisa-p01-b06` → `p001-06`）。
   *
   * 两套 id 命名空间不通用：已翻译页的正文行来自页级产物（位置编号 `p{页}-{序}`），
   * 检索/RAG 侧存的却是解析块 id。深链定位靠这张表把块锚点翻成行锚点；
   * 表里没有的块（公式 / 插图 / 表格，正文行里没有独立段落）退化为按 `page` 跳。
   */
  block_rows?: Record<string, string>;
  progress: ReadingProgress;
  annotations: Annotation[];
  highlights: Highlight[];
  notes: Note[];
  tasks: Array<Pick<TaskInfo, 'id' | 'type' | 'queue' | 'status' | 'progress' | 'message' | 'done_items' | 'total_items'>>;
  /** 目录：按 Markdown 标题结构（与事实源同源；伪代码假标题已剔除） */
  outline: Outline;
  stats: ReaderStats;
  since: number | null;
  generated_at: string;
  paper_dir: string;
}

export interface ModelConfig {
  id: string;
  name: string;
  model_type: string;
  provider: string;
  model_name: string | null;
  base_url: string | null;
  secret_ref: string | null;
  parameters: Record<string, any>;
  enabled?: boolean;
  is_default?: boolean;
  status?: string | null;
  last_tested_at?: string | null;
  has_secret?: boolean;
  is_mock?: boolean;
  /** 内置占位模型（离线兜底）：删掉后不会再自动重建，可在模型页「恢复内置占位」找回 */
  builtin?: boolean;
  /** 兼容旧接口：连通性测试结果可能直接挂在模型记录上 */
  latency_ms?: number | null;
}

/** `POST /models/{id}/test` 的返回（字段随 provider 略有差异，故保留动态键）。 */
export interface ModelTestResult {
  ok?: boolean;
  latency_ms?: number | null;
  latency?: number | null;
  dim?: number | null;
  device?: string | null;
  detail?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

export interface ModelTypeDefault {
  id: string;
  name: string;
  provider: string;
  model_name: string | null;
  builtin?: boolean;
  is_mock?: boolean;
}

export interface ModelPreset {
  id: string;
  label: string;
  provider: string;
}

export interface KnowledgeOverview {
  papers_total: number;
  papers_in_kb: number;
  chunks: number;
  chunks_by_type: Record<string, number>;
  /** 语言行数：`{zh: n, en: m, any: k}`（`any` = 语言无关的公式原子行） */
  chunks_by_lang?: Record<string, number>;
  /** 已入库 chunk 用到的 embedding 模型（后端解析成可读名字） */
  embedding_models: Array<{ id: string; label: string; model_name?: string | null }>;
  index_versions: Record<string, number>;
  vector_store: Record<string, any>;
  items: Array<{ id: string; title: string; status: string; in_knowledge_base: boolean; chunks: number }>;
}

export interface ChunkMeta {
  id: string;
  paper_id: string;
  block_id: string;
  chunk_type: string;
  page: number;
  /** 双语索引：`zh` 中文行 / `en` 原文行（同一 chunk 各一条） */
  lang?: string;
  section: string | null;
  preview: string;
  hash: string;
  tokens: number;
  index_version: number;
  embedding_model_id: string | null;
}

/** `/knowledge/chunks` 的一条（知识库页「浏览」用）。 */
export interface KnowledgeChunkRow {
  id: string;
  paper_id: string;
  block_id: string;
  chunk_type: string;
  page: number;
  lang?: string;
  section: string | null;
  preview: string;
  /** 图注（插图/表格 chunk 有）——列表里显示它比显示 "插图 <块 id>：…" 干净 */
  caption?: string | null;
  tokens: number;
  index_version: number;
  /** 插图/表格/公式的裁图；正文为 null */
  image_url?: string | null;
}

/** `/knowledge/chunk`（知识库页「展开全文」）：只有当前这一条，不做中英对照。 */
export interface KnowledgeChunkDetail {
  chunk_id: string;
  paper_id: string;
  paper_title?: string;
  block_id?: string | null;
  page: number;
  section?: string | null;
  chunk_type: string;
  lang: string;
  content: string;
  chars: number;
  tokens?: number | null;
  index_version?: number | null;
  image_url?: string | null;
  pair_id?: string | null;
}

/** `/rag/similar` 的一条命中（后端按白名单裁剪后的字段）。 */
export interface SimilarItem {
  chunk_id: string;
  paper_id: string;
  paper_title?: string;
  block_id?: string | null;
  page?: number;
  section?: string | null;
  chunk_type?: string;
  lang?: string;
  langs?: string[];
  snippet?: string;
  /** 图注（插图/表格条目）——比 snippet 的 "插图 <块 id>：…" 干净 */
  caption?: string | null;
  image_url?: string | null;
  scores?: Record<string, number | null>;
  ranks?: Record<string, number | null>;
}

export interface SimilarResult {
  implemented?: boolean;
  query?: string;
  scope?: string;
  items?: SimilarItem[];
  retrieval?: Record<string, any>;
}

export interface RagSource {
  paper_id?: string;
  paper_title?: string;
  block_id?: string;
  page?: number;
  section?: string | null;
  chunk_type?: string;
  lang?: string;
  /** 来源编号（`S1`、`S2`…），与答案里的 `[S1]` 对应 */
  label?: string;
  index?: number;
  snippet?: string;
  score?: number;
  [key: string]: any;
}

export interface RagValidation {
  grounded: boolean;
  cited: string[];
  unknown_citations: string[];
  quotes: number;
  unsupported_quotes: Array<{ quote: string; matched: string | null }>;
  math_quotes_unchecked?: string[];
  problems: string[];
  notes?: string[];
}

export interface RagAnswer {
  implemented: boolean;
  message?: string;
  answer?: string;
  sources?: RagSource[];
  grounded?: boolean;
  /** 模型明确表示"库内文献不足以回答"（回答以【无法回答】开头） */
  insufficient?: boolean;
  validation?: RagValidation;
  context?: { sources: number; chars: number; truncated: number };
  usage?: Record<string, any>;
  retrieval_metadata?: Record<string, any>;
}

export interface FigureItem {
  block_id: string;
  /** 图注行的行 id（存在时用它定位；重译后行号会变，故另带 page 兜底） */
  row_id?: string | null;
  page: number;
  caption: string;
  bbox: number[];
  number?: string;
  status: string;
  audit: Record<string, any>;
  image_url: string | null;
  /** 图 + 图注合成的「图表卡片」（放大看大图 / 导出用）；缺省时退回 image_url */
  card_url?: string | null;
  /** 同一张图被切成多段时的段号 / 总段数 / 段标签 */
  part?: number;
  parts?: number;
  part_label?: string;
}

/**
 * 「点击看大图」的目标。
 *
 * 不只带 URL，还带图注 —— 放大弹窗的标题要显示**这张图的标题**（用户口径），
 * 而不是干巴巴的"插图"两个字；图注本身通常已含图号（"Figure 3: …"），所以不再另加前缀。
 */
export interface ZoomTarget {
  url: string;
  caption?: string | null;
  /** 图注所在页（没有图注时兜底显示"插图 · 第 N 页"） */
  page?: number | null;
  number?: string | null;
}

export interface GlossaryTerm {
  source?: string;
  target?: string;
  en?: string;
  zh?: string;
  level?: string;
  [key: string]: any;
}

export interface BriefPayload {
  paper_id: string;
  domain?: string;
  style?: string;
  notes?: string[];
  /** 保持英文不译的词/缩写（模型给出，人工可改） */
  keep_as_is?: string[];
  glossary?: Record<string, string>;
  /** 与术语同一次调用产出的论文元数据（venue/year/tags/abstract_zh 等） */
  meta?: Record<string, any>;
  source?: string;
  prompt?: string;
  [key: string]: any;
}

/* ------------------- 第二阶段「论文理解」（阅读器「解读」页签用） ------------------- */

/**
 * 一条 chunk 引用（后端把短码解析成真实 chunk 之后的那一份）。
 *
 * `unit`（语义单元）也会出现在这里：理解读的就是 chunk，引用基准单位与知识库同一套
 * （用户口径：`来源 Chunk: B003` 这种小胶囊点一下要能跳回原文那一页）。
 */
export interface UnderstandingRefItem {
  code: string;
  chunk_id: string;
  chunk_type?: string | null;
  page?: number | null;
}

/** 解读结果（`GET /papers/{id}/understanding`，与 `understanding_service.to_dict` 对齐）。 */
export interface UnderstandingPayload {
  paper_id?: string;
  path?: 'a';
  label?: string;
  markdown?: string;
  refs?: string[];
  refs_resolved?: UnderstandingRefItem[];
  /** 重新翻译/重建索引后 chunk id 失效的引用：仍带页码，照样能跳（后端保留，不清空解读） */
  stale_refs?: UnderstandingRefItem[];
  invalid_refs?: string[];
  cross_road_refs?: string[];
  refs_ok?: boolean;
  model?: string;
  updated_at?: string;
  inventory_size?: number;
}

/**
 * `POST /rag/similar`（scope=paper）的一条命中。
 *
 * 阶段二契约给的是 `chunk_id / chunk_type / page / lang / preview（或 content）/ metadata`；
 * 这里额外兼容既有 `/rag/similar` 的字段名（`snippet` / `caption` / `scores`）——
 * 同一个接口被知识库页与阅读器共用，两边字段名不完全一样时前端不该二选一崩掉。
 */
export interface SemanticHit {
  chunk_id: string;
  paper_id?: string;
  block_id?: string | null;
  chunk_type?: string;
  page?: number | null;
  lang?: string;
  preview?: string;
  content?: string;
  caption?: string | null;
  snippet?: string;
  section?: string | null;
  image_url?: string | null;
  scores?: Record<string, number | null>;
  metadata?: Record<string, any> | null;
}

/** `POST /rag/similar` 的返回（`retrieval` = 检索漏斗，前端只在提示里用一两个数字）。 */
export interface SemanticResult {
  implemented?: boolean;
  message?: string;
  query?: string;
  scope?: string;
  items?: SemanticHit[];
  retrieval?: Record<string, any>;
}

export interface SettingsPayload {
  values: Record<string, any>;
  stored: Record<string, any>;
  editable: string[];
}

/* ------------------------------- 解析引擎 -------------------------------
 * `local` = 本地 PyMuPDF 版面解析（离线）；`mineru` = MinerU 云端解析（要 token + 联网）。
 * 导入弹窗的「解析引擎」二选一与设置页的「默认解析引擎」共用这一个联合类型，
 * 两处取值必须一致，所以类型只在这里写一次。 */
export type ParseEngine = 'local' | 'mineru';

/** MinerU 模型版本：`pipeline` 版面+公式更稳；`vlm` 大模型，复杂版面更强。 */
export type MineruModelVersion = 'pipeline' | 'vlm';

/** `GET /mineru/config`（`PUT` 返回同一形状）。**任何字段都可能缺省**，前端按"没有就退回默认值"读。 */
export interface MineruConfig {
  has_token: boolean;
  /** 掩码后的 token（`ab12…cd34`）；没有 token 时为 null —— 后端**从不回显明文**。 */
  token_masked: string | null;
  /** token 来源：file（密钥文件）/ env（环境变量）等 */
  token_source?: string | null;
  engine_default: ParseEngine;
  model_version: MineruModelVersion;
  language: string;
  /** 模型选项（带中文标签）——导入弹窗的「模型」下拉用它 */
  model_version_options?: Array<{ value: MineruModelVersion; label: string }>;
  /** OCR 语言选项（`value` + 中文名）——导入弹窗的「OCR 语言」下拉用它 */
  languages?: Array<{ value: string; label: string }>;
  base_url?: string;
  /** 密钥文件路径（设置页要显示它，用户才知道 token 存在哪） */
  token_file?: string | null;
  /** token 已配且可用（= `has_token` 且联网可用） */
  online_ready?: boolean;
}

/** `PUT /mineru/config` 的请求体：**只传要改的字段**（省略 = 不动）。 */
export interface MineruConfigPatch {
  token?: string;
  engine_default?: ParseEngine;
  model_version?: MineruModelVersion;
  language?: string;
  clear_token?: boolean;
}

/** `POST /mineru/verify`：**HTTP 恒为 200**，成败看 `ok` / `state`（失败也要能读到 message）。 */
export interface MineruVerifyResult {
  ok: boolean;
  /** invalid=token 无效 / expired=已过期 / network=连不上 / missing=没配 token */
  state: 'valid' | 'invalid' | 'expired' | 'network' | 'missing' | string;
  http_status?: number | null;
  code?: number | null;
  /** 一句可执行的中文提示（例如「Token 已过期（A0211）：请到 mineru.net 重新申请一个」） */
  message?: string;
  latency_ms?: number | null;
  token_masked?: string | null;
}

export interface HealthPayload {
  database?: Record<string, any>;
  vector_store?: Record<string, any>;
  task_runner?: Record<string, any>;
  data_dir?: string;
  error?: string;
}

/* --------------------------- 问答 agent（规划 §4.3） --------------------------- */

/** 引用：`ref` 是展示编号（`done` 阶段被压成 1..M）；跳转用 `paper_id + block_id/row_id`。 */
export interface AgentCitation {
  ref: number;
  paper_id: string;
  kind: string;
  page?: number | null;
  section?: string | null;
  block_id?: string | null;
  row_id?: string | null;
  snippet?: string;
  image_urls?: string[];
  label?: string;
}

/** 对话里上传/粘贴的图片（落盘在 `data/attachments`，消息上只存这份元数据）。 */
export interface AgentAttachment {
  attachment_id: string;
  name: string;
  mime: string;
  bytes: number;
  /** 后端给的相对路径，前端用 `assetUrl()` 拼成可访问 URL */
  url: string;
}

/**
 * 过程轨迹里的一步（多轮思考 / 工具调用 / 宿主阶段）。
 *
 * 形状由后端 `agent/trace.py` 定义，前端只负责翻译成中文短语与图标 ——
 * 存的是 `tool` 名而不是文案，所以以后改叫法不用动数据。
 */
export interface AgentStep {
  kind: 'stage' | 'thinking' | 'tool' | string;
  /** 第几轮模型调用（宿主动作用 0） */
  round?: number;
  /** `stage` / `thinking` 的文本（思考可能被后端截断，`truncated` 标记） */
  text?: string;
  truncated?: boolean;
  /** `tool` 的工具名（`__presearch__` = 宿主的预检索） */
  tool?: string;
  /** 工具入参摘要（"query=随机旋转 量化"） */
  digest?: string;
  status?: 'running' | 'done' | string;
  ms?: number | null;
}

/** 一轮问答的过程轨迹：有序的 `steps` + 整轮耗时 + 轮数。 */
export interface AgentRun {
  steps?: AgentStep[];
  elapsed_ms?: number;
  rounds?: number;
}

export interface AgentMessage {
  message_id: string;
  conversation_id: string;
  parent_id?: string | null;
  seq: number;
  /** `summary` = 压缩产生的那条要点（对话留痕，不是模型回答） */
  role: 'user' | 'assistant' | 'summary' | 'tool' | string;
  content: string;
  citations?: AgentCitation[];
  tool_trace?: Array<Record<string, any>>;
  /** 图片附件（用户提问那一轮才有） */
  attachments?: AgentAttachment[];
  /** 思考模式的思维链（只给折叠展示；不进正文、不进下一轮历史） */
  thinking?: string;
  /** 思考耗时（毫秒，模型调用 → 第一段正文） */
  thinking_ms?: number;
  /** 过程轨迹（多轮思考 + 工具调用）；老消息没有 → 前端从 `thinking`/`tool_trace` 拼一份 */
  run?: AgentRun;
  /** 同一父节点下的兄弟消息 id（分叉导航 ◀ n/m ▶ 用） */
  siblings?: string[];
  sibling_index?: number;
  finish_reason?: string | null;
  created_at?: string | null;
}

/** 会话上下文用量：圆环显示的就是它，点圆环即压缩。 */
export interface AgentUsage {
  tokens: number;
  budget: number;
  percent: number;
  messages: number;
  /** 参与上下文的消息数（压缩后只剩摘要那一条） */
  active: number;
  compressed: boolean;
  compressed_at?: string | null;
}

export interface AgentConversation {
  conversation_id: string;
  paper_id?: string | null;
  title: string;
  head_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  /** 会话详情里是消息数组；列表接口里是条数（数字）—— 用 `convCount()` 读 */
  messages?: AgentMessage[] | number;
  usage?: AgentUsage;
  /** 会话里出现过划词消息（后端自动判定，列表页据此分类） */
  has_selection?: boolean;
  kind?: 'selection' | 'normal';
}

/** 会话列表里那一条有几个消息（列表接口给数字，详情给数组）。 */
export function convCount(conv: AgentConversation | null | undefined): number {
  const value = conv?.messages;
  if (typeof value === 'number') return value;
  if (Array.isArray(value)) return value.length;
  return 0;
}

export interface AgentModelInfo {
  config_id?: string;
  provider?: string;
  model_name?: string;
  base_url?: string;
  /** 模型参数（thinking / reasoning_effort / max_tokens / top_p…），设置页只挑几个显示 */
  parameters?: Record<string, any>;
  usable?: boolean;
  is_mock?: boolean;
  has_key?: boolean;
  fallback_to_generation?: boolean;
  /** 视觉模型（`/agent/status` 顺带回一份，用于说明"图片问答不走它"） */
  vision?: AgentModelInfo | null;
}

export interface AgentStatus {
  enabled: boolean;
  running: boolean;
  endpoint: string;
  model: AgentModelInfo;
  /** 运行限额（轮数 / 检索预算 / 上下文预算 / 自动压缩触发线…），设置页用它做说明 */
  limits?: {
    max_rounds: number;
    search_budget: number;
    context_budget: number;
    auto_compress_percent: number;
    deadline_seconds: number;
    attachment_inline_limit: number;
    evidence_floor: number;
  };
}

/** 划词解释：选区 + 可选追问（`question` 为空时后端默认"解释这段内容"）。
 *
 * 锚点用 **`row_id`**（正文行 id，形如 `p004-02`）而不是 `block_id`：阅读器划词拿到的就是行 id，
 * 后端会按"行 id 里的页号 → 那一页的块列表"反查出真正的块（见 `runtime._block_of_row`）。
 */
export interface AgentSelection {
  text: string;
  /** 已经是块 id 时才传（很少用；正文划词给的是 row_id） */
  block_id?: string | null;
  /** 正文行 id（`p004-02`）——阅读器划词的锚点 */
  row_id?: string | null;
  /** Markdown 视图划词时的页号（那边没有行锚点） */
  page?: number | null;
  /** 位置的人话（"第 9 页 · Markdown" / "第 4 页 · PDF"）：写进消息正文与输入框胶囊，
   *  agent 那边也据此在提示词里交代"选区在第几页"（用户口径 2026-10-03）。 */
  where?: string | null;
  question?: string;
}
