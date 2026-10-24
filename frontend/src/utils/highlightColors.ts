/** 高亮色板：**与后端 `backend/services/annotation_service.py::HIGHLIGHT_PALETTE` 同一份**。
 *
 * 9 个色名 → hex，逐字一致（顺序也一致）：red / orange / yellow / green / blue / indigo /
 * violet / pink / purple。
 *
 * 为什么前端也要有这一份：库里现在**只存 hex**（后端 `_norm_color` 把色名映射成 hex 落库），
 * 而前端要在"落库之前"就把颜色画出来（取色点、"选的色"、笔记卡标题条）。名字表各存一份就会走散 ——
 * **改一处必须改两处**：后端那个 `HIGHLIGHT_PALETTE` 和这里，缺一条就会出现
 * "点这个色 → 落库是另一个色"。
 *
 * `pink` / `purple` 是**历史色名**（界面不再提供，存量高亮还认）：后端色板里同样保留着，
 * 所以这里也留 —— 两边条目必须一字不差。
 */
export const HL_PALETTE: Record<string, string> = {
  red: '#ff0000',
  orange: '#ffa500',
  yellow: '#e6b414',
  green: '#3ccd82',
  blue: '#55a5ff',
  indigo: '#4b0082',
  violet: '#8f00ff',
  pink: '#ff64a5',
  purple: '#965adc',
};

/** 荧光笔底色的浓度：正文（`domMarks` / `useMarkdown` / `NotesView`）统一写成 `<hex>66` = 40%。
 *
 * 取色点也必须用**同一档**：色点比正文浓一档，"选的色"和"看到的色"就又不一样了
 * （见 `ReaderView` 里把 `HL_PALETTE` 摊成 `--hl-sw-*` 的那段注释）。
 */
export const HL_BG_ALPHA_HEX = '66';

/** `#rrggbb` → `rgba(r, g, b, a)`；给不出合法 hex 时原样返回（自定义色 / 变量名照旧能写进 CSS）。 */
function hexToRgba(hex: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex;
  const n = Number.parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/** 高亮色名 → 可以直接写进 CSS 的**实色**（笔记卡标题染色、笔记侧栏那条左竖线用它）。
 *
 * **从 `HL_PALETTE` 派生**，不再各写一遍色值：以前这里是 9 个手写 rgba、`base.css` 的
 * `.hl-swatch.*` 又是另一套、`mark.hl.*` 再一套 —— 三份色值互相漂移，同一个"红"能差出两档。
 * 自定义色（`#rrggbb` / `rgb()` / `hsl()`）由 `highlightCss()` 原样放过，不走这张表。
 *
 * 色值以 `HL_PALETTE` 的 **hex 字面量**为准（`tests/test_highlight_color.py` 直接读它的文本、
 * 与后端 `HIGHLIGHT_PALETTE` 逐条比对），所以这里不再手抄第二份 rgba —— 浓度由 `hexToRgba` 派生。
 */
export const HL_COLOR_CSS: Record<string, string> = Object.fromEntries(
  Object.entries(HL_PALETTE).map(([name, hex]) => [name, hexToRgba(hex, 0.95)]),
);

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
