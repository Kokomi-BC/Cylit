<script setup lang="ts">
/**
 * AppIcon —— 全站唯一的图标入口（内联 SVG，`currentColor`）。
 *
 * 图标集：**Microsoft Fluent System Icons（Fluent 2，MIT）**，24px 网格（数据见 `fluentIcons.ts`）。
 *
 * 为什么换成它（用户口径 2026-12-05「统一使用微软 fluent svg 图标」）：
 * 原先这批图标是手绘的，**线性（1.6 描边）与填充（Material 形）混用**，同一个盒子里墨迹高度
 * 从 8.8px 到 12.8px 不等 —— 一排图标并排时粗细/大小都对不齐。Fluent 整套画在同一张网格上，
 * 墨迹高度中位数 18/24、绝大多数落在 18~20，regular 与 filled 只是填法不同，因此：
 *   · 组件里只给 `size`，不需要再按图标逐个校正（旧的「墨迹高 ≈ 11.25px」换算已删）；
 *   · 名字以 `-fill` 结尾 → 取 Fluent 的 **filled** 变体（右键菜单那一排用），其余取 regular。
 *
 * 少数**品牌标记**不在 Fluent 里（如 `github`），保留手绘路径，见下面的 EXTRA。
 *
 * 用法：`<AppIcon name="search" :size="16" />` / `<AppIcon name="copy-fill" :size="16" />`
 */
import { computed } from 'vue';
import { FLUENT_ICONS } from './fluentIcons';

const props = withDefaults(defineProps<{
  name: string;
  size?: number;
  /** 兼容旧调用：Fluent 是实心路径、没有描边，这个参数已经不影响渲染（`check` 那处还在传） */
  weight?: number;
}>(), { size: 16, weight: 1.6 });

/** Fluent 不收品牌图标：这几枚沿用原来的手绘路径（已按同一个 24 网格对齐）。 */
const EXTRA: Record<string, string> = {
  github: '<path fill="currentColor" stroke="none" d="M12 2.2a9.8 9.8 0 0 0-3.1 19.1c.49.09.67-.21.67-.47v-1.7c-2.7.59-3.3-1.3-3.3-1.3-.45-1.14-1.1-1.44-1.1-1.44-.9-.62.07-.6.07-.6 1 .07 1.52 1.03 1.52 1.03.88 1.52 2.32 1.08 2.88.83.09-.64.35-1.08.63-1.33-2.16-.25-4.44-1.08-4.44-4.8 0-1.06.38-1.93 1-2.61-.1-.25-.43-1.24.1-2.58 0 0 .81-.26 2.66 1a9.2 9.2 0 0 1 4.84 0c1.85-1.26 2.66-1 2.66-1 .53 1.34.2 2.33.1 2.58.62.68 1 1.55 1 2.61 0 3.73-2.29 4.55-4.46 4.79.35.3.66.9.66 1.81v2.68c0 .26.18.57.67.47A9.8 9.8 0 0 0 12 2.2z"/>',
};

/** `-fill` 结尾 = 要 Fluent 的填充变体；没有填充版就退回 regular。 */
const markup = computed(() => {
  const filled = props.name.endsWith('-fill');
  const base = props.name.replace(/-fill$/, '');
  const extra = EXTRA[base];
  if (extra) return extra;
  const entry = FLUENT_ICONS[base] ?? FLUENT_ICONS.info;
  return (filled && entry.f) || entry.r;
});
</script>

<template>
  <svg
    class="icon"
    :width="props.size"
    :height="props.size"
    viewBox="0 0 24 24"
    fill="currentColor"
    aria-hidden="true"
    focusable="false"
    :data-icon="props.name"
    v-html="markup"
  />
</template>
