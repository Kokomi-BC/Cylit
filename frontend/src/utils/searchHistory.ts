/** 知识库「搜索记录」：**只存本机浏览器**（localStorage），不进数据库、不上传。
 *
 * 口径（用户口径）："像浏览器那样的搜索记录" —— 最近搜过什么、点一下就重搜；
 * 管理入口在「设置 → 搜索记录」（列表 / 删单条 / 清空 / 改保留条数）。
 *
 * 几个刻意的选择：
 * - **同词去重置顶**：同一个词反复搜只留最新一条（浏览器的行为），否则列表会被一个词刷满；
 * - **上限可调**（默认 50）：超出的从尾部丢，避免本地存储无限长；
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
const LIMIT_KEY = 'papermind.kb.searchHistoryLimit.v1';
/** 同页/其它标签页之间同步用的自定义事件名（`storage` 事件只在**其它**标签页触发）。 */
const EVENT = 'papermind:search-history';

export const DEFAULT_LIMIT = 50;
export const LIMIT_CHOICES = [10, 20, 50, 100, 200];
const MAX_LIMIT = 500;

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

/** 读全部记录（新的在前）。坏数据 / 无法访问时返回空列表。 */
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
  return parsed.filter(isEntry);
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
  const next = [entry, ...rest].slice(0, historyLimit());
  writeRaw(JSON.stringify(next));
  return next;
}

/** 删一条（按 词 + 时间戳 定位：同词不会同时存在两条，时间戳足够唯一）。 */
export function removeSearch(target: SearchEntry): SearchEntry[] {
  const next = loadHistory().filter((e) => !(e.ts === target.ts && sameQuery(e.q, target.q)));
  writeRaw(JSON.stringify(next));
  return next;
}

/** 清空全部记录（保留上限设置）。 */
export function clearHistory(): void {
  writeRaw(null);
}

/** 当前保留上限（默认 50；非法值回退默认）。 */
export function historyLimit(): number {
  const s = storage();
  if (!s) return DEFAULT_LIMIT;
  let raw: string | null = null;
  try {
    raw = s.getItem(LIMIT_KEY);
  } catch {
    return DEFAULT_LIMIT;
  }
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_LIMIT;
  return Math.min(Math.floor(n), MAX_LIMIT);
}

/** 改保留上限：顺手把超出的旧记录裁掉。 */
export function setHistoryLimit(n: number): SearchEntry[] {
  const value = Math.max(1, Math.min(Math.floor(Number(n) || DEFAULT_LIMIT), MAX_LIMIT));
  const s = storage();
  if (s) {
    try {
      s.setItem(LIMIT_KEY, String(value));
    } catch {
      /* 同上：写不进去就用默认值 */
    }
  }
  const next = loadHistory().slice(0, value);
  writeRaw(JSON.stringify(next));
  return next;
}

/** 订阅变化（本页写入 + 其它标签页写入），返回取消订阅函数。 */
export function subscribeHistory(cb: () => void): () => void {
  const onLocal = () => cb();
  const onStorage = (ev: StorageEvent) => {
    if (ev.key === null || ev.key === KEY || ev.key === LIMIT_KEY) cb();
  };
  window.addEventListener(EVENT, onLocal);
  window.addEventListener('storage', onStorage);
  return () => {
    window.removeEventListener(EVENT, onLocal);
    window.removeEventListener('storage', onStorage);
  };
}
