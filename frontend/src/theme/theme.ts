/** 皮肤运行时：读写 `html[data-theme]` + localStorage，并广播换肤事件。
 *
 * 真值有**两份**、职责不同：
 * - `html[data-theme]` —— 给 CSS 用（`styles/themes/*.css` 与运行时注入的那张 <style> 都只认它）；
 * - localStorage —— 记住本机选择（`pm.theme`）与用户自定义主题（`pm.theme.custom`），
 *   下次进来先按它落值（`bootTheme()` 在挂载前调，避免"先渲染默认色再跳成皮肤色"的闪一下）。
 *
 * 组件不直接碰这两份，一律走 `getTheme()` / `setTheme()`（面板只负责把用户点的 id 递进来）。
 */

import {
  CUSTOM_THEME_STORAGE_KEY,
  DEFAULT_THEME_ID,
  PREVIEW_THEME_ID,
  THEME_CHANGE_EVENT,
  THEME_STORAGE_KEY,
  clearPreviewTheme,
  generatedThemeCss,
  getThemeDefinition,
  isThemeId,
  setCustomThemes,
  setPreviewTheme,
  type CustomTheme,
} from './registry';
import { normalizeSpec, type ThemeSpec } from './presets';

export * from './registry';
export * from './presets';
export * from './color';

/* --------------------------- 用户自定义主题 ---------------------------
 * 存在本机（`pm.theme.custom`）：一个数组，元素 = `{id, label, spec}`。
 * 读写都做**防御性校验**（localStorage 是可以被手改 / 被旧版本写坏的），
 * 坏数据直接丢掉而不是让设置页崩掉。
 */

function sanitize(raw: unknown): CustomTheme[] {
  if (!Array.isArray(raw)) return [];
  const out: CustomTheme[] = [];
  for (const item of raw) {
    const it = item as Partial<CustomTheme> | null;
    const id = `${it?.id ?? ''}`.trim();
    const label = `${it?.label ?? ''}`.trim();
    if (!id.startsWith('custom-') || !label) continue;
    out.push({ id, label: label.slice(0, 24), spec: normalizeSpec(it?.spec) });
    if (out.length >= 24) break;      // 上限：列表不该被无限撑长
  }
  return out;
}

/** 读用户自定义主题（并灌进注册表，`isThemeId` / 列表渲染都靠它）。 */
export function loadCustomThemes(): CustomTheme[] {
  let list: CustomTheme[] = [];
  try {
    const raw = localStorage.getItem(CUSTOM_THEME_STORAGE_KEY);
    list = raw ? sanitize(JSON.parse(raw)) : [];
  } catch {
    list = [];
  }
  setCustomThemes(list);
  return list;
}

function persistCustomThemes(list: CustomTheme[]): void {
  try {
    localStorage.setItem(CUSTOM_THEME_STORAGE_KEY, JSON.stringify(list));
  } catch {
    /* 隐私模式：存不下不影响本次生效 */
  }
  setCustomThemes(list);
}

/** 新建 / 覆盖一个自定义主题（id 已存在就是覆盖）。返回最终落定的 id。 */
export function saveCustomTheme(input: { id?: string; label: string; spec: ThemeSpec }): string {
  const list = loadCustomThemes();
  const label = `${input.label || ''}`.trim().slice(0, 24) || '自定义主题';
  const spec = normalizeSpec(input.spec);
  const id = input.id && input.id.startsWith('custom-')
    ? input.id
    : `custom-${Date.now().toString(36)}${Math.floor(Math.random() * 1e3).toString(36)}`;
  const idx = list.findIndex((t) => t.id === id);
  if (idx >= 0) list[idx] = { id, label, spec };
  else list.push({ id, label, spec });
  persistCustomThemes(list);
  return id;
}

/** 删除一个自定义主题（**只认 `custom-` 前缀**，内置主题删不掉）。 */
export function deleteCustomTheme(id: string): void {
  if (!id.startsWith('custom-')) return;
  persistCustomThemes(loadCustomThemes().filter((t) => t.id !== id));
  if (getTheme() === id) setTheme(DEFAULT_THEME_ID);
}

/* ------------------------------ 换肤 ------------------------------ */

/** 运行时生成的那些皮肤（预设 / 自定义 / 预览）合成的一张 <style>。 */
const VARS_STYLE_ID = 'pm-theme-vars';

/**
 * 把"有 spec 的皮肤"写成一张 `<style>` 塞进 `<head>`。
 *
 * 每次重写整张表（而不是逐个增删）：主题数量在几十的量级，整张重写的代价可以忽略，
 * 换来的是"不会有残留规则"——删掉的自定义主题立刻不再占位。
 */
export function applyGeneratedThemeCss(): void {
  if (typeof document === 'undefined') return;
  const css = generatedThemeCss();
  let el = document.getElementById(VARS_STYLE_ID) as HTMLStyleElement | null;
  if (!el) {
    el = document.createElement('style');
    el.id = VARS_STYLE_ID;
    document.head.appendChild(el);
  }
  el.textContent = css;
}

/** 从 localStorage 读上次的选择；没存过 / 存的是已下线的皮肤 → 默认值。 */
export function getStoredTheme(): string {
  if (typeof localStorage === 'undefined') return DEFAULT_THEME_ID;
  try {
    const raw = `${localStorage.getItem(THEME_STORAGE_KEY) || ''}`.trim();
    if (isThemeId(raw)) return raw;
  } catch {
    /* 隐私模式：读不到就算了 */
  }
  return DEFAULT_THEME_ID;
}

/** 当前生效的皮肤：以 DOM 为准（可能与 storage 不同步，比如另一个标签页刚换过）。 */
export function getTheme(): string {
  if (typeof document !== 'undefined') {
    const fromDom = document.documentElement.dataset.theme;
    if (isThemeId(fromDom)) return fromDom;
  }
  return getStoredTheme();
}

/** 写 storage + `<html data-theme>`，并广播事件。返回最终落定的皮肤 id。 */
export function setTheme(theme: string): string {
  const next = isThemeId(theme) ? theme : DEFAULT_THEME_ID;
  // 预览是临时皮肤：只改 DOM，不写 storage（否则刷新后会停在一个不存在的主题上）
  if (next !== PREVIEW_THEME_ID) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      /* 隐私模式：存不下不影响本次生效 */
    }
  }
  if (typeof document !== 'undefined') {
    const root = document.documentElement;
    root.dataset.theme = next;
    // 分组只给"个别组件要写 .theme-dark 特例"用；深色判定不靠它（颜色全在 CSS 令牌里）
    const def = getThemeDefinition(next);
    root.dataset.themeGroup = def?.group || 'auto';
    root.classList.toggle('theme-dark', def?.group === 'dark');
  }
  if (typeof window !== 'undefined') {
    try {
      window.dispatchEvent(new CustomEvent(THEME_CHANGE_EVENT, { detail: { theme: next } }));
    } catch {
      /* 事件发不出去不该影响换肤本身 */
    }
  }
  return next;
}

/**
 * 编辑器实时预览：把草稿挂成临时皮肤并立刻生效（不写 storage）。
 * 调用方要记住"进来之前是哪个主题"，取消时还原。
 */
export function previewTheme(label: string, spec: ThemeSpec): void {
  setPreviewTheme(label, spec);
  applyGeneratedThemeCss();
  setTheme(PREVIEW_THEME_ID);
}

/** 结束预览：丢掉临时皮肤并还原到 `restoreId`（缺省 = 默认主题）。 */
export function endPreview(restoreId?: string): string {
  clearPreviewTheme();
  applyGeneratedThemeCss();
  return setTheme(restoreId && isThemeId(restoreId) ? restoreId : DEFAULT_THEME_ID);
}

/** 入口最顶部调用一次（先于 `createApp().mount()`），把 storage 里的选择落到 DOM。 */
let booted = false;
export function bootTheme(): string {
  if (booted) return getTheme();
  booted = true;
  loadCustomThemes();          // 自定义主题要先登记，`isThemeId` 才认得上一次选的那套
  applyGeneratedThemeCss();
  return setTheme(getStoredTheme());
}

/** 设置页里改完自定义主题后调一次：重建 CSS（列表与色块都会跟着刷新）。 */
export function refreshCustomThemes(): CustomTheme[] {
  const list = loadCustomThemes();
  applyGeneratedThemeCss();
  return list;
}
