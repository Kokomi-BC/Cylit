/** 主题注册表：皮肤的唯一真值（移植自参照项目 retain-pdf 的 `ui/theme/registry.ts`）。
 *
 * 三类来源，**共用同一套 CSS 令牌**：
 * 1. **老皮肤**（本文件的 `THEME_REGISTRY`）：手写 `styles/themes/<id>.css`；
 * 2. **扩展预设**（`presets.ts` 的 `THEME_PRESETS`）：结构化配色，运行时生成令牌；
 * 3. **用户自定义**（localStorage `pm.theme.custom`）：与预设同一条生成路径。
 *
 * `id` 必须与 `html[data-theme]` 一致；`preview` 只是设置页色块的缩略色，
 * 不参与运行时换肤（真值永远是 CSS 令牌）。
 */

import { toRgb } from './color';
import { THEME_PRESETS, specToVars, type ThemeSpec } from './presets';

/** localStorage 键（沿用本站 `pm.<域>.<名>` 的命名）。 */
export const THEME_STORAGE_KEY = 'pm.theme';
/** 用户自定义主题（数组，见 `CustomTheme`）。 */
export const CUSTOM_THEME_STORAGE_KEY = 'pm.theme.custom';

/** 编辑器里"边调边看"用的临时主题 id（不落 storage）。 */
export const PREVIEW_THEME_ID = '__pm-preview__';

/** 默认 = 跟随系统：不选皮肤时就是 Cylit 原本的浅/深色观感。 */
export const DEFAULT_THEME_ID = 'auto';

export const THEME_CHANGE_EVENT = 'pm:theme-change';

/** 设置页色块预览用（与真正生效的令牌同源，仅用于缩略）。 */
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
 * 筛选维度：**一个主题库 + 轻量筛选**，不再按"开发来源 / 命名系列"分区
 * （那些是开发者视角，用户只需要浏览和挑）。一个主题可以同时属于多个维度
 * （比如自定义的深色主题 = 深色 + 自定义），筛选栏本身是单选。
 */
export type ThemeFacet = 'light' | 'dark' | 'color' | 'custom';

export interface ThemeDefinition {
  id: string;
  label: string;
  group: ThemeGroup;
  /** 列表排序，越小越靠前（内置主题在前、自定义在后） */
  order: number;
  preview: ThemePreview;
  /** 结构化配色：有它就能在运行时生成令牌（预设与自定义都有，老皮肤没有） */
  spec?: ThemeSpec;
  /** 用户自定义（可改名 / 改色 / 删除） */
  custom?: boolean;
}

/**
 * 老皮肤注册表（手写 CSS 的那几套）。顺序只影响 `listThemes()` 的兜底排序，面板按 `order` 排。
 */
export const THEME_REGISTRY: readonly ThemeDefinition[] = [
  {
    id: 'auto',
    label: '跟随系统',
    group: 'auto',
    order: 5,
    preview: { bg: '#f2f4f5', paper: '#ffffff', accent: '#0f7b8a', ink: '#171b1d', danger: '#b3261e' },
  },
  {
    id: 'classic',
    label: '经典',
    group: 'light',
    order: 10,
    preview: { bg: '#f5f5f7', paper: '#ffffff', accent: '#1d1d1f', ink: '#1d1d1f', danger: '#e0332a' },
  },
  {
    id: 'jiangnan',
    label: '素纸',
    group: 'accent',
    order: 20,
    preview: { bg: '#f1f0ed', paper: '#fbfaf8', accent: '#2a5f57', ink: '#1b1b1d', danger: '#c23b32' },
  },
  {
    id: 'mojia',
    label: '墨家',
    group: 'accent',
    order: 25,
    preview: { bg: '#f2efe8', paper: '#faf8f1', accent: '#4c6658', ink: '#26221b', danger: '#b23b32' },
  },
  {
    id: 'seacliff',
    label: '雾青',
    group: 'accent',
    order: 30,
    preview: { bg: '#eef1f4', paper: '#f8f9fb', accent: '#2d5f6e', ink: '#1a1d21', danger: '#c23b32' },
  },
  {
    id: 'night',
    label: '黛瓦夜色',
    group: 'dark',
    order: 40,
    preview: { bg: '#141618', paper: '#1e2226', accent: '#5aa88e', ink: '#e8e6e3', danger: '#e07068' },
  },
] as const;

/* --------------------------- 运行时注册（预设 / 自定义 / 预览） --------------------------- */

/** 由 `THEME_PRESETS` 生成的皮肤定义（色块缩略色也由 `spec` 现算，两处不会漂）。 */
function presetDefinition(id: string, label: string, spec: ThemeSpec, order: number): ThemeDefinition {
  return {
    id,
    label,
    group: spec.dark ? 'dark' : 'accent',
    order,
    spec,
    preview: {
      bg: spec.bg, paper: spec.surface, accent: spec.accent, ink: spec.ink,
      danger: spec.dark ? '#ff9a92' : '#b3261e',
    },
  };
}

const PRESET_DEFS: ThemeDefinition[] = THEME_PRESETS.map((p, i) =>
  presetDefinition(p.id, p.label, p.spec, 100 + i));

/** 用户自定义主题（内存里这一份 = 渲染用；真值在 localStorage，由 `theme.ts` 灌进来）。 */
let customDefs: ThemeDefinition[] = [];
/** 编辑器实时预览用的临时皮肤（不落 storage、不进列表）。 */
let previewDef: ThemeDefinition | null = null;

export interface CustomTheme {
  id: string;
  label: string;
  spec: ThemeSpec;
}

function customDefinition(t: CustomTheme, order: number): ThemeDefinition {
  const spec = t.spec;
  return {
    id: t.id,
    label: t.label,
    group: spec.dark ? 'dark' : 'accent',
    order,
    spec,
    custom: true,
    preview: {
      bg: spec.bg, paper: spec.surface, accent: spec.accent, ink: spec.ink,
      danger: spec.dark ? '#ff9a92' : '#b3261e',
    },
  };
}

/** 用一份自定义主题列表替换内存里的那一份（顺序即列表顺序）。 */
export function setCustomThemes(list: readonly CustomTheme[]): void {
  customDefs = list.map((t, i) => customDefinition(t, 200 + i));
}

/** 编辑器实时预览：把当前草稿挂成一个临时皮肤（`id = __pm-preview__`）。 */
export function setPreviewTheme(label: string, spec: ThemeSpec): void {
  previewDef = presetDefinition(PREVIEW_THEME_ID, label || '预览', spec, -1);
}

export function clearPreviewTheme(): void {
  previewDef = null;
}

/** 预览皮肤也要能被 `isThemeId` / `getThemeDefinition` 认出来。 */
function allDefinitions(): ThemeDefinition[] {
  return [...THEME_REGISTRY, ...PRESET_DEFS, ...customDefs, ...(previewDef ? [previewDef] : [])];
}

/** `ThemeId` 保留给"注册表里有、但调用方可能拿到旧字符串"的场景。 */
export type ThemeId = string;

/** 列表：内置（老皮肤 + 预设）在前，自定义在后；同一档按 `order`。 */
export function listThemes(): ThemeDefinition[] {
  return allDefinitions().sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
}

/* ------------------------------ 筛选 ------------------------------ */

/* 「彩色」的判据：**页面底色带不带色调**（而不是灰/白/黑）。
 * 为什么不用主色判断：几乎每套主题的主色都是彩色的，"彩色"就退化成了"全部"。
 * 阈值 0.2 是拿现有主题量出来的：默认底 #f2f4f5 ≈ .13、素纸 #f1f0ed ≈ .13 都算中性，
 * 雾蓝 #eef2f7 ≈ .37、暮夜 #1b1622 ≈ .36 算有色调。 */
const TINT_MIN = 0.2;

function saturation(hex: string): number {
  const [r, g, b] = toRgb(hex).map((n) => n / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max === min) return 0;
  const l = (max + min) / 2;
  return l > 0.5 ? (max - min) / (2 - max - min) : (max - min) / (max + min);
}

/** 一个主题落在哪些筛选维度里（可以同时属于多个：筛选栏本身是单选）。 */
export function themeFacets(def: ThemeDefinition): ThemeFacet[] {
  const out: ThemeFacet[] = [];
  // 「跟随系统」浅深两档都可能，两个筛选里都给
  if (def.group === 'auto') out.push('light', 'dark');
  else out.push(def.group === 'dark' ? 'dark' : 'light');
  if (saturation(def.preview.bg) >= TINT_MIN) out.push('color');
  if (def.custom) out.push('custom');
  return out;
}

export function getThemeDefinition(id: string): ThemeDefinition | undefined {
  return allDefinitions().find((t) => t.id === id);
}

export function isThemeId(value: unknown): value is string {
  return typeof value === 'string' && allDefinitions().some((t) => t.id === value);
}

/**
 * 所有**运行时生成**的皮肤 CSS（预设 + 自定义 + 预览）。
 * 老皮肤不走这里（它们有自己的 CSS 文件），所以只有"有 spec 的"才产出。
 */
export function generatedThemeCss(): string {
  return allDefinitions()
    .filter((t) => t.spec)
    .map((t) => {
      const body = Object.entries(specToVars(t.spec!))
        .map(([k, v]) => `  ${k}: ${v};`)
        .join('\n');
      return `[data-theme="${t.id}"] {\n${body}\n}`;
    })
    .join('\n\n');
}

