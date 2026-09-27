/** 工具函数：格式化 / 状态文案 / 防抖节流 / 剪贴板 / 目录树。 */

import type { OutlineItem, PairOrder, ViewMode } from '@/api/types';

export function fmtDate(value?: string | null): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtRelative(value?: string | null): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  return fmtDate(value);
}

export function pct(done?: number | null, total?: number | null): number {
  const d = Number(done) || 0;
  const t = Number(total) || 0;
  if (!t) return 0;
  return Math.max(0, Math.min(100, Math.round((d / t) * 1000) / 10));
}

export function truncate(text?: string | null, n = 120): string {
  const s = String(text ?? '');
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

export function roundMs(ms?: number | null): number {
  if (ms === undefined || ms === null) return 0;
  return ms >= 100 ? Math.round(ms) : Math.round(ms * 10) / 10;
}

export function debounce<F extends (...args: any[]) => void>(fn: F, ms = 300) {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const wrapped = (...args: Parameters<F>) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...args); }, ms);
  };
  wrapped.cancel = () => { if (timer) clearTimeout(timer); timer = null; };
  return wrapped;
}

export function throttle<F extends (...args: any[]) => void>(fn: F, ms = 180) {
  let last = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let lastArgs: Parameters<F> | null = null;
  return (...args: Parameters<F>) => {
    const now = Date.now();
    lastArgs = args;
    if (now - last >= ms) {
      last = now;
      fn(...args);
    } else if (!timer) {
      timer = setTimeout(() => {
        timer = null;
        last = Date.now();
        if (lastArgs) fn(...lastArgs);
      }, ms - (now - last));
    }
  };
}

export const PAPER_STATUS: Record<string, { label: string; cls: string }> = {
  imported: { label: '已导入', cls: '' },
  parsing: { label: '解析中', cls: 'accent' },
  parsed: { label: '已解析', cls: 'ok' },
  translating: { label: '翻译中', cls: 'accent' },
  translated: { label: '已翻译', cls: 'ok' },
  analyzing: { label: '分析中', cls: 'accent' },
  indexed: { label: '已入库', cls: 'accent' },
  failed: { label: '失败', cls: 'danger' },
  error: { label: '出错', cls: 'danger' },
};

export function paperStatus(status?: string | null) {
  return PAPER_STATUS[status ?? ''] ?? { label: status || '未知', cls: '' };
}

export const TASK_STATUS: Record<string, { label: string; cls: string }> = {
  pending: { label: '排队中', cls: '' },
  processing: { label: '执行中', cls: 'accent' },
  paused: { label: '已暂停', cls: 'warn' },
  done: { label: '已完成', cls: 'ok' },
  completed: { label: '已完成', cls: 'ok' },
  failed: { label: '失败', cls: 'danger' },
  cancelled: { label: '已取消', cls: '' },
  canceled: { label: '已取消', cls: '' },
};

export function taskStatus(status?: string | null) {
  return TASK_STATUS[status ?? ''] ?? { label: status || '未知', cls: '' };
}

export const TASK_TYPE_LABEL: Record<string, string> = {
  IMPORT: '导入',
  PARSE: '解析',
  TRANSLATE: '翻译',
  TRANSLATE_MD: '翻译',
  INDEX: '索引',
  RECONCILE: '对账',
  ANALYSIS: 'AI 分析',
  PDF_TRANSLATE: '版面 PDF 翻译',
  EXPORT: '导出',
};

export const BLOCK_TYPE_LABEL: Record<string, string> = {
  para: '段落',
  heading: '标题',
  formula: '公式',
  caption: '图注',
  figure: '插图',
  table: '表格',
  ref: '参考文献',
};

export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  const ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
}

/** 把一份内容存成文件（浏览器下载）。Blob 已经在手上时用 `saveBlob`。 */
export function downloadText(filename: string, text: string, type = 'text/markdown;charset=utf-8'): void {
  saveBlob(filename, new Blob([text], { type }));
}

/** 存二进制/任意 Blob（打包 zip、服务端导出的 Markdown 等）。 */
export function saveBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function safeJson<T = any>(value: unknown, fallback: T | null = null): T | null {
  if (value === null || value === undefined) return fallback;
  if (typeof value === 'object') return value as T;
  try { return JSON.parse(String(value)) as T; } catch { return fallback; }
}

export interface TocRow {
  id: string;
  page: number;
  level: number;
  number: string;
  /** 英文原文标题 */
  text: string;
  /** 中文译文标题（无译文时为空串） */
  textZh: string;
  /** 相对所属层级链的缩进深度（0 起），用于目录缩进 */
  depth: number;
  hasChildren: boolean;
  parentId: string | null;
}

/**
 * 目录：把后端给的**嵌套** outline 摊平成带 `depth` 的行（组件里不写递归）。
 *
 * 每个条目同时带英文原文与中文译文（页级翻译的标题行成对产出），
 * 因此目录可以「英文在上、中文在下」双语显示。
 */
export function flattenOutline(tree: OutlineItem[] | undefined | null, collapsed?: Set<string>): TocRow[] {
  const rows: TocRow[] = [];
  const walk = (nodes: OutlineItem[], depth: number, parentId: string | null) => {
    for (const node of nodes) {
      const hasChildren = !!node.children?.length;
      rows.push({
        id: node.block_id, page: node.page, level: node.level, number: node.number,
        text: node.text, textZh: node.text_zh ?? '', depth, hasChildren, parentId,
      });
      if (hasChildren && !collapsed?.has(node.block_id)) {
        walk(node.children, depth + 1, node.block_id);
      }
    }
  };
  walk(tree ?? [], 0, null);
  return rows;
}

/** 收集所有有子节点的目录 id（用于"全部折叠/展开"）。 */
export function outlineParentIds(tree: OutlineItem[] | undefined | null): string[] {
  const out: string[] = [];
  const walk = (nodes: OutlineItem[]) => {
    for (const n of nodes ?? []) {
      if (n.children?.length) { out.push(n.block_id); walk(n.children); }
    }
  };
  walk(tree ?? []);
  return out;
}

/** 按页分组（左右对照的两栏都以"页"为对齐锚点）。 */
export function groupByPage<T extends { page: number }>(blocks: T[]): Array<[number, T[]]> {
  const map = new Map<number, T[]>();
  for (const b of blocks) {
    const list = map.get(b.page);
    if (list) list.push(b);
    else map.set(b.page, [b]);
  }
  return [...map.entries()].sort((a, b) => a[0] - b[0]);
}

export function labelViewMode(mode: ViewMode): string {
  return { original: '原文', translation: '译文', bilingual: '对照' }[mode] ?? mode;
}

export function labelPairOrder(order: PairOrder): string {
  return (order === 'en_left' || order === 'en_top') ? '原文在左' : '译文在左';
}
