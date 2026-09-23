<script setup lang="ts">
/**
 * Menu —— 行内「更多操作」溢出菜单（圆角 pill 触发 + 圆角浮层）。
 *
 * 目的：把卡片/表格行里的次要操作（重译、重建索引、编辑、删除…）收进一层，
 * 让每行只留 **1 个主操作 + 1 个次操作**，避免「一排 8 个同权重按钮」的噪声。
 *
 * 行为：
 * - 点击触发 / `Esc` / 点击外部 → 关闭；打开后焦点落在第一项；
 * - `↑ ↓ Home End` 在菜单项之间移动，`Tab` 关闭并交还焦点（符合菜单角色预期）；
 * - 浮层**传送（Teleport）到 body** 并采用 fixed 定位：阅读器、表格这些带 `overflow: hidden`
 *   的容器不会把浮层裁掉（被裁掉时是看得见但点不到）；
 * - 下方空间不够时自动向上翻转，并始终限制在视口内。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';

// 注意：`<script setup>` 里不能写 ES 模块导出，所以本类型不对外 export；
// 调用方用同构的字面量类型即可（结构类型，天然兼容）。
interface MenuItem {
  key: string;
  label: string;
  icon?: string;
  /** 危险操作（删除等）：红色文案，置于末尾并用分隔线与普通项隔开。 */
  danger?: boolean;
  disabled?: boolean;
}

const props = withDefaults(defineProps<{
  items: MenuItem[];
  /** 触发按钮的无障碍名称（也是 tooltip）。 */
  label?: string;
  /** 浮层对齐方向。 */
  align?: 'start' | 'end';
  /** 优先向上弹出（表格末行）；上方空间不够时会自动改回向下。 */
  up?: boolean;
  /** 触发按钮尺寸：跟随所在工具条的控件高度（sm = 26px，md = 32px）。 */
  size?: 'sm' | 'md';
}>(), { label: '更多操作', align: 'end', up: false, size: 'sm' });

const emit = defineEmits<{ (e: 'select', key: string): void }>();

const open = ref(false);
const root = ref<HTMLElement | null>(null);
const panel = ref<HTMLElement | null>(null);
const trigger = ref<HTMLElement | null>(null);
/** 浮层坐标（fixed，相对视口）。 */
const pos = ref({ top: 0, left: 0 });

const GAP = 6;
const EDGE = 8;

/** 按触发按钮的位置摆浮层：优先请求的方向放不下就翻转，并夹到视口内。 */
async function place() {
  const btn = trigger.value;
  if (!btn) return;
  await nextTick();
  const r = btn.getBoundingClientRect();
  const el = panel.value;
  const panelH = el?.offsetHeight ?? 0;
  const panelW = el?.offsetWidth ?? 194;
  const below = window.innerHeight - r.bottom - EDGE;
  const above = r.top - EDGE;
  let up = props.up;
  if (up && above < panelH + GAP) up = false;
  if (!up && below < panelH + GAP && above > below) up = true;
  const top = up ? r.top - GAP - panelH : r.bottom + GAP;
  const rawLeft = props.align === 'end' ? r.right - panelW : r.left;
  pos.value = {
    top: Math.max(EDGE, Math.min(top, window.innerHeight - panelH - EDGE)),
    left: Math.max(EDGE, Math.min(rawLeft, window.innerWidth - panelW - EDGE)),
  };
}

/** 普通项在前、危险项在后，危险项前插入分隔线。 */
const groups = computed(() => {
  const normal = props.items.filter((i) => !i.danger);
  const danger = props.items.filter((i) => i.danger);
  return [normal, danger].filter((g) => g.length) as MenuItem[][];
});

function toggle() {
  open.value = !open.value;
  if (open.value) void place();
}

function close(refocus = false) {
  if (!open.value) return;
  open.value = false;
  if (refocus) trigger.value?.focus();
}

function pick(item: MenuItem) {
  if (item.disabled) return;
  close(true);
  emit('select', item.key);
}

function onDocPointer(ev: PointerEvent) {
  if (!open.value) return;
  const t = ev.target as Node;
  if (root.value?.contains(t) || panel.value?.contains(t)) return;
  close();
}

/** 滚动 / 改窗口尺寸时重新定位（跟随触发按钮，不随页面飘走）。 */
function onReposition() {
  if (open.value) void place();
}

function onKeydown(ev: KeyboardEvent) {
  if (!open.value) return;
  if (ev.key === 'Escape') { ev.preventDefault(); close(true); return; }
  if (ev.key === 'Tab') { close(); return; }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(ev.key)) return;
  ev.preventDefault();
  const nodes = Array.from(panel.value?.querySelectorAll<HTMLElement>('.menu-item:not(:disabled)') ?? []);
  if (!nodes.length) return;
  const current = nodes.findIndex((n) => n === document.activeElement);
  let next = 0;
  if (ev.key === 'ArrowDown') next = current < 0 ? 0 : (current + 1) % nodes.length;
  else if (ev.key === 'ArrowUp') next = current <= 0 ? nodes.length - 1 : current - 1;
  else if (ev.key === 'End') next = nodes.length - 1;
  nodes[next]?.focus();
}

watch(open, async (v) => {
  if (!v) return;
  await nextTick();
  void place();
  await nextTick();
  panel.value?.querySelector<HTMLElement>('.menu-item:not(:disabled)')?.focus();
});

onMounted(() => {
  document.addEventListener('pointerdown', onDocPointer, true);
  document.addEventListener('keydown', onKeydown);
  window.addEventListener('scroll', onReposition, true);
  window.addEventListener('resize', onReposition);
});
onUnmounted(() => {
  document.removeEventListener('pointerdown', onDocPointer, true);
  document.removeEventListener('keydown', onKeydown);
  window.removeEventListener('scroll', onReposition, true);
  window.removeEventListener('resize', onReposition);
});
</script>

<template>
  <span ref="root" class="menu-root">
    <button
      ref="trigger"
      type="button"
      class="btn icon menu-trigger"
      :class="[size === 'sm' ? 'sm' : '', { 'is-open': open }]"
      :title="label"
      :aria-label="label"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="toggle"
    >
      <AppIcon name="more" :size="14" />
    </button>
    <Teleport to="body">
      <Transition name="menu">
        <div
          v-if="open"
          ref="panel"
          class="menu-panel"
          :style="{ top: pos.top + 'px', left: pos.left + 'px' }"
          role="menu"
          :aria-label="label"
        >
          <template v-for="(group, gi) in groups" :key="gi">
            <div v-if="gi > 0" class="menu-sep" role="separator" />
            <button
              v-for="item in group"
              :key="item.key"
              type="button"
              class="menu-item"
              :class="{ danger: item.danger }"
              role="menuitem"
              :disabled="item.disabled"
              @click="pick(item)"
            >
              <AppIcon v-if="item.icon" :name="item.icon" :size="14" />
              <span class="menu-item-label">{{ item.label }}</span>
            </button>
          </template>
        </div>
      </Transition>
    </Teleport>
  </span>
</template>

<style scoped>
.menu-root { position: relative; display: inline-flex; }
.menu-trigger.is-open { background: var(--bg-pressed); border-color: color-mix(in srgb, var(--accent) 40%, var(--border-strong)); }

.menu-panel {
  position: fixed; z-index: 300; min-width: 194px;
  padding: 5px; display: flex; flex-direction: column; gap: 1px;
  background: var(--bg-elev);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  box-shadow: var(--shadow-flyout);
}

.menu-sep { height: 1px; margin: 4px 6px; background: var(--divider); }

.menu-item {
  display: flex; align-items: center; gap: 9px; width: 100%;
  padding: 7px 11px; border: 0; border-radius: var(--radius-ctl);
  background: transparent; color: var(--text);
  font-family: inherit; font-size: 12.5px; font-weight: 500; text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.menu-item:hover:not(:disabled) { background: var(--bg-hover); }
.menu-item:focus-visible { outline: none; box-shadow: var(--ring); background: var(--bg-hover); }
.menu-item:disabled { opacity: .42; cursor: default; }
.menu-item .icon { color: var(--text-dim); }
.menu-item.danger { color: var(--danger); }
.menu-item.danger .icon { color: var(--danger); }
.menu-item.danger:hover:not(:disabled) { background: var(--danger-soft); }
.menu-item-label { flex: 1; }

/* 浮层进出：轻微的位移 + 淡入，避免生硬弹出 */
.menu-enter-active, .menu-leave-active { transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease); }
.menu-enter-from, .menu-leave-to { opacity: 0; transform: translateY(-4px) scale(.98); }
</style>
