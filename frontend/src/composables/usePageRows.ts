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

/**
 * 解析阶段的行文本里**去掉插图 Markdown**（`![图注](../figures/<block>.png)`）。
 *
 * 为什么必须去掉（用户口径 + 实测）：解析产物把插图写成了 Markdown 图片语法，而插图在阅读器里
 * 是**另外渲染**的（`ReaderPageRows` 的 `.row-figure`：按图注行挂 `/figures/region/…?card=1`
 * 的「图+图注」卡片）。两处都画的结果就是**同一张图出现两次、且裁剪方式不同**：
 * - Markdown 那张 = 解析阶段的裸裁图（`figures/<block>.png`，紧跟文字、尺寸随栏宽）；
 * - `.row-figure` 那张 = 按图注锚定重裁的卡片。
 * 用户报的"第一个图片在页面顶部、大小不对、此处不该显示"就是前者（它挂在段落块里，
 * 而那个段落块常常在该页顶部）。
 *
 * ⚠️ 只在**阅读器的行模型**里剥：`full.md` / Markdown 视图 / 导出仍按原样带图
 * （那是"整篇 Markdown"的语义，图片本来就该在那儿）。
 */
function stripImageMarkdown(text: string): string {
  if (!text.includes('![')) return text;
  return text.replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\n{3,}/g, '\n\n').trim();
}

function rowFromBlock(block: Block, index: number): PageRow {
  const type = KNOWN_TYPES.has(block.type) ? block.type : 'para';
  const text = stripImageMarkdown(block.text ?? '');
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

/** 合成逐页行模型。参考文献**恒显示**（用户 2026-09-27：移除"显示参考文献"开关）。 */
export function buildPageGroups(
  pages: PageInfo[],
  translations: PageTranslation[],
  blocks: Block[],
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
    const rows: PageRow[] = tr?.rows?.length
      ? tr.rows.map((r, i) => ({ ...r, page, index: r.index ?? i }))
      : (blocksByPage.get(page) ?? [])
        .slice()
        .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
        .map((b, i) => rowFromBlock(b, i));
    groups.push({
      page,
      info: infoByPage.get(page) ?? null,
      rows,
      translation: tr,
      translated: !!tr?.rows?.length,
      align: tr?.align ?? (rows.length ? 'parsed' : 'empty'),
    });
  }
  return groups;
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
