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
  /**
   * 目录里显示的**原始 markdown 文本**（标题行去掉 `#` 之后的原文）。
   *
   * ⚠️ 为什么留原文而不是剥干净的纯文本：标题里本来就有 `$Q_{\text{mse}}$`、`**重点**`
   * 这类行内标记，目录要**按 Markdown 渲染**（公式出 KaTeX、粗体加粗）—— 与阅读器自己的目录
   * （`ReaderToc` 用 `inlineHtml` 渲染每一行）同一口径。剥成纯文本就会把 LaTeX 源码直接摆在
   * 目录里（用户报的就是这个："标签页的目录还不支持 Markdown"）。
   */
  md: string;
  /** 同一份标题的**纯文本**（tooltip / 可访问名；公式压成最接近屏上效果的文字） */
  text: string;
  /** 标题层级：1~6（`#` 的个数）；「相关概念」这类人工条目固定给 1 */
  level: number;
}

const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
const FENCE_RE = /^\s*(```|~~~)/;

/**
 * 标题 → 纯文本（**只给 tooltip / aria 用**；目录里显示的是渲染后的 `md`）。
 *
 * 公式要压成人看得懂的样子：`$Q_{\text{mse}}$` 在屏幕上就是 "Qmse"，
 * tooltip 里留 `Q_{\text{mse}}` 这种 LaTeX 源码反而像个报错。
 */
function plainHeading(raw: string): string {
  return raw
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')     // 图片 → alt
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')      // 链接 → 链接文字
    .replace(/`([^`]*)`/g, '$1')                  // 行内代码
    .replace(/(\*\*|__)(.*?)\1/g, '$2')           // 粗体
    .replace(/(\*|_)(.*?)\1/g, '$2')              // 斜体
    .replace(/\$([^$]*)\$/g, (_m, tex: string) => tex
      .replace(/\\text\{([^}]*)\}/g, '$1')        // \text{mse} → mse
      .replace(/\\[a-zA-Z]+/g, '')                // 其余 LaTeX 命令去掉
      .replace(/[_^{}\\$]/g, ''))                 // 上下标/花括号等符号去掉
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * 解析 `## / ### …` 标题。
 *
 * ⚠️ 序号按**标题**顺序生成（`${prefix}-h-0,1,2…`）：正文那边贴 id 时按同样的顺序数标题，
 * 两边同源才不会错位一格、点目录全滚错位置。
 */
export function buildInsightOutline(
  markdown: string | null | undefined,
  opts: { prefix: string } = { prefix: 'ins' },
): InsightHeading[] {
  const out: InsightHeading[] = [];
  let headingIndex = 0;
  let inFence = false;
  for (const line of String(markdown ?? '').split(/\r?\n/)) {
    if (FENCE_RE.test(line)) { inFence = !inFence; continue; }   // 代码块里的 `#` 不是标题
    if (inFence) continue;
    const m = HEADING_RE.exec(line);
    if (!m) continue;
    const text = plainHeading(m[2]);
    if (!text) continue;
    out.push({
      id: `${opts.prefix}-h-${headingIndex}`,
      md: m[2].trim(),                     // 原文：交给目录按 Markdown 渲染
      text,                                // 纯文本：tooltip / aria
      level: m[1].length,
    });
    headingIndex += 1;
  }
  return out;
}
