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

/** 公式条目：**从页级产物的 Markdown 里直接提取**（只读，不复核、不分级）。 */
export interface MarkdownFormula {
  id: string;
  page: number;
  /** 公式所在行的行 id（定位用） */
  row_id: string | null;
  latex: string;
  /** true = 行间公式（`$$…$$`），false = 行内（`$…$`） */
  display: boolean;
  /** 原文上下文片段 */
  context: string;
  /** 译文上下文片段 */
  context_zh: string;
  model?: string | null;
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
  /** 双语索引的行数：`{zh: n, en: m}` */
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

export interface SettingsPayload {
  values: Record<string, any>;
  stored: Record<string, any>;
  editable: string[];
}

export interface HealthPayload {
  database?: Record<string, any>;
  vector_store?: Record<string, any>;
  task_runner?: Record<string, any>;
  data_dir?: string;
  error?: string;
}
