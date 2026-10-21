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
  /** 中文标题（译文里 `《…》` 那一段；没翻译过就是 null） */
  title?: string | null;
  /** 英文标题（原文里切掉作者段与会议/年份/链接之后剩下的那一段） */
  title_en?: string | null;
  authors?: string | null;
  venue?: string | null;
  /** 这一条出现在**第几页**（接口保留：卡片上那枚「定位」按钮已按用户口径撤掉 —— 定不到精确那一行；
   *  将来要做"精确到条目"的定位时直接能用） */
  page_no?: number | null;
  year?: number | null;
  doi?: string | null;
  url?: string | null;
  /** 这一条里的 **GitHub 仓库地址**（没有就是 null）：侧栏并排显示"论文图标 + GitHub 图标"，
   *  分别跳论文页与仓库（用户口径 2026-10-07） */
  code_url?: string | null;
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

/**
 * 表格条目：从**页级产物**的行里抽出来的 Markdown 表（只读）。
 *
 * 与公式不同，**没有解析产物兜底**：解析那边的 table 块是按坐标拼的纯文本
 * （一张表的格子被连成一行），画不成表。所以未翻译的页不出条目。
 */
export interface MarkdownTable {
  id: string;
  page: number;
  /** 表格所在行的行 id（定位用） */
  row_id: string | null;
  /** 论文里这张表的编号（`表 1` 里的那个 `1`；说明里没写编号时是空串） */
  number: string;
  /** 表格说明：中文译文优先（没有译文才退回原文） */
  caption: string;
  /** 说明的原文（鼠标停一下对照用） */
  caption_original: string;
  /** 表格本体：GFM Markdown 表（`| a | b |` + 分隔行），译文是逐格翻过的中文表 */
  markdown: string;
  /** 表体行数（不含表头与分隔行） */
  rows: number;
  /** 列数 */
  cols: number;
  /** `translation` = 中文表；`original` = 这一页还没有译文，按原文显示 */
  source: 'translation' | 'original' | string;
  model?: string | null;
}

/** 触发浮层的那枚按钮在视口中的矩形：浮层贴着它下方打开（表格 / 公式 / 插图的「浮层打开」）。 */
export interface OverlayAnchor { x: number; y: number; w?: number; h?: number }

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
  /** 锚点：正文行 = 行 id；Markdown 视图 = `md:p<页号>`；原 PDF 页图 = `pdf:p<页号>` */
  block_id: string;
  text: string | null;
  start: number;
  end: number;
  color: 'yellow' | 'green' | 'blue' | 'pink' | 'purple' | string;
  /** 偏移量相对哪一侧的文字：**原文 en / 译文 zh 分开存**（同一行两侧共用一个 block_id） */
  side?: 'en' | 'zh';
  /**
   * 在哪一面标的（《修改计划》第三步起有三面）：正文行 `row` / Markdown 视图 `markdown` /
   * **原 PDF 页图 `pdf`**（偏移量相对该页 pdf.js 文字层的渲染文本）。
   * 三套锚点刻意**不统一**：PDF 与 Markdown 是两种页面存储，共用的是记录形状而不是位置模型。
   */
  scope?: 'row' | 'markdown' | 'pdf';
  /** 跨行高亮的分组：同一次划词切出来的几条记录共用它，删除时整组一起删 */
  group_id?: string | null;
  page?: number | null;
  created_at?: string | null;
  /**
   * 这条高亮锚定那一行的**正文 markdown**。只用来显示：当 text 是占位符
   * (U+FFFC，即在 PDF 里选中公式 / 图片块) 时，侧栏那条"原文"用它渲染出真正的公式。
   * 不参与定位 —— 偏移量仍按 text 那一份算。
   */
  quote_md?: string | null;
}

export interface Note {
  id: string;
  paper_id: string;
  block_id: string | null;
  page?: number | null;
  selected_text: string | null;
  content_md: string;
  /**
   * 笔记挂在哪条高亮上（《修改计划》第四步：PDF/Markdown → Highlight → Note）。
   * 后端在 `_note_dict` 里给出；老笔记（第四步之前建的）没有它 → 退回"只按 block_id 定位"。
   */
  highlight_id?: string | null;
  /** 这条笔记挂在哪一面：`row` / `markdown` / `pdf`（从 block_id 前缀推出来） */
  scope?: 'row' | 'markdown' | 'pdf';
  /** 原文是占位符时用来显示的正文 markdown（同 Highlight.quote_md） */
  quote_md?: string | null;
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
  /**
   * **无行可落的块**：没有正文行、但知道落在哪一页的块（公式 / 插图 / 表格在正文里没有独立段落）。
   *
   * `block_rows` 只收录"能匹配到行"的块；线上那篇 509 块里有 119 块不在表里，
   * 阅读器以前对它们一无所知，点引用只能退化成"整页闪一下"。这张表把落点显式说清楚：
   * 有 `page` 就到那一页；公式再带 `latex`（与引用里的同一套写法），
   * 让阅读器在页内认出那一枚 KaTeX 节点，落点精确到公式而不是整页。
   */
  row_less_blocks?: Array<{
    id: string;
    page?: number | null;
    /** 块类型（formula / figure / table …）：只有公式块会带 `latex` */
    type?: string | null;
    /** 公式的 LaTeX（页内定位用；签名匹配的规则见 ReaderView.findFormula） */
    latex?: string | null;
  }>;
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
  is_mock?: boolean;
}

/** 预设里**某一种类型**的完整配置（`generation` / `agent` / `embedding` / `reranker` / `vision`）。 */
export interface ModelPresetSpec {
  provider?: string;
  model_name?: string;
  base_url?: string | null;
  /** 密钥文件名（不是密钥内容） */
  secret_ref?: string | null;
  parameters?: Record<string, unknown>;
}

/** `GET /models/presets` 一条：可一键填满新建表单的模板（后端的空档位是 `null`）。 */
export interface ModelPreset {
  id: string;
  label: string;
  provider: string;
  generation?: ModelPresetSpec | null;
  agent?: ModelPresetSpec | null;
  embedding?: ModelPresetSpec | null;
  reranker?: ModelPresetSpec | null;
  vision?: ModelPresetSpec | null;
}

/** `GET /models/usage/stats` 的一天（后端已按天零填充、旧→新）。 */
export interface ModelUsageDay {
  date: string;
  prompt: number;
  completion: number;
  total: number;
}

/** 分模型消耗一行。 */
export interface ModelUsageByModel {
  /** 配置已被删掉时后端回 `null`（历史消耗仍要上榜） */
  model_config_id: string | null;
  name: string;
  model_name: string | null;
  prompt: number;
  completion: number;
  total: number;
}

/** 模型消耗统计（`GET /models/usage/stats?days=14`）。 */
export interface ModelUsageStats {
  days: ModelUsageDay[];
  by_model: ModelUsageByModel[];
  totals: { prompt: number; completion: number; total: number; requests: number };
  today: { date: string; total: number };
  range: { days: number; from: string; to: string };
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
  /** 图注的中文译文（未翻译的页为空，前端回退显示英文原文） */
  caption_zh?: string | null;
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
 * 一条 chunk 引用（后端把编号解析成真实 chunk 之后的那一份）。
 *
 * `unit`（语义单元）也会出现在这里：理解读的就是 chunk，引用基准单位与知识库同一套。
 * 正文里它被渲染成一枚跳转标号（[1]、[2]…），点一下回到原文那一段（用户口径 2026-11-28）。
 */
export interface UnderstandingRefItem {
  code: string;
  chunk_id: string;
  chunk_type?: string | null;
  page?: number | null;
  /**
   * 这条引用落在的**块 id**（后端查 chunk 表补的）。
   * 页码只能跳到整页，块 id 才能让阅读器落在原文那一段；老数据的 chunk 没记 block_id，
   * 所以是可选的（缺失时退化成跳页）。
   */
  block_id?: string | null;
  /**
   * 这条引用落在的**正文行 id**（p006-03，来自 chunk 元数据）。
   *
   * 比 block_id 更可靠：公式 chunk 的 block_id 是 row-<行 id> 甚至 …-x01 这种伪块 id，
   * 阅读器翻不成行，只能整页闪（用户 2026-12-03 报的那两条）。点标号优先用它。
   */
  row_id?: string | null;
  /**
   * 公式条目的 LaTeX（只有 formula 类有）。
   *
   * 公式与它所在的正文行是两条独立引用（用户 2026-12-03 的「56、57 为什么是同一块地方」）：
   * 行级落点必然重合，所以额外带上这条公式，标号才能落在那**一枚 KaTeX** 上。
   */
  latex?: string | null;
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
  /** 只给"默认值不在后端 `Settings` 字段上"的键（如 `agent_style`）：值被改过之后，
   *  设置页仍要能显示"默认是哪一段"，用户才能对比与恢复。 */
  defaults?: Record<string, any>;
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
  /** `tool` 的工具名（`paper_search` / `page_markdown_get` 这类，由模型自己调） */
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
  /** 上一轮真正发出去的 prompt 量（含命中前缀缓存的部分）；老会话没有这个快照 */
  input_tokens?: number;
  /** 上面那一份里命中前缀缓存的部分（DeepSeek 命中部分按更低单价计费） */
  cache_read_tokens?: number;
  /** 前缀缓存命中率（0–100）；没有快照时为 null —— 前端据此隐藏，不显示假的 0% */
  cache_hit_percent?: number | null;
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
  /** 这条会话单独选定的问答模型（模型配置 id；空 = 跟随 agent 默认模型） */
  model_config_id?: string | null;
  /** 这条会话单独选定的推理档（空 = 跟随该模型 `parameters.reasoning_effort`） */
  reasoning_effort?: string | null;
}

/** 会话级"用哪个模型 + 哪一档推理"（`PUT /agent/conversations/{id}`；空串 = 清成 NULL = 跟随默认）。 */
export interface AgentModelPick {
  model_config_id: string;
  reasoning_effort: string;
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
  };
}

/** agent 技能目录里的一条（`GET /agent/skills`）。
 *
 * `builtin` = 随仓库发的**自带技能**：可以禁用（`PUT /agent/skills/{name}`）但删不掉
 * （`DELETE` 对自带技能回 403，前端干脆不给卸载入口）；`chars` 是 SKILL.md 的字符数，
 * 设置页只用它判"这个技能是不是空的"。
 */
export interface AgentSkill {
  name: string;
  description: string;
  builtin: boolean;
  enabled: boolean;
  chars: number;
  updated_at?: string | null;
}

/** `GET /agent/skills`：技能表 + 技能目录（后端还没上线这一组接口时，前端按空态降级）。 */
export interface AgentSkillsPayload {
  skills: AgentSkill[];
  dir?: string;
}

/** `POST /agent/skills/upload`（multipart 字段名 `file`，.zip 内含 `<name>/SKILL.md`）。 */
export interface AgentSkillsUploadResult extends AgentSkillsPayload {
  /** 本次新增/覆盖的技能名（前端拿它拼"已上传：a、b"这句 toast） */
  added?: string[];
}

/** 运行限额（`GET/PUT /agent/limits`）：agent 服务的启动参数，**改完要重启 agent 服务**。 */
export interface AgentLimits {
  /** 单轮工具轮数上限 */
  max_rounds: number;
  /** 单轮检索次数预算 */
  search_budget: number;
  /** 单轮时限（秒） */
  deadline_seconds: number;
  /** 上下文预算（tokens） */
  context_budget: number;
  /** 自动压缩触发线（上下文用量的百分比） */
  compact_threshold: number;
  /** 单轮最多把几张附件图内联给模型（默认 12） */
  attachment_inline_limit: number;
  /** 改动是否还没被 agent 服务吃到（`PUT` 恒回 true；只读，不回传） */
  restart_required: boolean;
}

/** `PUT /agent/limits` 的请求体：六个数字，**不带 `restart_required`**。 */
export type AgentLimitsPatch = Omit<AgentLimits, 'restart_required'>;

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
  /**
   * 选区的**锚点快照**（`start` / `end` / `side` / `scope`）：对话浮层"回答下面那排色点"
   * 要用它给这段建高亮、把回答存成它的笔记。后端只读 `text` / `row_id` / `page` / `where`，
   * 多给的字段不会进请求体（见 `ReaderChat` 的 `ask`），所以安全。
   */
  start?: number;
  end?: number;
  side?: string;
  /** `row` / `markdown` / `pdf`：决定高亮锚点是行 id、`md:p<页>` 还是 `pdf:p<页>` */
  scope?: string;
  /** 高亮要存的原文：原 PDF 划词时它与 `text`（截断版，只给胶囊显示）不是同一个字符串 */
  quote?: string;
  /** 这一轮是「解释」：对话浮层据此**自动发送**，并把请求打到后端 `mode: "explain"` */
  explain?: boolean;
  /** 自动发送时用的那句问题（`explain` 为真时才有） */
  ask?: string;
}

/** 首页概览（`GET /api/v1/home/summary`）：首页刊头数据 rail 要的几个计数。
 *
 * 口径见 `backend/services/home/home_service.py`：`readings` 是"翻开过"的篇数，
 * `highlights` 按 `group_id` 去重后是"标注处数"，`understandings` 是"已解读的篇数"。 */
export interface HomeSummary {
  papers: number;
  readings: number;
  notes: number;
  highlights: number;
  understandings: number;
}
