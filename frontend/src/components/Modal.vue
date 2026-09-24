<script setup lang="ts">
/** 通用弹窗（受控：`open` + `@close`；支持 Esc 关闭）。
 *
 * `full` = 铺满视口（"全屏"）：遮罩不再留边距、弹窗不留圆角与最大宽高。
 * 用 CSS 铺满而不是 Fullscreen API：不弹浏览器权限提示、Esc/关闭逻辑不用另写一套，
 * 对使用者来说观感一样（遮罩的 z-index 本来就在标签条之上）。
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
          <button class="btn icon sm" title="关闭" @click="close">
            <AppIcon name="close" :size="14" />
          </button>
        </div>
        <div class="modal-body"><slot /></div>
        <div v-if="$slots.footer" class="modal-foot"><slot name="footer" /></div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-mask { position: fixed; inset: 0; background: rgba(12, 16, 24, .45); z-index: 150; display: flex; align-items: center; justify-content: center; padding: 24px; }
.modal { background: var(--bg-elev); border-radius: var(--radius); box-shadow: var(--shadow-2); width: min(720px, 100%); max-height: 86vh; display: flex; flex-direction: column; border: 1px solid var(--border); }
.modal.wide { width: min(1100px, 100%); }
/* 全屏：铺满视口。遮罩去掉内边距、弹窗去掉圆角/描边/最大宽高（贴着屏幕边看更"满"）。 */
.modal-mask.full { padding: 0; }
.modal.full {
  width: 100%; max-width: none; height: 100%; max-height: none;
  border-radius: 0; border: 0;
}
.modal.full .modal-head { padding-right: 10px; }
.modal-head { padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 650; display: flex; align-items: center; gap: 8px; }
/* 标题可能很长（放大插图时显示的是**图注**，一句话能有几十字）：按**一行截断**，
   省略号 + 原生 tooltip 看全文；`min-width: 0` 是必须的 —— 否则 flex 项不肯收缩，
   长标题会把右侧的按钮顶出弹窗。 */
.modal-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.modal-body { padding: 16px; overflow: auto; }
.modal-foot { padding: 12px 16px; border-top: 1px solid var(--border); display: flex; justify-content: flex-end; gap: 8px; }
</style>
