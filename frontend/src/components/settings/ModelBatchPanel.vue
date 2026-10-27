<script setup lang="ts">
/**
 * 「批量配置」轻量面板：同名多配置组里**只放真的需要一次改多份**的四块 ——
 * 密钥来源、计费、推理强度、计价规则。
 *
 * 为什么不复用「编辑模型」那张 5 步表单（上一轮的做法，已按用户口径拆掉）：
 * 那张表单的五个步骤里，名称 / Base URL / 参数 / 模型类型**都是单条语义**，
 * 批量写回去等于替用户把所有配置改成一个样子（尤其 model_type —— 这一组之所以成组，
 * 就是同一个名字被不同用途用着，抹平类型等于删配置）。真正"同一组想统一改"的只有这几块。
 *
 * **每块各自有脏标记，这是本组件最重要的约定**：
 * 组内值本来就不一致时，把"没动过的那块"一并写回去，等于替用户抹平他并不想改的差异 ——
 * 那就不是"分别拆成轻量面板"了。所以只有用户真的动过的块才会进 payload（其余块是 undefined，
 * 父级用 `in` 判断，别用 falsy 判断：`key_mode: 'auto'` 这种值本身不假）。
 */
import { computed, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import type { ModelConfig } from '@/api/types';
import Modal from '@/components/Modal.vue';
import AppIcon from '@/components/AppIcon.vue';
import PriceRuleEditor from '@/components/settings/PriceRuleEditor.vue';
import { BUILTIN_EFFORTS, EFFORT_VALUE_PLACEHOLDER } from '@/utils/reasoning';
import { draftOf, ruleDraftError, toRuleItem, type PriceRuleDraft } from '@/utils/priceRule';
import type { KeyRow } from './modelTypes';

/** 与 ModelPanel 里那份 ModelRow 同形（它是组件内私有类型，SFC 不能导出类型，故按同一形状写一遍；
 *  字段全部可选 —— 老后端不返回 key_mode 时界面要能照常渲染）。 */
type ModelRow = ModelConfig & {
  key_mode?: 'auto' | 'key' | 'none';
  provider_key_id?: string | null;
};

const props = withDefaults(defineProps<{
  open: boolean;
  name: string;
  /** 组内各配置的任务类型中文名（由 ModelPanel 按 TYPE_LABEL 算好传进来，这里不再抄一份表） */
  typeLabels: string[];
  providerId: string;
  models: ModelRow[];
  busy?: boolean;
  /** 父级逐条 PUT 的结果（「N 个成功 / M 个失败」+ 每行「名称 — 后端 message」） */
  error?: string;
}>(), {
  busy: false,
  error: '',
});

type BatchKey = { key_mode: string; provider_key_id: string | null };
type BatchBilling = {
  currency: string | null;
  price_in: number | null;
  price_out: number | null;
  price_cache_read: number | null;
  price_cache_write: number | null;
};

type BatchLevel = { value: string; desc: string };
/** 只发档位表：父级写回时 parameters 是**整对象替换**，device / timeout 这些键得由父级按每条自己合并 */
type BatchRules = { items: Record<string, unknown>[] };
/** 没动过的块不出现（父级用「'key' in payload」判断，别用 falsy：key_mode 的 'auto' 本身不假） */
type BatchPayload = { key?: BatchKey; billing?: BatchBilling; levels?: BatchLevel[]; rules?: BatchRules };

const emit = defineEmits<{
  (e: 'save', payload: BatchPayload): void;
  (e: 'close'): void;
}>();

/* ---------------------------- 区块 1：密钥来源 ---------------------------- */

type KeyMode = '' | 'auto' | 'key' | 'none';
const KEY_OPTIONS: Array<{ id: Exclude<KeyMode, ''>; label: string; hint: string }> = [
  { id: 'auto', label: '跟随提供方默认', hint: '用这家提供方的默认密钥（换了默认它跟着换）' },
  { id: 'key', label: '指定密钥', hint: '这一组固定用某一把密钥' },
  { id: 'none', label: '无需密钥', hint: '本机推理这类不校验密钥的端点' },
];

const keys = ref<KeyRow[]>([]);
const keysUnavailable = ref(false);
const keysLoading = ref(false);
const keyMode = ref<KeyMode>('');
const keyChoice = ref('');
/** 只有用户动过密钥这一块才发 key_*（见文件头） */
const keyDirty = ref(false);

/** 下拉里显示 `label · masked`（与 ModelForm 的 keyOptionLabel 同一套口径，只保留面板用得上的两段） */
function keyLabel(k: KeyRow): string {
  const bits = [k.label || k.id];
  if (k.masked) bits.push(k.masked);
  return bits.join(' · ');
}

async function loadKeys(providerId: string): Promise<void> {
  keys.value = [];
  keysUnavailable.value = false;
  if (!providerId) return;
  keysLoading.value = true;
  try {
    /* 后端回 {items}，老写法 / 别处契约用过 {keys} —— 两种都认（照 ModelForm.loadKeys） */
    const data = await api.get<{ items?: KeyRow[]; keys?: KeyRow[] }>(
      '/models/providers/' + encodeURIComponent(providerId) + '/keys',
    );
    const list = data?.keys ?? data?.items;
    keys.value = Array.isArray(list) ? list : [];
  } catch {
    /* 接口没上线（404）→ 降级：还能选「跟随默认 / 无需密钥」，别把面板整个卡死 */
    keysUnavailable.value = true;
    keys.value = [];
  } finally {
    keysLoading.value = false;
  }
}

/** 组内密钥来源是否一致（不一致时给标签 + 不预选任何一项） */
const keyMixed = computed<boolean>(() => {
  const sigs = props.models.map((m) => String(m.key_mode ?? '') + '|' + String(m.provider_key_id ?? ''));
  return new Set(sigs).size > 1;
});

function pickKeyMode(id: Exclude<KeyMode, ''>): void {
  keyMode.value = id;
  keyDirty.value = true;
  /* 离开「指定密钥」就清掉选中的那把：与 ModelForm 换密钥来源时清 provider_key_id 一致，
     免得把上一次的 key id 悄悄带进新选择里 */
  if (id !== 'key') keyChoice.value = '';
}

const keyError = computed<string>(() => {
  if (!keyDirty.value) return '';
  if (!keyMode.value) return '先选一种密钥来源';
  if (keyMode.value === 'key' && !keyChoice.value) return '选中的「指定密钥」还没挑具体哪一把';
  return '';
});

/* ---------------------------- 区块 2：计费 ---------------------------- */

type PriceField = 'price_in' | 'price_out' | 'price_cache_read' | 'price_cache_write';
const PRICE_FIELDS: PriceField[] = ['price_in', 'price_out', 'price_cache_read', 'price_cache_write'];
const PRICE_LABEL: Record<PriceField, string> = {
  price_in: '输入 Token 单价',
  price_out: '输出 Token 单价',
  price_cache_read: '缓存读单价',
  price_cache_write: '缓存写单价',
};

/** 该模型计费（关 = 不计费：四个单价一律 null）—— 与 ModelForm 那颗 switch 同一语义 */
const billingOn = ref(false);
const billingCurrency = ref('');
const prices = reactive<Record<PriceField, string>>({
  price_in: '', price_out: '', price_cache_read: '', price_cache_write: '',
});
/** 只有用户动过计费这一块才发 currency / price_*（见文件头） */
const billingDirty = ref(false);

/** 单价字符串 → 数字：空 = 未配置（null），非法（非有限数 / 负数）= 报错。口径照 ModelForm.parsePrice */
function parsePrice(raw: unknown): { ok: boolean; value: number | null } {
  const s = String(raw ?? '').trim();
  if (!s) return { ok: true, value: null };
  const n = Number(s);
  if (!Number.isFinite(n) || n < 0) return { ok: false, value: null };
  return { ok: true, value: n };
}

function priceText(v: unknown): string {
  if (v == null || v === '') return '';
  const n = Number(v);
  return Number.isFinite(n) ? String(n) : '';
}

/** 组内计费值（币种 + 四个单价）是否一致 */
function billingSig(m: ModelRow): string {
  return [
    String(m.currency ?? ''),
    priceText(m.price_in), priceText(m.price_out),
    priceText(m.price_cache_read), priceText(m.price_cache_write),
  ].join('|');
}
const billingMixed = computed<boolean>(() => new Set(props.models.map(billingSig)).size > 1);

/** 不计费态不校验单价（框本身 disabled），否则「填了半截再关开关」会被自己改不了的报错卡住保存 */
const priceError = computed<string>(() => {
  if (!billingOn.value) return '';
  const bad = PRICE_FIELDS.find((f) => !parsePrice(prices[f]).ok);
  return bad ? PRICE_LABEL[bad] + '必须是非负数字（可以填小数，如 0.28）' : '';
});

function markBilling(): void { billingDirty.value = true; }

/* ---------------------------- 区块 3：推理强度 ---------------------------- */

type LevelRow = { value: string; desc: string };
const levels = ref<LevelRow[]>([]);
const levelsDirty = ref(false);

/** 一条配置声明的档位表（老后端没有 parameters 时当空表） */
function levelsOf(m: ModelRow): LevelRow[] {
  const raw = (m.parameters || {}).reasoning_levels;
  return Array.isArray(raw)
    ? raw.map((it: any) => ({ value: String(it?.value ?? ''), desc: String(it?.desc ?? '') }))
    : [];
}

/** 空表与「没有这个键」等价（都是没配档位），别把它算成一次"组内不一致" */
function levelsSig(m: ModelRow): string {
  const rows = levelsOf(m);
  return rows.length ? JSON.stringify(rows.map((r) => [r.value, r.desc])) : '';
}
const levelsMixed = computed<boolean>(() => new Set(props.models.map(levelsSig)).size > 1);

function markLevels(): void { levelsDirty.value = true; }

function addLevel(): void { levels.value.push({ value: '', desc: '' }); markLevels(); }
function removeLevel(i: number): void { levels.value.splice(i, 1); markLevels(); }

function moveLevel(i: number, delta: number): void {
  const j = i + delta;
  if (j < 0 || j >= levels.value.length) return;
  levels.value.splice(j, 0, ...levels.value.splice(i, 1));
  markLevels();
}

/** 内置档位与 ModelForm 的「填入内置档位」同一个来源（@/utils/reasoning），不抄第二份表 */
function fillBuiltinLevels(): void {
  levels.value = BUILTIN_EFFORTS.map((v) => ({ value: v, desc: '' }));
  markLevels();
}

/** 口径照 ModelForm.levelError：每行都要有值，同一条配置内不许重复 */
const levelError = computed<string>(() => {
  if (!levelsDirty.value) return '';
  const seen = new Set<string>();
  for (let i = 0; i < levels.value.length; i += 1) {
    const v = levels.value[i].value.trim();
    if (!v) return '推理强度第 ' + String(i + 1) + ' 行还没填值';
    if (seen.has(v)) return '推理强度「' + v + '」重复了，同一模型内的值要唯一';
    seen.add(v);
  }
  return '';
});

/* ---------------------------- 区块 4：计价规则 ---------------------------- */

const ruleRows = ref<PriceRuleDraft[]>([]);
const rulesDirty = ref(false);
/** 复位 / 预填期间置真：编辑器是**就地改行**的（输入框直接 v-model 到 row 上），
 *  脏标记只能靠深监听，而这个开关让"不是用户动的那一次赋值"不算动过。 */
let applyingRules = false;
/** 预填的取消令牌：面板关了又开、或者这一批换了，晚到的响应不许覆盖新状态 */
let rulesToken = 0;

watch(ruleRows, () => { if (!applyingRules) rulesDirty.value = true; }, { deep: true, flush: 'sync' });

function setRuleRows(rows: PriceRuleDraft[]): void {
  applyingRules = true;
  ruleRows.value = rows;
  applyingRules = false;
}

/** 组内时段规则是否一致：列表接口只给条数（price_rule_count），逐条内容要单独 GET ——
 *  条数不同一定不一致；条数相同按一致处理，再由 loadRules 把代表配置的规则读来预填。 */
function rulesSig(m: ModelRow): string { return String(m.price_rule_count ?? 0); }
const rulesMixed = computed<boolean>(() => new Set(props.models.map(rulesSig)).size > 1);

/** 读代表配置的规则来预填。读不到就留空、**不报错**：这一块只有被用户动过才写回，
 *  空表本身不造成任何改动，而摆一条"读失败"只会让人以为批量面板坏了。 */
async function loadRules(modelId: string): Promise<void> {
  const token = rulesToken;
  try {
    const data = await api.get<{ items?: Record<string, any>[] }>(
      '/models/' + encodeURIComponent(modelId) + '/price-rules',
    );
    if (token !== rulesToken) return;
    const items = Array.isArray(data?.items) ? data.items : [];
    setRuleRows(items.map((row) => draftOf(row)));
  } catch {
    /* 降级：留空（与 ModelForm 的 loadPriceRules 同一套"读不到就留空"口径） */
  }
}

const ruleError = computed<string>(() => (rulesDirty.value ? ruleDraftError(ruleRows.value) : ''));

/* ---------------------------- 打开时复位 ---------------------------- */

/** 组内一致时按第一条预填；不一致时**不预选**（那正是「组内不一致」标签要说的） */
function reset(): void {
  keyDirty.value = false;
  billingDirty.value = false;
  keyChoice.value = '';

  const list = props.models;
  const first = list[0] || null;
  const consistent = !!first && !keyMixed.value;
  if (consistent && first) {
    keyMode.value = first.key_mode === 'none'
      ? 'none'
      : (first.key_mode === 'key' && first.provider_key_id ? 'key' : 'auto');
    if (keyMode.value === 'key') keyChoice.value = String(first.provider_key_id);
  } else {
    keyMode.value = '';
  }

  if (first && !billingMixed.value) {
    billingCurrency.value = String(first.currency ?? '');
    for (const f of PRICE_FIELDS) prices[f] = priceText(first[f]);
    billingOn.value = PRICE_FIELDS.some((f) => prices[f].trim() !== '');
  } else {
    billingCurrency.value = '';
    for (const f of PRICE_FIELDS) prices[f] = '';
    billingOn.value = false;
  }

  levelsDirty.value = false;
  rulesDirty.value = false;
  rulesToken += 1;                     // 作废还在路上的那一次预填
  if (first && !levelsMixed.value) levels.value = levelsOf(first);
  else levels.value = [];              // 不一致 → 表格留空（正是「组内不一致」标签要说的）
  setRuleRows([]);
  if (first && !rulesMixed.value) void loadRules(String(first.id || ''));

  void loadKeys(props.providerId);
}

/* open 变 true 就复位：openBatch 每次都换一批 models，不复位会把上一次的状态漏到下一次 */
watch(() => props.open, (v) => { if (v) reset(); }, { immediate: true });

const scopeLine = computed<string>(() => {
  const labels = props.typeLabels.join(' / ');
  return String(props.models.length) + ' 个配置' + (labels ? '：' + labels : '');
});

/** 四块都没动 → 不能保存（没有要写的东西，按下去只会发一堆空 body） */
const dirty = computed<boolean>(() => (
  keyDirty.value || billingDirty.value || levelsDirty.value || rulesDirty.value
));
const canSave = computed<boolean>(() => dirty.value && !props.busy
  && !keyError.value && !priceError.value && !levelError.value && !ruleError.value);

function save(): void {
  if (!canSave.value) return;
  const payload: BatchPayload = {};
  if (keyDirty.value) {
    payload.key = {
      key_mode: keyMode.value,
      provider_key_id: keyMode.value === 'key' ? (keyChoice.value || null) : null,
    };
  }
  if (billingDirty.value) {
    /* 关掉计费 = 这一块整体复位：四个单价与币种都写 null，不留一个"没意义却还在"的币种 */
    payload.billing = {
      currency: billingOn.value ? (billingCurrency.value || null) : null,
      price_in: billingOn.value ? parsePrice(prices.price_in).value : null,
      price_out: billingOn.value ? parsePrice(prices.price_out).value : null,
      price_cache_read: billingOn.value ? parsePrice(prices.price_cache_read).value : null,
      price_cache_write: billingOn.value ? parsePrice(prices.price_cache_write).value : null,
    };
  }
  if (levelsDirty.value) {
    /* 只发档位表：parameters 在父级是整对象替换，别的键（device / timeout）由它按每条自己合并 */
    payload.levels = levels.value
      .map((r) => ({ value: r.value.trim(), desc: (r.desc || '').trim() }))
      .filter((r) => !!r.value);
  }
  if (rulesDirty.value) {
    /* 分时段是**整批替换**：动过这一块 = 把这一套规则写给每一条配置（空表 = 清空全部时段） */
    payload.rules = { items: ruleRows.value.map((row) => toRuleItem(row)) };
  }
  emit('save', payload);
}
</script>

<template>
  <Modal :open="open" :title="'批量配置 · ' + name" @close="emit('close')">
    <div class="batch-panel" data-batch-panel>
      <!-- 范围先说清：这一组有几条、各是什么用途（类型不改，面板也不给改它的入口） -->
      <p class="scope-line" data-batch-scope>{{ scopeLine }}</p>

      <!-- ① 密钥来源 -->
      <section class="bsec">
        <div class="bsec-head">
          <span class="bsec-title">密钥来源</span>
          <span class="spacer"></span>
          <span class="mixed-tag" v-if="keyMixed" data-batch-mixed="key">组内不一致</span>
        </div>

        <div class="seg sq" role="group" aria-label="密钥来源" :data-batch-key-mode="keyMode">
          <button
            v-for="o in KEY_OPTIONS" :key="o.id" type="button"
            :class="{ active: keyMode === o.id }" :data-batch-key-opt="o.id"
            :aria-pressed="keyMode === o.id" :title="o.hint"
            @click="pickKeyMode(o.id)"
          >{{ o.label }}</button>
        </div>

        <label class="field" v-if="keyMode === 'key'">
          <span>密钥</span>
          <select
            v-model="keyChoice" data-batch-key-select
            aria-label="组内统一使用的密钥"
            @change="keyDirty = true"
          >
            <option value="">{{ keysLoading ? '读取密钥…' : '选择密钥…' }}</option>
            <option v-for="k in keys" :key="k.id" :value="k.id">{{ keyLabel(k) }}</option>
          </select>
        </label>

        <p class="hint-line" v-if="keysUnavailable" data-batch-keys-unavailable>
          密钥接口不可用（404）：只能选「跟随提供方默认」或「无需密钥」。
        </p>
        <p class="err-line" v-if="keyError" data-batch-key-error>{{ keyError }}</p>
      </section>

      <!-- ② 计费 -->
      <section class="bsec">
        <div class="bsec-head">
          <span class="bsec-title">计费</span>
          <span class="spacer"></span>
          <span class="mixed-tag" v-if="billingMixed" data-batch-mixed="billing">组内不一致</span>
        </div>

        <!-- 造型与 ModelForm 那颗 switch 同一套（.sw / .sw-track / .sw-knob）：真身是原生 checkbox，
             键盘、焦点、无障碍全白拿；轨道与圆点是纯装饰 -->
        <label class="sw-row">
          <span class="sw">
            <input
              type="checkbox" role="switch" v-model="billingOn" data-batch-billing-switch
              :aria-checked="billingOn" @change="markBilling"
            />
            <span class="sw-track" aria-hidden="true"><span class="sw-knob"></span></span>
          </span>
          <span>该模型计费（关闭 = 不计费，调用记为未计费）</span>
        </label>

        <label class="field">
          <span>币种</span>
          <select v-model="billingCurrency" data-batch-billing-currency @change="markBilling">
            <option value="">跟随厂商</option>
            <option value="CNY">人民币 CNY</option>
            <option value="USD">美元 USD</option>
          </select>
        </label>

        <div class="price-grid">
          <label class="field" v-for="f in PRICE_FIELDS" :key="f">
            <span>{{ PRICE_LABEL[f] }}<em class="unit">每百万 Token</em></span>
            <input
              v-model="prices[f]" inputmode="decimal" :data-batch-price="f"
              :disabled="!billingOn" @input="markBilling"
              :placeholder="f === 'price_in' ? '如 2（算得出钱的前提）' : (f === 'price_out' ? '如 8（算得出钱的前提）' : '可选，服务商提供该计价规则时填写')"
            />
          </label>
        </div>

        <p class="err-line" v-if="priceError" data-batch-price-error>{{ priceError }}</p>
      </section>

      <!-- ③ 推理强度：厂商档位名并不统一，所以档位逐模型配；这里只统一"这一组认哪几档" -->
      <section class="bsec" data-batch-levels>
        <div class="bsec-head">
          <span class="bsec-title">推理强度</span>
          <span class="spacer"></span>
          <span class="mixed-tag" v-if="levelsMixed" data-batch-mixed="levels">组内不一致</span>
        </div>

        <div class="lv-row" v-for="(r, i) in levels" :key="i">
          <input
            v-model="r.value" class="mono" :placeholder="EFFORT_VALUE_PLACEHOLDER"
            :aria-label="'推理强度第 ' + (i + 1) + ' 档的值'"
            :data-batch-level="'value:' + i" @input="markLevels"
          />
          <input
            v-model="r.desc" placeholder="说明（可空）"
            :aria-label="'推理强度第 ' + (i + 1) + ' 档的说明'"
            :data-batch-level="'desc:' + i" @input="markLevels"
          />
          <button type="button" class="btn icon sm ghost" title="上移" :disabled="i === 0" data-batch-level-up @click="moveLevel(i, -1)">
            <AppIcon name="chevron-up" :size="14" />
          </button>
          <button type="button" class="btn icon sm ghost" title="下移" :disabled="i === levels.length - 1" data-batch-level-down @click="moveLevel(i, 1)">
            <AppIcon name="chevron-down" :size="14" />
          </button>
          <button type="button" class="btn icon sm ghost lv-del" title="删除这一档" data-batch-level-del @click="removeLevel(i)">
            <AppIcon name="trash" :size="14" />
          </button>
        </div>

        <div class="lv-foot">
          <button type="button" class="btn sm ghost" data-batch-levels-add @click="addLevel">
            <AppIcon name="add" :size="14" />添加档位
          </button>
          <button type="button" class="btn sm ghost" data-batch-levels-fill @click="fillBuiltinLevels">填入内置档位</button>
        </div>

        <p class="err-line" v-if="levelError" data-batch-level-error>{{ levelError }}</p>
      </section>

      <!-- ④ 计价规则：**整批替换**语义（动过这一块 = 把这一套时段写给每一条配置） -->
      <section class="bsec" data-batch-rules>
        <div class="bsec-head">
          <span class="bsec-title">计价规则</span>
          <span class="spacer"></span>
          <span class="mixed-tag" v-if="rulesMixed" data-batch-mixed="rules">组内不一致</span>
        </div>

        <PriceRuleEditor v-model:rows="ruleRows" />

        <p class="err-line" v-if="ruleError" data-batch-rules-error>{{ ruleError }}</p>
      </section>

      <!-- 父级逐条 PUT 的总账：一行总账 + 每行「名称 — 后端 message」，必须能分行 -->
      <p class="err-line" v-if="error" data-batch-error>{{ error }}</p>
    </div>

    <template #footer>
      <button type="button" class="btn sm" data-batch-cancel @click="emit('close')">取消</button>
      <button type="button" class="btn sm primary" :disabled="!canSave" data-batch-save @click="save">
        {{ busy ? '保存中…' : '保存' }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.batch-panel { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.scope-line { margin: 0; font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); line-height: 1.5; }

/* 两块各自独立：一条细线分隔，眼睛一眼看出"这是两件互不牵连的事" */
.bsec {
  display: flex; flex-direction: column; gap: 10px; min-width: 0;
  padding-top: 12px; border-top: 1px solid var(--divider);
}
.bsec-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.bsec-title { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; color: var(--text); }
.mixed-tag {
  flex: none; padding: 1px 7px; border-radius: 999px;
  background: var(--bg-sunken); color: var(--text-dim);
  font-size: calc(11px * var(--ts, 1)); line-height: 1.7;
}

.batch-panel .seg.sq button:focus-visible { outline: none; box-shadow: var(--ring); }

/* 与 ModelForm 的 .sw-row/.sw/.sw-track/.sw-knob 同一套造型（scoped 样式不跨组件，故原样抄一份） */
.sw-row { display: flex; align-items: center; gap: 8px; font-size: calc(12.5px * var(--ts, 1)); color: var(--text); cursor: pointer; min-width: 0; }
.sw { position: relative; display: inline-flex; flex: none; width: 32px; height: 18px; }
.sw input {
  position: absolute; inset: 0; z-index: 1; margin: 0; width: 100%; height: 100%;
  opacity: 0; cursor: pointer;
}
.sw-track {
  position: absolute; inset: 0; border-radius: 999px;
  background: var(--bg-sunken); border: 1px solid var(--border);
  transition: background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.sw-knob {
  position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 50%;
  background: var(--text-faint);
  transition: transform var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.sw input:checked + .sw-track { background: var(--accent); border-color: var(--accent); }
.sw input:checked + .sw-track .sw-knob { transform: translateX(14px); background: var(--text-on-accent); }
.sw input:focus-visible + .sw-track { outline: none; box-shadow: var(--ring); }

/* 与 ModelForm 的 .lv-row/.lv-foot 同一套造型（scoped 样式不跨组件，故原样抄一份） */
.lv-row { display: grid; grid-template-columns: 96px minmax(0, 1fr) auto auto auto; gap: 6px; align-items: center; }
.lv-row input { width: 100%; height: var(--control-h-sm); min-width: 0; font-size: calc(12.5px * var(--ts, 1)); }
.lv-row input:focus-visible { outline: none; box-shadow: var(--ring); }
.lv-row .lv-del:hover:not(:disabled) { color: var(--danger); }
.lv-foot { display: flex; gap: 8px; flex-wrap: wrap; }

.price-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; min-width: 0; }
.unit { font-style: normal; font-size: calc(11px * var(--ts, 1)); font-weight: 400; color: var(--text-faint); }
.hint-line { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }
/* pre-line：父级的总账是「一行总账 + 每行一条失败」，必须按换行分行 */
.err-line {
  margin: 0; padding: 6px 10px; border-radius: var(--r-xs);
  background: var(--danger-soft); color: var(--danger);
  font-size: calc(12px * var(--ts, 1)); line-height: 1.5; white-space: pre-line; word-break: break-word;
}
</style>
