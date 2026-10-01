<script setup lang="ts">
/** 论文卡「壳」：封面 + 状态徽章 + 进度 + 标题/元信息 + hover 操作（移植自参照项目 retain-pdf 的 BookCard）。
 *
 * 纯展示：**不决定有哪些操作、也不发请求** —— 按钮语义与菜单项由父级注入（一句话的 label + 事件）。
 * 两种形态共用一套 DOM：`grid`（封面卡，默认）与 `list`（一行一条，缩略图 + 标题 + 元信息）。
 *
 * 封面用 `/api/v1/papers/{id}/pages/1`（后端按需渲染并缓存首页图）；读不到就退回占位块，
 * 不留一个破图图标（扫描件没解析、PDF 损坏都会走到这里）。
 */

import { computed, ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import Menu from '@/components/Menu.vue';

interface MenuItem {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
}

const props = withDefaults(defineProps<{
  title: string;
  coverUrl: string;
  /** 状态徽章（`paperStatus()` 那一份：label + cls）。 */
  badge: { label: string; cls: string };
  /** 一行元信息（页数 · 会议 · 年份 · 读于…） */
  meta: string;
  /** 翻译进度：null = 不显示进度条 */
  progress: { done: number; total: number } | null;
  /** 进度条下的收尾文案（已完成为一行灰字；空串 = 不显示） */
  footNote: string;
  /** 有没有正在跑的翻译任务（显示处理中遮罩 + 暂停/继续语义） */
  running: boolean;
  /** 主操作按钮文案（阅读） */
  readLabel?: string;
  /** 翻译类按钮：文案 + 是否出现（已译完的论文不给这个一级按钮） */
  actionLabel: string;
  showAction: boolean;
  busy: boolean;
  menu: MenuItem[];
  variant?: 'grid' | 'list';
  batch?: boolean;
  selected?: boolean;
}>(), {
  readLabel: '阅读',
  variant: 'grid',
  batch: false,
  selected: false,
});

const emit = defineEmits<{
  /** 卡片本体：打开详情（批量模式下=勾选，不发这个事件）。 */
  (e: 'open'): void;
  /** 阅读按钮：直接进阅读器（与"打开详情"是两个意图，不能合并）。 */
  (e: 'read'): void;
  (e: 'action'): void;
  (e: 'pick', key: string): void;
  (e: 'toggle'): void;
}>();

const coverFailed = ref(false);

const pct = computed(() => {
  const p = props.progress;
  if (!p || !p.total) return 0;
  return Math.max(0, Math.min(100, Math.round((p.done / p.total) * 100)));
});

/** 徽章图标：与状态语义一一对应，不按文案猜。 */
const badgeIcon = computed(() => {
  switch (props.badge.cls) {
    case 'ok': return 'check_circle';
    case 'warn': return 'pause';
    case 'danger': return 'warning';
    case 'accent': return 'bolt';
    default: return 'document';
  }
});

/** 卡片本体：批量模式下是"勾选"，否则是"打开详情"。 */
function onCard() {
  if (props.batch) { emit('toggle'); return; }
  emit('open');
}
</script>

<template>
  <article
    class="paper-card"
    :class="[variant, { batch, selected }]"
    role="button"
    tabindex="0"
    :aria-label="batch ? `选择：${title}` : `打开详情：${title}`"
    :aria-pressed="batch ? selected : undefined"
    @click="onCard"
    @keydown.enter.prevent="onCard"
    @keydown.space.prevent="onCard"
  >
    <div class="pc-cover">
      <img
        v-if="coverUrl && !coverFailed"
        :src="coverUrl"
        :alt="`${title} 首页`"
        loading="lazy"
        decoding="async"
        @error="coverFailed = true"
      />
      <div v-else class="pc-cover-empty" aria-hidden="true">
        <AppIcon name="document" :size="30" />
        <span>PDF</span>
      </div>

      <!-- 状态徽章：常驻右上角（封面上唯一"常亮"的标记） -->
      <span class="pc-badge" :class="badge.cls">
        <AppIcon :name="badgeIcon" :size="11" />{{ badge.label }}
      </span>

      <!-- 正在跑任务：压一层薄遮罩 + 转圈，比只改徽章文案更容易一眼看到 -->
      <div v-if="running" class="pc-processing" aria-hidden="true">
        <span class="pc-disc">
          <svg class="spinner" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /></svg>
        </span>
      </div>

      <div v-if="progress" class="pc-track" aria-hidden="true">
        <i :style="{ width: pct + '%' }" />
      </div>

      <!-- hover / 键盘聚焦时浮出的操作：阅读（主）+ 翻译类（次） -->
      <div v-if="variant === 'grid'" class="pc-hover">
        <button type="button" class="pc-act primary" :title="readLabel" @click.stop="emit('read')">
          <AppIcon name="document" :size="16" />
        </button>
        <button
          v-if="showAction"
          type="button"
          class="pc-act"
          :disabled="busy"
          :title="actionLabel"
          @click.stop="emit('action')"
        >
          <AppIcon name="translate" :size="15" />
        </button>
      </div>

      <!-- 批量勾选：只在批量模式出现（点卡片任意处都能勾） -->
      <span v-if="batch" class="pc-check" :class="{ on: selected }" aria-hidden="true">
        <AppIcon v-if="selected" name="check" :size="12" />
      </span>
    </div>

    <div class="pc-text">
      <div class="pc-name-row">
        <h3 class="pc-name" :title="title">{{ title }}</h3>
        <!-- 列表形态把状态挪到标题这一行（缩略图只有 48px，徽章挂在图上会盖住标题） -->
        <span v-if="variant === 'list'" class="pc-badge is-inline" :class="badge.cls">
          <AppIcon :name="badgeIcon" :size="11" />{{ badge.label }}
        </span>
        <!-- 菜单按钮只在指针悬停 / 键盘聚焦时显形，平时让标题占满（与 hover 操作同一套"按需出现"） -->
        <span class="pc-menu" @click.stop>
          <Menu :items="menu" :label="`更多操作：${title}`" @select="emit('pick', $event)" />
        </span>
      </div>
      <p class="pc-meta" :title="meta">{{ meta }}</p>
      <p v-if="footNote" class="pc-foot">
        <AppIcon v-if="progress === null && !running" name="check_circle" :size="12" />{{ footNote }}
      </p>
    </div>

    <div v-if="variant === 'list'" class="pc-inline-actions" @click.stop>
      <button type="button" class="btn sm" :title="readLabel" @click="emit('read')">
        <AppIcon name="document" :size="14" />{{ readLabel }}
      </button>
      <button
        v-if="showAction"
        type="button"
        class="btn sm"
        :disabled="busy"
        @click="emit('action')"
      >{{ actionLabel }}</button>
    </div>
  </article>
</template>

<style scoped>
.paper-card { display: block; text-align: left; cursor: pointer; }

/* ---------------- 封面 ---------------- */
.pc-cover {
  position: relative; width: 100%; aspect-ratio: 3 / 4; overflow: hidden;
  border-radius: 16px;
  background: color-mix(in srgb, var(--text) 6%, transparent);
  box-shadow: var(--shadow-1);
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.paper-card:hover .pc-cover { transform: translateY(-2px); box-shadow: var(--shadow-2); }
.paper-card:focus-visible { outline: none; }
.paper-card:focus-visible .pc-cover { box-shadow: var(--ring), var(--shadow-2); }
.paper-card.selected .pc-cover { box-shadow: 0 0 0 2px var(--accent), var(--shadow-1); }
.paper-card:active .pc-cover { transform: scale(.995); }

/* 首页整页渲染：**contain**（看得到整页），别裁掉页眉页脚 */
.pc-cover img {
  display: block; width: 100%; height: 100%;
  object-fit: contain; object-position: top center;
  background: var(--bg-elev);
}
.pc-cover-empty {
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px;
  width: 100%; height: 100%;
  background: linear-gradient(to bottom right,
    color-mix(in srgb, var(--text) 6%, transparent),
    color-mix(in srgb, var(--text) 2%, transparent));
  color: var(--text-faint);
}
.pc-cover-empty span { font-size: 10px; letter-spacing: .08em; }

/* ---------------- 徽章 / 进度 / 处理中 ---------------- */
.pc-badge {
  position: absolute; right: 8px; top: 8px; z-index: 2;
  display: inline-flex; align-items: center; gap: 4px;
  height: 20px; padding: 0 8px; border-radius: 999px;
  font-size: 10px; font-weight: 600; line-height: 1; white-space: nowrap;
  /* 徽章压在论文首页图上：用极强玻璃（20px 模糊）把封面那点字迹糊开，自身读数才清楚。
     不画描边、不留内阴影（深色下都会成发光边），层次靠渐变本体 + 柔光投影。 */
  --glass-top: 86%;
  --glass-bottom: 76%;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-top), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-bottom), transparent));
  color: var(--text-dim);
  border: 0;
  box-shadow: 0 1px 3px rgba(0, 0, 0, .12);
  backdrop-filter: blur(20px) saturate(2.2);
  -webkit-backdrop-filter: blur(20px) saturate(2.2);
}
/* 深色皮肤下：徽章加实一档（`:global()` 写整条选择器，见 ReaderView 的注释） */
:global(html.theme-dark .pc-badge) {
  --glass-top: 94%;
  --glass-bottom: 88%;
}
@media (prefers-color-scheme: dark) {
  :global(html[data-theme="auto"] .pc-badge),
  :global(html:not([data-theme]) .pc-badge) {
    --glass-top: 94%;
    --glass-bottom: 88%;
  }
}
/* 语义色只用于状态：完成走实心强调（图1 里「已翻译」就是这一枚），
   进行中走浅底（遮罩上的转圈已经在说"在跑"），失败走红。 */
.pc-badge.ok { background: var(--accent); color: var(--text-on-accent); border-color: transparent; }
.pc-badge.accent { background: var(--accent-soft); color: var(--accent-text); border-color: transparent; }
.pc-badge.warn { background: var(--warn-soft); color: var(--warn); border-color: transparent; }
.pc-badge.danger { background: var(--danger-soft); color: var(--danger); border-color: transparent; }
/* 列表形态：同一个徽章改成"正文里的一枚"，不再绝对定位在缩略图上 */
.pc-badge.is-inline { position: static; right: auto; top: auto; height: 18px; padding: 0 7px; flex: none; }

.pc-processing {
  position: absolute; inset: 0; z-index: 1; display: flex; align-items: center; justify-content: center;
  background: color-mix(in srgb, var(--bg) 34%, transparent);
  pointer-events: none;
}
.pc-disc {
  display: inline-flex; align-items: center; justify-content: center;
  width: 44px; height: 44px; border-radius: 999px;
  background: color-mix(in srgb, var(--bg-elev) 90%, transparent);
  box-shadow: var(--shadow-2);
}

.pc-track {
  position: absolute; left: 0; right: 0; bottom: 0; z-index: 2; height: 4px;
  background: color-mix(in srgb, var(--text) 14%, transparent);
}
.pc-track > i { display: block; height: 100%; background: var(--accent); transition: width var(--dur-slow) var(--ease); }

/* ---------------- hover 操作 ---------------- */
.pc-hover {
  position: absolute; inset: 0; z-index: 3;
  display: flex; align-items: center; justify-content: center; gap: 8px;
  background: color-mix(in srgb, var(--text) 32%, transparent);
  opacity: 0; pointer-events: none;
  transition: opacity var(--dur) var(--ease);
}
.paper-card:hover .pc-hover,
.paper-card:focus-within .pc-hover { opacity: 1; }
.paper-card.batch .pc-hover { display: none; }
.pc-act {
  pointer-events: auto; cursor: pointer;
  width: 40px; height: 40px; border-radius: 999px;
  display: inline-flex; align-items: center; justify-content: center;
  background: color-mix(in srgb, var(--bg-elev) 95%, transparent); color: var(--text);
  box-shadow: var(--shadow-2);
  transition: transform var(--dur-fast) var(--ease), background var(--dur) var(--ease), opacity var(--dur) var(--ease);
}
.pc-act:hover:not(:disabled) { background: var(--bg-elev); }
.pc-act:active:not(:disabled) { transform: scale(.92); }
.pc-act:disabled { opacity: .5; cursor: default; }
.pc-act.primary { background: var(--accent); color: var(--text-on-accent); }
.pc-act.primary:hover { background: var(--accent-hover); }

/* 批量勾选圈：底色要比封面深（封面本身是白纸，浅色圈会"看不见"） */
.pc-check {
  position: absolute; left: 8px; top: 8px; z-index: 4;
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 999px;
  border: 1.5px solid color-mix(in srgb, var(--bg-elev) 92%, transparent);
  background: color-mix(in srgb, var(--text) 24%, transparent);
  box-shadow: 0 1px 4px rgba(0, 0, 0, .25);
  color: transparent;
}
.pc-check.on {
  background: var(--accent);
  border-color: color-mix(in srgb, var(--bg-elev) 85%, transparent);
  color: var(--text-on-accent);
}

/* ---------------- 文字块 ---------------- */
.pc-text { display: grid; gap: 2px; margin-top: 8px; min-width: 0; }
.pc-name-row { display: flex; align-items: flex-start; gap: 2px; min-width: 0; }
.pc-name {
  flex: 1; min-width: 0; margin: 0;
  font-size: 12.5px; font-weight: 600; line-height: 1.4; color: var(--text);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  transition: color var(--dur) var(--ease);
}
.paper-card:hover .pc-name { color: var(--accent-text); }
.pc-meta {
  margin: 0; font-size: 10.5px; color: var(--text-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pc-foot {
  display: flex; align-items: center; gap: 4px; margin: 2px 0 0;
  font-size: 10.5px; color: var(--text-dim);
}
/* 菜单按钮：平时隐形但**保留占位**（不占位会让标题在悬停时跳一下） */
.pc-menu { flex: none; opacity: 0; transition: opacity var(--dur) var(--ease); }
.paper-card:hover .pc-menu,
.paper-card:focus-within .pc-menu { opacity: 1; }

/* ---------------- 列表形态 ---------------- */
.paper-card.list {
  display: flex; align-items: flex-start; gap: 14px;
  padding: 12px; border-radius: var(--r-sm);
  transition: background var(--dur) var(--ease);
}
.paper-card.list:hover { background: var(--bg-hover); }
.paper-card.list:hover .pc-cover { transform: none; box-shadow: var(--shadow-1); }
.paper-card.list .pc-cover {
  flex: none; width: 48px; aspect-ratio: 3 / 4; border-radius: 10px;
  box-shadow: var(--shadow-1);
}
.paper-card.list .pc-cover > .pc-badge { display: none; }
.paper-card.list .pc-text { flex: 1; margin-top: 0; }
/* 列表里标题**按内容收缩**：徽章要紧跟在标题后面，被 `flex: 1` 推到最右边会像另一个按钮 */
.paper-card.list .pc-name { flex: 0 1 auto; font-size: 14px; }
.paper-card.list .pc-meta { font-size: 11px; }
.pc-inline-actions { display: flex; align-items: center; gap: 6px; flex: none; align-self: center; }

/* 窄屏：hover 操作在触屏上永远不会出现，改成常显（否则手机上进不了阅读器） */
@media (hover: none) {
  .paper-card:not(.batch) .pc-hover { opacity: 1; background: color-mix(in srgb, var(--text) 22%, transparent); }
  .pc-menu { opacity: 1; }
}
</style>
