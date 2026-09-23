<script setup lang="ts">
/**
 * ConfirmModal —— 通用二次确认弹窗。
 *
 * 替代原生 `window.confirm`：原生弹窗无法跟随主题、无法统一圆角与动效，
 * 而且在 Windows 上样式与产品完全脱节。删除类不可逆操作一律走这里。
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
        <AppIcon :name="danger ? 'warning' : 'info'" :size="18" />
      </span>
      <div class="cf-body">
        <p class="cf-text" v-if="body">{{ body }}</p>
        <p class="cf-target" v-if="target">{{ target }}</p>
      </div>
    </div>
    <template #footer>
      <button class="btn" :disabled="busy" @click="emit('cancel')">{{ cancelLabel }}</button>
      <button class="btn" :class="danger ? 'danger' : 'primary'" :disabled="busy" @click="emit('confirm')">
        {{ busy ? '处理中…' : confirmLabel }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.confirm { display: flex; gap: 12px; align-items: flex-start; }
.cf-icon {
  flex: none; display: flex; align-items: center; justify-content: center;
  width: 36px; height: 36px; border-radius: var(--radius-ctl);
  background: var(--accent-soft); color: var(--accent-text);
}
.cf-icon.danger { background: var(--danger-soft); color: var(--danger); }
.cf-body { min-width: 0; flex: 1; }
.cf-text { margin: 0; font-size: 13px; color: var(--text-dim); line-height: 1.6; }
.cf-target {
  margin: 8px 0 0; padding: 10px 12px; font-size: 13px; font-weight: 600;
  background: var(--bg-sunken); border-radius: var(--r-sm); overflow-wrap: anywhere;
}
</style>
