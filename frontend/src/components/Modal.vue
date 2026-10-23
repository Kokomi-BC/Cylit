<script setup lang="ts">
/** 通用弹窗（受控：`open` + `@close`；支持 Esc 关闭）。
 *
 * 版式（2026-10-03 重做，用户口径"所有 HTML 弹窗重做，使用简洁新样式"）：
 * **去掉两条横线**（头部/底部的 1px 分隔）—— 分隔线把弹窗切成三段，看起来像表单。
 * 现在只有"标题 + 内容 + 操作"三块留白，层级靠**字重与留白**表达：
 *   - 圆角用 `--radius-lg`，描边极淡（`--border`），投影一档（`--shadow-2`）；
 *   - 标题 15px/650，关闭是右上角的幽灵图标钮（不占标题行的高度）；
 *   - 底部按钮右对齐、与内容同留白。
 * `full` = 铺满视口（"全屏"）：遮罩不留边距、弹窗不留圆角与最大宽高。
 * 用 CSS 铺满而不是 Fullscreen API：不弹浏览器权限提示、Esc/关闭逻辑不用另写一套。
 */
import { onMounted, onUnmounted } from 'vue';
import AppIcon from '@/components/AppIcon.vue';

const props = withDefaults(defineProps<{
  open: boolean;
  title?: string;
  wide?: boolean;
  closeOnMask?: boolean;
  /** 铺满整个视口（全屏） */
  full?: boolean;
  /** 窄版（440px）：二次确认那种"一句话 + 两枚按钮"的弹窗用它 ——
   *  默认宽度是给表单 / 正文用的，确认框套上去左右会空出一大片（用户口径 2026-12-02）。 */
  narrow?: boolean;
}>(), { title: '', wide: false, closeOnMask: true, full: false, narrow: false });

const emit = defineEmits<{ (e: 'close'): void }>();

function close() { emit('close'); }
/** 遮罩按下过没有：只有「按在遮罩上、也在遮罩上松开」才算点遮罩。
 *  否则在弹窗里拖选文字、手滑松到遮罩上时，浏览器把 click 派发到两者共同祖先（就是遮罩），
 *  @click.self 判定成立 → 弹窗被误关（用户口径 2026-12-02）。 */
let downOnMask = false;
function onMaskDown(ev: PointerEvent) {
  downOnMask = ev.target === ev.currentTarget;
}
function onMask() {
  if (!props.closeOnMask || !downOnMask) return;
  close();
}
function onKey(ev: KeyboardEvent) { if (ev.key === 'Escape' && props.open) close(); }

onMounted(() => document.addEventListener('keydown', onKey));
onUnmounted(() => document.removeEventListener('keydown', onKey));
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open" class="modal-mask" :class="{ full }"
      @pointerdown="onMaskDown" @click.self="onMask"
    >
      <div class="modal" :class="{ wide, full, narrow }" role="dialog" aria-modal="true">
        <div class="modal-head">
          <span class="modal-title" :title="title">{{ title }}</span>
          <span class="spacer" />
          <!-- 头部右侧的附加工具（如「全屏」）：由使用方通过 #tools 插槽提供 -->
          <slot name="tools" />
          <button class="modal-x" title="关闭" @click="close">
            <AppIcon name="close" :size="15" />
          </button>
        </div>
        <div class="modal-body"><slot /></div>
        <div v-if="$slots.footer" class="modal-foot"><slot name="footer" /></div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-mask {
  /* 250：必须高于提供方抽屉（180）与图片查看器（240）—— 否则从抽屉里点「删除密钥」，
     确认框会落在抽屉**下面**，遮罩也压不住它（用户口径 2026-12-02）。
     菜单（300）/右键（360）/选中菜单（340）仍在它之上，浮层不会被弹窗盖住。 */
  position: fixed; inset: 0; z-index: 250; display: flex; align-items: center; justify-content: center;
  padding: 24px;
  background: color-mix(in srgb, var(--bg-sunken) 40%, rgba(12, 16, 24, .45));
  backdrop-filter: blur(3px);
  -webkit-backdrop-filter: blur(3px);
}
.modal {
  display: flex; flex-direction: column;
  /* 默认宽从 680 收到 620（用户口径 2026-12-02：弹窗都太宽，读起来"空")。
     表单类仍然够用；真正需要宽的自己传 wide。 */
  width: min(620px, 100%); max-height: 86vh;
  background: var(--bg-elev); border: 1px solid var(--border);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-2);
}
.modal.wide { width: min(1100px, 100%); }
.modal.narrow { width: min(440px, 100%); }
/* 全屏：铺满视口。遮罩去掉内边距、弹窗去掉圆角/描边/最大宽高（贴着屏幕边看更"满"）。 */
.modal-mask.full { padding: 0; backdrop-filter: none; -webkit-backdrop-filter: none; }
.modal.full { width: 100%; max-width: none; height: 100%; max-height: none; border-radius: 0; border: 0; }

/* 头部**不画分隔线**，只留白（见文件头的说明） */
.modal-head {
  display: flex; align-items: center; gap: 8px;
  padding: 16px 16px 4px 20px;
  font-size: calc(15px * var(--ts, 1)); font-weight: 650; color: var(--text);
}
/* 标题可能很长（放大插图时显示的是**图注**，一句话能有几十字）：按**一行截断**，
   省略号 + 原生 tooltip 看全文；`min-width: 0` 是必须的 —— 否则 flex 项不肯收缩，
   长标题会把右侧的按钮顶出弹窗。 */
.modal-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 关闭：幽灵圆钮，hover 才浮一层浅底（不要描边、不要阴影 —— 它贴在弹窗内部） */
.modal-x {
  flex: none; width: 28px; height: 28px; padding: 0; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: var(--radius-ctl); background: transparent; color: var(--text-faint);
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.modal-x:hover { background: var(--bg-hover); color: var(--text); }
.modal-x:focus-visible { outline: none; box-shadow: var(--ring); }

.modal-body { padding: 12px 20px 20px; overflow: auto; }
.modal-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 0 20px 18px; }
/* 底部按钮的**键盘焦点框**（用户口径 2026-12-02：焦点落在按钮上时要"多一个框"）。
   base.css 那圈 --ring 柔光在浅底上偏淡，再叠一条主色实描边 —— 键盘用户一眼看出
   现在按下去的是哪一个（Tab 走一遍就能看到）；鼠标点击不会亮，避免每次点都闪一圈。 */
.modal-foot .btn:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 72%, transparent);
  outline-offset: 2px;
}
</style>
