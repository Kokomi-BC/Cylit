/**
 * 阅读器的**行模型**：把「页级翻译产物」与「解析块」合成统一的逐页行列表。
 *
 * 为什么需要它：页级翻译的产物（`pages.json` → `page_translations`）里每一行
 * 都同时带原文与译文，**天然一一对应**；而还没有翻译的页只能退回解析块
 * （只有原文）。两者合成同一个 `PageGroup` 结构后，渲染 / 搜索 / 目录 / 定位
 * 都只面对一种形状，不需要"对齐算法"。
 */
import type { Block, PageInfo, PageRow, PageTranslation } from '@/api/types';

export interface PageGroup {
  page: number;
  info: PageInfo | null;
  rows: PageRow[];
  /** 页级产物（没有翻译时为 null） */
  translation: PageTranslation | null;
  translated: boolean;
  /** 该页的行对齐质量：exact / structure / empty */
  align: string;
}

const HEADING_NUMBER = /^(\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z](?:\.\d{1,2}){1,3})\b/;
const KNOWN_TYPES = new Set([
  'heading', 'para', 'formula', 'caption', 'list', 'table', 'code', 'ref',
]);

function stripHashes(text: string): string {
  return text.replace(/^[\s#]+/, '').trim();
}

function rowFromBlock(block: Block, index: number): PageRow {
  const type = KNOWN_TYPES.has(block.type) ? block.type : 'para';
  const text = block.text ?? '';
  const number = type === 'heading'
    ? (HEADING_NUMBER.exec(stripHashes(text))?.[1] ?? '')
    : '';
  return {
    // 未翻译页退回解析块：行 id 直接用 block_id（高亮/批注锚点仍然稳定）
    id: block.id,
    page: block.page,
    index,
    type,
    level: type === 'heading' ? Math.min(4, Math.max(1, block.heading_level ?? 2)) : 0,
    number,
    original: text,
    translation: block.translation ?? '',
  };
}

/** 合成逐页行模型；`showRefs=false` 时过滤参考文献行。 */
export function buildPageGroups(
  pages: PageInfo[],
  translations: PageTranslation[],
  blocks: Block[],
  showRefs: boolean,
): PageGroup[] {
  const trByPage = new Map<number, PageTranslation>();
  for (const t of translations ?? []) trByPage.set(t.page, t);

  const blocksByPage = new Map<number, Block[]>();
  for (const b of blocks ?? []) {
    const list = blocksByPage.get(b.page);
    if (list) list.push(b);
    else blocksByPage.set(b.page, [b]);
  }

  const infoByPage = new Map<number, PageInfo>();
  for (const p of pages ?? []) infoByPage.set(p.page, p);

  const numbers = new Set<number>([...infoByPage.keys(), ...trByPage.keys(), ...blocksByPage.keys()]);
  const groups: PageGroup[] = [];
  for (const page of [...numbers].sort((a, b) => a - b)) {
    const tr = trByPage.get(page) ?? null;
    let rows: PageRow[] = tr?.rows?.length
      ? tr.rows.map((r, i) => ({ ...r, page, index: r.index ?? i }))
      : (blocksByPage.get(page) ?? [])
        .slice()
        .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
        .map((b, i) => rowFromBlock(b, i));
    if (!showRefs) rows = rows.filter((r) => r.type !== 'ref');
    groups.push({
      page,
      info: infoByPage.get(page) ?? null,
      rows,
      translation: tr,
      translated: !!tr?.rows?.length,
      align: tr?.align ?? (rows.length ? 'parsed' : 'empty'),
    });
  }
  markBoundaries(groups);
  return groups;
}

/**
 * 跨页句的显示归属：上页末行译文**让位**给下页首行，避免「半截词 + 整句重译」同时出现。
 *
 * 背景（实测 p001-06 → p002-00）：页图把 `communication bot-` 折断在两页，上页译成「通信瓶-」、
 * 下页又从头译了整句「…间的瓶颈。」——读者看到的是半截 + 重复。
 * 处理：上页末行标 `zhDeferred`（译文格显示「续下页」），下页首行（`continued_from_prev`）
 * 承载完整句子。标记由后端按**原文**判定（`continues` / `continued_from_prev`）。
 */
function markBoundaries(groups: PageGroup[]): void {
  for (let i = 0; i < groups.length - 1; i += 1) {
    const tail = groups[i].rows[groups[i].rows.length - 1];
    const head = groups[i + 1].rows[0];
    if (!tail || !head) continue;
    if (tail.continues && head.continued_from_prev) tail.zhDeferred = true;
  }
}

/** 全局行序（跨页递增）：搜索、上一段/下一段导航用。 */
export function flattenRows(groups: PageGroup[]): PageRow[] {
  const out: PageRow[] = [];
  let order = 0;
  for (const g of groups) {
    for (const r of g.rows) out.push({ ...r, order: order++ });
  }
  return out;
}

export function pageStats(group: PageGroup): { done: number; total: number } {
  const total = group.rows.length;
  const done = group.rows.filter((r) => r.original && r.translation).length;
  return { done, total };
}
