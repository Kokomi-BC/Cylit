/**
 * 主题配色：**结构化配置 + 一套推导**，不再为每个主题手写一份 CSS 文件。
 *
 * 一个主题只需要 7 个颜色（用户能看懂的那 7 个）；其余二十多个令牌（悬浮底色、
 * 分隔线、按钮上的文字、阴影、纹理……）由 `specToVars()` 从这里推出来 ——
 * 所以加一个主题 = 加一条数据，预设、调色板、用户自定义**走完全同一条路**。
 *
 * 形状令牌（圆角 / 间距 / 动效 / 字体）**不在这里**：它们是全站语言，不随主题变。
 * 老皮肤（`styles/themes/*.css`）继续用同一套令牌名，两套机制可以并存。
 */

import { alpha, darken, isHex, lighten, mix, readableOn, toHex } from './color';

/** 一个主题的全部"用户可调"颜色。 */
export interface ThemeSpec {
  /** 页面背景 */
  bg: string;
  /** 卡片 / 面板背景 */
  surface: string;
  /** 主要文字 */
  ink: string;
  /** 次要文字 */
  dim: string;
  /** 主色（主按钮 / 选中态 / 进度） */
  accent: string;
  /** 强调浅底（悬浮、标签底色）；缺省由主色推 */
  accentSoft?: string;
  /** 边框 */
  border: string;
  /** 深色底（决定文字/阴影/语义色的那一档） */
  dark?: boolean;
}

/** 深色 / 浅色两档语义色：状态色必须一眼认得出，所以不跟着主色跑。 */
const SEMANTIC = {
  light: { ok: '#2a7d32', okSoft: '#e6f5e7', warn: '#8a5a00', warnSoft: '#fdf2d9', danger: '#b3261e', dangerSoft: '#fdeceb' },
  dark: { ok: '#74d17c', okSoft: '#16301a', warn: '#f0c464', warnSoft: '#362a0d', danger: '#ff9a92', dangerSoft: '#3a1a17' },
} as const;

/**
 * 主题配置 → CSS 令牌（与 `styles/tokens.css` 的名字一一对应）。
 *
 * 推导规则刻意简单（好解释也好预测）：
 * - `sunken / layer`：在"页面底"和"卡片面"之间插值 —— 深色下自然更深、浅色下自然更浅；
 * - `hover / pressed / divider / border-strong`：拿**文字色**带一点点透明度，深浅两档都成立；
 * - `accent-hover / accent-text`：深色底往白走、浅色底往黑走（对比度方向相反）；
 * - 主按钮上的文字按**主色亮度**选黑白，用户选亮黄也不会白字压黄底。
 */
export function specToVars(spec: ThemeSpec): Record<string, string> {
  const dark = !!spec.dark;
  const bg = toHex(spec.bg);
  const surface = toHex(spec.surface);
  const ink = toHex(spec.ink);
  const dim = toHex(spec.dim);
  const accent = toHex(spec.accent);
  const border = isHex(spec.border) ? toHex(spec.border) : alpha(ink, dark ? 0.16 : 0.12);
  const sem = dark ? SEMANTIC.dark : SEMANTIC.light;

  // 悬浮层的"标尺色"：浅色底用墨色加深，深色底用白色提亮
  const overlay = dark ? '#ffffff' : ink;

  return {
    'color-scheme': dark ? 'dark' : 'light',

    '--bg': bg,
    '--bg-elev': surface,
    '--bg-sunken': dark ? darken(bg, 0.35) : mix(bg, surface, 0.62),
    '--bg-layer': mix(bg, surface, 0.5),
    '--bg-hover': alpha(overlay, dark ? 0.06 : 0.045),
    '--bg-pressed': alpha(overlay, dark ? 0.04 : 0.08),
    '--bg-subtle': alpha(overlay, dark ? 0.035 : 0.028),
    '--smoke': dark ? 'rgba(0, 0, 0, .55)' : alpha(ink, 0.38),

    '--border': border,
    '--border-strong': alpha(overlay, dark ? 0.19 : 0.19),
    '--divider': alpha(overlay, dark ? 0.09 : 0.085),

    '--text': ink,
    '--text-dim': dim,
    '--text-faint': mix(dim, bg, 0.42),
    '--text-on-accent': readableOn(accent),

    '--accent': accent,
    '--accent-hover': dark ? lighten(accent, 0.14) : darken(accent, 0.14),
    '--accent-soft': spec.accentSoft && isHex(spec.accentSoft)
      ? toHex(spec.accentSoft)
      : (dark ? mix(bg, accent, 0.22) : mix(surface, accent, 0.11)),
    '--accent-text': dark ? lighten(accent, 0.3) : darken(accent, 0.24),
    '--selection-bg': alpha(accent, 0.26),

    '--ok': sem.ok, '--ok-soft': sem.okSoft,
    '--warn': sem.warn, '--warn-soft': sem.warnSoft,
    '--danger': sem.danger, '--danger-soft': sem.dangerSoft,

    '--shadow-1': dark
      ? '0 1px 2px rgba(0, 0, 0, .30), 0 8px 20px -12px rgba(0, 0, 0, .50)'
      : `0 1px 2px ${alpha(ink, 0.04)}, 0 6px 16px -10px ${alpha(ink, 0.12)}`,
    '--shadow-2': dark
      ? '0 2px 8px rgba(0, 0, 0, .38), 0 18px 36px -18px rgba(0, 0, 0, .60)'
      : `0 2px 6px ${alpha(ink, 0.06)}, 0 16px 32px -16px ${alpha(ink, 0.18)}`,
    '--shadow-flyout': dark
      ? '0 14px 24px -8px rgba(0, 0, 0, .50), 0 34px 60px -26px rgba(0, 0, 0, .70)'
      : `0 12px 20px -8px ${alpha(ink, 0.12)}, 0 32px 56px -24px ${alpha(ink, 0.26)}`,
    '--shadow-press': dark
      ? 'inset 0 1px 2px rgba(0, 0, 0, .40)'
      : `inset 0 1px 2px ${alpha(ink, 0.10)}`,

    '--bg-texture': dark
      ? `radial-gradient(1100px 560px at 10% -10%, ${alpha(accent, 0.12)}, transparent 72%), `
        + 'radial-gradient(900px 480px at 100% 0%, rgba(255, 255, 255, .03), transparent 65%)'
      : `radial-gradient(1200px 600px at 12% -8%, ${alpha(accent, 0.07)}, transparent 70%), `
        + `radial-gradient(900px 500px at 100% 0%, ${alpha(ink, 0.03)}, transparent 65%)`,
  };
}

/* ============================ 预设主题 ============================ */

export interface ThemePreset {
  id: string;
  label: string;
  spec: ThemeSpec;
}

/**
 * 主题库：只按"好不好看"组织，**不再分系列**（系列是开发者视角，用户只需要浏览与筛选）。
 * 每一套都换了完整配色（底色 / 卡片 / 边框 / 文字 / 主色），不是只改一个主色。
 * 颜色一律克制，高饱和只出现在用户主动挑选的「多巴胺」调色板里。
 */
export const THEME_PRESETS: readonly ThemePreset[] = [
  /* ---- 中性 ---- */
  {
    id: 'mistblue', label: '雾蓝',
    spec: { bg: '#eef2f7', surface: '#fbfcfe', ink: '#1b2028', dim: '#5f6a78', accent: '#3a6ea8', border: '#1b202814' },
  },
  {
    id: 'sky', label: '天青',
    spec: { bg: '#eaf5fb', surface: '#f8fcff', ink: '#142430', dim: '#55707f', accent: '#2b8cc4', border: '#14243014' },
  },
  {
    id: 'indigo', label: '靛青',
    spec: { bg: '#eef0f8', surface: '#fafbff', ink: '#1a1c2c', dim: '#5c6076', accent: '#3b4a8f', border: '#1a1c2c14' },
  },
  {
    id: 'celadon', label: '青岚',
    spec: { bg: '#eef4f1', surface: '#f9fdfb', ink: '#1a2320', dim: '#5b6b65', accent: '#2f7d6a', border: '#1a232014' },
  },
  {
    id: 'turquoise', label: '松石',
    spec: { bg: '#ecf3f4', surface: '#f8fdfd', ink: '#16242a', dim: '#566a70', accent: '#16707f', border: '#16242a14' },
  },
  {
    id: 'mint', label: '薄荷',
    spec: { bg: '#edf6f2', surface: '#f9fefc', ink: '#17241f', dim: '#586b63', accent: '#2f8f70', border: '#17241f14' },
  },
  {
    id: 'moss', label: '苔绿',
    spec: { bg: '#eef2ea', surface: '#fafcf7', ink: '#1c2118', dim: '#606a58', accent: '#5a7d3f', border: '#1c211814' },
  },
  {
    id: 'amber', label: '琥珀',
    spec: { bg: '#f7f2e9', surface: '#fffaf0', ink: '#2a2318', dim: '#6f6455', accent: '#a9721c', border: '#2a231814' },
  },
  {
    id: 'sand', label: '暖砂',
    spec: { bg: '#f6f1e8', surface: '#fffcf6', ink: '#2a241a', dim: '#6f6656', accent: '#b0603f', border: '#2a241a14' },
  },
  {
    id: 'coffee', label: '咖啡',
    spec: { bg: '#f4efe9', surface: '#fdfaf6', ink: '#241d16', dim: '#6b6055', accent: '#6b4f34', border: '#241d1614' },
  },
  {
    id: 'grape', label: '葡萄紫',
    spec: { bg: '#f4f0f8', surface: '#fcf9ff', ink: '#241d2e', dim: '#655c75', accent: '#6d4a9c', border: '#241d2e14' },
  },
  {
    id: 'lotus', label: '藕荷',
    spec: { bg: '#f6f0f4', surface: '#fdfafc', ink: '#291f2a', dim: '#70606c', accent: '#8a5a86', border: '#291f2a14' },
  },
  {
    id: 'rose', label: '玫瑰',
    spec: { bg: '#f8f0f1', surface: '#fffafa', ink: '#2c1c1f', dim: '#756066', accent: '#a8485c', border: '#2c1c1f14' },
  },
  {
    id: 'sakura', label: '樱粉',
    spec: { bg: '#faf1f3', surface: '#fffbfc', ink: '#2e2023', dim: '#7a676c', accent: '#c2607f', border: '#2e202314' },
  },

  /* ---- 深色 ---- */
  {
    id: 'deepsea', label: '深海',
    spec: { bg: '#10202b', surface: '#172c39', ink: '#e6eef3', dim: '#a9bcc7', accent: '#4fb3c9', border: '#ffffff1f', dark: true },
  },
  {
    id: 'navy', label: '夜蓝',
    spec: { bg: '#0f1a2b', surface: '#16243a', ink: '#e4ecf6', dim: '#9fb2c8', accent: '#5b93d6', border: '#ffffff1f', dark: true },
  },
  {
    id: 'dusk', label: '暮夜',
    spec: { bg: '#1b1622', surface: '#251e2e', ink: '#efe9f4', dim: '#b3a8c0', accent: '#9b7fd4', border: '#ffffff1f', dark: true },
  },
  {
    id: 'graphite', label: '石墨',
    spec: { bg: '#1a1c1e', surface: '#24272a', ink: '#ecedee', dim: '#a9adb2', accent: '#7fa8c9', border: '#ffffff1f', dark: true },
  },
  {
    id: 'obsidian', label: '曜石',
    spec: { bg: '#131415', surface: '#1c1e20', ink: '#e9eaec', dim: '#a6a9ad', accent: '#b0894f', border: '#ffffff1f', dark: true },
  },
] as const;

/* ============================ 调色板 ============================ */

export interface ThemePalette {
  id: string;
  label: string;
  /** 学术 = 克制（默认那一档）；多巴胺 = 高明度高饱和，用户主动选才用 */
  group: 'academic' | 'dopamine';
  spec: ThemeSpec;
}

/**
 * 调色板：一次把整套颜色铺好（不是只改主色），用户可以在此基础上继续微调。
 * 「多巴胺」那组刻意做得明快刺眼 —— 它只出现在自定义编辑器里，默认主题不受影响。
 */
export const THEME_PALETTES: readonly ThemePalette[] = [
  {
    id: 'soft-academic', label: '柔和学术', group: 'academic',
    spec: { bg: '#f2f4f5', surface: '#ffffff', ink: '#171b1d', dim: '#5b6469', accent: '#0f7b8a', border: '#0d202614' },
  },
  {
    id: 'calm-blue', label: '冷静蓝', group: 'academic',
    spec: { bg: '#eef2f7', surface: '#fbfcfe', ink: '#1b2028', dim: '#5f6a78', accent: '#3a6ea8', border: '#1b202814' },
  },
  {
    id: 'forest', label: '森林绿', group: 'academic',
    spec: { bg: '#eef3ee', surface: '#f9fcf9', ink: '#1a211a', dim: '#5c6a5c', accent: '#2f6b45', border: '#1a211a14' },
  },
  {
    id: 'warm-orange', label: '暖橙', group: 'academic',
    spec: { bg: '#f7f1ea', surface: '#fffbf6', ink: '#2a221a', dim: '#6f6355', accent: '#b4661f', border: '#2a221a14' },
  },
  {
    id: 'violet', label: '紫罗兰', group: 'academic',
    spec: { bg: '#f3f0f8', surface: '#fbf9ff', ink: '#231d2c', dim: '#645b73', accent: '#6a4b9e', border: '#231d2c14' },
  },
  {
    id: 'rosewood', label: '玫瑰', group: 'academic',
    spec: { bg: '#f8f0f1', surface: '#fffafa', ink: '#2c1c1f', dim: '#756066', accent: '#a8485c', border: '#2c1c1f14' },
  },
  {
    id: 'teal', label: '青绿', group: 'academic',
    spec: { bg: '#ecf4f3', surface: '#f8fdfc', ink: '#162322', dim: '#556a68', accent: '#14776b', border: '#16232214' },
  },
  {
    id: 'night', label: '深色夜间', group: 'academic',
    spec: { bg: '#141618', surface: '#1e2226', ink: '#e8e6e3', dim: '#a9adb2', accent: '#5aa88e', border: '#ffffff1f', dark: true },
  },

  /* ---- 多巴胺：高明度 / 高饱和 / 明快，只在用户主动挑的时候出现 ---- */
  {
    id: 'dopamine-electric-blue', label: '电光蓝', group: 'dopamine',
    spec: { bg: '#eef4ff', surface: '#ffffff', ink: '#101a2e', dim: '#55617a', accent: '#1f6bff', border: '#101a2e1a' },
  },
  {
    id: 'dopamine-neon-cyan', label: '荧光青', group: 'dopamine',
    spec: { bg: '#e9fbfb', surface: '#ffffff', ink: '#0d2226', dim: '#4f6a6e', accent: '#00b3c4', border: '#0d22261a' },
  },
  {
    id: 'dopamine-vivid-purple', label: '亮紫', group: 'dopamine',
    spec: { bg: '#f5efff', surface: '#ffffff', ink: '#1d1330', dim: '#61527e', accent: '#7c3aed', border: '#1d13301a' },
  },
  {
    id: 'dopamine-hot-pink', label: '活力粉', group: 'dopamine',
    spec: { bg: '#fff0f6', surface: '#ffffff', ink: '#2c1220', dim: '#7a5468', accent: '#e0308a', border: '#2c12201a' },
  },
  {
    id: 'dopamine-coral', label: '珊瑚橙', group: 'dopamine',
    spec: { bg: '#fff2ec', surface: '#ffffff', ink: '#2e1710', dim: '#7d5c50', accent: '#f4623a', border: '#2e17101a' },
  },
  {
    id: 'dopamine-lemon', label: '柠檬黄', group: 'dopamine',
    spec: { bg: '#fffbe8', surface: '#ffffff', ink: '#2b2408', dim: '#756a44', accent: '#e0b100', border: '#2b24081a' },
  },
  {
    id: 'dopamine-lime', label: '青柠绿', group: 'dopamine',
    spec: { bg: '#f2fce9', surface: '#ffffff', ink: '#16250c', dim: '#5d7050', accent: '#65b800', border: '#16250c1a' },
  },
  {
    id: 'dopamine-blue-violet', label: '蓝紫', group: 'dopamine',
    spec: { bg: '#eff0ff', surface: '#ffffff', ink: '#141430', dim: '#565a80', accent: '#4f46e5', border: '#1414301a' },
  },
] as const;

/** 预设 / 自定义都在找的那几条：把 `spec` 补齐成一份完整、合法的配置。 */
export function normalizeSpec(input: Partial<ThemeSpec> | null | undefined): ThemeSpec {
  const base = THEME_PALETTES[0].spec;
  const pick = (v: unknown, fallback: string) => (isHex(v) ? toHex(v as string) : fallback);
  return {
    bg: pick(input?.bg, base.bg),
    surface: pick(input?.surface, base.surface),
    ink: pick(input?.ink, base.ink),
    dim: pick(input?.dim, base.dim),
    accent: pick(input?.accent, base.accent),
    accentSoft: isHex(input?.accentSoft) ? toHex(input!.accentSoft as string) : undefined,
    border: pick(input?.border, base.border),
    dark: !!input?.dark,
  };
}
