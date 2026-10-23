<script setup lang="ts">
/**
 * 分时段单价的行编辑器（只负责画 + 收集，不负责提交）。
 *
 * 一行 = 名称（可空）+ 开始 / 结束（HH:MM）+ 输入价 + 输出价 + 缓存读 / 写（可选）+ 删除。
 * 版式是**两行一张卡**（时间一行、四个价一行）：弹窗只有 620px，七列挤一行必然错位。
 * 校验在 @/utils/priceRule 里（与提交时用的是同一份），这里只把红字摆到行下面。
 */
import { computed } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { ruleIssues, type PriceRuleDraft } from '@/utils/priceRule';
import { emptyRule } from '@/utils/priceRule';

const props = withDefaults(defineProps<{
  rows: PriceRuleDraft[];
  /** 币种符号（¥ / $），只用在占位符里，避免"这个数是什么钱"要靠猜 */
  symbol?: string;
}>(), { symbol: '' });

const emit = defineEmits<{ (e: 'update:rows', rows: PriceRuleDraft[]): void }>();

const issues = computed(() => ruleIssues(props.rows));

function issueAt(index: number): string {
  return issues.value.find((item) => item.index === index)?.message || '';
}

function add(): void {
  emit('update:rows', [...props.rows, emptyRule()]);
}

function remove(index: number): void {
  const next = props.rows.slice();
  next.splice(index, 1);
  emit('update:rows', next);
}

function pricePlaceholder(label: string): string {
  return '如 ' + (props.symbol || '') + label;
}
</script>

<template>
  <div class="pr-editor">
    <div class="pr-row" v-for="(row, i) in rows" :key="i" :data-rule-row="i">
      <div class="pr-time">
        <input v-model="row.label" class="pr-name" placeholder="如 高峰" :maxlength="64" :data-rule="'label:' + i" />
        <input v-model="row.start" class="pr-clock mono" inputmode="numeric" placeholder="09:00" :data-rule="'start:' + i" />
        <span class="pr-dash" aria-hidden="true">–</span>
        <input v-model="row.end" class="pr-clock mono" inputmode="numeric" placeholder="18:00" :data-rule="'end:' + i" />
        <button type="button" class="btn icon sm ghost pr-del" title="删除这一段" :data-rule="'del:' + i" @click="remove(i)">
          <AppIcon name="trash" :size="14" />
        </button>
      </div>
      <div class="pr-price">
        <label class="pr-pf">
          <span>输入</span>
          <input v-model="row.price_in" inputmode="decimal" :placeholder="pricePlaceholder('2')" :data-rule="'in:' + i" />
        </label>
        <label class="pr-pf">
          <span>输出</span>
          <input v-model="row.price_out" inputmode="decimal" :placeholder="pricePlaceholder('8')" :data-rule="'out:' + i" />
        </label>
        <label class="pr-pf">
          <span>缓存读<em>可选</em></span>
          <input v-model="row.price_cache_read" inputmode="decimal" placeholder="可选" :data-rule="'cr:' + i" />
        </label>
        <label class="pr-pf">
          <span>缓存写<em>可选</em></span>
          <input v-model="row.price_cache_write" inputmode="decimal" placeholder="可选" :data-rule="'cw:' + i" />
        </label>
      </div>
      <p class="pr-err" v-if="issueAt(i)" :data-rule-error="i">{{ issueAt(i) }}</p>
    </div>

    <div class="pr-foot">
      <button type="button" class="btn sm ghost" data-add-rule @click="add">
        <AppIcon name="add" :size="14" />添加时段
      </button>
      <span class="pr-unit">价格单位：每百万 Token</span>
    </div>
  </div>
</template>

<style scoped>
.pr-editor { display: flex; flex-direction: column; gap: 8px; min-width: 0; }

.pr-row {
  display: flex; flex-direction: column; gap: 6px; min-width: 0;
  padding: 8px 10px; border: 1px solid var(--divider); border-radius: var(--r-sm);
  background: var(--bg-sunken);
}
.pr-time {
  display: grid; grid-template-columns: minmax(0, 1fr) 74px 8px 74px auto;
  align-items: center; gap: 6px; min-width: 0;
}
.pr-time input { width: 100%; min-width: 0; height: var(--control-h-sm); font-size: calc(12.5px * var(--ts, 1)); }
.pr-clock { text-align: center; font-variant-numeric: tabular-nums; }
.pr-dash { text-align: center; color: var(--text-faint); }
.pr-del:hover:not(:disabled) { color: var(--danger); }

.pr-price { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; min-width: 0; }
.pr-pf { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.pr-pf > span {
  display: flex; align-items: baseline; gap: 4px; min-width: 0;
  font-size: calc(10.5px * var(--ts, 1)); font-weight: 600; color: var(--text-faint);
}
.pr-pf em { font-style: normal; font-weight: 500; font-size: calc(10px * var(--ts, 1)); color: var(--text-faint); opacity: .8; }
.pr-pf input { width: 100%; min-width: 0; height: var(--control-h-sm); font-size: calc(12.5px * var(--ts, 1)); }

.pr-err {
  margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5;
  color: var(--danger); overflow-wrap: anywhere;
}

.pr-foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pr-unit { font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); }

@media (max-width: 520px) {
  .pr-price { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .pr-time { grid-template-columns: minmax(0, 1fr) 66px 8px 66px auto; }
}
</style>
