<script setup lang="ts">
/**
 * ProviderSelector —— 可搜索的提供方选择器（下拉，键盘可达，**绝不溢出视口**）。
 *
 * 为什么是「按钮 + 浮层」而不是就地铺开：新增模型是在弹窗里做的，候选有十几家，
 * 就地铺开会把弹窗顶到视口外。浮层 `Teleport` 到 body + `position: fixed`，
 * 坐标由触发按钮实测得出，宽度夹到 `100vw - 16px`，高度取上下剩余空间的较大那侧
 * —— 360px 宽也不会溢出，下方放不下就向上翻转。
 *
 * 对外契约（C 阶段的新增模型表单复用，**签名别改**）：
 *   props  { modelValue, providers, loading?, error? }
 *   emit   update:modelValue
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { vendorColorIcon } from '@/assets/model-icons';
import type { ProviderRow } from './modelTypes';

const props = withDefaults(defineProps<{
  modelValue: string;
  providers: ProviderRow[];
  loading?: boolean;
  error?: string;
}>(), { loading: false, error: '' });

const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>();

const open = ref(false);
const query = ref('');
const activeIdx = ref(0);
const triggerEl = ref<HTMLButtonElement | null>(null);
const panelEl = ref<HTMLDivElement | null>(null);
const searchEl = ref<HTMLInputElement | null>(null);
/** 浮层坐标（fixed，单位 px，全部夹在视口内） */
const box = ref({ left: 0, top: 0, width: 280, maxH: 300 });

const EDGE = 8;      // 与视口边缘的最小距离
const GAP = 6;       // 与触发按钮的间距
const MAX_H = 320;   // 浮层最大高度（再高就要内部滚动）

const selected = computed<ProviderRow | null>(
  () => props.providers.find((p) => p.id === props.modelValue) ?? null,
);

const filtered = computed<ProviderRow[]>(() => {
  const kw = query.value.trim().toLowerCase();
  if (!kw) return props.providers;
  return props.providers.filter((p) => `${p.label || ''} ${p.id || ''} ${p.note || ''}`
    .toLowerCase().includes(kw));
});

function ico(id?: string | null): string {
  return vendorColorIcon(id);
}

/** 按触发按钮的位置摆浮层：下方不够就向上翻，宽高都夹在视口内。 */
function place(): void {
  const el = triggerEl.value;
  if (!el) return;
  const r = el.getBoundingClientRect();
  const vw = Math.max(240, window.innerWidth);
  const vh = Math.max(240, window.innerHeight);
  const width = Math.min(Math.max(r.width, 240), vw - EDGE * 2);
  const left = Math.min(Math.max(EDGE, r.left), Math.max(EDGE, vw - width - EDGE));
  const below = vh - r.bottom - GAP - EDGE;
  const above = r.top - GAP - EDGE;
  const flip = below < 160 && above > below;
  const maxH = Math.max(120, Math.min(MAX_H, flip ? above : below));
  const top = flip ? Math.max(EDGE, r.top - GAP - maxH) : r.bottom + GAP;
  box.value = { left, top, width, maxH };
}

function scrollActiveIntoView(): void {
  void nextTick(() => {
    panelEl.value?.querySelector<HTMLElement>('.ps-opt.active')?.scrollIntoView({ block: 'nearest' });
  });
}

function openPanel(): void {
  if (open.value) return;
  open.value = true;
  query.value = '';
  const i = filtered.value.findIndex((p) => p.id === props.modelValue);
  activeIdx.value = i >= 0 ? i : 0;
  void nextTick(() => {
    place();
    searchEl.value?.focus();
    scrollActiveIntoView();
  });
}

function closePanel(refocus = false): void {
  if (!open.value) return;
  open.value = false;
  if (refocus) triggerEl.value?.focus();
}

function toggle(): void {
  if (open.value) closePanel(true);
  else openPanel();
}

function pick(p: ProviderRow): void {
  emit('update:modelValue', p.id);
  closePanel(true);
}

function move(delta: number): void {
  const n = filtered.value.length;
  if (!n) return;
  activeIdx.value = (activeIdx.value + delta + n) % n;
  scrollActiveIntoView();
}

/** 触发按钮上的键盘：↓ / Enter / 空格都能打开（原生 button 的 Enter、空格先被 preventDefault 吃掉）。 */
function onTriggerKey(ev: KeyboardEvent): void {
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp' || ev.key === 'Enter' || ev.key === ' ') {
    ev.preventDefault();
    openPanel();
  }
}

/** 浮层里的键盘：↑↓ / Home / End / Enter / Esc / Tab。 */
function onPanelKey(ev: KeyboardEvent): void {
  if (ev.key === 'Escape') {
    ev.preventDefault();
    ev.stopPropagation();   // 别让 Esc 顺带把外层的弹窗 / 抽屉关掉
    closePanel(true);
    return;
  }
  if (ev.key === 'ArrowDown') { ev.preventDefault(); move(1); return; }
  if (ev.key === 'ArrowUp') { ev.preventDefault(); move(-1); return; }
  if (ev.key === 'Home') { ev.preventDefault(); activeIdx.value = 0; scrollActiveIntoView(); return; }
  if (ev.key === 'End') { ev.preventDefault(); activeIdx.value = Math.max(0, filtered.value.length - 1); scrollActiveIntoView(); return; }
  if (ev.key === 'Enter') {
    ev.preventDefault();
    const row = filtered.value[activeIdx.value];
    if (row) pick(row);
    return;
  }
  if (ev.key === 'Tab') closePanel();
}

function onDocPointer(ev: MouseEvent): void {
  if (!open.value) return;
  const t = ev.target as Node | null;
  if (triggerEl.value?.contains(t) || panelEl.value?.contains(t)) return;
  closePanel();
}

function onViewportChange(): void {
  if (open.value) place();
}

function detach(): void {
  window.removeEventListener('pointerdown', onDocPointer, true);
  window.removeEventListener('resize', onViewportChange);
  window.removeEventListener('scroll', onViewportChange, true);
}

watch(open, (v) => {
  if (typeof window === 'undefined') return;
  if (v) {
    window.addEventListener('pointerdown', onDocPointer, true);
    window.addEventListener('resize', onViewportChange);
    window.addEventListener('scroll', onViewportChange, true);
  } else {
    detach();
  }
});

onBeforeUnmount(detach);
</script>

<template>
  <div class="ps-root">
    <button
      type="button" ref="triggerEl" class="ps-trigger" :class="{ open }"
      aria-haspopup="listbox" :aria-expanded="open" data-prov-trigger
      @click="toggle" @keydown="onTriggerKey"
    >
      <img v-if="ico(selected?.id || modelValue)" class="pv-ico" :src="ico(selected?.id || modelValue)" alt="">
      <span v-else class="pv-fallback"><AppIcon name="model" :size="13" /></span>
      <span class="ps-main">
        <span class="ps-name truncate">{{ selected?.label || (loading ? '正在读取提供方…' : '选择提供方') }}</span>
        <span class="ps-sub truncate">
          {{ selected ? (selected.note || selected.base_url || '') : '官方提供方会自动填好 Base URL' }}
        </span>
      </span>
      <AppIcon :name="open ? 'chevron-up' : 'chevron-down'" :size="13" class="ps-chev" />
    </button>

    <Teleport to="body">
      <div
        v-if="open" ref="panelEl" class="ps-panel"
        :style="{ left: box.left + 'px', top: box.top + 'px', width: box.width + 'px', maxHeight: box.maxH + 'px' }"
        @keydown="onPanelKey"
      >
        <label class="ps-search">
          <AppIcon name="search" :size="13" />
          <input
            ref="searchEl" v-model="query" type="search" spellcheck="false"
            placeholder="搜索提供方…" aria-label="搜索提供方"
            @input="activeIdx = 0"
          />
        </label>
        <p class="ps-err" v-if="error">{{ error }}</p>
        <div class="ps-list" role="listbox" aria-label="提供方">
          <button
            v-for="(p, i) in filtered" :key="p.id" type="button"
            class="ps-opt" :class="{ active: i === activeIdx, sel: p.id === modelValue }"
            role="option" :aria-selected="p.id === modelValue" :data-provider="p.id"
            @mouseenter="activeIdx = i" @click="pick(p)"
          >
            <img v-if="ico(p.id)" class="pv-ico" :src="ico(p.id)" alt="">
            <span v-else class="pv-fallback"><AppIcon name="model" :size="13" /></span>
            <span class="ps-opt-main">
              <span class="ps-opt-name truncate">{{ p.label || p.id }}</span>
              <span class="ps-opt-note truncate">{{ p.note || p.base_url || '' }}</span>
            </span>
            <span class="ps-kind" v-if="p.kind === 'custom'">自定义</span>
            <AppIcon v-if="p.id === modelValue" name="check" :size="13" class="ps-ok" />
          </button>
          <div class="ps-none" v-if="!filtered.length">
            {{ loading ? '正在读取提供方…'
              : (query.trim() ? '没有匹配的提供方 —— 换个词试试' : '还没有可用的提供方') }}
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.ps-root { min-width: 0; }
.ps-trigger {
  display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0;
  padding: 7px 10px; border: 1px solid var(--divider); border-radius: var(--radius-sm);
  background: var(--bg-layer); color: var(--text); font: inherit; text-align: left; cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.ps-trigger:hover { background: var(--bg-hover); }
.ps-trigger:focus-visible { outline: none; box-shadow: var(--ring); }
.ps-trigger.open { border-color: color-mix(in srgb, var(--accent) 45%, var(--divider)); }
.ps-main { display: flex; flex-direction: column; gap: 1px; flex: 1 1 auto; min-width: 0; }
.ps-name { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; }
.ps-sub { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
.ps-chev { color: var(--text-faint); flex: none; }

/* 浮层：指针自动定位（见 place()），这里只管外观与"绝不溢出" */
.ps-panel {
  position: fixed; z-index: 210;
  display: flex; flex-direction: column; gap: 6px; padding: 8px;
  box-sizing: border-box;
  background: var(--bg-elev); border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm); box-shadow: var(--shadow-2);
}
.ps-search {
  display: flex; align-items: center; gap: 7px; flex: none;
  height: 30px; padding: 0 9px; border-radius: var(--radius-sm);
  background: var(--bg-sunken); border: 1px solid var(--border); color: var(--text-faint);
}
.ps-search:focus-within { border-color: var(--accent); box-shadow: var(--ring); }
.ps-search input {
  width: 100%; min-width: 0; height: 100%; padding: 0; border: 0; border-radius: 0;
  background: transparent; box-shadow: none; color: var(--text);
  font-size: calc(12.5px * var(--ts, 1));
}
.ps-search input:hover, .ps-search input:focus { background: transparent; border: 0; box-shadow: none; }
.ps-err { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--danger); }
.ps-list { flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
.ps-opt {
  display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0;
  padding: 7px 8px; border: 0; border-radius: var(--radius-sm);
  background: transparent; color: var(--text); font: inherit; text-align: left; cursor: pointer;
}
.ps-opt.active { background: var(--bg-hover); }
.ps-opt.sel { color: var(--accent-text); }
.ps-opt:focus-visible { outline: none; box-shadow: var(--ring); }
.ps-opt-main { display: flex; flex-direction: column; gap: 1px; flex: 1 1 auto; min-width: 0; }
.ps-opt-name { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; }
.ps-opt-note { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
.ps-kind {
  flex: none; padding: 0 5px; height: 16px; display: inline-flex; align-items: center;
  border-radius: 999px; background: var(--bg-sunken); color: var(--text-faint);
  font-size: calc(10px * var(--ts, 1)); font-weight: 600;
}
.ps-ok { flex: none; color: var(--accent); }
.ps-none { padding: 12px 8px; color: var(--text-faint); font-size: calc(12px * var(--ts, 1)); line-height: 1.5; }

/* 厂商彩色图标：统一视觉框、保持比例；缺图标时用占位方块（不渲染 src=""） */
.pv-ico { flex: none; width: 16px; height: 16px; object-fit: contain; border-radius: 3px; }
.pv-fallback {
  flex: none; display: inline-grid; place-items: center;
  width: 16px; height: 16px; border-radius: 3px;
  background: var(--bg-sunken); color: var(--text-faint);
}
</style>
