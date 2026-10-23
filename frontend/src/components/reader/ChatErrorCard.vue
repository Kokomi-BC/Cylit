<script setup lang="ts">
/**
 * 对话页的**错误卡**（取代以前那一行红字）。
 *
 * 为什么值得一张卡：以前界面上只剩一句 `message`，于是"你自己设的额度用完了"和"上游把我们限流了"
 * 长得一模一样 —— 用户唯一能做的动作就是再点一次发送。卡片只做三件事：
 * 1. 把后端给的 `kind` 翻成人话标题（限额 / 限流 / 欠费 / 鉴权 / 参数 / 上游 5xx / 超时 / 断网 /
 *    流被截断 / 没配模型 / 未知）；
 * 2. 把**机器可核对**的东西（HTTP 状态、上游错误码与原文、模型、trace_id）收进默认收起的
 *    `<details>`：不占视线，要查的时候一个字都不少；
 * 3. 什么也不猜：字段没给就**整行不渲染**（绝不写"未知错误码"这种假值）。
 *
 * **没有「重试」按钮**：重发会再落一次库，重试机制没做就不给控件；
 * 限额卡也没有「去调额度」的跳转 —— 那是设置页的事，按钮只会把人带出这段对话。
 *
 * 这份组件只读 `ChatErrorView`（形状见 `api/types.ts`），不做任何分类判断：
 * 分类是后端/`postSse` 的职责，这里判一遍必然出现"两套口径"。
 */
import { computed } from 'vue';
import type { ChatErrorView } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{ error: ChatErrorView }>();

interface Row { key: string; label: string; value: string; mono?: boolean }

/** 一句标题就够了：细节全在下面两段里，别在这里堆状态码 */
const KIND_TITLE: Record<string, string> = {
  limit: '本周期额度已用尽，这次提问被拦下了',
  rate_limit: '上游接口限流了',
  quota: '上游账户额度不足',
  auth: '上游鉴权失败',
  bad_request: '上游不接受这个请求',
  server: '上游服务出错了',
  timeout: '等上游回复超时了',
  network: '连不上后端',
  stream: '回答流中途断了',
  not_configured: '还没有可用的模型',
  unknown: '这次提问失败了',
};

const PERIOD_WORD: Record<string, string> = { day: '今日', week: '本周', month: '本月' };
const SYMBOL: Record<string, string> = { CNY: '¥', USD: '$' };

const isLimit = computed(() => props.error.kind === 'limit');
const title = computed(() => KIND_TITLE[props.error.kind] ?? KIND_TITLE.unknown);

/** 只认真实数字：`0` 是有效值，`null` / 缺键 / 空串一律当"没给" */
function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}
function text(v: unknown): string {
  return typeof v === 'string' ? v.trim() : '';
}
function money(v: number, currency: string): string {
  const code = currency.toUpperCase();
  return (SYMBOL[code] ?? (code ? code + ' ' : '')) + v.toFixed(2);
}

/* 顶部两枚"一眼要看到"的徽标：HTTP 状态、上游错误码。缺哪个就少哪个，绝不留空位。 */
const chips = computed<Row[]>(() => {
  const e = props.error;
  const out: Row[] = [];
  const status = num(e.http_status);
  if (status !== null) out.push({ key: 'http_status', label: '', value: 'HTTP ' + status, mono: true });
  if (text(e.provider_code)) out.push({ key: 'provider_code', label: '', value: text(e.provider_code), mono: true });
  return out;
});

/* 限额卡的正文：字段名照 `backend/services/model/limits.py` 的 `_snapshot_in` 快照，
   前端一个数都不重算（两套算法迟早对不上，用户会以为限额坏了）。 */
const limitRows = computed<Row[]>(() => {
  const l = props.error.limit;
  if (!l) return [];
  const currency = text(l.currency);
  const rows: Row[] = [];
  if (text(l.label)) rows.push({ key: 'limit_label', label: '受限对象', value: text(l.label) });
  if (text(l.period)) {
    rows.push({ key: 'limit_period', label: '周期', value: PERIOD_WORD[text(l.period)] ?? text(l.period) });
  }
  const spent = num(l.spent);
  if (spent !== null) rows.push({ key: 'limit_spent', label: '本周期已用', value: money(spent, currency), mono: true });
  const amount = num(l.amount);
  if (amount !== null) rows.push({ key: 'limit_amount', label: '额度', value: money(amount, currency), mono: true });
  const percent = num(l.percent);
  if (percent !== null) {
    rows.push({ key: 'limit_percent', label: '已用比例', value: percent.toFixed(1) + '%', mono: true });
  }
  const remaining = num(l.remaining);
  if (remaining !== null) {
    rows.push({ key: 'limit_remaining', label: '剩余', value: money(remaining, currency), mono: true });
  }
  const reset = text(l.reset_at) || text(l.period_end);
  if (reset) rows.push({ key: 'limit_reset', label: '重置时间', value: reset, mono: true });
  return rows;
});

/* 收起区：给人念给客服 / 贴进 issue 的那几个值。上游原文单独一行放宽，不截断（后端已脱敏且 ≤500）。 */
const detailRows = computed<Row[]>(() => {
  const e = props.error;
  const rows: Row[] = [];
  if (text(e.code)) rows.push({ key: 'code', label: '错误码', value: text(e.code), mono: true });
  if (text(e.provider_type)) rows.push({ key: 'provider_type', label: '上游类型', value: text(e.provider_type), mono: true });
  if (text(e.provider_message)) rows.push({ key: 'provider_message', label: '上游原文', value: text(e.provider_message) });
  if (text(e.model)) rows.push({ key: 'model', label: '模型', value: text(e.model), mono: true });
  if (text(e.provider_label)) rows.push({ key: 'provider_label', label: '供应商', value: text(e.provider_label) });
  const retryAfter = num(e.retry_after);
  if (retryAfter !== null) {
    rows.push({ key: 'retry_after', label: '建议等待', value: retryAfter + ' 秒', mono: true });
  }
  if (e.retryable) rows.push({ key: 'retryable', label: '能否重试', value: '过一会儿可以再问一次' });
  if (text(e.trace_id)) rows.push({ key: 'trace_id', label: 'trace_id', value: text(e.trace_id), mono: true });
  return rows;
});
</script>

<template>
  <div
    class="ec" :class="{ 'is-limit': isLimit }" role="alert"
    data-error-card :data-error-kind="error.kind"
  >
    <span class="ec-ico"><AppIcon name="warning" :size="14" /></span>
    <div class="ec-main">
      <p class="ec-title" data-error-title>{{ title }}</p>
      <p v-if="error.message" class="ec-msg" data-error-message>{{ error.message }}</p>

      <!-- 限额 = 我们自己的闸门，不是故障。这句话必须写在卡上：
           否则用户第一反应是"服务坏了"，去重装 / 换网，而真正要做的是调额度或等周期翻篇。 -->
      <p v-if="isLimit" class="ec-note" data-error-note>
        这是你自己设的额度（设置 · 模型 · 限额），不是上游或服务故障；周期翻篇会自动重置。
      </p>

      <div v-if="limitRows.length" class="ec-rows" data-error-limit>
        <div v-for="r in limitRows" :key="r.key" class="ec-row" :data-error-row="r.key">
          <span class="ec-k">{{ r.label }}</span>
          <span class="ec-v" :class="{ mono: r.mono }">{{ r.value }}</span>
        </div>
      </div>

      <div v-if="chips.length" class="ec-chips">
        <span
          v-for="c in chips" :key="c.key" class="ec-chip"
          :class="{ mono: c.mono }" :data-error-row="c.key"
        >{{ c.value }}</span>
      </div>

      <details v-if="detailRows.length" class="ec-det" data-error-details>
        <summary class="ec-sum">技术细节</summary>
        <div class="ec-rows">
          <div v-for="r in detailRows" :key="r.key" class="ec-row" :data-error-row="r.key">
            <span class="ec-k">{{ r.label }}</span>
            <span class="ec-v" :class="{ mono: r.mono }">{{ r.value }}</span>
          </div>
        </div>
      </details>
    </div>
  </div>
</template>

<style scoped>
/* 颜色只有两档：限额 = 琥珀（不是故障），其余 = 仓库里现有的告警色（--danger / --danger-soft），
   圆角与内距沿用 LimitBanner / badge 那一套令牌，不另造视觉语言。 */
.ec {
  display: flex; align-items: flex-start; gap: 8px;
  margin: 6px 0 2px; padding: 10px 11px;
  border: 1px solid color-mix(in srgb, var(--danger) 32%, var(--border));
  border-radius: var(--radius);
  background: var(--danger-soft); color: var(--danger);
}
.ec.is-limit {
  border-color: color-mix(in srgb, var(--warn) 32%, var(--border));
  background: var(--warn-soft); color: var(--warn);
}
.ec-ico { flex: none; display: grid; place-items: center; width: 18px; height: 18px; margin-top: 1px; }
.ec-main { display: flex; flex-direction: column; gap: 5px; min-width: 0; flex: 1; }
.ec-title {
  margin: 0; font-size: calc(13px * var(--ts, 1)); font-weight: 650; line-height: 1.45;
  overflow-wrap: anywhere;
}
/* 正文用中性色：只有标题吃告警色，否则整块红/黄会让人读不下去 */
.ec-msg, .ec-note {
  margin: 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.6;
  color: color-mix(in srgb, currentColor 22%, var(--text)); overflow-wrap: anywhere;
  user-select: text;
}
.ec-note { color: color-mix(in srgb, currentColor 55%, var(--text)); }
.ec-rows { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.ec-row { display: flex; gap: 8px; min-width: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.55; }
.ec-k { flex: none; color: color-mix(in srgb, currentColor 55%, var(--text)); }
.ec-v { min-width: 0; color: var(--text); overflow-wrap: anywhere; user-select: text; }
.ec-v.mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.ec-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.ec-chip {
  padding: 1px 6px; border-radius: var(--radius-sm);
  font-size: calc(11px * var(--ts, 1)); line-height: 1.55;
  background: color-mix(in srgb, currentColor 12%, transparent); color: inherit;
  user-select: text;
}
.ec-det { min-width: 0; }
.ec-sum {
  cursor: pointer; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6;
  color: color-mix(in srgb, currentColor 62%, var(--text));
}
.ec-sum:hover { color: inherit; }
.ec-det > .ec-rows { margin-top: 4px; }
</style>
