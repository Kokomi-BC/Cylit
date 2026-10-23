<script setup lang="ts">
/**
 * 模型页 · **限额**块（裸块：不描边、不投影、不填底 —— 与「消耗统计 / 字体设置」同一观感，
 * 只用留白与 1px 细线分区）。
 *
 * ## 口径（后端 services/model/limits.py 是唯一真源）
 * · spent / remaining / percent **一律用后端返回值**，前端绝不自算额度：两套算法迟早对不上，
 *   用户会以为限额坏了；amount 只用来回填输入框。
 * · spent 已含**未过期预留**（与闸门同一口径）：列表上说还剩多少，说了就真能用。
 * · usable=false → 该作用域下还有模型没配单价，这条限额**照样会拦**，只是拦不准，必须说出来；
 *   unpriced_rows>0 → 窗口内有算不出钱的调用，已用金额只是**下限**；
 *   foreign_currency_rows>0 → 那些行不计入累计（不同币种永不相加），但也不能装作不存在。
 * · 保存失败是 400，**后端 message 原样显示**（「该作用域下还有模型没配单价，不能设限额…」
 *   这句话前端造不出来，也不该造），且**保留用户已经填好的内容**。
 *
 * ## 候选来源（不发明接口）
 * GET /models（模型）、GET /models/keys（密钥）、GET /models/providers（厂商）。
 * 第一次打开编辑行时才拉，平时不白占一次请求。
 */
import { computed, reactive, ref } from 'vue';
import { api } from '@/api/client';
import type {
  ModelLimitItem, ModelLimitPeriod, ModelLimitScope, ModelLimitsSummary,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{ summary: ModelLimitsSummary | null; loading?: boolean }>();
const emit = defineEmits<{ (e: 'refresh'): void }>();

const SCOPES: Array<{ id: ModelLimitScope; label: string; hint: string }> = [
  { id: 'model', label: '模型', hint: '这一条模型自己的累计' },
  { id: 'key', label: '密钥', hint: '用这个密钥的全部调用合计（跨模型）' },
  { id: 'vendor', label: '厂商', hint: '这家所有模型合计' },
];
const PERIODS: Array<{ id: ModelLimitPeriod | 'none'; label: string }> = [
  { id: 'none', label: '不限额' },
  { id: 'day', label: '每日' },
  { id: 'week', label: '每周' },
  { id: 'month', label: '每月' },
];
const SCOPE_CN: Record<ModelLimitScope, string> = { model: '模型', key: '密钥', vendor: '厂商' };
const PERIOD_CN: Record<ModelLimitPeriod, string> = { day: '每日', week: '每周', month: '每月' };
const SYMBOL: Record<string, string> = { CNY: '¥', USD: '$' };

interface Option { id: string; label: string; hint?: string }

/* ---------------------------- 后端数据 ---------------------------- */

const items = computed<ModelLimitItem[]>(() => props.summary?.items ?? []);
const level = computed<'none' | 'warn' | 'exceeded'>(() => props.summary?.level ?? 'none');
const hasAny = computed<boolean>(() => items.value.length > 0);
const enabledCount = computed<number>(() => items.value.filter((it) => it.enabled).length);
/** 现状条只讲一条：summary.most_urgent（没有启用的限额时退回第一条启用的） */
const focusItem = computed<ModelLimitItem | null>(
  () => props.summary?.most_urgent ?? items.value.find((it) => it.enabled) ?? null,
);

const levelSentence = computed<string>(() => {
  if (level.value === 'exceeded') return '已有限额用尽，新请求会被本地闸门拦住（429）';
  if (level.value === 'warn') return '已有限额用掉 80% 以上，本周期请注意';
  return '所有限额都在阈值内（到 80% 才算接近）';
});

/* ---------------------------- 候选实体 ---------------------------- */

const targets = reactive<Record<ModelLimitScope, Option[]>>({ model: [], key: [], vendor: [] });
const targetsLoaded = ref(false);
const targetsLoading = ref(false);
const targetsError = ref('');

async function loadTargets(): Promise<void> {
  if (targetsLoaded.value || targetsLoading.value) return;
  targetsLoading.value = true;
  targetsError.value = '';
  try {
    const [models, keys, providers] = await Promise.all([
      api.get<{ items?: Array<Record<string, any>> }>('/models'),
      api.get<{ items?: Array<Record<string, any>> }>('/models/keys'),
      api.get<{ providers?: Array<Record<string, any>> }>('/models/providers'),
    ]);
    targets.model = (models?.items ?? [])
      .filter((m) => m && m.id)
      .map((m) => ({
        id: String(m.id),
        label: String(m.name || m.model_name || m.id),
        hint: m.has_price === false ? '未配单价' : '',
      }));
    targets.key = (keys?.items ?? [])
      .filter((k) => k && k.id)
      .map((k) => ({ id: String(k.id), label: String(k.label || k.id) }));
    targets.vendor = (providers?.providers ?? [])
      .filter((p) => p && p.id)
      .map((p) => ({ id: String(p.id), label: String(p.label || p.id) }));
    targetsLoaded.value = true;
  } catch (err: any) {
    targetsError.value = '候选列表读取失败：' + String(err?.message ?? err);
  } finally {
    targetsLoading.value = false;
  }
}

/* ---------------------------- 编辑行 ---------------------------- */

const open = ref(false);
const editingId = ref('');
const saving = ref(false);
const formError = ref('');
const listError = ref('');
const pendingRemove = ref('');
const form = reactive({
  scope: 'model' as ModelLimitScope,
  scopeId: '',
  period: 'day' as ModelLimitPeriod | 'none',
  amount: '',
  currency: 'CNY',
});

const targetOptions = computed<Option[]>(() => {
  const list = targets[form.scope];
  /* 厂商级的说明写进下拉选项本身：只挂在提示行里，选的人看不见 */
  if (form.scope !== 'vendor') return list;
  return list.map((o) => ({ id: o.id, label: o.label + '（这家所有模型合计）', hint: o.hint }));
});

const scopeHint = computed<string>(() => {
  if (form.scope === 'vendor') return '厂商级 = 这家所有模型合计（跨模型，不是每条单独算）';
  if (form.scope === 'key') return '密钥级 = 用这个密钥的全部调用合计（跨模型）';
  return '模型级 = 这一条模型自己的累计';
});

const amountText = computed<string>(() => form.amount.trim());
const amountDecimals = computed<number>(() => {
  const m = /^\d+(?:\.(\d+))?$/.exec(amountText.value);
  return m && m[1] ? m[1].length : 0;
});
/** 两位小数只是**提示**，不拦提交：后端按你输入的原值存。 */
const amountHint = computed<string>(() => (amountDecimals.value > 2 ? '建议保留两位小数（不强制）' : ''));

const canSubmit = computed<boolean>(() => {
  if (saving.value || !form.scopeId) return false;
  if (form.period === 'none') return true;
  const n = Number(amountText.value);
  return amountText.value !== '' && Number.isFinite(n) && n > 0;
});

function ensureScopeTarget(): void {
  const list = targets[form.scope];
  /* 只有唯一候选时替你选上（本地库常常只有一个模型）；多个候选一律留空，别替你决定 */
  if (!form.scopeId && list.length === 1) form.scopeId = list[0].id;
}

async function openCreate(): Promise<void> {
  editingId.value = '';
  form.scope = 'model';
  form.scopeId = '';
  form.period = 'day';
  form.amount = '';
  form.currency = 'CNY';
  formError.value = '';
  pendingRemove.value = '';
  open.value = true;
  await loadTargets();
  ensureScopeTarget();
}

function openEdit(it: ModelLimitItem): void {
  editingId.value = it.id;
  form.scope = it.scope;
  form.scopeId = it.scope_id;
  form.period = it.period;
  form.amount = String(it.amount);
  form.currency = it.currency || 'CNY';
  formError.value = '';
  pendingRemove.value = '';
  open.value = true;
  void loadTargets().then(ensureScopeTarget);
}

function pickScope(scope: ModelLimitScope): void {
  if (form.scope === scope) return;
  form.scope = scope;
  form.scopeId = '';
  formError.value = '';
  ensureScopeTarget();
}

function closeEdit(): void {
  open.value = false;
  editingId.value = '';
  formError.value = '';
  pendingRemove.value = '';
  saving.value = false;
}

async function save(): Promise<void> {
  if (saving.value) return;
  formError.value = '';
  if (!form.scopeId) {
    formError.value = '先选一个目标（模型 / 密钥 / 厂商）';
    return;
  }
  const scope = form.scope;
  const scopeId = form.scopeId;

  /* 「不限额」= 删掉这条（period:none 是后端唯一能表达"从此不限额"的写法） */
  if (form.period === 'none') {
    saving.value = true;
    try {
      await api.put('/models/limits', { scope, scope_id: scopeId, enabled: false, period: 'none' });
      closeEdit();
      emit('refresh');
    } catch (err: any) {
      formError.value = String(err?.message ?? err);
    } finally {
      saving.value = false;
    }
    return;
  }

  const amount = Number(amountText.value);
  if (amountText.value === '' || !Number.isFinite(amount) || amount <= 0) {
    formError.value = '金额要填一个大于 0 的数字（要"不限额"请切到「不限额」）';
    return;
  }

  saving.value = true;
  try {
    await api.put('/models/limits', {
      scope, scope_id: scopeId, enabled: true,
      period: form.period, amount, currency: form.currency, note: '',
    });
    closeEdit();
    emit('refresh');
  } catch (err: any) {
    /* 400 的 message 是后端写的（「该作用域下还有模型没配单价，不能设限额：X」）：
       原样放在行内，输入框一个字段都不清空，改完能直接再存。 */
    formError.value = String(err?.message ?? err);
  } finally {
    saving.value = false;
  }
}

/* 删除一律两步：第一下变「确认删除」，第二下才真删（不是弹窗，也不留一个一点就没的按钮） */
async function onEditorDelete(): Promise<void> {
  const id = editingId.value;
  if (!id) return;
  if (pendingRemove.value !== id) {
    pendingRemove.value = id;
    return;
  }
  listError.value = '';
  try {
    await api.del('/models/limits/' + encodeURIComponent(id));
    closeEdit();
    emit('refresh');
  } catch (err: any) {
    formError.value = String(err?.message ?? err);
  }
}

async function onRowDelete(it: ModelLimitItem): Promise<void> {
  if (pendingRemove.value !== it.id) {
    pendingRemove.value = it.id;
    return;
  }
  pendingRemove.value = '';
  listError.value = '';
  try {
    await api.del('/models/limits/' + encodeURIComponent(it.id));
    if (editingId.value === it.id) closeEdit();
    emit('refresh');
  } catch (err: any) {
    listError.value = '删除失败：' + String(err?.message ?? err);
  }
}

/* ---------------------------- 显示格式 ---------------------------- */

function money(value: number, currency: string): string {
  const n = Number(value);
  const v = Number.isFinite(n) ? n : 0;
  const code = String(currency || '').toUpperCase();
  return (SYMBOL[code] ?? (code ? code + ' ' : '')) + v.toFixed(2);
}
function pct(value: number): string {
  const n = Number(value);
  return (Number.isFinite(n) ? n : 0).toFixed(1) + '%';
}
function barWidth(value: number): string {
  const n = Number(value);
  return Math.min(100, Math.max(0, Number.isFinite(n) ? n : 0)) + '%';
}
function tone(value: number): string {
  const n = Number(value);
  if (n >= 100) return 'danger';
  if (n >= 80) return 'warn';
  return 'ok';
}
</script>

<template>
  <section class="limits" id="model-limits" data-limit-block>
    <header class="limits-head">
      <h3 class="limits-title"><AppIcon name="ban" :size="14" /><span>限额</span></h3>
      <p class="limits-desc">日 / 周 / 月 × 模型 / 密钥 / 厂商；用尽即拦（429，不是供应商的错误）</p>
      <span class="spacer"></span>
      <button type="button" class="btn sm" data-limit-add v-if="!open" @click="openCreate">
        <AppIcon name="add" :size="13" />新增限额
      </button>
    </header>

    <p class="limits-load" v-if="loading && !summary">限额载入中…</p>

    <!-- ① 现状条：一条都没有 → 一行字 + 真按钮（不弹假窗口，点了就在这一块里展开编辑行） -->
    <div class="lim-status is-empty" v-else-if="!hasAny" data-limit-empty>
      <span class="ls-none">还没有设置限额</span>
      <button type="button" class="btn sm" data-limit-add-empty @click="openCreate">
        <AppIcon name="add" :size="13" />新增限额
      </button>
    </div>

    <!-- ② 有配置 → 一句话 + 百分比条 + 已用/限额 + 重置时间 -->
    <div class="lim-status" v-else :data-limit-level="level">
      <div class="ls-line">
        <span class="ls-dot" :class="level === 'exceeded' ? 'danger' : level" />
        <span class="ls-text">{{ levelSentence }}</span>
        <span class="spacer"></span>
        <span class="ls-count">{{ enabledCount }} 条启用 / 共 {{ items.length }} 条</span>
      </div>
      <template v-if="focusItem">
        <div class="ls-bar bar" role="progressbar" :aria-valuenow="focusItem.percent" aria-valuemin="0" aria-valuemax="100"
             :aria-label="'限额已用 ' + pct(focusItem.percent)">
          <span class="bar-fill" :class="tone(focusItem.percent)" :style="{ width: barWidth(focusItem.percent) }" />
        </div>
        <div class="ls-meta">
          <span class="mono">已用 {{ money(focusItem.spent, focusItem.currency) }} / 限额 {{ money(focusItem.amount, focusItem.currency) }}（{{ focusItem.currency }}）</span>
          <span class="sep">·</span>
          <span class="mono">{{ pct(focusItem.percent) }}</span>
          <span class="sep">·</span>
          <span>{{ PERIOD_CN[focusItem.period] }}重置 {{ focusItem.reset_at || '—' }}</span>
        </div>
      </template>
      <div class="ls-meta" v-else>
        <span>已配置的限额都处于停用状态（停用的限额不拦任何调用）</span>
      </div>
    </div>

    <!-- ③ 新增 / 编辑行：就在这一块里展开，不是弹窗 -->
    <div class="lim-editor" v-if="open" data-limit-editor>
      <div class="le-row">
        <span class="le-k">作用域</span>
        <div class="seg sq" role="group" aria-label="限额作用域">
          <button
            v-for="s in SCOPES" :key="s.id" type="button"
            :class="{ active: form.scope === s.id }" :data-limit-scope="s.id"
            :aria-pressed="form.scope === s.id" :title="s.hint"
            @click="pickScope(s.id)"
          >{{ s.label }}</button>
        </div>
        <span class="le-hint">{{ scopeHint }}</span>
      </div>

      <div class="le-row">
        <span class="le-k">目标</span>
        <select class="sel" v-model="form.scopeId" :disabled="targetsLoading" data-limit-target
                :aria-label="SCOPE_CN[form.scope] + '目标'">
          <option value="">{{ targetsLoading ? '读取候选…' : '选择' + SCOPE_CN[form.scope] }}</option>
          <option v-for="o in targetOptions" :key="o.id" :value="o.id">{{ o.label }}{{ o.hint ? '（' + o.hint + '）' : '' }}</option>
        </select>
        <span class="le-hint warn" v-if="targetsError" data-limit-targets-error>{{ targetsError }}</span>
        <span class="le-hint" v-else-if="!targetsLoading && !targetOptions.length">这一级还没有可选的{{ SCOPE_CN[form.scope] }}</span>
      </div>

      <div class="le-row">
        <span class="le-k">周期</span>
        <div class="seg sq" role="group" aria-label="限额周期">
          <button
            v-for="p in PERIODS" :key="p.id" type="button"
            :class="{ active: form.period === p.id }" :data-limit-period="p.id"
            :aria-pressed="form.period === p.id"
            @click="form.period = p.id"
          >{{ p.label }}</button>
        </div>
        <span class="le-hint" v-if="form.period === 'none'" data-limit-none-hint>选「不限额」保存 = 停用（这条限额会被删掉）</span>
      </div>

      <!-- 不限额时金额输入整行不显示：一个不限额的金额填了也没意义 -->
      <div class="le-row" v-if="form.period !== 'none'" data-limit-amount-row>
        <span class="le-k">金额</span>
        <input class="inp" type="text" inputmode="decimal" v-model="form.amount"
               placeholder="例如 10.00" data-limit-amount aria-label="限额金额">
        <select class="sel cur" v-model="form.currency" data-limit-currency aria-label="币种">
          <option value="CNY">CNY（¥）</option>
          <option value="USD">USD（$）</option>
        </select>
        <span class="le-hint" v-if="amountHint">{{ amountHint }}</span>
      </div>

      <p class="le-err" v-if="formError" data-limit-error>{{ formError }}</p>
      <p class="le-err" v-else-if="listError">{{ listError }}</p>

      <div class="le-actions">
        <button type="button" class="btn sm primary" :disabled="!canSubmit" data-limit-save @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
        <button type="button" class="btn sm" data-limit-cancel @click="closeEdit">取消</button>
        <span class="spacer"></span>
        <button type="button" class="btn sm danger" v-if="editingId" data-limit-delete @click="onEditorDelete">
          <AppIcon name="trash" :size="13" />{{ pendingRemove === editingId ? '确认删除' : '删除' }}
        </button>
      </div>
    </div>

    <!-- ④ 已配置列表 -->
    <p class="lim-list-err" v-if="listError && !open" data-limit-list-error>{{ listError }}</p>
    <div class="lim-list" v-if="hasAny" data-limit-list>
      <div
        class="lim-row" v-for="it in items" :key="it.id"
        :class="{ off: !it.enabled }" :data-limit-row="it.scope + ':' + it.scope_id"
      >
        <div class="lr-top">
          <span class="lr-scope">{{ SCOPE_CN[it.scope] }}</span>
          <span class="sep">·</span>
          <span class="lr-name truncate" :title="it.scope_id">{{ it.label }}</span>
          <span class="badge dim" v-if="!it.enabled">已停用</span>
          <span class="spacer"></span>
          <button type="button" class="lnk" :data-limit-edit="it.id" @click="openEdit(it)">编辑</button>
          <button type="button" class="lnk danger" :data-limit-remove="it.id" @click="onRowDelete(it)">
            {{ pendingRemove === it.id ? '确认删除' : '删除' }}
          </button>
        </div>

        <div class="lr-meta">
          <span>{{ PERIOD_CN[it.period] }}</span>
          <span class="sep">·</span>
          <span class="mono">限额 {{ money(it.amount, it.currency) }}</span>
          <span class="sep">·</span>
          <span class="mono">已用 {{ money(it.spent, it.currency) }}</span>
          <span class="sep">·</span>
          <span class="mono">剩余 {{ money(it.remaining, it.currency) }}</span>
          <span class="sep">·</span>
          <span class="mono lr-pct" :class="tone(it.percent)">{{ pct(it.percent) }}</span>
          <span class="sep">·</span>
          <span class="lr-reset">重置 {{ it.reset_at || '—' }}</span>
        </div>

        <div class="lr-bar bar" role="progressbar" :aria-valuenow="it.percent" aria-valuemin="0" aria-valuemax="100"
             :aria-label="it.label + ' 限额已用 ' + pct(it.percent)">
          <span class="bar-fill" :class="it.enabled ? tone(it.percent) : 'off'" :style="{ width: barWidth(it.percent) }" />
        </div>

        <!-- 诚实提示：这三句都是"数字看起来比实际小"的原因，藏起来就等于骗人 -->
        <p class="lr-note" v-if="!it.usable" data-limit-note="unpriced-models">
          <AppIcon name="warning" :size="12" />
          <span>该作用域下还有模型没配单价，这条限额拦不准（先去配单价）</span>
        </p>
        <p class="lr-note" v-if="it.unpriced_rows > 0" data-limit-note="unpriced-rows">
          <AppIcon name="info" :size="12" />
          <span>另有 {{ it.unpriced_rows }} 次调用未计费，已用金额只是下限</span>
        </p>
        <p class="lr-note" v-if="it.foreign_currency_rows > 0" data-limit-note="foreign">
          <AppIcon name="info" :size="12" />
          <span>另有 {{ it.foreign_currency_rows }} 次其它币种的调用未计入（不同币种不计入同一累计）</span>
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 裸块：不描边、不投影、不填底 —— 与「消耗统计 / 字体设置」同一观感 */
.limits {
  min-width: 0; display: flex; flex-direction: column; gap: 10px;
  padding: 0; border: 0; background: transparent; box-shadow: none;
}
.limits-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.limits-title {
  display: inline-flex; align-items: center; gap: 7px; margin: 0;
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 650; color: var(--text); white-space: nowrap;
}
.limits-desc {
  margin: 0; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.icon { display: block; }
.limits-load { margin: 0; font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }

/* ---------------- 现状条 ---------------- */
.lim-status {
  display: flex; flex-direction: column; gap: 7px;
  padding: 10px 12px; border-radius: var(--r-sm);
  background: var(--bg-sunken);
}
.lim-status.is-empty { flex-direction: row; align-items: center; gap: 12px; }
.ls-none { font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint); }
.ls-line { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.ls-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--ok); }
.ls-dot.warn { background: var(--warn); }
.ls-dot.danger { background: var(--danger); }
.ls-text { font-size: calc(12.5px * var(--ts, 1)); color: var(--text); font-weight: 600; }
.ls-count { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.ls-meta {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim);
}

/* 百分比条：颜色跟档位走（ok / warn / danger），宽度就是后端给的 percent（封顶 100） */
.bar { height: 6px; border-radius: 999px; background: var(--bg-hover); overflow: hidden; }
.ls-bar { height: 6px; }
.bar-fill { display: block; height: 100%; border-radius: 999px; background: var(--ok); transition: width var(--dur) var(--ease); }
.bar-fill.warn { background: var(--warn); }
.bar-fill.danger { background: var(--danger); }
.bar-fill.off { background: var(--text-faint); opacity: .5; }

/* ---------------- 编辑行 ---------------- */
.lim-editor {
  display: flex; flex-direction: column; gap: 8px;
  padding: 10px 0 2px; border-top: 1px solid var(--divider);
}
.le-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.le-k {
  flex: none; width: 42px; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}
.lim-editor .seg.sq { height: 26px; }
.lim-editor .seg.sq button { height: 20px; padding: 0 10px; font-size: calc(11.5px * var(--ts, 1)); }
.lim-editor .seg.sq button:focus-visible { outline: none; box-shadow: var(--ring); }
.sel {
  height: 26px; max-width: 300px; min-width: 120px; padding: 0 8px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); cursor: pointer;
}
.sel.cur { max-width: 130px; min-width: 96px; }
.sel:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.inp {
  height: 26px; width: 128px; padding: 0 9px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); font-variant-numeric: tabular-nums;
}
.inp:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.le-hint { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); min-width: 0; }
.le-hint.warn { color: var(--warn); }
.le-err {
  margin: 0; padding: 6px 10px; border-radius: var(--r-xs);
  background: var(--danger-soft); color: var(--danger);
  font-size: calc(12px * var(--ts, 1)); line-height: 1.5; white-space: pre-wrap; word-break: break-word;
}
.le-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

/* ---------------- 列表 ---------------- */
.lim-list { display: flex; flex-direction: column; }
.lim-list-err {
  margin: 0; font-size: calc(12px * var(--ts, 1)); color: var(--danger);
}
.lim-row {
  display: flex; flex-direction: column; gap: 6px;
  padding: 10px 0; border-top: 1px solid var(--divider);
  min-width: 0;
}
.lim-row.off { opacity: .6; }
.lr-top { display: flex; align-items: center; gap: 6px; min-width: 0; }
.lr-scope {
  flex: none; padding: 1px 6px; border-radius: var(--r-xs);
  background: var(--bg-sunken); color: var(--text-dim);
  font-size: calc(11px * var(--ts, 1));
}
.lr-name { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; color: var(--text); min-width: 0; }
.lr-meta {
  display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim);
}
.lr-reset { color: var(--text-faint); }
.lr-pct.warn { color: var(--warn); }
.lr-pct.danger { color: var(--danger); }
.lr-bar { max-width: 100%; }
.lr-note {
  display: flex; align-items: flex-start; gap: 5px; margin: 0;
  font-size: calc(11px * var(--ts, 1)); color: var(--warn); line-height: 1.5;
}
.lr-note .icon { flex: none; margin-top: 2px; }
.sep { color: var(--text-faint); }
.mono { font-variant-numeric: tabular-nums; }
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.spacer { flex: 1; }

/* 文字链（不用 .btn：这两枚只是"改 / 删"，不该和保存按钮抢重量） */
.lnk {
  appearance: none; border: 0; background: transparent; padding: 2px 4px; border-radius: var(--r-xs);
  font-family: inherit; font-size: calc(11.5px * var(--ts, 1)); color: var(--accent-text); cursor: pointer;
}
.lnk:hover { background: var(--bg-hover); }
.lnk:focus-visible { outline: none; box-shadow: var(--ring); }
.lnk.danger { color: var(--danger); }
</style>
