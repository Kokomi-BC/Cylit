<script setup lang="ts">
/**
 * 「批量配置」轻量面板：同名多配置组里**只放真的需要一次改多份**的两块 —— 密钥来源、计费。
 *
 * 为什么不复用「编辑模型」那张 5 步表单（上一轮的做法，已按用户口径拆掉）：
 * 那张表单的五个步骤里，名称 / Base URL / 参数 / 模型类型**都是单条语义**，
 * 批量写回去等于替用户把所有配置改成一个样子（尤其 model_type —— 这一组之所以成组，
 * 就是同一个名字被不同用途用着，抹平类型等于删配置）。真正"同一组想统一改"的只有这两块。
 *
 * **两块各自有脏标记，这是本组件最重要的约定**：
 * 组内值本来就不一致时，把"没动过的那块"一并写回去，等于替用户抹平他并不想改的差异 ——
 * 那就不是"分别拆成轻量面板"了。所以只有用户真的动过的块才会进 payload（另一块是 undefined，
 * 父级用 `in` 判断，别用 falsy 判断：`key_mode: 'auto'` 这种值本身不假）。
 */
import { computed, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import type { ModelConfig } from '@/api/types';
import Modal from '@/components/Modal.vue';
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

const emit = defineEmits<{
  (e: 'save', payload: { key?: BatchKey; billing?: BatchBilling }): void;
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

  void loadKeys(props.providerId);
}

/* open 变 true 就复位：openBatch 每次都换一批 models，不复位会把上一次的状态漏到下一次 */
watch(() => props.open, (v) => { if (v) reset(); }, { immediate: true });

const scopeLine = computed<string>(() => {
  const labels = props.typeLabels.join(' / ');
  return String(props.models.length) + ' 个配置' + (labels ? '：' + labels : '');
});

/** 两块都没动 → 不能保存（没有要写的东西，按下去只会发一堆空 body） */
const dirty = computed<boolean>(() => keyDirty.value || billingDirty.value);
const canSave = computed<boolean>(() => dirty.value && !props.busy && !keyError.value && !priceError.value);

function save(): void {
  if (!canSave.value) return;
  const payload: { key?: BatchKey; billing?: BatchBilling } = {};
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
        <p class="hint-line" v-if="keyMixed">组内各配置的密钥来源不同，所以没有预选；这一块不改就不写回。</p>
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

        <p class="hint-line" v-if="!billingOn" data-batch-billing-off>
          该模型不计费：调用记成未计费（cost 为空），也不能给它设限额
        </p>

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
        <p class="hint-line" data-batch-price-rules-note>
          分时段单价仍按单条配置维护（整批替换语义，批量改它风险远大于收益）。
        </p>
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
