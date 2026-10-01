/** 主题注册表：皮肤的唯一真值（移植自参照项目 retain-pdf 的 `ui/theme/registry.ts`）。
 *
 * 加一套皮肤 = 加一项 + `styles/themes/<id>.css` 里加一个 `[data-theme="<id>"]` 块，
 * **不要**在任何组件里 hardcode 皮肤 id 列表（设置页的外观面板读注册表渲染）。
 *
 * `id` 必须与 `html[data-theme]` 和 CSS 文件名一致；`preview` 只是设置页色块的缩略色，
 * 不参与运行时换肤（真值永远是 CSS）。
 */

/** localStorage 键（沿用本站 `pm.<域>.<名>` 的命名）。 */
export const THEME_STORAGE_KEY = 'pm.theme';

/** 默认 = 跟随系统：不选皮肤时就是 Cylit 原本的浅/深色观感。 */
export const DEFAULT_THEME_ID = 'auto';

export const THEME_CHANGE_EVENT = 'pm:theme-change';

/** 设置页色块预览用（与 themes/<id>.css 的主色一致，仅用于缩略）。 */
export interface ThemePreview {
  bg: string;
  paper: string;
  accent: string;
  ink: string;
  danger: string;
}

/** `auto` = 跟随系统（同一套令牌的浅色/深色两版）；其余是固定配色皮肤。 */
export type ThemeGroup = 'auto' | 'light' | 'dark' | 'accent';

/**
 * 主题系列（产品线维度，与明暗正交）：基础 / 诸子百家……
 * 新系列 = 此表加一行，外观面板自动出现新分区。
 */
export interface ThemeSeries {
  id: string;
  label: string;
  /** 分区排序，越小越靠前 */
  order: number;
}

export const THEME_SERIES: readonly ThemeSeries[] = [
  { id: 'base', label: '基础', order: 10 },
  { id: 'baijia', label: '诸子百家', order: 20 },
] as const;

export interface ThemeDefinition {
  id: string;
  label: string;
  description: string;
  group: ThemeGroup;
  /** 列表排序，越小越靠前 */
  order: number;
  preview: ThemePreview;
  /** 所属系列 id（THEME_SERIES），缺省归入 base */
  series?: string;
}

/**
 * 注册表真值。顺序只影响 `listThemes()` 的兜底排序，面板按 `order` 排。
 */
export const THEME_REGISTRY: readonly ThemeDefinition[] = [
  {
    id: 'auto',
    label: '跟随系统',
    description: '浅色 / 深色跟着 Windows 走',
    group: 'auto',
    order: 5,
    preview: { bg: '#f2f4f5', paper: '#ffffff', accent: '#0f7b8a', ink: '#171b1d', danger: '#b3261e' },
  },
  {
    id: 'classic',
    label: '经典',
    description: '黑白灰克制，主按钮走墨色',
    group: 'light',
    order: 10,
    preview: { bg: '#f5f5f7', paper: '#ffffff', accent: '#1d1d1f', ink: '#1d1d1f', danger: '#e0332a' },
  },
  {
    id: 'jiangnan',
    label: '素纸',
    description: '冷石灰底 · 冷青绿强调',
    group: 'accent',
    order: 20,
    preview: { bg: '#f1f0ed', paper: '#fbfaf8', accent: '#2a5f57', ink: '#1b1b1d', danger: '#c23b32' },
  },
  {
    id: 'mojia',
    label: '墨家',
    description: '素绢暖底 · 青铜绿强调',
    group: 'accent',
    order: 25,
    series: 'baijia',
    preview: { bg: '#f2efe8', paper: '#faf8f1', accent: '#4c6658', ink: '#26221b', danger: '#b23b32' },
  },
  {
    id: 'seacliff',
    label: '雾青',
    description: '冷灰蓝底 · 青灰强调',
    group: 'accent',
    order: 30,
    preview: { bg: '#eef1f4', paper: '#f8f9fb', accent: '#2d5f6e', ink: '#1a1d21', danger: '#c23b32' },
  },
  {
    id: 'night',
    label: '黛瓦夜色',
    description: '深底阅读 · 黛瓦墨黑',
    group: 'dark',
    order: 40,
    preview: { bg: '#141618', paper: '#1e2226', accent: '#5aa88e', ink: '#e8e6e3', danger: '#e07068' },
  },
] as const;

/** `ThemeId` 保留给"注册表里有、但调用方可能拿到旧字符串"的场景。 */
export type ThemeId = string;

const GROUP_LABEL: Record<ThemeGroup, string> = {
  auto: '自动',
  light: '浅色',
  dark: '深色',
  accent: '意境',
};

export function themeGroupLabel(group: ThemeGroup): string {
  return GROUP_LABEL[group] || group;
}

export function listThemes(): ThemeDefinition[] {
  return [...THEME_REGISTRY].sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
}

/**
 * 按系列分组（外观面板消费）：系列按 THEME_SERIES.order 排，
 * 未登记 series 的皮肤归入 base；空系列不出现。
 */
export function listThemesBySeries(): { series: string; label: string; themes: ThemeDefinition[] }[] {
  const map = new Map<string, ThemeDefinition[]>();
  for (const t of listThemes()) {
    const key = t.series && THEME_SERIES.some((s) => s.id === t.series) ? t.series : 'base';
    const list = map.get(key) || [];
    list.push(t);
    map.set(key, list);
  }
  return [...THEME_SERIES]
    .sort((a, b) => a.order - b.order)
    .filter((s) => (map.get(s.id) || []).length > 0)
    .map((s) => ({ series: s.id, label: s.label, themes: map.get(s.id) || [] }));
}

export function getThemeDefinition(id: string): ThemeDefinition | undefined {
  return THEME_REGISTRY.find((t) => t.id === id);
}

export function isThemeId(value: unknown): value is string {
  return typeof value === 'string' && THEME_REGISTRY.some((t) => t.id === value);
}
