<script setup lang="ts">
/**
 * ContextMenu —— 阅读器的**毛玻璃右键菜单**（用户口径 2026-10-08）。
 *
 * 与 `Menu.vue`（工具条上的"更多操作"）的区别只有两点，其余行为同一套：
 * - 它**按鼠标点**定位（`Menu` 是贴在触发按钮下面）；
 * - 它是**毛玻璃**（半透明底 + `backdrop-filter`），因为它是浮在正文上的、面积又小。
 *
 * 行为：传送（Teleport）到 body + fixed 定位；点外面 / `Esc` / 滚动 / 窗口变化 → 关闭；
 * 打开后焦点落在第一项，`↑ ↓ Home End` 在项间移动（`Tab` 关闭）；点位靠边时自动往里挪。
 * **只显示真实存在的快捷键**：没有实现的动作不写提示（别编一个 Ctrl+X 出来）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';

// 注意：`<script setup>` 里不能写模块导出，调用方用同构的字面量类型即可（结构类型）
interface CtxItem {
  key: string;
  label: string;
  icon?: string;
  /** 右侧那列快捷键提示（只在**真有**快捷键的项上写） */
  shortcut?: string;
  danger?: boolean;
  disabled?: boolean;
  /** 这一项**之前**插一条分隔线 */
  sep?: boolean;
}

const props = withDefaults(defineProps<{
  open: boolean;
  /** 鼠标点（视口坐标） */
  x: number;
  y: number;
  items: CtxItem[];
  width?: number;
}>(), { width: 218 });

const emit = defineEmits<{
  (e: 'select', key: string): void;
  (e: 'close'): void;
  /** 摆完位报一次真实矩形：划词胶囊要**贴在菜单上沿**（用户口径 2026-10-08） */
  (e: 'placed', rect: { left: number; top: number; width: number; height: number }): void;
}>();

const panel = ref<HTMLElement | null>(null);
const pos = ref({ left: 0, top: 0 });

const EDGE = 8;
const GAP = 2;

/** 就以鼠标点为左上角；右边/下边放不下就往里挪（必要时翻到点的左上侧）。 */
async function place() {
  await nextTick();
  const el = panel.value;
  if (!el) return;
  const w = el.offsetWidth || props.width;
  const h = el.offsetHeight || 220;
  let left = props.x + GAP;
  let top = props.y + GAP;
  if (left + w > window.innerWidth - EDGE) left = Math.max(EDGE, props.x - w - GAP);
  if (top + h > window.innerHeight - EDGE) top = Math.max(EDGE, props.y - h - GAP);
  pos.value = { left: Math.round(left), top: Math.round(top) };
  emit('placed', { left: pos.value.left, top: pos.value.top, width: Math.round(w), height: Math.round(h) });
}

function close() {
  if (props.open) emit('close');
}

function pick(item: CtxItem) {
  if (item.disabled) return;
  emit('close');                     // 先收菜单：动作里可能要动 DOM（点浮层里的东西同理）
  emit('select', item.key);
}

function onDocPointer(ev: PointerEvent) {
  if (!props.open) return;
  if (panel.value?.contains(ev.target as Node)) return;
  close();
}

/** 滚动一下就收：菜单长在鼠标点上，页面一动它就跟正文对不上了（与划词菜单同一处理） */
function onScroll() { close(); }

function onKeydown(ev: KeyboardEvent) {
  if (!props.open) return;
  if (ev.key === 'Escape') { ev.preventDefault(); close(); return; }
  if (ev.key === 'Tab') { close(); return; }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(ev.key)) return;
  ev.preventDefault();
  const nodes = Array.from(panel.value?.querySelectorAll<HTMLElement>('.ctx-item:not(:disabled)') ?? []);
  if (!nodes.length) return;
  const cur = nodes.findIndex((n) => n === document.activeElement);
  let next = 0;
  if (ev.key === 'ArrowDown') next = cur < 0 ? 0 : (cur + 1) % nodes.length;
  else if (ev.key === 'ArrowUp') next = cur <= 0 ? nodes.length - 1 : cur - 1;
  else if (ev.key === 'End') next = nodes.length - 1;
  nodes[next]?.focus();
}

watch(() => props.open, async (v) => {
  if (!v) return;
  await place();
  await nextTick();
  panel.value?.querySelector<HTMLElement>('.ctx-item:not(:disabled)')?.focus();
});
watch(() => [props.x, props.y] as const, () => { if (props.open) void place(); });

onMounted(() => {
  document.addEventListener('pointerdown', onDocPointer, true);
  document.addEventListener('keydown', onKeydown, true);
  window.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', onScroll);
  window.addEventListener('blur', close);
});
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocPointer, true);
  document.removeEventListener('keydown', onKeydown, true);
  window.removeEventListener('scroll', onScroll, true);
  window.removeEventListener('resize', onScroll);
  window.removeEventListener('blur', close);
});
</script>

<template>
  <Teleport to="body">
    <Transition name="ctx">
      <div
        v-if="props.open"
        ref="panel"
        class="ctx-panel"
        data-ctx-menu
        role="menu"
        :style="{ left: pos.left + 'px', top: pos.top + 'px', width: props.width + 'px' }"
      >
        <template v-for="(it, i) in props.items" :key="it.key">
          <div v-if="it.sep && i > 0" class="ctx-sep" role="separator" />
          <button
            type="button"
            class="ctx-item"
            :class="{ danger: it.danger }"
            role="menuitem"
            :disabled="it.disabled"
            :data-ctx-key="it.key"
            @click="pick(it)"
          >
            <AppIcon v-if="it.icon" :name="it.icon" :size="15" />
            <span class="ctx-label">{{ it.label }}</span>
            <span v-if="it.shortcut" class="ctx-key">{{ it.shortcut }}</span>
          </button>
        </template>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 材质 = **顶部那条（原文/译文对照）胶囊的同款玻璃**（用户口径 2026-10-08："更改这两个材质为
   顶部的胶囊的材质"）：上下渐变 + `blur(48px) saturate(2.6)`、不画描边，靠 `--shadow-flyout` 浮起来。
   深浅两套主题用 `--glass-bar-top/bottom` 两个令牌，与顶部胶囊完全同一份。 */
.ctx-panel {
  position: fixed; z-index: 320; padding: 6px;
  display: flex; flex-direction: column; gap: 1px;
  border-radius: 12px;
  /* 「特别透亮」的玻璃：白底只留一成多（再多就是白上加白，看不到玻璃），
     真正给"透亮"感的是 backdrop 里的 **brightness + saturate** —— 把背后的正文提亮、加饱和，
     再糊开；顶边一道高光当作玻璃的棱。 */
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) 20%, transparent),
    color-mix(in srgb, var(--bg-elev) 10%, transparent));
  backdrop-filter: blur(40px) saturate(2.4) brightness(1.14);
  -webkit-backdrop-filter: blur(40px) saturate(2.4) brightness(1.14);
  border: 1px solid color-mix(in srgb, var(--border-strong) 55%, transparent);
  box-shadow: var(--shadow-flyout),
    inset 0 1px 0 color-mix(in srgb, var(--bg-elev) 70%, transparent);
  user-select: none;
}
.ctx-sep { height: 1px; margin: 4px 6px; background: var(--divider); }
.ctx-item {
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 7px 10px; border: 0; border-radius: 8px;
  background: transparent; color: var(--text);
  font-family: inherit; font-size: 12.5px; font-weight: 500; text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.ctx-item:hover:not(:disabled), .ctx-item:focus-visible {
  outline: none; background: color-mix(in srgb, var(--accent) 14%, transparent);
}
.ctx-item:disabled { opacity: .42; cursor: default; }
.ctx-item .icon { color: var(--text-dim); flex: none; }
.ctx-label { flex: 1; min-width: 0; }
.ctx-key { flex: none; font-family: var(--mono); font-size: 11px; color: var(--text-faint); }
.ctx-item.danger { color: var(--danger); }
.ctx-item.danger .icon { color: var(--danger); }
.ctx-item.danger:hover:not(:disabled) { background: var(--danger-soft); }

.ctx-enter-active, .ctx-leave-active {
  transition: opacity var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
}
.ctx-enter-from, .ctx-leave-to { opacity: 0; transform: scale(.98) translateY(-2px); }
</style>
