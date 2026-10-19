/** 高亮色名 → 实际色值。
 *
 * 与 base.css 的 `.hl-swatch.*` / `mark.hl.*` **同一组颜色**，只是这里给的是"画在别处"用的一份：
 * 笔记卡的标题染色、笔记侧栏那条左竖线都读它。自定义色（`#rrggbb`）原样返回。
 */
export const HL_COLOR_CSS: Record<string, string> = {
  red: 'rgba(255, 0, 0, .9)',
  orange: 'rgba(255, 165, 0, .95)',
  yellow: 'rgba(230, 180, 20, .95)',
  green: 'rgba(60, 205, 130, .95)',
  blue: 'rgba(85, 165, 255, .95)',
  indigo: 'rgba(75, 0, 130, .95)',
  violet: 'rgba(143, 0, 255, .9)',
};

/** 给一个高亮色名 / 自定义色，返回可以直接写进 CSS 的色值；认不出来就用 `fallback`。 */
export function highlightCss(color: string, fallback = ''): string {
  if (!color) return fallback;
  return color.startsWith('#') ? color : (HL_COLOR_CSS[color] ?? fallback);
}