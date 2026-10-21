/** 高亮色名 → 实际色值。
 *
 * 与 base.css 的 `.hl-swatch.*` / `mark.hl.*` **同一组颜色**，只是这里给的是"画在别处"用的一份：
 * 笔记卡的标题染色、笔记侧栏那条左竖线都读它。自定义色（`#rrggbb` / `rgb()` / `hsl()`）原样返回。
 *
 * ⚠️ `pink` / `purple` 是**历史色名**（后端 `HIGHLIGHT_COLORS` 仍接受它们，存量高亮靠它们渲染）。
 * 这里曾经漏掉这两个 —— 于是"通过对话存成笔记"的那种颜色（`pink`）在卡片左上角取不到色值，
 * `highlightCss()` 返回空串，渲染出来就是**黑的**（用户 2026-12-06 报的"左上角是黑色的"）。
 */
export const HL_COLOR_CSS: Record<string, string> = {
  red: 'rgba(255, 0, 0, .9)',
  orange: 'rgba(255, 165, 0, .95)',
  yellow: 'rgba(230, 180, 20, .95)',
  green: 'rgba(60, 205, 130, .95)',
  blue: 'rgba(85, 165, 255, .95)',
  indigo: 'rgba(75, 0, 130, .95)',
  violet: 'rgba(143, 0, 255, .9)',
  pink: 'rgba(255, 100, 165, .95)',
  purple: 'rgba(150, 90, 220, .95)',
};

/** 给一个高亮色名 / 自定义色，返回可以直接写进 CSS 的色值；认不出来就用 `fallback`。
 *
 * 兜底**不再是空串**：空串写进 `background` 等于"没有颜色"，在深色主题上看着就是黑的。
 * 认不出时退回主题色（`var(--accent)`）至少能看出"这里有一条标注"。
 */
export function highlightCss(color: string, fallback = 'var(--accent)'): string {
  if (!color) return fallback;
  if (color.startsWith('#') || /^(?:rgb|hsl|color)\s*\(/.test(color)) return color;
  return HL_COLOR_CSS[color] ?? fallback;
}
