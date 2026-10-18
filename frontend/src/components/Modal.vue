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
}>(), { title: '', wide: false, closeOnMask: true, full: false });

const emit = defineEmits<{ (e: 'close'): void }>();

function close() { emit('close'); }
function onMask() { if (props.closeOnMask) close(); }
function onKey(ev: KeyboardEvent) { if (ev.key === 'Escape' && props.open) close(); }

onMounted(() => document.addEventListener('keydown', onKey));
onUnmounted(() => document.removeEventListener('keydown', onKey));
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="modal-mask" :class="{ full }" @click.self="onMask">
      <div class="modal" :class="{ wide, full }" role="dialog" aria-modal="true">
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
  position: fixed; inset: 0; z-index: 150; display: flex; align-items: center; justify-content: center;
  padding: 24px;
  background: color-mix(in srgb, var(--bg-sunken) 40%, rgba(12, 16, 24, .45));
  backdrop-filter: blur(3px);
  -webkit-backdrop-filter: blur(3px);
}
.modal {
  display: flex; flex-direction: column;
  width: min(680px, 100%); max-height: 86vh;
  background: var(--bg-elev); border: 1px solid var(--border);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-2);
}
.modal.wide { width: min(1100px, 100%); }
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
</style>
