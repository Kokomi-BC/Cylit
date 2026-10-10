<script setup lang="ts">
/** 划词浮动菜单：解释 / 笔记 / 复制 / 高亮（颜色）/ 关闭。
 *
 * `anchored` = 选区是否有块锚点：
 *   - 正文行（`[data-row-id]`）划词：可以写高亮/笔记（它们靠 `block_id + 字符偏移` 定位）；
 *   - **原 PDF 划词**：只有文字，没有块锚点 → 只留「解释 / 复制」（不写高亮、不存笔记）。
 *
 * ⚠️ 容器上必须是 `@mousedown.prevent.stop`：
 *   **点菜单的默认动作会把页面上的选区清掉**（浏览器行为）。以前只写了 `.stop`，于是真实鼠标
 *   点色块时 —— mousedown 清选区 → mouseup 时 `onMouseUp` 看到「选区已折叠」就 `show = false`
 *   → 菜单在自己的 click 触发前就被卸载 → **点高亮毫无反应**（用户报的正是这个；
 *   `.click()` 那种合成事件不会触发默认动作，所以自检探针反而测不出来）。
 *   `preventDefault` 保住选区，点完菜单还能看到自己选了哪一段。
 */
import AppIcon from '@/components/AppIcon.vue';
import { computed, nextTick, ref, watch } from 'vue';

const props = withDefaults(defineProps<{
  visible: boolean;
  x: number;
  y: number;
  colors: string[];
  anchored?: boolean;
  /**
   * 是否给「笔记」这一项。
   * 笔记靠 `block_id` 定位回正文，而 **Markdown 视图**里的锚点是 `md:p<页号>`（正文里没有这个块），
   * 存下来会变成一条"点了定位却跳不过去"的死笔记 —— 所以那边只给高亮/解释/复制。
   */
  allowNote?: boolean;
  /** 选区/点击处盖住了几处已有高亮：>0 才显示「禁止」图标（删除对应高亮） */
  hlCount?: number;
  /**
   * 选区/点击处的那条高亮**已经有笔记**了（《修改计划》第四步）：
   * 有的话这一项是「打开笔记」（跳到侧栏那条笔记），否则是「笔记」（新建，挂到这条高亮上）。
   */
  hasNote?: boolean;
}>(), { anchored: true, allowNote: true, hlCount: 0, hasNote: false });

const emit = defineEmits<{
  (e: 'explain'): void;
  (e: 'note'): void;
  (e: 'copy'): void;
  (e: 'highlight', color: string): void;
  /** 删除选区/点击处对应的高亮（跨行高亮整组一起删） */
  (e: 'unhighlight'): void;
  (e: 'close'): void;
}>();

/**
 * 贴边回拉：不要用"视口宽 - 猜的常量"去夹 `x`。
 * 菜单宽度随内容变（带锚点时还有 5 个色块），猜小了就会把右侧推到屏外；
 * 而且 `left` 一定，容器可用宽度 = 视口 - left，flex 子项默认会**收缩** →
 * 按钮变成竖排的"AI 解\n释"（用户报过）。
 * 做法：先按原位渲染 → 量出真实宽度 → 把溢出量用 `shift` 拉回来。
 */
const el = ref<HTMLElement | null>(null);
const shift = ref(0);
const style = computed(() => ({
  left: `${Math.max(8, props.x - shift.value)}px`,
  top: `${props.y}px`,
}));

async function reclamp() {
  if (!props.visible) { shift.value = 0; return; }
  await nextTick();
  const w = el.value?.offsetWidth ?? 0;
  shift.value = Math.max(0, props.x + w - (window.innerWidth - 10));
}
watch(() => [props.visible, props.x, props.y] as const, reclamp, { immediate: true });

const COLOR_TITLE: Record<string, string> = {
  red: '红', orange: '橙', yellow: '黄', green: '绿', blue: '蓝',
  indigo: '靖蓝', violet: '紫', pink: '粉', purple: '中性',
};
</script>

<template>
  <div
    v-if="props.visible"
    ref="el"
    class="selection-menu"
    :style="style"
    data-selection-menu
    @mousedown.prevent.stop
  >
    <!-- **只留图标**（用户口径 2026-10-08）：文字标签全撤掉，按钮变成正方形图标键，
         名称走 `title` / `aria-label`（悬停能看到、读屏也读得到）。
         「加入对话」也撤掉了 —— 那件事现在在**右键菜单**的「添加到对话框」里。 -->
    <button title="Cyli 解释（只依据论文内容）" aria-label="Cyli 解释" @click="emit('explain')">
      <AppIcon name="sparkle" :size="15" />
    </button>
    <!-- 原 PDF 栏（pdf.js 文字层）没有块锚点 → 那一栏只留「解释 / 复制」。
         「笔记」这一项只挂在**已有高亮**上：用户口径是"只有插入了高亮，才能在高亮的基础上
         加笔记" —— 没有高亮时它根本不出现，引导用户先点一个颜色标出来。 -->
    <button
      v-if="props.anchored && props.allowNote && props.hlCount > 0"
      :title="props.hasNote ? '打开这条笔记' : '给这段文字写笔记（会挂在这处高亮上）'"
      :aria-label="props.hasNote ? '打开笔记' : '添加笔记'"
      @click="emit('note')"
    >
      <AppIcon name="note" :size="15" />
    </button>
    <button title="复制" aria-label="复制" @click="emit('copy')">
      <AppIcon name="copy" :size="15" />
    </button>
    <span v-if="props.anchored" class="row" style="padding:0 5px;gap:6px">
      <span
        v-for="c in props.colors"
        :key="c"
        :class="['hl-swatch', c]"
        :title="`标成${COLOR_TITLE[c] ?? c}`"
        @click="emit('highlight', c)"
      />
      <!-- 自定义颜色（用户口径 2026-10-10）：一个**原生取色器**盖在七彩圆点上（与目标站同做法），
           选中即按那个 `#rrggbb` 高亮 —— 后端已放行实体色，不再是"静默变黄"。 -->
      <label class="hl-swatch custom" title="自定义颜色">
        <input type="color" value="#ffd700" data-hl-custom aria-label="自定义高亮颜色"
               @input="emit('highlight', ($event.target as HTMLInputElement).value)">
      </label>
    </span>
    <!-- 删除高亮：**禁止符号**（用户口径：删除走菜单，不再点高亮本身） -->
    <button
      v-if="props.hlCount > 0"
      class="danger"
      :title="props.hlCount > 1 ? `删除这处高亮（共 ${props.hlCount} 段）` : '删除高亮'"
      aria-label="删除高亮"
      @click="emit('unhighlight')"
    >
      <AppIcon name="ban" :size="15" />
    </button>
    <button title="关闭" aria-label="关闭" @click="emit('close')">
      <AppIcon name="close" :size="15" />
    </button>
  </div>
</template>

<style scoped>
/* 划词浮层：**与右键菜单同一款玻璃**（用户口径 2026-10-09："上面的胶囊使用同样的材质"）。
   材质本身走 `--glass-flyout-*`（见 tokens.css）：半透明底 + 弱模糊 + 1px 实描边 ——
   两处共用一份，改"透不透"只改令牌。这里只保留自己特有的两件事：
   ① 形状是**胶囊**（`border-radius: 999px`，与顶部那条对照胶囊一致）；
   ② 里面每一项都是**只带图标的胶囊键**（文字标签已撤），高度对齐 `.btn.sm`（26px）。
   ⚠️ 层级要**高过右键菜单**（`ContextMenu` 是 320）：右键时两个会同时在场上，
   而且右键之后它会被**挪到菜单上沿**（见 ReaderView 的 `pillPos`）。 */
.selection-menu {
  position: fixed; z-index: 340; padding: 3px;
  background: var(--glass-flyout-bg);
  backdrop-filter: var(--glass-flyout-blur);
  -webkit-backdrop-filter: var(--glass-flyout-blur);
  border: 1px solid var(--border);
  border-radius: 999px; box-shadow: var(--shadow-flyout);
  /* 不换行、不收缩：换行会让按钮变成竖排（“AI 解 / 释”） */
  display: flex; align-items: center; gap: 2px; flex-wrap: nowrap; white-space: nowrap;
  max-width: calc(100vw - 16px);
  /* 弹出（用户口径 2026-10-10："没有动画"）：根节点是 `v-if`，每次划词重新挂载，动画重放。 */
  animation: sm-pop .16s var(--ease);
}
@keyframes sm-pop {
  from { opacity: 0; transform: scale(.94) translateY(3px); }
  to { opacity: 1; transform: none; }
}
.selection-menu button {
  border: 0; background: transparent; color: var(--text-dim);
  height: var(--control-h-sm); width: var(--control-h-sm); padding: 0;
  border-radius: var(--radius-ctl); cursor: pointer; font-family: inherit;
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto; white-space: nowrap;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease),
    transform var(--dur-fast) var(--ease);
}
.selection-menu button:hover { background: var(--bg-hover); color: var(--text); }
/* 按下反馈（借鉴目标站：图标键按下去要有"陷一下"的触感） */
.selection-menu button:active { transform: scale(.9); }
/* 删除高亮：危险的破坏性动作，用语义色区分（与全站的 danger 一致） */
.selection-menu button.danger { color: var(--danger); }
.selection-menu button.danger:hover { background: var(--danger-soft); color: var(--danger); }
/* 色块一行：与按钮**同高**（26px），圆点垂直居中 —— 否则整条浮层会一高一低。
   色块本身（`.hl-swatch` 与那组颜色）在 `base.css`：AI 解释浮层的"标出这段"用的是同一份。 */
.selection-menu .row { display: inline-flex; align-items: center; height: var(--control-h-sm); }
/* 自定义色那枚：**七彩圆点**（取色器本体是透明的，盖满整枚，点哪都能唤起原生取色器）。
   ⚠️ 描边**不能改成透明**（用户口径 2026-10-10："图标有点瑕疵"）——
   其余色点都带那圈 1px 描边，只它没有，尺寸看着就比旁边小一圈、像浮在外面。 */
.hl-swatch.custom {
  position: relative; overflow: hidden;
  background: conic-gradient(from 90deg, #ff0080, #ff8c00, #ffe600, #00d26a, #00b3ff, #7c3aed, #ff0080);
}
.hl-swatch.custom input {
  position: absolute; inset: 0; width: 100%; height: 100%; padding: 0; border: 0;
  opacity: 0; cursor: pointer; appearance: none;
}
</style>
