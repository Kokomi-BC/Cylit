<script setup lang="ts">
/**
 * 站内二次确认的**全局宿主**（2026-10-03 新增）。
 *
 * 为什么要有它：`confirm()` 那套原生弹窗在 Windows 上样式与产品完全脱节（不能跟主题、
 * 不能统一圆角与动效），而"每个视图各自放一个 ConfirmModal"又会让每个文件都多一份
 * 状态与模板。这里把弹窗挂在 `App.vue` 上一次，任何地方 `await askConfirm({...})` 即可。
 *
 * 用法见 `stores/confirm.ts`：`if (!(await askConfirm({ title, body, danger: true }))) return;`
 */
import ConfirmModal from '@/components/ConfirmModal.vue';
import { confirmState, resolveConfirm } from '@/stores/confirm';
</script>

<template>
  <ConfirmModal
    :open="confirmState.open"
    :title="confirmState.title"
    :body="confirmState.body"
    :target="confirmState.target"
    :confirm-label="confirmState.confirmLabel"
    :cancel-label="confirmState.cancelLabel"
    :danger="confirmState.danger"
    @confirm="resolveConfirm(true)"
    @cancel="resolveConfirm(false)"
  />
</template>
