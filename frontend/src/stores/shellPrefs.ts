/**
 * 外壳偏好：左侧栏的手动宽度（展开 / 收起成图标轨）。
 *
 * 为什么单独一个文件：这属于"用户对**外壳**的偏好"，和标签（`stores/tabs`）、
 * 导航页的分区记忆（`stores/navMemory`）是三条互不相干的线；塞进任何一个都会让它变得
 * 说不清楚。键名延续 `pm.shell.*` 这一族。
 *
 * ⚠️ 手动状态只在 **≥1024px** 生效：更窄时侧栏由 CSS 强制收成 64px 图标轨，
 * <720px 干脆整栏隐藏（见 `AppSidebar` 的媒体查询与 `base.css` 的 `--rail-w`）。
 */
import { readonly, ref } from 'vue';

const STORAGE_KEY = 'pm.shell.rail';

function load(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'collapsed';
  } catch {
    return false;      /* 隐私模式：当默认展开 */
  }
}

const collapsed = ref(load());

/** 侧栏是否处于**手动**收起态（只读：改状态一律走 `setRailCollapsed` / `toggleRail`） */
export const railCollapsed = readonly(collapsed);

export function setRailCollapsed(value: boolean): void {
  collapsed.value = value;
  try {
    localStorage.setItem(STORAGE_KEY, value ? 'collapsed' : 'expanded');
  } catch { /* 隐私模式忽略：界面照常切换，只是记不住 */ }
}

export function toggleRail(): void {
  setRailCollapsed(!collapsed.value);
}
