<script lang="ts">
/** 浮层只需要这几个字段；调用方传更宽的行类型进来也可以（结构类型）。
 *  写在普通 script 块里是因为泛型约束必须落在模块作用域，script setup 里的声明在 setup 函数内。 */
export interface SwitcherRow {
  id: string;
  name?: string | null;
  model_name?: string | null;
  provider?: string | null;
  is_default?: boolean;
}
</script>

<script setup lang="ts" generic="T extends SwitcherRow">
/**
 * ModelSwitcher —— 任务分类卡上的「切换模型」浮层（受控：open + @close）。
 *
 * 为什么 Teleport + fixed：卡片处在设置页的滚动容器里，就地 absolute 会被
 * overflow 裁掉（看得见点不到）。坐标按触发元素实测，下方放不下就向上翻，
 * 四边都夹在视口内 —— 与 Menu.vue 同一套做法，翻页/滚动时重新定位。
 *
 * 行内「⋯」复用 Menu.vue（它自己也 Teleport 到 body），五个动作只做**转发**：
 * 接口调用全留在调用方，组件本身不认识任何后端语义。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import Menu from '@/components/Menu.vue';
import { vendorColorIcon } from '@/assets/model-icons';

const props = withDefaults(defineProps<{
  open: boolean;
  /** 触发元素：定位基准，关闭时焦点也还给它。 */
  anchor?: HTMLElement | null;
  title?: string;
  rows: T[];
  currentId?: string;
  loading?: boolean;
  /** 正在测试 / 删除 / 改名的行：禁用该行的选中与「⋯」里的写操作。 */
  busyIds?: string[];
  emptyText?: string;
}>(), {
  anchor: null,
  title: '选择模型',
  currentId: '',
  loading: false,
  busyIds: () => [],
  emptyText: '这一类还没有模型',
});

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'select', row: T): void;
  (e: 'action', payload: { key: string; row: T }): void;
  (e: 'add'): void;
}>();

defineSlots<{ meta?: (props: { model: T }) => unknown }>();

const panel = ref<HTMLElement | null>(null);
const listEl = ref<HTMLElement | null>(null);
const searchEl = ref<HTMLInputElement | null>(null);
const addEl = ref<HTMLButtonElement | null>(null);
const query = ref('');
/** 浮层坐标（fixed，相对视口）。 */
const pos = ref({ top: 0, left: 0 });

const EDGE = 8;
const GAP = 6;

/** 条目少的时候不摆搜索框：三条模型还要先搜一遍反而挡路。 */
const searchable = computed(() => props.rows.length > 6);

const filtered = computed<T[]>(() => {
  const kw = query.value.trim().toLowerCase();
  if (!kw) return props.rows;
  return props.rows.filter((r) => `${r.name || ''} ${r.model_name || ''} ${r.id}`.toLowerCase().includes(kw));
});

function ico(row: T): string { return vendorColorIcon(row.provider); }
function busy(row: T): boolean { return props.busyIds.includes(row.id); }

function place(): void {
  const a = props.anchor;
  if (!a) return;
  const r = a.getBoundingClientRect();
  const el = panel.value;
  const h = el?.offsetHeight ?? 0;
  const w = el?.offsetWidth ?? Math.min(360, window.innerWidth - 24);
  const below = window.innerHeight - r.bottom - EDGE;
  const above = r.top - EDGE;
  const up = below < h + GAP && above > below;
  const top = up ? r.top - GAP - h : r.bottom + GAP;
  pos.value = {
    top: Math.max(EDGE, Math.min(top, window.innerHeight - h - EDGE)),
    left: Math.max(EDGE, Math.min(r.left, window.innerWidth - w - EDGE)),
  };
}

function close(refocus = false): void {
  emit('close');
  if (refocus) props.anchor?.focus();
}

function add(): void { emit('add'); }

function pick(row: T): void {
  if (busy(row)) return;
  emit('select', row);
}

function menuItems(row: T) {
  return [
    { key: 'test', label: '测试连通性', icon: 'bolt', disabled: busy(row) },
    { key: 'default', label: '设为默认', icon: 'check_circle', disabled: busy(row) || !!row.is_default },
    { key: 'edit', label: '编辑配置', icon: 'edit' },
    { key: 'rename', label: '重命名', icon: 'tag' },
    { key: 'delete', label: '删除', icon: 'trash', danger: true, disabled: busy(row) },
  ];
}

/** 行内「⋯」的浮层同样 Teleport 到 body：它开着时按键与点按都先归它，
 *  否则 Esc 会一层关两层、点菜单项还会被当成「点了外部」。 */
function rowMenuOpen(): boolean { return !!document.querySelector('.menu-panel'); }

function onRowKey(ev: KeyboardEvent, row: T): void {
  if (ev.target !== ev.currentTarget) return;
  ev.preventDefault();
  pick(row);
}

/** 浮层内可 Tab 到的元素：搜索框 → 各行 → 行内「⋯」 → 底部「+ 添加模型」。
 *  取 DOM 顺序即可（行与它的「⋯」本来就是一前一后），Tab 循环到头绕回第一个。 */
function focusables(): HTMLElement[] {
  const root = panel.value;
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>(
    'input, button:not(:disabled), [tabindex]:not([tabindex="-1"])',
  ));
}

function onKeydown(ev: KeyboardEvent): void {
  if (rowMenuOpen()) return;
  if (ev.key === 'Escape') { ev.preventDefault(); close(true); return; }
  /* Tab 不关浮层：行内「⋯」的四个动作要能纯键盘走到。
     一律 preventDefault 后自己挪焦点，所以焦点不会漏到页面其它元素上。 */
  if (ev.key === 'Tab') {
    const nodes = focusables();
    if (!nodes.length) return;
    ev.preventDefault();
    const cur = nodes.indexOf(document.activeElement as HTMLElement);
    const step = ev.shiftKey ? -1 : 1;
    const next = cur < 0
      ? (ev.shiftKey ? nodes.length - 1 : 0)
      : (cur + step + nodes.length) % nodes.length;
    nodes[next]?.focus();
    nodes[next]?.scrollIntoView({ block: 'nearest' });
    return;
  }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(ev.key)) return;
  ev.preventDefault();
  const nodes = Array.from(listEl.value?.querySelectorAll<HTMLElement>('.ms-row') ?? []);
  if (!nodes.length) return;
  const cur = nodes.findIndex((n) => n === document.activeElement);
  let next = 0;
  if (ev.key === 'ArrowDown') next = cur < 0 ? 0 : (cur + 1) % nodes.length;
  else if (ev.key === 'ArrowUp') next = cur <= 0 ? nodes.length - 1 : cur - 1;
  else if (ev.key === 'End') next = nodes.length - 1;
  nodes[next]?.focus();
  nodes[next]?.scrollIntoView({ block: 'nearest' });
}

function pickFirst(): void {
  const first = filtered.value[0];
  if (first) pick(first);
}

function onDocPointer(ev: PointerEvent): void {
  if (!props.open) return;
  const t = ev.target as HTMLElement | null;
  if (!t?.closest) return;
  if (panel.value?.contains(t)) return;
  if (t.closest('.menu-panel')) return;
  if (props.anchor?.contains(t)) return;      // 触发元素自己负责开合
  close(false);
}

function onReposition(): void {
  if (props.open) place();
}

/** 「⋯」的菜单也 Teleport 到 body，按 Tab 时 Menu 会自己收起并不拦默认行为 ——
 *  浏览器的焦点落点可能在浮层外。这里把它拉回这一行的「⋯」上（下一个 Tab 接着往下走）。 */
function onDocKeydown(ev: KeyboardEvent): void {
  if (!props.open || ev.key !== 'Tab') return;
  const t = ev.target as HTMLElement | null;
  if (!t?.closest) return;
  if (panel.value?.contains(t)) return;        // 浮层内部由面板自己的 Tab 循环处理
  if (!t.closest('.menu-panel')) return;
  ev.preventDefault();
  const trig = document.querySelector<HTMLElement>('.ms .menu-trigger.is-open');
  if (trig) { trig.focus(); return; }
  focusables()[0]?.focus();
}

/* 锚点一起盯着：已经开着的时候点另一张卡，open 一直是 true，
   只盯 open 就不会重算位置，浮层会赖在上一张卡旁边。 */
watch([() => props.open, () => props.anchor], async ([v]) => {
  if (!v) return;
  query.value = '';
  await nextTick();
  place();
  await nextTick();
  place();
  if (searchable.value) { searchEl.value?.focus(); return; }
  const rows = Array.from(listEl.value?.querySelectorAll<HTMLElement>('.ms-row') ?? []);
  const target = rows.find((n) => n.dataset.switchModel === props.currentId) ?? rows[0];
  if (target) {
    target.focus();
    target.scrollIntoView({ block: 'nearest' });
  } else {
    addEl.value?.focus();
  }
});

onMounted(() => {
  document.addEventListener('pointerdown', onDocPointer, true);
  document.addEventListener('keydown', onDocKeydown);
  window.addEventListener('scroll', onReposition, true);
  window.addEventListener('resize', onReposition);
});
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocPointer, true);
  document.removeEventListener('keydown', onDocKeydown);
  window.removeEventListener('scroll', onReposition, true);
  window.removeEventListener('resize', onReposition);
});
</script>

<template>
  <Teleport to="body">
    <Transition name="ms">
      <div
        v-if="open" ref="panel" class="ms" role="dialog" :aria-label="title"
        :style="{ top: pos.top + 'px', left: pos.left + 'px' }"
        data-model-switcher @keydown="onKeydown"
      >
        <div class="ms-head" v-if="searchable">
          <AppIcon name="search" :size="13" class="ms-search-ico" />
          <input
            ref="searchEl" v-model="query" class="ms-search" type="text" spellcheck="false"
            placeholder="搜索模型名或 Model ID" @keydown.enter.prevent="pickFirst"
          >
        </div>

        <div class="ms-skel" v-if="loading && !rows.length">
          <span class="sk sk-line" /><span class="sk sk-line" /><span class="sk sk-line" />
        </div>

        <div class="ms-list" ref="listEl" role="listbox" :aria-label="title" v-else-if="filtered.length">
          <div
            v-for="row in filtered" :key="row.id"
            class="ms-row" :class="{ current: row.id === currentId, busy: busy(row) }"
            role="option" :aria-selected="row.id === currentId" tabindex="0"
            :data-switch-model="row.id"
            @click="pick(row)" @keydown="onRowKey($event, row)"
          >
            <span class="ms-flag"><AppIcon v-if="row.id === currentId" name="check" :size="13" /></span>
            <img v-if="ico(row)" class="ms-ico" :src="ico(row)" alt="">
            <span v-else class="ms-ico fallback"><AppIcon name="model" :size="11" /></span>
            <span class="ms-text">
              <span class="ms-name truncate" :title="row.name || row.id">{{ row.name || '（未命名）' }}</span>
              <span class="ms-id mono truncate" :title="row.model_name || ''">{{ row.model_name || '—' }}</span>
            </span>
            <span class="ms-meta" v-if="$slots.meta"><slot name="meta" :model="row" /></span>
            <!-- stop：不挡的话点「⋯」会连带把这一行当成"选中"，一开菜单就换了模型 -->
            <Menu
              class="ms-menu" :items="menuItems(row)" size="sm" @click.stop
              :label="(row.name || row.id) + ' 的更多操作'"
              @select="(key: string) => emit('action', { key, row })"
            />
          </div>
        </div>

        <div class="ms-empty" v-else>
          <span class="truncate">{{ query.trim() ? '没有匹配的模型' : emptyText }}</span>
          <button v-if="!rows.length" ref="addEl" type="button" class="btn sm" data-switch-empty-add @click="add">
            <AppIcon name="add" :size="13" />添加模型
          </button>
        </div>

        <div class="ms-foot" v-if="rows.length">
          <button ref="addEl" type="button" class="btn sm ghost" data-switch-add @click="add">
            <AppIcon name="add" :size="13" />添加模型
          </button>
          <span class="spacer" />
          <span class="ms-count">共 {{ rows.length }} 个</span>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 浮层本体：宽度与最大高度跟着视口收，绝不超出屏幕 */
.ms {
  position: fixed; z-index: 160;
  display: flex; flex-direction: column;
  width: min(360px, calc(100vw - 24px));
  max-height: min(420px, 70vh);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-flyout);
  overflow: hidden;
}

/* 搜索：只在条目多时出现，靠一条细分隔线与列表分段 */
.ms-head {
  display: flex; align-items: center; gap: 7px; flex: none;
  padding: 9px 10px 8px;
  border-bottom: 1px solid var(--divider);
}
.ms-search-ico { flex: none; color: var(--text-faint); }
.ms-search {
  flex: 1 1 auto; min-width: 0; height: var(--control-h-sm);
  padding: 0 8px; font-size: calc(11.5px * var(--ts, 1));
}

.ms-list {
  flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
  display: flex; flex-direction: column; gap: 1px;
  padding: 6px;
  /* 别写 scrollbar-width（Chromium 121+ 认了它，下面那组 ::-webkit-scrollbar 就整段失效） */
}
.ms-list::-webkit-scrollbar { width: 8px; }
.ms-list::-webkit-scrollbar-thumb { background: var(--divider); border-radius: var(--r-pill); }
.ms-list::-webkit-scrollbar-track { background: transparent; }

.ms-row {
  display: flex; align-items: center; gap: 8px; flex: none;
  min-height: 42px; padding: 5px 7px; min-width: 0;
  border-radius: var(--r-sm); cursor: pointer;
  transition: background var(--dur-fast) var(--ease);
}
.ms-row:hover { background: var(--bg-hover); }
/* 列表会裁掉外扩的阴影（浮层 overflow:hidden + 列表 overflow-y:auto），焦点环画在内侧 */
.ms-row:focus-visible { background: var(--bg-hover); outline: 2px solid var(--accent); outline-offset: -2px; }
/* 选中行：极轻底 + 左侧勾，不做整行彩色 */
.ms-row.current { background: var(--accent-soft); }
.ms-row.busy { opacity: .55; cursor: default; }
.ms-flag { flex: none; width: 14px; display: inline-flex; justify-content: center; color: var(--accent); }

.ms-ico { flex: none; object-fit: contain; width: 20px; height: 20px; border-radius: 4px; }
.ms-ico.fallback {
  display: inline-grid; place-items: center;
  background: var(--bg-sunken); color: var(--text-faint);
}

.ms-text { display: flex; flex-direction: column; gap: 1px; flex: 1 1 auto; min-width: 0; }
.ms-name { font-size: calc(13px * var(--ts, 1)); font-weight: 600; color: var(--text); min-width: 0; }
.ms-id { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); min-width: 0; }

.ms-meta { display: inline-flex; align-items: center; gap: 4px; flex: none; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }

/* 「⋯」默认藏起来，行 hover / 键盘焦点时才显形（触屏没有 hover，常显） */
.ms-menu { flex: none; opacity: 0; transition: opacity var(--dur-fast) var(--ease); }
.ms-row:hover .ms-menu, .ms-row:focus-within .ms-menu { opacity: 1; }
@media (hover: none) { .ms-menu { opacity: 1; } }

.ms-skel { display: flex; flex-direction: column; gap: 10px; padding: 14px 12px; }

.ms-empty {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 14px 12px; color: var(--text-faint);
  font-size: calc(12px * var(--ts, 1));
}
.ms-empty .truncate { min-width: 0; flex: 1 1 auto; }

.ms-foot {
  display: flex; align-items: center; gap: 8px; flex: none;
  padding: 7px 8px; border-top: 1px solid var(--divider);
}
.ms-count { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); font-variant-numeric: tabular-nums; }

/* 进出：只做淡入与 4px 位移，不做缩放/长动画 */
.ms-enter-active, .ms-leave-active { transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease); }
.ms-enter-from, .ms-leave-to { opacity: 0; transform: translateY(-4px); }
</style>
