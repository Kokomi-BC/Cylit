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
  /** 色板：给了就在这一行右侧排一排圆点（复用全站 `.hl-swatch` 那份颜色）。
   *  点圆点 = 用那个颜色执行；点行本身 = 默认色（用户口径 2026-10-10）。 */
  swatches?: string[];
}

const props = withDefaults(defineProps<{
  open: boolean;
  /** 鼠标点（视口坐标） */
  x: number;
  y: number;
  items: CtxItem[];
  width?: number;
  /**
   * 贴在**选区上方**打开（触摸长按用）：此时 `x` 是选区中心、`y` 是选区上沿。
   * 目的是别盖住选区本身，也别盖住两端那两颗拖动手柄（用户口径 2026-10-17）。
   */
  above?: boolean;
  /**
   * **触摸屏样式**：照 Android 的"文本工具条"—— 一排**图标按钮**浮在选区上方，
   * 不带遮罩、不挡两端手柄（用户口径 2026-10-17：要类似 Android 的，且不要影响拖动条）。
   */
  compact?: boolean;
}>(), { width: 218, above: false, compact: false });

const emit = defineEmits<{
  (e: 'select', key: string): void;
  /** 点了色板里的某个色点（`CtxItem.swatches`）：带颜色名回去，调用方直接按色高亮 */
  (e: 'color', color: string): void;
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
  if (props.above || props.compact) {
    // 贴在选区**上方**：水平居中、竖直压在选区上沿之外（28px 留出手柄的小尾巴）；
    // 上面放不下就翻到选区下方（Android 也是这么处理的）
    left = props.x - w / 2;
    top = props.y - 28 - h;
    if (top < EDGE) top = Math.min(window.innerHeight - h - EDGE, props.y + 44);
    /* 上面算出来的 top 仍可能是负的（调用方给的 y 本身就在屏幕外）：再夹一次，
       否则面板"在 DOM 里、屏幕上看不见"。 */
    top = Math.max(EDGE, Math.min(top, window.innerHeight - h - EDGE));
    left = Math.max(EDGE, Math.min(left, window.innerWidth - w - EDGE));
  } else {
    if (left + w > window.innerWidth - EDGE) left = Math.max(EDGE, props.x - w - GAP);
    if (top + h > window.innerHeight - EDGE) top = Math.max(EDGE, props.y - h - GAP);
  }
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

/** 点了色板里的某个色点：与 `pick` 同一节奏（先收菜单再动手），只是多带一个颜色。 */
function pickColor(color: string) {
  emit('close');
  emit('color', color);
}

function onDocPointer(ev: PointerEvent) {
  if (!props.open) return;
  const t = ev.target as Node | null;
  if (panel.value?.contains(t)) return;
  /* 划词胶囊是**另一个浮层**，而且右键时它就贴在菜单上沿（`ReaderView.pillPos` 算的）：
     点它的时候如果把菜单先收掉，胶囊会在 `pointerdown` 与 `click` **之间**被挪回选区那边，
     于是那次点击落在空气上 —— 表现就是"右键之后点胶囊没反应"（用户口径 2026-10-10）。
     所以胶囊上按下的这一下要放行：菜单先别关，等胶囊把这一下点完。 */
  if ((t as HTMLElement | null)?.closest?.('[data-selection-menu]')) return;
  // 拖选区两端的手柄同理：按在手柄上是为了**扩大选区**，菜单先别收
  if ((t as HTMLElement | null)?.closest?.('.sel-handle')) return;
  close();
}

/**
 * 滚动一下就收：菜单长在鼠标点上，页面一动它就跟正文对不上了（与划词菜单同一处理）。
 *
 * ⚠️ 但**刚打开的那一小会儿**要放行：触摸屏拖手柄自动滚动时，松手那一刻还有几帧
 * scroll 在路上，菜单刚摆好就被这些余波收掉 —— 表现就是"滚了一大段松手，工具条不出现"。
 */
let openedAt = 0;
watch(() => props.open, (v) => { if (v) openedAt = Date.now(); });
function onScroll() {
  if (Date.now() - openedAt < 300) return;
  close();
}

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
        :class="{ 'ctx-bar': props.compact }"
        data-ctx-menu
        role="menu"
        tabindex="-1"
        :style="props.compact
          ? { left: pos.left + 'px', top: pos.top + 'px' }
          : { left: pos.left + 'px', top: pos.top + 'px', width: props.width + 'px' }"
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
            <!-- 色板：与划词胶囊/AI 解释浮层**共用** `.hl-swatch` 那份颜色（base.css），
                 不另起一套色值。`@click.stop` —— 点色点只按那个色，不再走行本身的默认色。 -->
            <span v-if="it.swatches?.length" class="ctx-colors">
              <span
                v-for="c in it.swatches"
                :key="c"
                class="hl-swatch"
                :class="c"
                :title="`标成${c}色高亮`"
                @click.stop="pickColor(c)"
              />
            </span>
            <span v-else-if="it.shortcut" class="ctx-key">{{ it.shortcut }}</span>
          </button>
        </template>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 材质 = **与顶部胶囊同一块玻璃**（用户口径 2026-10-18：右键菜单和这些菜单都改成胶囊
   那个材质、样式类似）。配方直接照抄 .reader-capsule：上亮下暗的渐变本体 + 48px 饱和模糊
   + 柔光投影，**不画描边**（描边在深色下会变成一圈发光环），所以浅色/深色/各套皮肤
   都和胶囊一起变，两处不会再长得不一样。 */
.ctx-panel {
  position: fixed; z-index: 320; padding: 6px;
  display: flex; flex-direction: column; gap: 1px;
  border-radius: 14px;
  min-width: 180px; max-height: 90vh; overflow-y: auto;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-top, 80%), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-bottom, 68%), transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  border: 0;
  box-shadow: var(--shadow-flyout);
  user-select: none;
}
/* 细滚动条（目标站口径：4px、中性色） */
.ctx-panel::-webkit-scrollbar { width: 4px; }
.ctx-panel::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--text) 24%, transparent); border-radius: 999px; }
.ctx-panel::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--text) 38%, transparent); }
/* 面板只当"焦点容器"用（`tabindex=-1`），别画浏览器默认的聚焦框 */
.ctx-panel:focus, .ctx-panel:focus-visible { outline: none; }

/* ---------------- 触摸屏：Android 式**悬浮文本工具条** ----------------
 * 一排图标按钮浮在选区上方：不带遮罩（否则拖动手柄会被它挡住），也不显示快捷键。
 * 每个按钮 40×40，够手指点；放不下就横向滚（极少见）。 */
.ctx-panel.ctx-bar {
  display: flex; flex-direction: row; align-items: center; gap: 0;
  width: auto; min-width: 0; max-width: calc(100vw - 16px);
  padding: 5px 6px; border-radius: 999px;
  overflow-x: auto; overflow-y: hidden; scrollbar-width: none;
}
.ctx-panel.ctx-bar::-webkit-scrollbar { display: none; }
/* 触摸条里不要分隔线：一排图标本来就靠图标区分，多一条线只会显得空 */
.ctx-panel.ctx-bar .ctx-sep { display: none; }
.ctx-panel.ctx-bar .ctx-item {
  width: 36px; height: 36px; flex: none; padding: 0;
  justify-content: center; gap: 0; border-radius: 999px;
}
.ctx-panel.ctx-bar .ctx-key,
.ctx-panel.ctx-bar .ctx-label { display: none; }
.ctx-panel.ctx-bar .ctx-item:active:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  color: var(--accent-text);
}
/* 色板：排在最前面的那一小排圆点（触摸态把"划词胶囊"并进来了，所以这里要能直接选色） */
.ctx-panel.ctx-bar .ctx-colors {
  display: flex; align-items: center; gap: 5px; padding: 0 4px 0 2px;
  /* 触摸条里色板不再跟在图标后面，去掉左外边距，否则左边会空一块（用户口径 2026-10-18） */
  margin-left: 0;
}
.ctx-panel.ctx-bar .ctx-colors::after {
  content: ''; width: 1px; height: 20px; margin-left: 3px; background: var(--border);
}
.ctx-panel.ctx-bar .ctx-item:has(.ctx-colors) { width: auto; height: 36px; padding: 0 2px; }

.ctx-sep { height: 1px; margin: 4px 0; background: var(--border); }

.ctx-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 8px 12px; border: 0; border-radius: 8px;
  background: transparent; color: var(--text);
  font-family: inherit; font-size: calc(14px * var(--ts, 1)); font-weight: 500; text-align: left; cursor: pointer;
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
.ctx-key { flex: none; margin-left: 16px; font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
/* 色板（用户口径 2026-10-10）：一排圆点排在行右侧，与弹层右侧内边距对齐；
   圆点本身用 `base.css` 的 `.hl-swatch`（与划词胶囊同一份颜色），这里只管排布。 */
.ctx-colors { flex: none; display: inline-flex; align-items: center; gap: 6px; margin-left: 16px; }
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
