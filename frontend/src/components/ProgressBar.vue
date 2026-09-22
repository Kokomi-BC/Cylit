<script setup lang="ts">
/** 进度条（翻译 / 任务通用）。 */
import { computed } from 'vue';
import { pct } from '@/utils/format';

const props = withDefaults(defineProps<{
  done?: number | null;
  total?: number | null;
  value?: number | null;
  okAt?: number;
}>(), { done: 0, total: 0, value: null, okAt: 100 });

const percent = computed(() => (props.value !== null && props.value !== undefined
  ? Math.max(0, Math.min(100, Number(props.value)))
  : pct(props.done, props.total)));
</script>

<template>
  <div class="bar" :class="{ ok: percent >= okAt }">
    <i :style="{ width: percent + '%' }" />
  </div>
</template>
