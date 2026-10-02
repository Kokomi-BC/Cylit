<script setup lang="ts">
/**
 * ConfirmModal —— 通用二次确认弹窗（站内，替代原生 `confirm()`）。
 *
 * 版式（2026-10-03 重做）：**图标 + 一句话 + 被操作对象**，恢复"简洁"那一版 ——
 * 一个 36px 的圆形图标块放在左边，文字在右；没有分隔线、没有多余说明。
 * 危险操作换红底图标 + 红色确认按钮。
 *
 * 一般不用直接引用它：全局挂一次 `ConfirmHost`，然后 `await askConfirm({...})`
 * （见 `stores/confirm.ts`），这样任何地方（含工具函数）都能用同一种弹窗。
 */
import Modal from '@/components/Modal.vue';
import AppIcon from '@/components/AppIcon.vue';

withDefaults(defineProps<{
  open: boolean;
  title?: string;
  /** 一句话说清后果（不写“确定要删除吗”这种废话）。 */
  body?: string;
  /** 被操作对象的名字 / 摘要，会以浅底卡片强调。 */
  target?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 危险操作：确认按钮用红色，图标也换成警告。 */
  danger?: boolean;
  busy?: boolean;
}>(), {
  title: '确认操作',
  body: '',
  target: '',
  confirmLabel: '确认',
  cancelLabel: '取消',
  danger: false,
  busy: false,
});

const emit = defineEmits<{ (e: 'confirm'): void; (e: 'cancel'): void }>();
</script>

<template>
  <Modal :open="open" :title="title" @close="emit('cancel')">
    <div class="confirm">
      <span class="cf-icon" :class="{ danger }">
        <AppIcon :name="danger ? 'warning' : 'info'" :size="17" />
      </span>
      <div class="cf-body">
        <p class="cf-text" v-if="body">{{ body }}</p>
        <p class="cf-target" v-if="target">{{ target }}</p>
      </div>
    </div>
    <template #footer>
      <button class="btn" :disabled="busy" @click="emit('cancel')">{{ cancelLabel }}</button>
      <button class="btn" :class="danger ? 'danger solid' : 'primary'" :disabled="busy" @click="emit('confirm')">
        {{ busy ? '处理中…' : confirmLabel }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.confirm { display: flex; gap: 12px; align-items: flex-start; }
.cf-icon {
  flex: none; display: flex; align-items: center; justify-content: center;
  width: 34px; height: 34px; border-radius: var(--radius-ctl);
  background: var(--accent-soft); color: var(--accent-text);
}
.cf-icon.danger { background: var(--danger-soft); color: var(--danger); }
.cf-body { min-width: 0; flex: 1; padding-top: 1px; }
.cf-text { margin: 0; font-size: 13px; color: var(--text-dim); line-height: 1.65; }
.cf-target {
  margin: 8px 0 0; padding: 9px 12px; font-size: 13px; font-weight: 600;
  background: var(--bg-sunken); border-radius: var(--r-sm); overflow-wrap: anywhere;
}
/* 危险确认做成**实心红钮**：一个描边红字按钮在"确认删除"这一步太轻了 */
.btn.danger.solid { background: var(--danger); border-color: transparent; color: #fff; box-shadow: none; }
.btn.danger.solid:hover:not(:disabled) { background: var(--danger); filter: brightness(1.06); }
</style>
