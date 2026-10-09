<script setup lang="ts">
/**
 * ContextMenu —— 阅读器的**毛玻璃右键菜单**（用户口径 2026-10-08）。
 *
 * 与 `Menu.vue`（工具条上的"更多操作"）的区别只有两点，其余行为同一套：
 * - 它**按鼠标点**定位（`Menu` 是贴在触发按钮下面）；
 * - 它是**毛玻璃**（半透明底 + `backdrop-filter`），因为它是浮在正文上的、面积又小。
 *
 * 材质/图标/顺序（2026-10-09）：按用户口径**复刻 fakenewsdetect.art 的右键菜单** ——
 * 半透明底 + `blur(8px)` + **1px 实描边** + 双层柔阴影、菜单项 14px / 8px 12px 内边距 /
 * 悬停 = 强调色淡底 + 强调色文字 + **一圈 1px 强调色描边**、开合走 `menuPopup`（scale(.8) → 1）。
 * 颜色不写死目标站的 hex，全部落回本项目令牌（`--bg/--border/--accent/--shadow-flyout`），
 * 所以浅色、深色、各套皮肤都自动跟随 —— 这一版**取代**了第八十六轮那套"白底 20%/10% +
 * blur(40px) saturate/brightness"的透亮配方。
 *
 * 行为：传送（Teleport）到 body + fixed 定位；点外面 / `Esc` / 滚动 / 窗口变化 → 关闭；
 * 打开后焦点落在**面板本身**（`tabindex=-1`，不预高亮任何一项 —— 否则第一项会被
 * `:focus-visible` 的悬停式样式染上，看着"一直处于鼠标悬浮状态"；用户口径 2026-10-09），
 * `↑ ↓ Home End` 在项间移动（`Tab` 关闭）；点位靠边时自动往里挪。
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
  /* 焦点给**面板自己**，不给第一项（用户口径 2026-10-09）：
     聚焦某项会让它拿到 `:focus-visible` 的悬停式样式，看上去"一直悬停"。
     键盘 `↑ ↓` 仍然照常把焦点移到项上（见 `onKeydown`），无障碍不受影响。 */
  panel.value?.focus();
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
        tabindex="-1"
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
            <AppIcon v-if="it.icon" :name="it.icon" :size="14" />
            <span class="ctx-label">{{ it.label }}</span>
            <span v-if="it.shortcut" class="ctx-key">{{ it.shortcut }}</span>
          </button>
        </template>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 材质 = **复刻 fakenewsdetect.art 的右键菜单玻璃**（用户口径 2026-10-09）。
   配方：半透明底 + 弱模糊 + **一圈 1px 实描边** + 双层柔阴影 —— 靠描边与阴影把菜单
   "托起来"，而不是靠加厚白底。底色与模糊都走 `--glass-flyout-*`（见 tokens.css），
   与**划词胶囊同一份**，所以两处永远一致、且浅色/深色/各套皮肤都自动跟随。 */
.ctx-panel {
  position: fixed; z-index: 320; padding: 6px;
  display: flex; flex-direction: column; gap: 1px;
  border-radius: 12px;                          /* 目标站 --radius-md */
  min-width: 180px; max-height: 90vh; overflow-y: auto;
  background: var(--glass-flyout-bg);
  backdrop-filter: var(--glass-flyout-blur);
  -webkit-backdrop-filter: var(--glass-flyout-blur);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-flyout);
  user-select: none;
}
/* 细滚动条（目标站口径：4px、中性色） */
.ctx-panel::-webkit-scrollbar { width: 4px; }
.ctx-panel::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 2px; }
.ctx-panel::-webkit-scrollbar-thumb:hover { background: var(--text-faint); }
/* 面板只当"焦点容器"用（`tabindex=-1`），别画浏览器默认的聚焦框 */
.ctx-panel:focus, .ctx-panel:focus-visible { outline: none; }

.ctx-sep { height: 1px; margin: 4px 0; background: var(--border); }

.ctx-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 8px 12px; border: 0; border-radius: 8px;
  background: transparent; color: var(--text);
  font-family: inherit; font-size: 14px; font-weight: 500; text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}
/* 悬停 = 强调色淡底 + 强调色文字 + **一圈 1px 强调色描边**（目标站口径）。
   图标是 `currentColor`，所以跟着一起变强调色。 */
.ctx-item:hover:not(:disabled), .ctx-item:focus-visible {
  outline: none;
  background: color-mix(in srgb, var(--accent) 15%, transparent);
  color: var(--accent-text);
  box-shadow: 0 0 0 1px var(--accent);
}
.ctx-item:disabled { opacity: .5; cursor: default; }
.ctx-item .icon { flex: none; }
.ctx-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 快捷键提示（目标站口径：12px、次要文字色、右对齐、至少隔开 16px） */
.ctx-key { flex: none; margin-left: 16px; font-size: 12px; color: var(--text-dim); }
.ctx-item.danger { color: var(--danger); }
.ctx-item.danger:hover:not(:disabled), .ctx-item.danger:focus-visible {
  background: var(--danger-soft); color: var(--danger); box-shadow: 0 0 0 1px var(--danger);
}

/* 开合动效 = 目标站的 `menuPopup`（scale(.8) translateY(-20px) → scale(1)），0.2s 标准缓动 */
.ctx-enter-active { transition: opacity .2s var(--ease), transform .2s var(--ease); }
.ctx-leave-active { transition: opacity var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease); }
.ctx-enter-from { opacity: 0; transform: scale(.8) translateY(-20px); }
.ctx-leave-to { opacity: 0; transform: scale(.98); }
</style>
