/** 知识库「搜索记录」：**只存本机浏览器**（localStorage），不进数据库、不上传。
 *
 * 口径（用户口径）："像浏览器那样的搜索记录" —— 最近搜过什么、点一下就重搜；
 * 管理入口在**知识库页的搜索框下拉**（点一条重搜 / 单条删除 / 清空记录）——
 * 设置页原来那一区 2026-12 撤掉了（用户口径：清空历史归知识库，别在设置里再摊一份）。
 *
 * 几个刻意的选择：
 * - **同词去重置顶**：同一个词反复搜只留最新一条（浏览器的行为），否则列表会被一个词刷满；
 * - **固定只留最近 20 条**（用户口径 2026-12，原来那档"可调上限"一起撤掉）：
 *   超出的从尾部丢；老口径存下来的更长的列表，读取时就被裁到 20 条并落盘；
 * - **读写全部 try/catch**：隐私模式 / 配额用尽时 localStorage 会直接抛异常，
 *   记录失败不该让整个页面报错（浏览器的记录也不是关键数据）；
 * - **坏数据当没有**：JSON 解析失败或结构不对就当空列表，不弹错、不阻塞页面。
 */

/** 记录来自哪种检索。现在只有 `search`（相似检索）；问答/浏览要接入时扩这个联合类型。 */
export type SearchMode = 'search' | 'qa' | 'browse';

/** 检索范围：全库 / 跨文献（排除某篇）/ 单篇论文。 */
export type SearchScope = 'all' | 'cross' | 'paper';

export interface SearchEntry {
  /** 搜索词（原始大小写保留，去重按归一化后的比较） */
  q: string;
  /** 毫秒时间戳 */
  ts: number;
  mode: SearchMode;
  scope: SearchScope;
  /** `scope = paper | cross` 时有意义 */
  paper_id: string | null;
  /** 命中条数（拿不到时为 null，只作展示） */
  hits: number | null;
}

const KEY = 'papermind.kb.searchHistory.v1';
/** 老版本「保留条数」的键：口径改成固定 20 条后没人读了，读到就清掉（见文件末 `dropLegacyLimitKey`）。 */
const LEGACY_LIMIT_KEY = 'papermind.kb.searchHistoryLimit.v1';
/** 同页/其它标签页之间同步用的自定义事件名（`storage` 事件只在**其它**标签页触发）。 */
const EVENT = 'papermind:search-history';

/** 本机最多留多少条：固定值，不再给用户调（设置页那一档已随「搜索记录」一区一起撤掉）。 */
export const HISTORY_LIMIT = 20;

const MODES: SearchMode[] = ['search', 'qa', 'browse'];
const SCOPES: SearchScope[] = ['all', 'cross', 'paper'];

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;   // 隐私模式下访问 localStorage 本身就可能抛
  }
}

function readRaw(): string | null {
  const s = storage();
  if (!s) return null;
  try {
    return s.getItem(KEY);
  } catch {
    return null;
  }
}

function writeRaw(value: string | null): void {
  const s = storage();
  if (!s) return;
  try {
    if (value === null) s.removeItem(KEY);
    else s.setItem(KEY, value);
    window.dispatchEvent(new CustomEvent(EVENT));
  } catch {
    /* 配额 / 隐私模式：记录失败就算了，不影响检索本身 */
  }
}

/** 一条记录是否长得对（坏数据一律丢掉，不猜）。 */
function isEntry(v: unknown): v is SearchEntry {
  const e = v as SearchEntry | null;
  return !!e && typeof e === 'object'
    && typeof e.q === 'string' && e.q.trim().length > 0
    && typeof e.ts === 'number' && Number.isFinite(e.ts)
    && MODES.includes(e.mode) && SCOPES.includes(e.scope)
    && (e.paper_id === null || typeof e.paper_id === 'string')
    && (e.hits === null || typeof e.hits === 'number');
}

/** 读全部记录（新的在前，最多 `HISTORY_LIMIT` 条）。坏数据 / 无法访问时返回空列表。 */
export function loadHistory(): SearchEntry[] {
  const raw = readRaw();
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const all = parsed.filter(isEntry);
  const kept = all.slice(0, HISTORY_LIMIT);
  // 老口径能调到 200 条：读到这一份时就地裁到 20，别让 localStorage 一直挂着长列表
  if (kept.length < all.length) writeRaw(JSON.stringify(kept));
  return kept;
}

function normalize(q: string): string {
  return q.trim().toLowerCase().replace(/\s+/g, ' ');
}

/** 归一化后的词是否相同（去重用）。 */
function sameQuery(a: string, b: string): boolean {
  return normalize(a) === normalize(b);
}

/**
 * 记一条搜索：同词去重置顶 → 截到上限 → 落盘。
 * 返回落盘后的列表（调用方直接拿去渲染，不用再读一遍）。
 */
export function addSearch(input: {
  q: string; mode?: SearchMode; scope?: SearchScope;
  paper_id?: string | null; hits?: number | null;
}): SearchEntry[] {
  const q = String(input.q ?? '').trim();
  if (!q) return loadHistory();
  const entry: SearchEntry = {
    q,
    ts: Date.now(),
    mode: input.mode ?? 'search',
    scope: input.scope ?? 'all',
    paper_id: input.paper_id ?? null,
    hits: input.hits ?? null,
  };
  const rest = loadHistory().filter((e) => !sameQuery(e.q, q));
  const next = [entry, ...rest].slice(0, HISTORY_LIMIT);
  writeRaw(JSON.stringify(next));
  return next;
}

/** 删一条（按 词 + 时间戳 定位：同词不会同时存在两条，时间戳足够唯一）。 */
export function removeSearch(target: SearchEntry): SearchEntry[] {
  const next = loadHistory().filter((e) => !(e.ts === target.ts && sameQuery(e.q, target.q)));
  writeRaw(JSON.stringify(next));
  return next;
}

/** 清空全部记录。 */
export function clearHistory(): void {
  writeRaw(null);
}

/** 订阅变化（本页写入 + 其它标签页写入），返回取消订阅函数。 */
export function subscribeHistory(cb: () => void): () => void {
  const onLocal = () => cb();
  const onStorage = (ev: StorageEvent) => {
    if (ev.key === null || ev.key === KEY) cb();
  };
  window.addEventListener(EVENT, onLocal);
  window.addEventListener('storage', onStorage);
  return () => {
    window.removeEventListener(EVENT, onLocal);
    window.removeEventListener('storage', onStorage);
  };
}

/** 清掉老版本那个"保留条数"键（模块加载时跑一次；清不掉也不影响任何行为）。 */
function dropLegacyLimitKey(): void {
  const s = storage();
  if (!s) return;
  try {
    s.removeItem(LEGACY_LIMIT_KEY);
  } catch {
    /* 隐私模式 / 配额：读不到 localStorage 时本来就没有这个键 */
  }
}
dropLegacyLimitKey();
