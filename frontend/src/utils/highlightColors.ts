/** 高亮色板：**与后端 `backend/services/annotation_service.py::HIGHLIGHT_PALETTE` 同一份**。
 *
 * 「多巴胺」8 色（2026-12 用户口径：所有菜单的色板都换成这一套），逐字一致（顺序也一致）：
 * strawberry / tangerine / lemon / mint / lagoon / vega / grape / sakura。
 *
 * 历史那 9 个色名（red / orange / … / purple）**已经删干净**：库里存量高亮已全部重刷成这 8 个
 * hex，所以不需要再"认回老色名"——`paletteNameOf` 只认下面这 8 条，认不出的一律算自定义色。
 * 后端 `HIGHLIGHT_PALETTE` 现在也是这 8 条，两边键集合必须**完全相同**（少一条会红在
 * `tests/test_highlight_color.py::test_palette_matches_the_frontend_palette`）。
 *
 * hex 这里用大写写，后端 `_norm_color` 落库时统一转小写 —— 渲染一律大小写不敏感
 * （`paletteNameOf` 反查就是这么做的）。
 *
 * 为什么前端也要有这一份：库里**只存 hex**（后端 `_norm_color` 把色名映射成 hex 落库），
 * 而前端要在"落库之前"就把颜色画出来（取色点、"选的色"、笔记卡标题条）。名字表各存一份就会走散 ——
 * **改一处必须改两处**：后端那个 `HIGHLIGHT_PALETTE` 和这里，缺一条就会出现
 * "点这个色 → 落库是另一个色"。
 */
export const HL_PALETTE: Record<string, string> = {
  strawberry: '#FF4D6D',
  tangerine: '#FF8A3D',
  lemon: '#FFC93C',
  mint: '#3DDC84',
  lagoon: '#22D3EE',
  vega: '#4D7CFF',
  grape: '#A855F7',
  sakura: '#FF5CC8',
};

/** 菜单里列这 8 个（**顺序即菜单顺序**）= 色板全量。
 *
 * 为什么还要单独一条列表：菜单的**顺序**由它定（`HL_PALETTE` 是 Record，顺序只是书写顺序）；
 * 色点是数据驱动（行内 `--hl-sw`），所以加减色只改 `HL_PALETTE` + 这里两处，CSS 一行都不用动。 */
export const HL_SWATCHES: string[] = [
  'strawberry', 'tangerine', 'lemon', 'mint', 'lagoon', 'vega', 'grape', 'sakura',
];

/** 色 id → 中文色名（色点 `title` 用）。 */
export const HL_SWATCH_LABELS: Record<string, string> = {
  strawberry: '草莓红', tangerine: '橘子橙', lemon: '柠檬黄', mint: '薄荷绿',
  lagoon: '湖蓝', vega: '电光蓝', grape: '葡萄紫', sakura: '樱花粉',
};

/** `[色 id, hex]` 按 `HL_SWATCHES` 的顺序 —— 菜单渲染只吃这一份（不用再去查 `HL_PALETTE`）。 */
export const HL_SWATCH_HEX: Array<[string, string]> = HL_SWATCHES.map(
  (id) => [id, HL_PALETTE[id]] as [string, string],
);

/** 荧光笔底色的浓度：正文（`domMarks` / `useMarkdown` / `NotesView`）统一写成 `<hex>66` = 40%。
 *
 * 取色点也必须用**同一档**：色点比正文浓一档，"选的色"和"看到的色"就又不一样了
 * （色点由 `hlSwatchCss` 写进行内 `--hl-sw`，见 `base.css` 的 `.hl-swatch`）。
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

/** 存下来的高亮色 → **色 id**（这 8 个之一）；自定义色 / 认不出的一律 `null`。
 *
 * 为什么需要它：库里**只存 hex**（后端 `_norm_color` 落库前把色名换成 hex，且 hex 是大写写进来、
 * 落库转小写），而"当前是这个色"的判定以前写成 `stored === 'red'` —— hex 永远不等于色名，
 * 那圈"当前色"的环从来没亮过。所以统一**按归一化 hex 反查**：大小写不敏感，色 id 也原样认
 * （前端拿 id 画点、发给后端时更要认）。
 */
export function paletteNameOf(color?: string | null): string | null {
  const c = String(color ?? '').trim().toLowerCase();
  if (!c) return null;
  for (const [name, hex] of Object.entries(HL_PALETTE)) {
    if (name === c || hex.toLowerCase() === c) return name;
  }
  return null;
}


/** 色点该带哪个 class：色 id 原样（**只是钩子**，配色已不再走 CSS 类 —— 见 `base.css` 的
 *  `.hl-swatch`），自定义色一律 `custom-hex`。 */
export function swatchClassOf(color?: string | null): string {
  return paletteNameOf(color) ?? 'custom-hex';
}

/** 色 id / 任意色 → 中文色名（色点 `title` 用）：排不上号的老色回它自己的 id。 */
export function swatchLabel(color?: string | null): string {
  const id = paletteNameOf(color) ?? String(color ?? '').trim();
  return HL_SWATCH_LABELS[id] ?? id;
}

/** 色点里那层"色"的 CSS 值 —— **与 `mark.hl` 的 `--hl-bg` 同一口径**（见 `utils/domMarks.ts`
 *  的 `makeMark`）：色 id / `#rrggbb` 一律补 `66`（40%），`rgb()` / `hsl()` 交给 `color-mix` 取
 *  40%，3/8 位 hex 原样 —— 逐条与 `makeMark` 对齐，色点才不会比正文浓一档。
 *  ⚠️ 这里只给"色"，底色（不透明 `var(--bg)`）由 `base.css` 的 `.hl-swatch` 垫 —— 两者叠起来
 *  才与"正文 mark 压在阅读面上的 40%"是同一个合成结果。 */
export function hlSwatchCss(color?: string | null): string {
  const raw = String(color ?? '').trim();
  const id = paletteNameOf(raw);
  const c = id ? HL_PALETTE[id] : raw;
  if (/^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(c)) {
    return c.length === 7 ? c + HL_BG_ALPHA_HEX : c;
  }
  if (/^(?:rgba?|hsla?)\(/i.test(c)) return 'color-mix(in srgb, ' + c + ' 40%, transparent)';
  return c;
}

/** 色点的行内样式：**只写变量 `--hl-sw`**（不写 `background`）—— 底色与合成只有
 *  `base.css` 的 `.hl-swatch` 那一条，菜单只负责说"这是哪个色"。 */
export function swatchStyleOf(color?: string | null): Record<string, string> | undefined {
  const css = hlSwatchCss(color);
  return css ? { '--hl-sw': css } : undefined;
}
