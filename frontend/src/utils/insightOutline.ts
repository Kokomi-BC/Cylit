/**
 * Markdown → 目录大纲（**纯函数**，两个整页与左栏两个页签共用）。
 *
 * 为什么要自己解析而不是从渲染后的 DOM 里抓：目录要在**正文渲染之前**就能画出来
 * （首屏就有一份完整目录，不必等 KaTeX/图片就位），而且"大纲有几条"必须和正文里的标题
 * **一一对应** —— 两边都从这个函数来，才不会出现"目录里有一条、正文里没有"。
 *
 * anchor id 用**序号**而不是标题文字（`${prefix}-h-3`）：同一篇 markdown 里
 * "实验"/"方法"这种标题可能重复出现，用文字当 id 必然撞车（撞了之后 `getElementById`
 * 只会拿到第一个，点目录永远滚到同一处）。序号还自带稳定性：重新渲染一次 id 不变。
 *
 * `prefix` 由调用方给（整页 / 左栏页签各一套），这样即使两个视图同时挂在 DOM 里也不会撞 id。
 */
export interface InsightHeading {
  /** 稳定 anchor id（前端生成，不依赖 Markdown 渲染器） */
  id: string;
  /** 目录里显示的文字（已去掉 markdown 标记） */
  text: string;
  /** 标题层级：1~6（`#` 的个数）；「相关概念」这类人工条目固定给 1 */
  level: number;
}

const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
const FENCE_RE = /^\s*(```|~~~)/;

/** 去掉标题里的行内标记：目录里要的是"人话"，不是 `**方法**` 这种源码。 */
function plainHeading(raw: string): string {
  return raw
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')     // 图片 → alt
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')      // 链接 → 链接文字
    .replace(/`([^`]*)`/g, '$1')                  // 行内代码
    .replace(/(\*\*|__)(.*?)\1/g, '$2')           // 粗体
    .replace(/(\*|_)(.*?)\1/g, '$2')              // 斜体
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * 解析 `## / ### …` 标题。
 *
 * `concepts: true` 时在最前面插一条「相关概念」（`${prefix}-concepts`）——「阅读地图」页
 * 第一块就是相关概念卡片，它没有 markdown 标题，但目录里必须有它，否则读者从目录点不到那块。
 * ⚠️ 标题的序号**只数标题**（不含这条人工条目）：这样带不带 concepts，标题 id 都一样 ——
 * 否则正文那边（只管标题）和目录那边（多一条）会错位一格，点目录全滚错位置。
 */
export function buildInsightOutline(
  markdown: string | null | undefined,
  opts: { prefix: string; concepts?: boolean } = { prefix: 'ins' },
): InsightHeading[] {
  const out: InsightHeading[] = [];
  if (opts.concepts) out.push({ id: `${opts.prefix}-concepts`, text: '相关概念', level: 1 });
  let headingIndex = 0;
  let inFence = false;
  for (const line of String(markdown ?? '').split(/\r?\n/)) {
    if (FENCE_RE.test(line)) { inFence = !inFence; continue; }   // 代码块里的 `#` 不是标题
    if (inFence) continue;
    const m = HEADING_RE.exec(line);
    if (!m) continue;
    const text = plainHeading(m[2]);
    if (!text) continue;
    out.push({ id: `${opts.prefix}-h-${headingIndex}`, text, level: m[1].length });
    headingIndex += 1;
  }
  return out;
}
