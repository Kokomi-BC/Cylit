/**
 * 显示大小：**整体缩放 + 五类字号**的单一真值（本机偏好，只写 localStorage）。
 *
 * 为什么不用 `transform: scale` / `zoom`：它们连布局一起缩放，侧栏宽度、表格列宽、
 * 固定高度的控件都会被撑破；这里只把"倍数"写成 CSS 变量，让字号自己乘上去，
 * 布局尺寸一条都不动。
 *
 * 级联链路：本模块把 `--ts-<类>` 落在 `:root`（值 = master × 该类），
 * `base.css` 再把 `--ts` 盖到各分类根上，子树里所有
 * `calc(Npx * var(--ts, 1))` 于是自动吃到对应倍数。
 */

import { reactive } from 'vue';

export type DisplayScale = {
  /** 整体缩放：所有分类都再乘一次 */
  master: number;
  /** 正文（论文、笔记的阅读字号） */
  body: number;
  /** 对话 */
  chat: number;
  /** 笔记 */
  note: number;
  /** 侧栏 */
  side: number;
  /** 界面（按钮、表格、表单等） */
  ui: number;
};

export type DisplayCategory = Exclude<keyof DisplayScale, 'master'>;

export const SCALE_STORAGE_KEY = 'cylit:display-scale';

/** 步进 5%：够细，又不会让用户按到手酸 */
export const SCALE_STEP = 0.05;
export const MASTER_MIN = 0.85;
export const MASTER_MAX = 1.25;
export const CATEGORY_MIN = 0.85;
export const CATEGORY_MAX = 1.5;

/** 分类顺序 = 设置页里那五行的顺序（正文 / 对话 / 笔记 / 侧栏 / 界面） */
export const DISPLAY_CATEGORIES: ReadonlyArray<DisplayCategory> = ['body', 'chat', 'note', 'side', 'ui'];

const DEFAULT_SCALE: DisplayScale = { master: 1, body: 1, chat: 1, note: 1, side: 1, ui: 1 };

function round2(value: number): number {
  // 步进是 0.05，浮点累加会留下 1.1500000000000001 这种尾巴；落库与显示都按 2 位小数收口
  return Math.round(value * 100) / 100;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function readNumber(raw: unknown, fallback: number, min: number, max: number): number {
  const n = typeof raw === 'number' ? raw : Number.parseFloat(String(raw ?? ''));
  return Number.isFinite(n) ? clamp(round2(n), min, max) : fallback;
}

/** 任何来源（storage、旧版本、手改过的 JSON）都先过这一层：坏值回落 1，越界钳到边界。 */
export function normalizeDisplayScale(raw: unknown): DisplayScale {
  const src = (raw && typeof raw === 'object' ? raw : {}) as Partial<Record<keyof DisplayScale, unknown>>;
  return {
    master: readNumber(src.master, 1, MASTER_MIN, MASTER_MAX),
    body: readNumber(src.body, 1, CATEGORY_MIN, CATEGORY_MAX),
    chat: readNumber(src.chat, 1, CATEGORY_MIN, CATEGORY_MAX),
    note: readNumber(src.note, 1, CATEGORY_MIN, CATEGORY_MAX),
    side: readNumber(src.side, 1, CATEGORY_MIN, CATEGORY_MAX),
    ui: readNumber(src.ui, 1, CATEGORY_MIN, CATEGORY_MAX),
  };
}

function loadDisplayScale(): DisplayScale {
  try {
    return normalizeDisplayScale(JSON.parse(localStorage.getItem(SCALE_STORAGE_KEY) || 'null'));
  } catch {
    return { ...DEFAULT_SCALE };
  }
}

/** 面板直接绑这个对象（改它就是改偏好）；外部只经 setScale / setCategory / resetScale 落值。 */
export const displayScale = reactive<DisplayScale>(loadDisplayScale());

/** 把有效值写到 `:root`：分类根只负责"读哪个变量"，真正的倍数都在这儿算。 */
export function applyDisplayScale(scale: DisplayScale = displayScale): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  for (const key of DISPLAY_CATEGORIES) {
    root.style.setProperty(`--ts-${key}`, (scale.master * scale[key]).toFixed(3));
  }
}

function commit(): void {
  applyDisplayScale();
  try {
    localStorage.setItem(SCALE_STORAGE_KEY, JSON.stringify(displayScale));
  } catch {
    /* 隐私模式 / 配额满：这一会儿仍然生效，只是下次进不来 */
  }
}

/** 整体缩放（85%–125%）。 */
export function setScale(next: number): void {
  displayScale.master = clamp(round2(next), MASTER_MIN, MASTER_MAX);
  commit();
}

/** 某一类字号（85%–150%）；注意页面上的最终倍数是 master × 该类。 */
export function setCategory(key: DisplayCategory, next: number): void {
  if (!DISPLAY_CATEGORIES.includes(key)) return;
  displayScale[key] = clamp(round2(next), CATEGORY_MIN, CATEGORY_MAX);
  commit();
}

/** 一键回到 100%（整体与五类一起复位）。 */
export function resetScale(): void {
  Object.assign(displayScale, DEFAULT_SCALE);
  commit();
}

/** 入口最顶部、`bootTheme()` 之后调用一次：挂载前落值，避免先按 100% 排一遍版再跳。 */
let booted = false;
export function bootDisplayScale(): DisplayScale {
  if (!booted) {
    booted = true;
    applyDisplayScale();
  }
  return { ...displayScale };
}
