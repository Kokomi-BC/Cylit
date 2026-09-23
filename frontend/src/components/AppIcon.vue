<script setup lang="ts">
/**
 * AppIcon —— 全站唯一的图标入口（内联 SVG，`currentColor`）。
 *
 * 约束（本轮前端规范）：
 * - **只用 SVG**：不用字体图标、不用 emoji、不用 `←/×/▸/＋` 这类字符充当图标；
 * - WinUI 3 观感：24 网格、1.6 描边、圆头圆角、无填充色块，颜色跟随文本；
 * - 图标名保持语义化（`search/refresh/translate/...`），禁止在视图里内联 `<svg>`。
 *
 * 用法：`<AppIcon name="search" :size="16" />`
 */
import { computed } from 'vue';

const props = withDefaults(defineProps<{
  name: string;
  size?: number;
  /** 线宽（WinUI 常规 1.6；小尺寸图标可给 1.8 更清晰） */
  weight?: number;
}>(), { size: 16, weight: 1.6 });

/** name → <svg> 内部标记（纯静态，无用户数据参与，故可安全用 v-html）。 */
const ICONS: Record<string, string> = {
  // ---- 导航 / 结构 ----
  library: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10v16H5.5A1.5 1.5 0 0 1 4 18.5z"/><path d="M12 4h4.5A1.5 1.5 0 0 1 18 5.5v13a1.5 1.5 0 0 1-1.5 1.5H12z"/>',
  knowledge: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.66 3.13 3 7 3s7-1.34 7-3V6"/><path d="M5 12c0 1.66 3.13 3 7 3s7-1.34 7-3"/>',
  note: '<path d="M5 4.5A1.5 1.5 0 0 1 6.5 3h11A1.5 1.5 0 0 1 19 4.5V15l-5 6H6.5A1.5 1.5 0 0 1 5 19.5z"/><path d="M19 15h-5v6"/><path d="M9 8h6M9 12h4"/>',
  model: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>',
  task: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.2V12l3.4 2"/>',
  settings: '<path d="M4 7h16M4 12h16M4 17h16"/><circle cx="9" cy="7" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="8" cy="17" r="2"/>',
  list: '<path d="M4 6.5h16M4 12h16M4 17.5h10"/>',
  panel: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M12 4.5v15"/>',
  layers: '<path d="M12 3.5 21 8l-9 4.5L3 8z"/><path d="M4.6 12.4 12 16l7.4-3.6M4.6 16.4 12 20l7.4-3.6"/>',

  // ---- 方向 / 折叠 ----
  back: '<path d="M19 12H5"/><path d="M11 6 5 12l6 6"/>',
  forward: '<path d="M5 12h14"/><path d="M13 6l6 6-6 6"/>',
  'chevron-down': '<path d="M6 9.5 12 15.5 18 9.5"/>',
  'chevron-up': '<path d="M6 14.5 12 8.5 18 14.5"/>',
  'chevron-left': '<path d="M14.5 6 8.5 12l6 6"/>',
  'chevron-right': '<path d="M9.5 6l6 6-6 6"/>',
  'unfold-more': '<path d="M8 9.5 12 5.5l4 4"/><path d="M8 14.5l4 4 4-4"/>',
  'unfold-less': '<path d="M8 5.5l4 4 4-4"/><path d="M8 18.5l4-4 4 4"/>',
  // 溢出菜单（等价于 Phosphor 的 DotsThree）：把次要操作收起来，避免按钮墙
  more: '<circle cx="6" cy="12" r="1.35"/><circle cx="12" cy="12" r="1.35"/><circle cx="18" cy="12" r="1.35"/>',

  // ---- 动作 ----
  add: '<path d="M12 5v14M5 12h14"/>',
  remove: '<path d="M5 12h14"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  check: '<path d="M5 12.5 9.5 17 19 7.5"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
  edit: '<path d="M4 20h4L20 8l-4-4L4 16z"/><path d="M14 6l4 4"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V5h6v2"/><path d="M6.5 7 7.5 20h9L17.5 7"/><path d="M10.5 11v5M13.5 11v5"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.3-4.3"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.4 6.3"/><path d="M20 5.5V11h-5.5"/>',
  filter: '<path d="M4 6h16l-6.2 7.2V19l-3.6 1.6v-7.4z"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-8.5 8.5"/><path d="M18 14v5.5A1.5 1.5 0 0 1 16.5 21h-11A1.5 1.5 0 0 1 4 19.5v-11A1.5 1.5 0 0 1 5.5 7H11"/>',
  upload: '<path d="M12 16.5V4.5"/><path d="M8 8.5l4-4 4 4"/><path d="M4 16v2.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V16"/>',
  download: '<path d="M12 4.5v12"/><path d="M8 12.5l4 4 4-4"/><path d="M4 17v1.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V17"/>',
  play: '<path d="M8 5.5l11 6.5-11 6.5z"/>',
  pause: '<path d="M9.5 5.5v13M14.5 5.5v13"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
  'zoom-in': '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.3-4.3M11 8.6v4.8M8.6 11h4.8"/>',
  'zoom-out': '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.3-4.3M8.6 11h4.8"/>',
  locate: '<circle cx="12" cy="12" r="7"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/><circle cx="12" cy="12" r="1.4"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.66 0l3-3A4 4 0 0 0 13 5.34l-1.4 1.4"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3A4 4 0 0 0 11 18.66l1.4-1.4"/>',
  'link-off': '<path d="M10 14a4 4 0 0 0 5.66 0l1.1-1.1"/><path d="M13 5.34l1.4-1.4M4.5 4.5l15 15"/>',

  // ---- 内容类型 ----
  document: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
  translate: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.4 2.5 3.6 5.4 3.6 8.5s-1.2 6-3.6 8.5c-2.4-2.5-3.6-5.4-3.6-8.5S9.6 6 12 3.5z"/>',
  formula: '<path d="M17 5H7l6 7-6 7h10"/>',
  image: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.4"/><path d="M21 16.5 16 11l-6 6.5-2.5-2.5L3 19.5"/>',
  table: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M9.5 10v9M15.5 10v9"/>',
  code: '<path d="M9 8l-4 4 4 4M15 8l4 4-4 4"/>',
  quote: '<path d="M9 6H5.5A1.5 1.5 0 0 0 4 7.5V11h5V7.5"/><path d="M20 6h-3.5A1.5 1.5 0 0 0 15 7.5V11h5V7.5"/><path d="M4 11v3.5C4 17 5.6 18.6 8 18.6M15 11v3.5c0 2.5 1.6 4.1 4 4.1"/>',
  sparkle: '<path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9z"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/>',

  // ---- 状态 ----
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8h.01"/>',
  warning: '<path d="M12 4.2 21 19.5H3z"/><path d="M12 9.8v4M12 16.6h.01"/>',
  error: '<circle cx="12" cy="12" r="8.5"/><path d="M9.2 9.2l5.6 5.6M14.8 9.2l-5.6 5.6"/>',
  check_circle: '<circle cx="12" cy="12" r="8.5"/><path d="M8.2 12.3l2.6 2.6 5-5.2"/>',
  server: '<rect x="3" y="4.5" width="18" height="6" rx="2"/><rect x="3" y="13.5" width="18" height="6" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  bolt: '<path d="M13.5 3 5.5 13.5H11L10.5 21l8-10.5H13z"/>',
};

const markup = computed(() => ICONS[props.name] ?? ICONS.info);
</script>

<template>
  <svg
    class="icon"
    :width="props.size"
    :height="props.size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="props.weight"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
    v-html="markup"
  />
</template>
