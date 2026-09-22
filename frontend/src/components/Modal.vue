<script setup lang="ts">
/** 通用弹窗（受控：`open` + `@close`；支持 Esc 关闭）。 */
import { onMounted, onUnmounted } from 'vue';
import AppIcon from '@/components/AppIcon.vue';

const props = withDefaults(defineProps<{
  open: boolean;
  title?: string;
  wide?: boolean;
  closeOnMask?: boolean;
}>(), { title: '', wide: false, closeOnMask: true });

const emit = defineEmits<{ (e: 'close'): void }>();

function close() { emit('close'); }
function onMask() { if (props.closeOnMask) close(); }
function onKey(ev: KeyboardEvent) { if (ev.key === 'Escape' && props.open) close(); }

onMounted(() => document.addEventListener('keydown', onKey));
onUnmounted(() => document.removeEventListener('keydown', onKey));
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="modal-mask" @click.self="onMask">
      <div class="modal" :class="{ wide }" role="dialog" aria-modal="true">
        <div class="modal-head">
          <span>{{ title }}</span>
          <span class="spacer" />
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
.modal-head { padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 650; display: flex; align-items: center; }
.modal-body { padding: 16px; overflow: auto; }
.modal-foot { padding: 12px 16px; border-top: 1px solid var(--border); display: flex; justify-content: flex-end; gap: 8px; }
</style>
