/** 皮肤运行时：读写 `html[data-theme]` + localStorage，并广播换肤事件。
 *
 * 真值有**两份**、职责不同：
 * - `html[data-theme]` —— 给 CSS 用（`styles/themes/*.css` 只认它）；
 * - localStorage（`pm.theme`）—— 记住本机选择，下次进来先按它落值（`bootTheme()` 在挂载前调，
 *   避免"先渲染默认色再跳成皮肤色"的闪一下）。
 *
 * 组件不直接碰这两份，一律走 `getTheme()` / `setTheme()`（面板只负责把用户点的 id 递进来）。
 */

import {
  DEFAULT_THEME_ID,
  THEME_CHANGE_EVENT,
  THEME_STORAGE_KEY,
  getThemeDefinition,
  isThemeId,
} from './registry';

export * from './registry';

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
  try {
    localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch {
    /* 隐私模式：存不下不影响本次生效 */
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

/** 入口最顶部调用一次（先于 `createApp().mount()`），把 storage 里的选择落到 DOM。 */
let booted = false;
export function bootTheme(): string {
  if (booted) return getTheme();
  booted = true;
  return setTheme(getStoredTheme());
}
