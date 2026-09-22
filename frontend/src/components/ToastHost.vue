<script setup lang="ts">
/** Toast 宿主：统一承载成功/警告/错误提示（点击可关闭）。 */
import { dismissToast, state } from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';

/** 提示级别 → 图标（全部走 AppIcon，不使用 emoji）。 */
const ICON: Record<string, string> = {
  ok: 'check_circle', success: 'check_circle', warn: 'warning', warning: 'warning',
  error: 'error', danger: 'error',
};

function iconOf(kind?: string | null): string {
  return ICON[String(kind ?? '')] || 'info';
}
</script>

<template>
  <div class="toast-host">
    <TransitionGroup name="toast">
      <div v-for="t in state.toasts" :key="t.id" :class="['toast', t.kind]" @click="dismissToast(t.id)">
        <AppIcon :name="iconOf(t.kind)" :size="15" />
        <span class="toast-text">{{ t.message }}</span>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-host { position: fixed; right: 16px; bottom: 16px; z-index: 200; display: flex; flex-direction: column; gap: 8px; max-width: 400px; }
.toast {
  cursor: pointer;
  display: flex; align-items: flex-start; gap: 8px;
  background: var(--bg-elev); border: 1px solid var(--border-strong); border-left: 3px solid var(--accent);
  border-radius: var(--radius-sm); box-shadow: var(--shadow-2); padding: 9px 12px; font-size: 13px;
}
.toast .icon { margin-top: 1px; color: var(--accent-text); }
.toast-text { min-width: 0; word-break: break-word; }
.toast.ok { border-left-color: var(--ok); }
.toast.ok .icon { color: var(--ok); }
.toast.warn { border-left-color: var(--warn); }
.toast.warn .icon { color: var(--warn); }
.toast.error { border-left-color: var(--danger); }
.toast.error .icon { color: var(--danger); }
.toast-enter-active, .toast-leave-active { transition: all .18s ease; }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translateY(6px); }
</style>
