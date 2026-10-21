<script setup lang="ts">
/**
 * ConfirmModal —— 通用二次确认弹窗（站内，替代原生 `confirm()`）。
 *
 * 版式（2026-12-02 再收一次，用户口径"内容更简约、宽度更短"）：
 * **一句话 + 被操作对象的名字**，没有图标块、没有灰底卡片 —— 左边那枚 34px 的圆图标
 * 在一个只读一句话的框里属于纯装饰，去掉之后视线直接落在"会发生什么"上。
 * 宽度走 Modal 的 narrow（440px）；危险操作只由**红色确认按钮**表达。
 *
 * 一般不用直接引用它：全局挂一次 `ConfirmHost`，然后 `await askConfirm({...})`
 * （见 `stores/confirm.ts`），这样任何地方（含工具函数）都能用同一种弹窗。
 */
import Modal from '@/components/Modal.vue';

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
  <Modal :open="open" :title="title" narrow @close="emit('cancel')">
    <p class="cf-text" v-if="body">{{ body }}</p>
    <!-- 被操作对象单独一行、**不加底色块**：它是"哪个东西"的答案，一行加重字就够 -->
    <p class="cf-target" v-if="target">{{ target }}</p>
    <template #footer>
      <button class="btn" :disabled="busy" @click="emit('cancel')">{{ cancelLabel }}</button>
      <button class="btn" :class="danger ? 'danger solid' : 'primary'" :disabled="busy" @click="emit('confirm')">
        {{ busy ? '处理中…' : confirmLabel }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.cf-text {
  margin: 0; font-size: calc(13px * var(--ts, 1)); color: var(--text-dim);
  line-height: 1.7; overflow-wrap: anywhere;
}
.cf-target {
  margin: 10px 0 0; font-size: calc(13.5px * var(--ts, 1)); font-weight: 650;
  color: var(--text); overflow-wrap: anywhere;
}
/* 危险确认做成**实心红钮**：一个描边红字按钮在"确认删除"这一步太轻了 */
.btn.danger.solid { background: var(--danger); border-color: transparent; color: #fff; box-shadow: none; }
.btn.danger.solid:hover:not(:disabled) { background: var(--danger); filter: brightness(1.06); }
</style>
