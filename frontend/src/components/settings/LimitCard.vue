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
 *   这句话前端造不出来，也不该造），且**保留用户已经填好的内容**、卡片不关。
 *
 * ## 卡片（Modal）而不是行内展开
 * 新增 / 编辑都在 Modal.vue（与 ModelForm 同一个组件）里做：Esc / 点遮罩 = 取消。
 *
 * ## 模型选择按「名称 + 供应商」合并成一项
 * 后端限额是**按单个 model_config** 存的（scope=model 的 scope_id 就是一个配置 id），
 * 所以这里只是**显示层**合并：name（去首尾空白）+ provider 都相同的配置算一项；
 * 名字相同但供应商不同的**不合并**（文案里的供应商就是区分它们的东西）。
 * 选中一个合并项保存 = **为组里每个配置各写一条同样的限额**（各自独立累计，不是共享额度，
 * 卡片里明说 N 与这句话）；列表也把同一组合成一行，编辑 = 改组里每一条，删除 = 删组里每一条。
 *
 * ## 候选来源（不发明接口）
 * GET /models（模型）、GET /models/keys（密钥）、GET /models/providers（厂商）。
 * 打开卡片时必拉；列表里出现模型级限额时也拉（不然没法按组显示）。
 */
import { computed, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import type {
  ModelLimitItem, ModelLimitPeriod, ModelLimitScope, ModelLimitsSummary,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import { vendorColorIcon } from '@/assets/model-icons';

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

interface ModelRow { id: string; name: string; provider: string; modelName: string; hasPrice: boolean | null }
/** 一组同 name + 同 provider 的配置（后端限额按**单个配置**存，所以这里只是显示层的一项） */
interface ModelGroup { key: string; name: string; provider: string; providerLabel: string; ids: string[]; unpriced: boolean }

const modelRows = ref<ModelRow[]>([]);
const providerLabels = ref<Record<string, string>>({});
const keyOptions = ref<Option[]>([]);
const vendorOptions = ref<Option[]>([]);
const targetsLoaded = ref(false);
const targetsLoading = ref(false);
const targetsError = ref('');

/** 合并键：name（去首尾空白）与 provider 都相同才算同一项。中间放一个 NUL，避免拼接歧义。 */
function groupKeyOf(name: string, provider: string): string {
  return String(name ?? '').trim() + '\u0000' + String(provider ?? '').trim();
}

const modelById = computed<Record<string, ModelRow>>(() => {
  const map: Record<string, ModelRow> = {};
  for (const m of modelRows.value) map[m.id] = m;
  return map;
});

const modelGroups = computed<ModelGroup[]>(() => {
  const out: ModelGroup[] = [];
  const index = new Map<string, ModelGroup>();
  for (const m of modelRows.value) {
    const key = groupKeyOf(m.name, m.provider);
    let g = index.get(key);
    if (!g) {
      g = {
        key,
        name: m.name,
        provider: m.provider,
        providerLabel: providerLabels.value[m.provider] || m.provider || '（未标供应商）',
        ids: [],
        unpriced: false,
      };
      index.set(key, g);
      out.push(g);
    }
    g.ids.push(m.id);
    if (m.hasPrice === false) g.unpriced = true;
  }
  return out;
});

async function loadTargets(): Promise<void> {
  if (targetsLoading.value) return;
  if (targetsLoaded.value) return;
  targetsLoading.value = true;
  targetsError.value = '';
  try {
    const [models, keys, providers] = await Promise.all([
      api.get<{ items?: Array<Record<string, any>> }>('/models'),
      api.get<{ items?: Array<Record<string, any>> }>('/models/keys'),
      api.get<{ providers?: Array<Record<string, any>> }>('/models/providers'),
    ]);
    const labelMap: Record<string, string> = {};
    for (const p of providers?.providers ?? []) {
      if (p && p.id) labelMap[String(p.id)] = String(p.label || p.id);
    }
    providerLabels.value = labelMap;
    modelRows.value = (models?.items ?? [])
      .filter((m) => m && m.id)
      .map((m) => ({
        id: String(m.id),
        name: String(m.name || m.model_name || m.id),
        provider: String(m.provider || ''),
        modelName: String(m.model_name || ''),
        hasPrice: typeof m.has_price === 'boolean' ? m.has_price : null,
      }));
    keyOptions.value = (keys?.items ?? [])
      .filter((k) => k && k.id)
      .map((k) => ({ id: String(k.id), label: String(k.label || k.id) }));
    vendorOptions.value = (providers?.providers ?? [])
      .filter((p) => p && p.id)
      .map((p) => ({ id: String(p.id), label: String(p.label || p.id) }));
    targetsLoaded.value = true;
  } catch (err: any) {
    targetsError.value = '候选列表读取失败：' + String(err?.message ?? err);
  } finally {
    targetsLoading.value = false;
  }
}

/** 列表里出现模型级限额时就得拉一次模型（否则没法按「名称 + 供应商」合并行） */
const needsModels = computed<boolean>(() => items.value.some((it) => it.scope === 'model'));
watch(needsModels, (v) => { if (v) void loadTargets(); }, { immediate: true });

/* ---------------------------- 卡片 ---------------------------- */

const open = ref(false);
/** 编辑中的后端条目（空 = 新增）；编辑时目标固定，保存 / 删除都作用在这些条目上 */
const editingItems = ref<ModelLimitItem[]>([]);
const editing = computed<boolean>(() => editingItems.value.length > 0);
const editorTitle = computed<string>(() => (editing.value ? '编辑限额' : '新增限额'));

const saving = ref(false);
const formError = ref('');
const listError = ref('');
const pendingRemove = ref('');
const report = ref<{ ok: string[]; fails: Array<{ name: string; message: string }> } | null>(null);
const form = reactive({
  scope: 'model' as ModelLimitScope,
  scopeId: '',
  period: 'day' as ModelLimitPeriod | 'none',
  amount: '',
  currency: 'CNY',
});

const targetOptions = computed<Option[]>(() => {
  if (form.scope === 'key') return keyOptions.value;
  if (form.scope === 'model') {
    /* 模型：名称 + 间隔点 + 供应商 label（供应商名来自 /models/providers，不另造别名表） */
    return modelGroups.value.map((g) => ({
      id: g.key,
      label: g.name + ' · ' + g.providerLabel,
      hint: g.unpriced ? '未配单价' : '',
    }));
  }
  /* 厂商级的说明写进下拉选项本身：只挂在提示行里，选的人看不见 */
  return vendorOptions.value.map((o) => ({
    id: o.id, label: o.label + '（这家所有模型合计）', hint: o.hint,
  }));
});

/** 当前选择落到**后端条目**上会写几个配置（模型级合并项 = N；密钥 / 厂商 = 1） */
const selectedIds = computed<string[]>(() => {
  if (!form.scopeId) return [];
  if (form.scope !== 'model') return [form.scopeId];
  const g = modelGroups.value.find((x) => x.key === form.scopeId);
  return g ? [...g.ids] : [];
});
const selectedCount = computed<number>(() => selectedIds.value.length);
/** 这次保存 / 删除落在几条后端记录上（编辑时就是被编辑的那一组） */
const actionCount = computed<number>(() => (editing.value ? editingItems.value.length : selectedCount.value));

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
  if (saving.value) return false;
  if (editing.value) return true;
  if (!selectedCount.value) return false;
  if (form.period === 'none') return true;
  const n = Number(amountText.value);
  return amountText.value !== '' && Number.isFinite(n) && n > 0;
});

/* ---------- 小工具 ---------- */

function memberLabel(it: ModelLimitItem): string {
  const m = it.scope === 'model' ? modelById.value[it.scope_id] : null;
  if (m?.modelName) return m.modelName;
  if (m?.provider) return m.name;
  return it.label || it.scope_id;
}
function vendorIconOf(provider?: string | null): string {
  return vendorColorIcon(provider);
}

/* ---------- 打开 / 关闭 ---------- */

function resetForm(): void {
  formError.value = '';
  report.value = null;
  pendingRemove.value = '';
  saving.value = false;
}

async function openCreate(): Promise<void> {
  editingItems.value = [];
  resetForm();
  form.scope = 'model';
  form.scopeId = '';
  form.period = 'day';
  form.amount = '';
  form.currency = 'CNY';
  open.value = true;
  await loadTargets();
  ensureScopeTarget();
}

/** 选作用域：清掉上一个作用域的选择（模型级是合并键，密钥 / 厂商是实体 id） */
function pickScope(scope: ModelLimitScope): void {
  if (editing.value || form.scope === scope) return;
  form.scope = scope;
  form.scopeId = '';
  formError.value = '';
  ensureScopeTarget();
}

/** 编辑一组（模型级同 name+provider 的多条，或密钥 / 厂商的单条） */
async function openEdit(row: { scope: ModelLimitScope; items: ModelLimitItem[] }): Promise<void> {
  editingItems.value = [...row.items];
  resetForm();
  const first = row.items[0];
  form.scope = first.scope;
  form.period = first.period;
  form.amount = String(first.amount);
  form.currency = first.currency || 'CNY';
  form.scopeId = '';
  await loadTargets();
  /* 模型级：换成这一组的合并键（下拉里才有对应项）；取不到就留空，但保存仍按原条目走 */
  if (first.scope === 'model') {
    const m = modelById.value[first.scope_id];
    form.scopeId = m ? groupKeyOf(m.name, m.provider) : '';
  } else {
    form.scopeId = first.scope_id;
  }
  open.value = true;
}

function closeEditor(): void {
  open.value = false;
  editingItems.value = [];
  resetForm();
}

function ensureScopeTarget(): void {
  /* 只有唯一候选时替你选上（本地库常常只有一个模型）；多个候选一律留空，别替你决定 */
  if (form.scopeId) return;
  const list = targetOptions.value;
  if (list.length === 1) form.scopeId = list[0].id;
}

/* ---------- 保存 ---------- */

/** 保存落到哪些后端条目上；name 只用于失败时点名 */
function saveTargets(): Array<{ id: string; name: string }> {
  if (editing.value) {
    return editingItems.value.map((it) => ({ id: it.scope_id, name: memberLabel(it) }));
  }
  if (form.scope === 'model') {
    const g = modelGroups.value.find((x) => x.key === form.scopeId);
    if (!g) return [];
    return g.ids.map((id) => {
      const m = modelById.value[id];
      return { id, name: (m?.modelName || m?.name || id) };
    });
  }
  const opt = targetOptions.value.find((o) => o.id === form.scopeId);
  return [{ id: form.scopeId, name: opt?.label || form.scopeId }];
}

async function save(): Promise<void> {
  if (saving.value) return;
  formError.value = '';
  report.value = null;
  const targets = saveTargets();
  if (!targets.length) {
    formError.value = '先选一个目标（模型 / 密钥 / 厂商）';
    return;
  }
  const unlimited = form.period === 'none';
  let amount = 0;
  if (!unlimited) {
    amount = Number(amountText.value);
    if (amountText.value === '' || !Number.isFinite(amount) || amount <= 0) {
      formError.value = '金额要填一个大于 0 的数字（要"不限额"请切到「不限额」）';
      return;
    }
  }

  saving.value = true;
  const ok: string[] = [];
  const fails: Array<{ name: string; message: string }> = [];
  for (const t of targets) {
    try {
      /* 「不限额」= 停用（period:none 是后端唯一能表达"从此不限额"的写法） */
      await api.put('/models/limits', unlimited
        ? { scope: form.scope, scope_id: t.id, enabled: false, period: 'none' }
        : {
          scope: form.scope, scope_id: t.id, enabled: true,
          period: form.period, amount, currency: form.currency, note: '',
        });
      ok.push(t.name);
    } catch (err: any) {
      /* 400 的 message 是后端写的（「该作用域下还有模型没配单价，不能设限额：X」）：
         原样放在卡片里，输入框一个字段都不清空，改完能直接再存。 */
      fails.push({ name: t.name, message: String(err?.message ?? err) });
    }
  }
  saving.value = false;

  if (!fails.length) {
    closeEditor();
    emit('refresh');
    return;
  }
  /* 只写一个目标时红字就是后端原文（一个字都不加）；一组多个时逐条点名 */
  if (targets.length === 1) formError.value = fails[0].message;
  else report.value = { ok, fails };
  if (ok.length) emit('refresh');   // 已经写进去的那几条，列表要跟上
}

/* ---------- 删除（两下确认，沿用现状） ---------- */

async function onEditorDelete(): Promise<void> {
  if (!editing.value) return;
  if (pendingRemove.value !== 'editor') {
    pendingRemove.value = 'editor';
    return;
  }
  pendingRemove.value = '';
  formError.value = '';
  report.value = null;
  const fails: Array<{ name: string; message: string }> = [];
  const ok: string[] = [];
  for (const it of editingItems.value) {
    try {
      await api.del('/models/limits/' + encodeURIComponent(it.id));
      ok.push(memberLabel(it));
    } catch (err: any) {
      fails.push({ name: memberLabel(it), message: String(err?.message ?? err) });
    }
  }
  if (!fails.length) {
    closeEditor();
    emit('refresh');
    return;
  }
  report.value = { ok, fails };
  if (ok.length) emit('refresh');
}

async function onRowDelete(row: { key: string; items: ModelLimitItem[] }): Promise<void> {
  if (pendingRemove.value !== row.key) {
    pendingRemove.value = row.key;
    return;
  }
  pendingRemove.value = '';
  listError.value = '';
  const fails: Array<{ name: string; message: string }> = [];
  for (const it of row.items) {
    try {
      await api.del('/models/limits/' + encodeURIComponent(it.id));
    } catch (err: any) {
      fails.push({ name: memberLabel(it), message: String(err?.message ?? err) });
    }
  }
  if (fails.length) {
    listError.value = '删除失败：' + fails.map((f) => f.name + ' — ' + f.message).join('；');
  }
  emit('refresh');
}

/* ---------------------------- 列表：同一 name+provider 合成一行 ---------------------------- */

interface LimitRowView {
  key: string;
  scope: ModelLimitScope;
  label: string;
  providerLabel: string;
  vendor: string;
  items: ModelLimitItem[];
  multiple: boolean;
  uniform: boolean;
  allOff: boolean;
  unusable: boolean;
  unpriced: number;
  foreign: number;
}

const rows = computed<LimitRowView[]>(() => {
  const out: LimitRowView[] = [];
  const index = new Map<string, LimitRowView>();
  for (const it of items.value) {
    const m = it.scope === 'model' ? modelById.value[it.scope_id] : null;
    const key = m ? groupKeyOf(m.name, m.provider) : 'row:' + it.id;
    let row = index.get(key);
    if (!row) {
      row = {
        key,
        scope: it.scope,
        label: m ? m.name : (it.label || it.scope_id),
        providerLabel: m ? (providerLabels.value[m.provider] || m.provider || '') : '',
        vendor: m ? m.provider : '',
        items: [],
        multiple: false,
        uniform: true,
        allOff: true,
        unusable: false,
        unpriced: 0,
        foreign: 0,
      };
      index.set(key, row);
      out.push(row);
    }
    row.items.push(it);
  }
  for (const row of out) {
    const first = row.items[0];
    row.multiple = row.items.length > 1;
    row.uniform = row.items.every((x) => x.period === first.period
      && Number(x.amount) === Number(first.amount)
      && String(x.currency) === String(first.currency));
    row.allOff = row.items.every((x) => !x.enabled);
    row.unusable = row.items.some((x) => x.enabled && !x.usable);
    row.unpriced = row.items.reduce((a, x) => a + Number(x.unpriced_rows || 0), 0);
    row.foreign = row.items.reduce((a, x) => a + Number(x.foreign_currency_rows || 0), 0);
  }
  return out;
});

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
    </header>

    <p class="limits-load" v-if="loading && !summary">限额载入中…</p>

    <!-- ① 现状条：一条都没有 → 一行字 + 真按钮（点了弹出卡片） -->
    <div class="lim-status is-empty" v-else-if="!hasAny" data-limit-empty>
      <span class="ls-none">还没有设置限额</span>
      <button type="button" class="btn sm" data-limit-add data-limit-add-empty @click="openCreate">
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

    <!-- ③ 已配置列表：同一 name+provider 的多条合成一行 -->
    <p class="lim-list-err" v-if="listError" data-limit-list-error>{{ listError }}</p>
    <div class="lim-list" v-if="hasAny" data-limit-list>
      <div
        class="lim-row" v-for="row in rows" :key="row.key"
        :class="{ off: row.allOff }" :data-limit-row="row.scope + ':' + row.items.map((x) => x.scope_id).join(',')"
      >
        <div class="lr-top">
          <span class="lr-scope">{{ SCOPE_CN[row.scope] }}</span>
          <img
            v-if="row.vendor && vendorIconOf(row.vendor)" class="lr-ico"
            :src="vendorIconOf(row.vendor)" :alt="row.providerLabel"
          />
          <span class="lr-name truncate" :title="row.items.map((x) => x.scope_id).join(' / ')">{{ row.label }}</span>
          <template v-if="row.providerLabel">
            <span class="sep">·</span>
            <span class="lr-prov truncate">{{ row.providerLabel }}</span>
          </template>
          <span class="badge dim" v-if="row.multiple" data-limit-group-count>{{ row.items.length }} 个配置</span>
          <span class="badge dim" v-if="row.allOff">已停用</span>
          <span class="spacer"></span>
          <button type="button" class="lnk" :data-limit-edit="row.items[0].id" @click="openEdit(row)">编辑</button>
          <button type="button" class="lnk danger" :data-limit-remove="row.items[0].id" @click="onRowDelete(row)">
            {{ pendingRemove === row.key ? '确认删除' : '删除' }}
          </button>
        </div>

        <div class="lr-meta">
          <template v-if="row.uniform">
            <span>{{ PERIOD_CN[row.items[0].period] }}</span>
            <span class="sep">·</span>
            <span class="mono">限额 {{ money(row.items[0].amount, row.items[0].currency) }}</span>
          </template>
          <span v-else>组内周期 / 金额不一致</span>
          <template v-if="!row.multiple">
            <span class="sep">·</span>
            <span class="mono">已用 {{ money(row.items[0].spent, row.items[0].currency) }}</span>
            <span class="sep">·</span>
            <span class="mono">剩余 {{ money(row.items[0].remaining, row.items[0].currency) }}</span>
            <span class="sep">·</span>
            <span class="mono lr-pct" :class="tone(row.items[0].percent)">{{ pct(row.items[0].percent) }}</span>
            <span class="sep">·</span>
            <span class="lr-reset">重置 {{ row.items[0].reset_at || '—' }}</span>
          </template>
          <template v-else>
            <span class="sep">·</span>
            <span class="lr-reset">各自独立累计，不是共享额度</span>
          </template>
        </div>

        <!-- 一组多个：各自的用量 / 百分比都摆出来（合成一行也不能把数字糊成一个） -->
        <template v-if="row.multiple">
          <div class="lr-member" v-for="it in row.items" :key="it.id" data-limit-member>
            <span class="lm-name truncate" :title="it.scope_id">{{ memberLabel(it) }}</span>
            <span class="sep">·</span>
            <span class="mono">已用 {{ money(it.spent, it.currency) }} / {{ money(it.amount, it.currency) }}</span>
            <span class="sep">·</span>
            <span class="mono lr-pct" :class="tone(it.percent)">{{ pct(it.percent) }}</span>
            <span class="sep">·</span>
            <span class="lr-reset">重置 {{ it.reset_at || '—' }}</span>
            <span class="badge dim" v-if="!it.enabled">已停用</span>
          </div>
        </template>

        <div class="lr-bar bar" v-if="!row.multiple" role="progressbar" :aria-valuenow="row.items[0].percent"
             aria-valuemin="0" aria-valuemax="100"
             :aria-label="row.label + ' 限额已用 ' + pct(row.items[0].percent)">
          <span class="bar-fill" :class="row.allOff ? 'off' : tone(row.items[0].percent)"
                :style="{ width: barWidth(row.items[0].percent) }" />
        </div>

        <!-- 诚实提示：这几句都是"数字看起来比实际小"的原因，藏起来就等于骗人 -->
        <p class="lr-note" v-if="row.unusable" data-limit-note="unpriced-models">
          <AppIcon name="warning" :size="12" />
          <span>该作用域下还有模型没配单价，这条限额拦不准（先去配单价）</span>
        </p>
        <p class="lr-note" v-if="row.unpriced > 0" data-limit-note="unpriced-rows">
          <AppIcon name="info" :size="12" />
          <span>另有 {{ row.unpriced }} 次调用未计费，已用金额只是下限</span>
        </p>
        <p class="lr-note" v-if="row.foreign > 0" data-limit-note="foreign">
          <AppIcon name="info" :size="12" />
          <span>另有 {{ row.foreign }} 次其它币种的调用未计入（不同币种不计入同一累计）</span>
        </p>
      </div>
    </div>

    <!-- ③¾ 新增入口只留一个：没有限额时在上面那条空态里，有限额时**跟在列表末尾**
         （用户口径 2026-10：原来标题栏和空态各有一个「新增限额」，重复了） -->
    <div class="lim-add-row" v-if="hasAny" data-limit-add-row>
      <button type="button" class="btn sm" data-limit-add @click="openCreate">
        <AppIcon name="add" :size="13" />新增限额
      </button>
    </div>

    <!-- ④ 新增 / 编辑卡片（Modal：Esc / 点遮罩 = 取消） -->
    <Modal :open="open" :title="editorTitle" @close="closeEditor">
      <div class="lim-editor" data-limit-editor>
        <div class="le-row">
          <span class="le-k">作用域</span>
          <div class="seg sq" role="group" aria-label="限额作用域">
            <button
              v-for="s in SCOPES" :key="s.id" type="button"
              :class="{ active: form.scope === s.id }" :data-limit-scope="s.id"
              :aria-pressed="form.scope === s.id" :title="s.hint" :disabled="editing"
              @click="pickScope(s.id)"
            >{{ s.label }}</button>
          </div>
          <span class="le-hint">{{ scopeHint }}</span>
        </div>

        <div class="le-row">
          <span class="le-k">目标</span>
          <select class="sel" v-model="form.scopeId" :disabled="targetsLoading || editing" data-limit-target
                  :aria-label="SCOPE_CN[form.scope] + '目标'">
            <option value="">{{ targetsLoading ? '读取候选…' : '选择' + SCOPE_CN[form.scope] }}</option>
            <option v-for="o in targetOptions" :key="o.id" :value="o.id">{{ o.label }}{{ o.hint ? '（' + o.hint + '）' : '' }}</option>
          </select>
          <span class="le-hint" v-if="editing" data-limit-target-locked>编辑时目标固定（要换目标请先删除再新增）</span>
          <span class="le-hint warn" v-else-if="targetsError" data-limit-targets-error>{{ targetsError }}</span>
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
          <span class="le-hint" v-if="form.period === 'none'" data-limit-none-hint>
            选「不限额」保存 = 停用（这一组 {{ actionCount }} 条都会被删掉）
          </span>
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

        <!-- 合并项：明说会写 N 条、且各自独立累计（N=1 时不出现） -->
        <p class="le-group" v-if="actionCount > 1" data-limit-group-warn>
          <AppIcon name="info" :size="12" />
          <span>这一组有 {{ actionCount }} 个配置，将分别为它们各设一条同样的限额（各自独立累计，不是共享额度）</span>
        </p>

        <!-- 编辑：当前用量一律用后端值 -->
        <div class="le-usage" v-if="editing" data-limit-usage>
          <div class="lu-line" v-for="it in editingItems" :key="it.id">
            <span class="lm-name truncate" :title="it.scope_id">{{ memberLabel(it) }}</span>
            <span class="sep">·</span>
            <span class="mono">已用 {{ money(it.spent, it.currency) }} / 限额 {{ money(it.amount, it.currency) }}</span>
            <span class="sep">·</span>
            <span class="mono">剩余 {{ money(it.remaining, it.currency) }}</span>
            <span class="sep">·</span>
            <span class="mono">{{ pct(it.percent) }}</span>
            <span class="sep">·</span>
            <span>{{ PERIOD_CN[it.period] }}重置 {{ it.reset_at || '—' }}</span>
          </div>
        </div>

        <p class="le-err" v-if="formError" data-limit-error>{{ formError }}</p>
        <div class="le-report" v-if="report" data-limit-report>
          <p class="le-err" v-if="report.ok.length" data-limit-report-ok>已写入 {{ report.ok.length }} 条：{{ report.ok.join('、') }}</p>
          <p class="le-err" v-for="f in report.fails" :key="f.name" data-limit-report-fail>失败：{{ f.name }} — {{ f.message }}</p>
        </div>
      </div>

      <template #footer>
        <button
          type="button" class="btn sm danger ef-left" v-if="editing" data-limit-delete
          @click="onEditorDelete"
        >
          <AppIcon name="trash" :size="13" />{{ pendingRemove === 'editor' ? '确认删除' : '删除' }}
        </button>
        <button type="button" class="btn sm" data-limit-cancel @click="closeEditor">取消</button>
        <button type="button" class="btn sm primary" :disabled="!canSubmit" data-limit-save @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </template>
    </Modal>
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

/* ---------------- 卡片内容 ---------------- */
.lim-editor { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.le-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.le-k {
  flex: none; width: 42px; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}
.lim-editor .seg.sq { height: 26px; }
.lim-editor .seg.sq button { height: 20px; padding: 0 10px; font-size: calc(11.5px * var(--ts, 1)); }
.lim-editor .seg.sq button:focus-visible { outline: none; box-shadow: var(--ring); }
.sel {
  height: 26px; max-width: 100%; min-width: 0; flex: 1 1 160px; padding: 0 8px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); cursor: pointer;
}
.sel.cur { flex: 0 0 auto; max-width: 130px; min-width: 96px; }
.sel:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.sel:disabled { opacity: .7; cursor: default; }
.inp {
  height: 26px; width: 128px; padding: 0 9px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); font-variant-numeric: tabular-nums;
}
.inp:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.le-hint { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); min-width: 0; }
.le-hint.warn { color: var(--warn); }
.le-group {
  display: flex; align-items: flex-start; gap: 5px; margin: 0;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim); line-height: 1.5;
}
.le-group .icon { flex: none; margin-top: 2px; }
.le-usage {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px 10px; border-radius: var(--r-sm); background: var(--bg-sunken);
}
.lu-line {
  display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim); min-width: 0;
}
.le-err {
  margin: 0; padding: 6px 10px; border-radius: var(--r-xs);
  background: var(--danger-soft); color: var(--danger);
  font-size: calc(12px * var(--ts, 1)); line-height: 1.5; white-space: pre-wrap; word-break: break-word;
}
.le-report { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.ef-left { margin-right: auto; }

/* ---------------- 列表 ---------------- */
.lim-list { display: flex; flex-direction: column; }
/* 新增入口跟在列表末尾（标题栏那个已按用户口径删掉）：留一点上间距，别和最后一行糊在一起 */
.lim-add-row { display: flex; margin-top: 8px; }
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
.lr-ico { flex: none; width: 14px; height: 14px; object-fit: contain; }
.lr-name { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; color: var(--text); min-width: 0; }
.lr-prov { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim); min-width: 0; }
.lr-meta {
  display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim);
}
.lr-member {
  display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
  padding-left: 4px; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); min-width: 0;
}
.lm-name { max-width: 180px; color: var(--text-dim); }
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
