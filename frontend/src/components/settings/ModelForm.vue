<script setup lang="ts">
/**
 * 新建 / 编辑模型的**两步式表单**（弹窗内）。
 *
 * Step 1 是提供方卡片（数据来自 `GET /models/presets`，卡片上的小字是预设的 base_url 主机名）；
 * 点一张卡 → Step 2，base_url / 密钥文件名 / parameters 已按预设填好并**收进「高级」**，
 * 明面上只剩「名称 + 模型名」两个字段。编辑既有模型直接开在 Step 2。
 *
 * 保存动作不在这里做：校验通过后 `emit('submit', payload)`，由 ModelPanel 走 `POST /models`。
 */
import { computed, reactive, ref, watch } from 'vue';
import type { ModelConfig, ModelPreset, ModelPresetSpec } from '@/api/types';
import { toast } from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';

const props = withDefaults(defineProps<{
  open: boolean;
  mode: 'create' | 'edit';
  target?: ModelConfig | null;
  /** 从哪个分区点的新建（agent 区 → 类型默认停在 agent） */
  defaultType?: string;
  types?: string[];
  typeLabels?: Record<string, string>;
  presets?: ModelPreset[];
  secrets?: string[];
  busy?: boolean;
}>(), {
  target: null,
  defaultType: 'agent',
  types: () => [],
  typeLabels: () => ({}),
  presets: () => [],
  secrets: () => [],
  busy: false,
});

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'submit', payload: Record<string, unknown>): void;
}>();

const SPEC_KEYS = ['agent', 'generation', 'vision', 'embedding', 'reranker'] as const;

const step = ref<1 | 2>(1);
const advanced = ref(false);
const presetId = ref('');
const entryType = ref('agent');
const form = reactive({
  name: '', model_type: 'agent', provider: '', model_name: '',
  base_url: '', secret_ref: '', parametersText: '{}', enabled: true,
});

/* ---------------------------- 推理等级 ----------------------------
 * 问答模型可以在 `parameters.reasoning_levels` 里自己定档位（`[{ value, desc? }]`，
 * 顺序就是展示顺序），默认档仍是 `parameters.reasoning_effort`；两者都留空 = 用内置
 * low / medium / high。这块以**解析后的对象**为工作副本：只重写这两个键，「高级」里
 * 手写的其它参数原样保留；那份 JSON 一旦不是合法对象，这块只显示一行红字、不写回。
 */
const BUILTIN_LEVELS = ['off', 'low', 'medium', 'high', 'max'];

interface LevelRow { value: string; desc: string }

const levels = ref<LevelRow[]>([]);
const defaultLevel = ref('');
const paramsBroken = ref('');

/** 合法 JSON 对象 → 对象（空串当 `{}`）；非法 / 不是对象 → null，由调用方给提示。 */
function parseParamsSafe(text: string): Record<string, any> | null {
  const s = String(text ?? '').trim();
  if (!s) return {};
  try {
    const parsed = JSON.parse(s);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
  } catch { /* 落到最后统一返回 null */ }
  return null;
}

/** 编辑器状态 ← 那份 JSON（打开弹窗 / 换预设 / 手改 textarea 时走这里）。 */
function syncReasoning() {
  const p = parseParamsSafe(form.parametersText);
  if (!p) {
    paramsBroken.value = '当前 parameters 不是合法 JSON，先修好再用这块编辑';
    levels.value = [];
    defaultLevel.value = '';
    return;
  }
  paramsBroken.value = '';
  const raw = p.reasoning_levels;
  levels.value = Array.isArray(raw)
    ? raw.map((it: any) => ({ value: String(it?.value ?? ''), desc: String(it?.desc ?? '') }))
    : [];
  const eff = p.reasoning_effort;
  defaultLevel.value = eff === undefined || eff === null ? '' : String(eff);
}

/** 刚写回去的那份文本：由它引起的 watch 不再反过来重读编辑器状态。 */
let writtenText = '';

/** 编辑器状态 → 那份 JSON：只动这两个键，别的一律原样带回。 */
function writeReasoning() {
  if (paramsBroken.value) return;
  const p = parseParamsSafe(form.parametersText);
  if (!p) return;
  const next: Record<string, any> = { ...p };
  const rows = levels.value
    .map((r) => ({ value: r.value.trim(), desc: (r.desc || '').trim() }))
    .filter((r) => r.value)                   // 值还空着的行先不落盘（提交时会被校验拦下）
    .map((r) => (r.desc ? r : { value: r.value }));
  if (rows.length) next.reasoning_levels = rows;
  else delete next.reasoning_levels;
  const eff = defaultLevel.value.trim();
  if (eff) next.reasoning_effort = eff;
  else delete next.reasoning_effort;
  const text = Object.keys(next).length ? JSON.stringify(next, null, 2) : '{}';
  if (text === form.parametersText) return;
  writtenText = text;
  form.parametersText = text;
}

function addLevel() {
  levels.value.push({ value: '', desc: '' });
}

function removeLevel(i: number) {
  levels.value.splice(i, 1);
}

/** 上移 / 下移一位：顺序即展示顺序，所以直接搬数组。 */
function moveLevel(i: number, delta: number) {
  const j = i + delta;
  if (j < 0 || j >= levels.value.length) return;
  levels.value.splice(j, 0, ...levels.value.splice(i, 1));
}

/** 未配置时的一键兜底：内置三档，说明留空。 */
function fillBuiltinLevels() {
  levels.value = BUILTIN_LEVELS.map((v) => ({ value: v, desc: '' }));
}

function onDefaultChange(e: Event) {
  defaultLevel.value = (e.target as HTMLSelectElement).value;
}

/** 默认档下拉的选项：已配的档位（含刚加的）+ 手写在 JSON 里、不在档位表里的那个值。 */
const levelValues = computed(() => {
  const out: string[] = [];
  for (const r of levels.value) {
    const v = r.value.trim();
    if (v && !out.includes(v)) out.push(v);
  }
  const d = defaultLevel.value.trim();
  if (d && !out.includes(d)) out.push(d);
  return out;
});

/** 值不能为空、同一模型内不能重复；有错就 toast 拦下提交，不静默丢档位。 */
function levelError(): string {
  const seen = new Set<string>();
  for (let i = 0; i < levels.value.length; i++) {
    const v = levels.value[i].value.trim();
    if (!v) return `推理等级第 ${i + 1} 行还没填值`;
    if (seen.has(v)) return `推理等级「${v}」重复了，同一模型内的值要唯一`;
    seen.add(v);
  }
  return '';
}

/** 改动先落到状态、再回写成 JSON（深监听 = v-model 已经生效之后，不赌事件先后）。 */
watch(() => form.parametersText, (t) => { if (t !== writtenText) syncReasoning(); });
watch(levels, writeReasoning, { deep: true });
watch(defaultLevel, writeReasoning);

function init() {
  presetId.value = '';
  advanced.value = false;
  entryType.value = props.defaultType || 'agent';
  if (props.mode === 'edit' && props.target) {
    const m = props.target;
    step.value = 2;
    Object.assign(form, {
      name: m.name || '',
      model_type: m.model_type || entryType.value,
      provider: m.provider || '',
      model_name: m.model_name || '',
      base_url: m.base_url || '',
      secret_ref: m.secret_ref || '',
      parametersText: JSON.stringify(m.parameters || {}, null, 2),
      enabled: m.enabled !== false,
    });
    return;
  }
  step.value = 1;
  Object.assign(form, {
    name: '', model_type: entryType.value, provider: '', model_name: '',
    base_url: '', secret_ref: '', parametersText: '{}', enabled: true,
  });
}

watch(() => props.open, (v) => { if (v) { init(); syncReasoning(); } }, { immediate: true });

/* ---------------------------- 预设 ---------------------------- */

function specOf(p: ModelPreset, type: string): ModelPresetSpec | null {
  const raw = p as any;
  const direct = raw[type];
  if (direct && typeof direct === 'object') return direct as ModelPresetSpec;
  for (const k of SPEC_KEYS) {
    const s = raw[k];
    if (s && typeof s === 'object') return s as ModelPresetSpec;
  }
  return null;
}

function hostOf(url?: string | null): string {
  if (!url) return '';
  try { return new URL(url).host; } catch { return String(url); }
}

const cards = computed(() => props.presets.map((p) => {
  const spec = specOf(p, entryType.value);
  return {
    id: p.id,
    preset: p,
    label: p.label || p.id,
    host: hostOf(spec?.base_url) || p.provider || '—',
    modelName: spec?.model_name || '',
  };
}));

const presetById = computed(() => props.presets.find((p) => p.id === presetId.value) ?? null);

function applySpec(spec: ModelPresetSpec | null, preset: ModelPreset | null) {
  form.provider = spec?.provider || preset?.provider || '';
  form.base_url = spec?.base_url || '';
  form.secret_ref = spec?.secret_ref || '';
  const params = spec?.parameters;
  form.parametersText = params && Object.keys(params).length ? JSON.stringify(params, null, 2) : '{}';
  if (spec?.model_name) {
    form.model_name = spec.model_name;
    if (!form.name) form.name = spec.model_name;
  }
}

function pickPreset(p: ModelPreset | null) {
  presetId.value = p ? p.id : '';
  if (p) applySpec(specOf(p, entryType.value), p);
  else {
    form.provider = '';
    form.model_name = '';
    form.base_url = '';
    form.secret_ref = '';
    form.parametersText = '{}';
    form.name = '';
  }
  step.value = 2;
}

/** 换类型时，已选预设的对应档位重新填一遍（问答要思考、翻译要关思考，两者参数不同）。 */
watch(() => form.model_type, (t) => {
  const p = presetById.value;
  if (!p || props.mode !== 'create') return;
  const spec = (p as any)[t];
  if (spec && typeof spec === 'object') applySpec(spec as ModelPresetSpec, p);
});

const modelChips = computed(() => {
  const out = new Set<string>();
  const p = presetById.value;
  if (p) {
    const raw = p as any;
    for (const k of SPEC_KEYS) {
      const s = raw[k];
      if (s && typeof s === 'object' && s.model_name) out.add(String(s.model_name));
    }
  }
  if (form.model_name) out.add(form.model_name);
  return [...out];
});

/** 「高级」收起时的一行摘要：填到哪儿了，一眼看得见。 */
const advSummary = computed(() => {
  const parts = [hostOf(form.base_url) || form.base_url || '未填 base_url', form.secret_ref || '无密钥'];
  return parts.join(' · ');
});

/* ---------------------------- 提交 ---------------------------- */

function parseParameters(text: string): Record<string, any> {
  const s = String(text ?? '').trim();
  if (!s) return {};
  let parsed: any;
  try {
    parsed = JSON.parse(s);
  } catch (err: any) {
    throw new Error(`parameters 不是合法 JSON：${err.message}`);
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('parameters 必须是 JSON 对象');
  }
  return parsed;
}

function submit() {
  if (!form.name.trim()) { toast('请填写名称', 'warn'); return; }
  writeReasoning();                    // 档位编辑器的最后一次改动先落地
  let parameters: Record<string, any>;
  try {
    parameters = parseParameters(form.parametersText);
  } catch (err: any) {
    advanced.value = true;
    toast(err.message, 'error', 8000);
    return;
  }
  if (form.model_type === 'agent') {
    const bad = levelError();
    if (bad) { toast(bad, 'error', 6000); return; }
  }
  const payload: Record<string, unknown> = {
    name: form.name.trim(),
    model_type: form.model_type,
    provider: form.provider.trim() || 'openai_compatible',
    model_name: form.model_name.trim() || null,
    base_url: form.base_url.trim() || null,
    secret_ref: form.secret_ref.trim() || null,
    parameters,
  };
  if (props.mode === 'edit') payload.enabled = !!form.enabled;
  emit('submit', payload);
}

/** 点建议 chip：填模型名；名称还空着就顺手用它兜个底。 */
function pickModelName(m: string) {
  form.model_name = m;
  if (!form.name) form.name = m;
}

function labelOf(t: string): string {
  return props.typeLabels[t] || t;
}
</script>

<template>
  <Modal
    :open="open" wide
    :title="mode === 'edit' ? '编辑模型' : '新建模型'"
    @close="emit('close')"
  >
    <!-- Step 1：提供方卡片（点一张就进 Step 2） -->
    <div class="providers" v-if="step === 1">
      <button
        type="button" class="prov"
        v-for="c in cards" :key="c.id"
        :class="{ on: presetId === c.id }"
        @click="pickPreset(c.preset)"
      >
        <span class="pv-label truncate">{{ c.label }}</span>
        <span class="pv-host mono truncate">{{ c.host }}</span>
        <span class="pv-model mono truncate">{{ c.modelName || '—' }}</span>
      </button>
      <button type="button" class="prov custom" @click="pickPreset(null)">
        <span class="pv-label">自定义</span>
        <span class="pv-host">自己填地址与密钥</span>
        <span class="pv-model mono">—</span>
      </button>
    </div>

    <!-- Step 2：名称 + 模型名；其余收进「高级」 -->
    <div v-else>
      <div class="seg types" role="group" aria-label="模型类型">
        <button
          type="button" v-for="t in types" :key="t"
          :class="{ active: form.model_type === t }"
          @click="form.model_type = t"
        >{{ labelOf(t) }}</button>
      </div>

      <div class="grid cols-2 gap-top">
        <label class="field">名称
          <input v-model="form.name" :placeholder="form.model_name || '界面显示用'" />
        </label>
        <label class="field">模型名
          <input v-model="form.model_name" class="mono" placeholder="provider 侧的 model" />
        </label>
      </div>

      <div class="chips" v-if="modelChips.length">
        <button
          type="button" class="chip mono" v-for="m in modelChips" :key="m"
          :class="{ on: form.model_name === m }"
          @click="pickModelName(m)"
        >{{ m }}</button>
      </div>

      <!-- 推理等级：问答模型专属，独立于「高级」，默认展开。写回 parameters.reasoning_levels -->
      <div class="levels" v-if="form.model_type === 'agent'">
        <div class="lv-head">
          <span class="lv-title">推理等级</span>
          <span class="spacer"></span>
          <span class="tiny faint">{{ levels.length ? `${levels.length} 档` : '未配置' }}</span>
        </div>

        <p class="lv-broken" v-if="paramsBroken">{{ paramsBroken }}</p>
        <template v-else>
          <div class="lv-row" v-for="(r, i) in levels" :key="i">
            <input v-model="r.value" class="mono" placeholder="值，如 high" />
            <input v-model="r.desc" placeholder="说明（可空），如 更慢更稳，适合硬题" />
            <button type="button" class="btn icon sm ghost" title="上移" :disabled="i === 0" @click="moveLevel(i, -1)">
              <AppIcon name="chevron-up" :size="14" />
            </button>
            <button
              type="button" class="btn icon sm ghost" title="下移" :disabled="i === levels.length - 1"
              @click="moveLevel(i, 1)"
            >
              <AppIcon name="chevron-down" :size="14" />
            </button>
            <button type="button" class="btn icon sm ghost lv-del" title="删除这一档" @click="removeLevel(i)">
              <AppIcon name="trash" :size="14" />
            </button>
          </div>

          <div class="lv-foot">
            <button type="button" class="btn sm ghost" @click="addLevel">
              <AppIcon name="add" :size="14" />添加档位
            </button>
          </div>

          <p class="lv-hint tiny faint" v-if="!levels.length">
            未配置时用内置 off / low / medium / high / max
            <button type="button" class="lv-fill" @click="fillBuiltinLevels">填入内置三档</button>
          </p>

          <label class="field lv-default">默认档
            <select :value="defaultLevel" @change="onDefaultChange">
              <option value="">不设置</option>
              <option v-for="v in levelValues" :key="v" :value="v">{{ v }}</option>
            </select>
          </label>
        </template>
      </div>

      <button type="button" class="adv-toggle" @click="advanced = !advanced">
        <AppIcon :name="advanced ? 'chevron-up' : 'chevron-down'" :size="14" />
        <span>高级</span>
        <span class="spacer"></span>
        <span class="tiny faint truncate" v-if="!advanced">{{ advSummary }}</span>
      </button>

      <div class="adv" v-if="advanced">
        <div class="grid cols-2">
          <label class="field">提供方
            <input v-model="form.provider" class="mono" placeholder="deepseek / dashscope / ollama" />
          </label>
          <label class="field">base_url
            <input v-model="form.base_url" class="mono" placeholder="https://…/v1" />
          </label>
        </div>

        <label class="field gap-top">密钥文件名
          <input v-model="form.secret_ref" class="mono" placeholder="provider.key" />
        </label>
        <div class="chips" v-if="secrets.length">
          <button
            type="button" class="chip mono" v-for="s in secrets" :key="s"
            :class="{ on: form.secret_ref === s }"
            @click="form.secret_ref = s"
          >{{ s }}</button>
        </div>

        <label class="field gap-top">parameters
          <textarea v-model="form.parametersText" rows="5" class="mono" placeholder="{}"></textarea>
        </label>

        <label class="row small gap-top" v-if="mode === 'edit'">
          <input type="checkbox" v-model="form.enabled" /> 启用
        </label>
      </div>
    </div>

    <template #footer>
      <button class="btn" v-if="step === 2 && mode === 'create'" @click="step = 1">返回</button>
      <button class="btn" @click="emit('close')">取消</button>
      <button class="btn primary" v-if="step === 2" :disabled="busy" @click="submit">
        {{ busy ? '保存中…' : '保存' }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.gap-top { margin-top: 10px; }

/* 提供方卡片：等宽网格，卡片里三行文字（名字 / 地址 / 模型名）。
   选中态用强调色描边 + 浅底，不用阴影 —— 一张卡不该比弹窗更"重"。 */
.providers {
  display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fill, minmax(196px, 1fr));
}
.prov {
  display: flex; flex-direction: column; gap: 3px; align-items: flex-start;
  padding: 12px 14px; min-height: 74px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text); cursor: pointer;
  text-align: left; transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.prov:hover {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
  background: var(--bg-hover);
}
.prov:focus-visible { outline: none; box-shadow: var(--ring); }
.prov.on { border-color: var(--accent); background: var(--accent-soft); }
.pv-label { font-size: 13px; font-weight: 600; max-width: 100%; }
.pv-host { font-size: 11.5px; color: var(--text-dim); max-width: 100%; }
.pv-model { font-size: 11px; color: var(--text-faint); max-width: 100%; }
.prov.custom { border-style: dashed; }

/* 类型：全站那枚胶囊，一屏放得下六档 */
.types { height: var(--control-h-sm); }
.types button { height: calc(var(--control-h-sm) - 6px); font-size: 12px; padding: 0 11px; }

/* 模型名建议 chips：与「预设」同一套胶囊语言 */
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.chip {
  height: 26px; padding: 0 10px;
  border: 1px solid var(--divider); border-radius: 999px;
  background: transparent; color: var(--text-dim);
  font-size: 11.5px; cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.chip:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--divider)); background: var(--bg-hover); color: var(--text); }
.chip.on { border-color: var(--accent); background: var(--accent-soft); color: var(--accent-text); }

/* 「高级」折页：一条通栏的低调按钮，右侧带一行当前值摘要 */
.adv-toggle {
  display: flex; align-items: center; gap: 6px; width: 100%;
  margin-top: 12px; padding: 7px 2px;
  border: 0; border-top: 1px solid var(--divider);
  background: transparent; color: var(--text-dim); font-size: 12.5px; cursor: pointer;
}
.adv-toggle:hover { color: var(--text); }
.adv { padding-top: 10px; }

/* 推理等级：一行一档（值 / 说明 / 上移 / 下移 / 删），下方是默认档。
   说明空着也没关系 —— 它是给人看的，不参与请求。 */
.levels {
  margin-top: 12px; padding-top: 10px;
  border-top: 1px solid var(--divider);
}
.lv-head { display: flex; align-items: baseline; gap: 8px; }
.lv-title { font-size: 12.5px; color: var(--text-dim); }
.lv-broken { margin: 8px 0 0; font-size: 12px; line-height: 1.5; color: var(--danger); }
.lv-row {
  display: grid; grid-template-columns: 108px minmax(0, 1fr) auto auto auto;
  gap: 6px; align-items: center; margin-top: 8px;
}
.lv-row input { width: 100%; height: var(--control-h-sm); font-size: 12.5px; }
.lv-row .btn.icon { color: var(--text-faint); }
.lv-row .btn.icon:hover:not(:disabled) { color: var(--text); }
.lv-row .lv-del:hover:not(:disabled) { color: var(--danger); }
.lv-foot { margin-top: 8px; }
.lv-hint { margin: 8px 0 0; line-height: 1.6; }
.lv-fill {
  padding: 0; margin-left: 4px; border: 0; background: transparent;
  color: var(--accent-text); font-size: 11px; cursor: pointer;
}
.lv-fill:hover { text-decoration: underline; }
.lv-default { margin-top: 10px; }
</style>
