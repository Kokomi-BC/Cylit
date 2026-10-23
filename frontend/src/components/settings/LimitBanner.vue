<script setup lang="ts">
/**
 * 模型页顶部 · **限额横幅**（只在「设置 · 模型」这一页出现，别的页面不放）。
 *
 * ## 它讲哪一条
 * 只讲后端 `GET /models/limits/summary` 的 `most_urgent`：
 * · `level=warn`（任一 ≥80%）→ 警示色；
 * · `level=exceeded`（任一 ≥100%）→ 危险色，**覆盖** warn；
 * · `level=none` → 整块不渲染（横幅不出现，不留空）。
 * percent / spent / remaining / reset_at 全部照抄后端，前端一个数都不算。
 *
 * ## 关闭 = 只隐藏这一次提醒，**不解除限额**
 * 记住的是「哪一条限额（id）+ 它的 reset_at（本周期结束时刻）」：
 *   localStorage key = `cylit.model-limit-banner.dismissed`
 *   value = JSON `{ [限额id]: reset_at }`
 * 判定 = `dismissed[id] === reset_at`。所以
 * · 刷新页面：本周期关过 → 继续不显示（真的记住）；
 * · 周期翻篇（reset_at 变了）或这条限额被删掉重建（id 变了）→ 横幅**自然回来**。
 * 存不进 localStorage（隐私模式）就退化成"本次会话记得"，不影响主流程。
 *
 * ## 不给的操作
 * 这里**没有**任何「继续调用 / 忽略 / 强制发送」按钮：限额是后端闸门，
 * 前端给一个"绕过"按钮只会骗人。
 */
import { computed, ref } from 'vue';
import type { ModelLimitItem, ModelLimitsSummary } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{ summary: ModelLimitsSummary | null }>();

/** 见文件头：key 自拟，口径写在注释里 */
const STORAGE_KEY = 'cylit.model-limit-banner.dismissed';
const PERIOD_WORD: Record<string, string> = { day: '今日', week: '本周', month: '本月' };
const SYMBOL: Record<string, string> = { CNY: '¥', USD: '$' };
const ADVICE = '可选：换一条这家其它已配置的密钥（在该模型的「密钥来源」里切）/ 调高这条限额 / 等这个周期结束自动重置。';

function readDismissed(): Record<string, string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, string>;
    }
  } catch {
    /* 隐私模式 / 坏 JSON：当作没关过 */
  }
  return {};
}

const dismissed = ref<Record<string, string>>(readDismissed());

const level = computed<'none' | 'warn' | 'exceeded'>(() => props.summary?.level ?? 'none');
const item = computed<ModelLimitItem | null>(
  () => (level.value === 'none' ? null : props.summary?.most_urgent ?? null),
);
const visible = computed<boolean>(() => {
  const it = item.value;
  if (!it) return false;
  return dismissed.value[it.id] !== it.reset_at;
});
const count = computed<number>(() => props.summary?.items?.length ?? 0);

function money(value: number, currency: string): string {
  const n = Number(value);
  const v = Number.isFinite(n) ? n : 0;
  const code = String(currency || '').toUpperCase();
  return (SYMBOL[code] ?? (code ? code + ' ' : '')) + v.toFixed(2);
}
/** 与后端 JSON 同一个字面量：82.0 → "82.0%"（四舍五入只到一位，绝不重算） */
function pct(value: number): string {
  const n = Number(value);
  return (Number.isFinite(n) ? n : 0).toFixed(1) + '%';
}

const periodWord = computed<string>(() => PERIOD_WORD[item.value?.period ?? ''] ?? '本期');

const title = computed<string>(() => {
  const it = item.value;
  if (!it) return '';
  if (level.value === 'exceeded') return periodWord.value + '模型费用额度已用尽，新请求已被限制';
  return periodWord.value + '模型费用已使用 ' + pct(it.percent)
    + '，剩余额度 ' + money(it.remaining, it.currency);
});

const scopeWord = computed<string>(() => {
  const scope = item.value?.scope;
  return scope === 'key' ? '密钥' : scope === 'vendor' ? '厂商' : '模型';
});

const subline = computed<string>(() => {
  const it = item.value;
  if (!it) return '';
  const who = scopeWord.value + ' · ' + it.label;
  const used = '本周期已用 ' + money(it.spent, it.currency) + ' / 限额 ' + money(it.amount, it.currency)
    + '（' + pct(it.percent) + '）';
  const reset = '重置 ' + (it.reset_at || '—');
  return who + ' · ' + used + ' · ' + reset;
});

function dismiss(): void {
  const it = item.value;
  if (!it) return;
  dismissed.value = { ...dismissed.value, [it.id]: it.reset_at };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(dismissed.value));
  } catch {
    /* 存不了就只记在本次会话里：横幅仍然立刻消失 */
  }
}

/** 「查看全部（N）」= 真滚动到下面那块限额列表（不展开、不假设它就在视口里） */
function scrollToLimits(): void {
  const el = document.getElementById('model-limits');
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
</script>

<template>
  <div
    class="lim-banner" v-if="visible" :class="level"
    role="status" data-limit-banner :data-banner-level="level"
  >
    <span class="lb-ico">
      <AppIcon :name="level === 'exceeded' ? 'ban' : 'warning'" :size="16" />
    </span>
    <div class="lb-body">
      <p class="lb-title" data-banner-title>{{ title }}</p>
      <p class="lb-sub" data-banner-sub>{{ subline }}</p>
      <p class="lb-advice" v-if="level === 'exceeded'" data-banner-advice>{{ ADVICE }}</p>
    </div>
    <div class="lb-side">
      <button type="button" class="lb-link" data-banner-all @click="scrollToLimits">查看全部（{{ count }}）</button>
      <button
        type="button" class="lb-close" data-banner-close
        aria-label="关闭这条提醒（不解除限额）" title="只隐藏这一次提醒，不解除限额"
        @click="dismiss"
      >
        <AppIcon name="close" :size="13" />
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 圆角矩形横幅：颜色只跟档位走（warn / exceeded），底色是 tokens.css 里那对 soft */
.lim-banner {
  display: flex; align-items: flex-start; gap: 10px;
  min-width: 0; padding: 11px 12px;
  border: 1px solid transparent; border-radius: var(--r-md);
  background: var(--warn-soft); color: var(--warn);
}
.lim-banner.exceeded { background: var(--danger-soft); color: var(--danger); }
.lb-ico { flex: none; display: grid; place-items: center; width: 20px; height: 20px; margin-top: 1px; }
.lb-body { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
.lb-title {
  margin: 0; font-size: calc(13px * var(--ts, 1)); font-weight: 650; line-height: 1.45;
  overflow-wrap: anywhere;
}
.lb-sub {
  margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.55;
  color: color-mix(in srgb, currentColor 78%, var(--text));
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere;
}
.lb-advice {
  margin: 2px 0 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.55;
  color: color-mix(in srgb, currentColor 86%, var(--text)); overflow-wrap: anywhere;
}
.lb-side { flex: none; display: flex; align-items: center; gap: 4px; }
.lb-link {
  appearance: none; border: 0; background: transparent; padding: 2px 6px;
  border-radius: var(--r-xs); cursor: pointer; white-space: nowrap;
  font-family: inherit; font-size: calc(11.5px * var(--ts, 1)); font-weight: 600;
  color: inherit; text-decoration: underline; text-underline-offset: 2px;
}
.lb-link:hover { background: color-mix(in srgb, currentColor 12%, transparent); }
.lb-link:focus-visible { outline: none; box-shadow: var(--ring); }
.lb-close {
  appearance: none; border: 0; background: transparent; cursor: pointer;
  width: 22px; height: 22px; display: grid; place-items: center;
  border-radius: var(--r-xs); color: inherit; opacity: .8;
}
.lb-close:hover { opacity: 1; background: color-mix(in srgb, currentColor 12%, transparent); }
.lb-close:focus-visible { outline: none; box-shadow: var(--ring); }
.icon { display: block; }

/* 窄屏：链接与 × 落到正文下面一行，横幅不横向顶出去 */
@media (max-width: 720px) {
  .lim-banner { flex-wrap: wrap; }
  .lb-body { flex-basis: calc(100% - 34px); }
  .lb-side { margin-left: 30px; }
}
</style>
