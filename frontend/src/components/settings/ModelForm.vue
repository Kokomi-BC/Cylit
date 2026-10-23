<script setup lang="ts">
/**
 * 新建 / 编辑模型的**纵向五步流**（弹窗内，单列、最多 560px 宽）。
 *
 * 1 选择提供方  → 可搜索下拉（图标 + 名称 + 一句说明），取代原来的厂商卡片墙
 * 2 连接与认证  → 已配置的提供方只读展示 Base URL / 认证方式（点右上角「连接设置」才可编辑）；
 *                 默认密钥自动选中（key_mode=auto），可换某一把或「+ 新建密钥」
 * 3 选择模型    → 先拉 POST /models/probe-models 再搜索选 Model ID；拉不到就手填。
 *                 **显示名称 name 与 Model ID model_name 是两个字段**，不互相猜
 * 4 任务用途    → model_type 单选；「设为该任务默认模型」单独勾选、**默认不勾**
 * 5 测试并保存  → 配置填写 / 连接测试 / 保存结果 三块各自独立；保存失败保留全部输入
 *
 * 后端降级：新接口（/models/providers、/models/providers/{id}/keys）在老进程上可能
 * 404 / 被 /models/{model_id} 捕获 —— 那时第一步只留「自定义提供方」、密钥只留
 * 「跟随默认 / 无需密钥」两档并给出提示，不白屏、不卡死。
 *
 * 注：**预设（presets）体系已下线** —— 这里不请求 /models/presets、不渲染任何预设卡片，
 * 第三步只靠 probe-models + 手填。defaultPreset 只是保留的入参名（父组件还在传），不再生效。
 *
 * 保存动作仍然交给父组件（emit('submit', payload)），错误由父级 toast + 本组件按 busy
 * 的起落判定「已提交但窗口没关」＝失败，并把输入原样留在屏幕上。
 *
 * 安全口径：明文只在「新建密钥」输入框的局部状态里，提交后立刻清空；不回显、不落 storage。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import type { ModelConfig, ModelPreset } from '@/api/types';
import { api } from '@/api/client';
import { reportError, toast } from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import { vendorColorIcon } from '@/assets/model-icons';
import type { KeyRow, ProviderRow } from './modelTypes';

const props = withDefaults(defineProps<{
  open: boolean;
  mode: 'create' | 'edit';
  target?: ModelConfig | null;
  /** 从哪个分区点的新建（agent 区 → 类型默认停在 agent） */
  defaultType?: string;
  /** 从类型卡片的「+」里挑中的预设 id：直接开在第二步，provider / base_url / 参数已填好 */
  defaultPreset?: string;
  /** 从「自定义提供方」进来：跳过第一步，直接开在第二步（名称 / Base URL / Model ID 必填） */
  customEntry?: boolean;
  /** 官方提供方卡片带进来的厂商（provider / base_url 由它预填） */
  seed?: { id: string; label?: string; base_url?: string; auth?: string } | null;
  types?: string[];
  typeLabels?: Record<string, string>;
  presets?: ModelPreset[];
  secrets?: string[];
  busy?: boolean;
  /** 可选：父组件拿到的保存错误文案（不传也能跑） */
  saveError?: string;
}>(), {
  target: null,
  defaultType: 'agent',
  defaultPreset: '',
  customEntry: false,
  seed: null,
  types: () => [],
  typeLabels: () => ({}),
  presets: () => [],
  secrets: () => [],
  busy: false,
  saveError: '',
});

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'submit', payload: Record<string, unknown>): void;
}>();

/* ---------------------------- 步骤 ---------------------------- */

const STEPS = [
  { id: 1, label: '选择提供方' },
  { id: 2, label: '连接与认证' },
  { id: 3, label: '选择模型' },
  { id: 4, label: '任务用途' },
  { id: 5, label: '测试并保存' },
] as const;

const step = ref<number>(1);
/** 已走到过的最远一步：回退时不超过它（防止跳步绕过校验） */
const reached = ref<number>(1);
/** 用户亲手改过「显示名称」没有：没改过就跟着 Model ID 自动填（不沿用上一次的记忆） */
const nameTouched = ref(false);

function goStep(n: number): void {
  if (n < 1 || n > 5) return;
  if (n > reached.value) return;
  step.value = n;
}

function prevStep(): void {
  if (step.value > 1) step.value -= 1;
}

function advance(): void {
  step.value = Math.min(5, step.value + 1);
  reached.value = Math.max(reached.value, step.value);
}

/* ---------------------------- 表单状态 ---------------------------- */

type AuthKind = 'bearer' | 'none';

const FORM_TYPES = ['generation', 'agent', 'embedding', 'reranker', 'vision'];

const form = reactive({
  name: '',
  model_type: 'agent',
  provider: '',
  model_name: '',
  base_url: '',
  secret_ref: '',
  provider_key_id: '',
  parametersText: '{}',
  enabled: true,
  is_default: false,
});

const typesList = computed<string[]>(() => (props.types && props.types.length ? props.types : FORM_TYPES));

function labelOf(t: string): string {
  return props.typeLabels?.[t] || t;
}

const formError = ref('');

/* ---------------------------- 第一步：提供方 ---------------------------- */

const providers = ref<ProviderRow[]>([]);
const providersLoading = ref(false);
/** 提供方目录接口不可用 → 退回预设目录（老后端的正常路径，不是错误） */
const providersFallback = ref(false);
const providerQuery = ref('');
const selectedProviderId = ref('');
const customMode = ref(false);

const selectedProvider = computed<ProviderRow | null>(() => {
  const id = selectedProviderId.value;
  if (!id) return null;
  return providers.value.find((p) => p.id === id) ?? null;
});

function hostOf(url?: string | null): string {
  if (!url) return '';
  try { return new URL(url).host; } catch { return String(url); }
}

function iconOf(id?: string | null): string {
  return vendorColorIcon(id);
}

async function loadProviders(): Promise<void> {
  providersLoading.value = true;
  providersFallback.value = false;
  try {
    const data = await api.get<{ providers?: ProviderRow[] }>('/models/providers');
    const rows = Array.isArray(data?.providers) ? data.providers : [];
    providers.value = rows;
    providersFallback.value = rows.length === 0;
  } catch {
    /* 404 / 400（老后端把 providers 当成 model_id 了）：**不拿预设凑数**（预设体系已下线），
       只留「自定义提供方」这一条路，并写清楚原因。 */
    providers.value = [];
    providersFallback.value = true;
  } finally {
    providersLoading.value = false;
  }
}

const filteredProviders = computed<ProviderRow[]>(() => {
  const q = providerQuery.value.trim().toLowerCase();
  if (!q) return providers.value;
  return providers.value.filter((p) => {
    const hay = ((p.label || '') + ' ' + (p.id || '') + ' ' + (p.note || '')).toLowerCase();
    return hay.includes(q);
  });
});

function providerNote(p: ProviderRow): string {
  const bits: string[] = [];
  if (p.note) bits.push(p.note);
  else if (p.base_url) bits.push(hostOf(p.base_url));
  if (p.configured) bits.push(String(p.configured) + ' 个模型');
  if (p.key_count) bits.push(String(p.key_count) + ' 把密钥');
  return bits.join(' · ');
}

/* ---------------------------- 第二步：连接与认证 ---------------------------- */

const authKind = ref<AuthKind>('bearer');
const connEditing = ref(false);

const connReadonly = computed<boolean>(() => {
  if (customMode.value) return false;
  if (connEditing.value) return false;
  const p = selectedProvider.value;
  if (!p) return false;
  return !!(p.configured || p.has_base_url || p.base_url);
});

const showConnFields = computed<boolean>(() => !connReadonly.value);

const keys = ref<KeyRow[]>([]);
const unassignedKeys = ref<KeyRow[]>([]);
const keysLoading = ref(false);
/** 密钥管理接口不可用（404）：只留「跟随默认 / 无需密钥」两档 */
const keysUnavailable = ref(false);

/** 'auto' | 'none' | 某把密钥的 id */
const keyChoice = ref<string>('auto');

const defaultKeyLabel = computed<string>(() => {
  const p = selectedProvider.value;
  if (p?.default_key_label) return String(p.default_key_label);
  const d = keys.value.find((k) => k.is_default);
  return d ? (d.label || d.id) : '';
});

const autoKeyLabel = computed<string>(() => {
  if (keysUnavailable.value) return '跟随提供方默认密钥';
  return defaultKeyLabel.value
    ? '跟随提供方默认密钥（' + defaultKeyLabel.value + '）'
    : '跟随提供方默认密钥';
});

const keyMode = computed<'auto' | 'key' | 'none'>(() => {
  if (keyChoice.value === 'none') return 'none';
  if (!keyChoice.value || keyChoice.value === 'auto') return 'auto';
  return 'key';
});

function keyStatusText(status?: string | null): string {
  if (!status) return '未测试';
  if (status === 'ok') return '可用';
  if (status === 'invalid' || status === 'error') return '不可用';
  if (status === 'missing') return '文件缺失';
  return String(status);
}

function keyOptionLabel(k: KeyRow): string {
  const bits = [k.label || k.id];
  if (k.masked) bits.push(k.masked);
  if (k.is_default) bits.push('默认');
  bits.push(k.unassigned ? '未归属' : keyStatusText(k.status));
  return bits.join(' · ');
}

async function loadKeys(providerId: string): Promise<void> {
  keys.value = [];
  unassignedKeys.value = [];
  keysUnavailable.value = false;
  if (!providerId) return;
  keysLoading.value = true;
  try {
    /* 真实后端回 {items, unassigned}（信封 data 里）；老写法 / 别处契约用过 keys，两种都认 */
    const data = await api.get<{ items?: KeyRow[]; keys?: KeyRow[]; unassigned?: KeyRow[] }>(
      '/models/providers/' + encodeURIComponent(providerId) + '/keys',
    );
    const list = data?.keys ?? data?.items;
    keys.value = Array.isArray(list) ? list : [];
    unassignedKeys.value = Array.isArray(data?.unassigned) ? data.unassigned : [];
  } catch {
    /* 接口没上线（404）→ 降级：只给两档，别把整个表单卡死 */
    keysUnavailable.value = true;
    keys.value = [];
    unassignedKeys.value = [];
  } finally {
    keysLoading.value = false;
  }
}

/* 新建密钥：明文只在这两个局部状态里，成功即清空 */
const newKeyOpen = ref(false);
const newKeyLabel = ref('');
const newKeyValue = ref('');
const newKeyBusy = ref(false);
const newKeyError = ref('');

const canCreateKey = computed<boolean>(() => !!form.provider && !keysUnavailable.value);

async function createKey(): Promise<void> {
  const label = newKeyLabel.value.trim();
  const value = newKeyValue.value.trim();
  newKeyError.value = '';
  if (!label) { newKeyError.value = '给这把密钥起个名字'; return; }
  if (!value) { newKeyError.value = '粘贴密钥内容'; return; }
  newKeyBusy.value = true;
  try {
    const res = await api.post<any>(
      '/models/providers/' + encodeURIComponent(form.provider) + '/keys',
      { label, api_key: value },
    );
    newKeyValue.value = '';                 // 明文不留痕
    newKeyLabel.value = '';
    newKeyOpen.value = false;
    await loadKeys(form.provider);
    const created = (res?.item ?? res?.key ?? res) as any;
    const id = String(created?.id || '');
    keyChoice.value = id || 'auto';
    toast('密钥已添加并选中', 'ok');
  } catch (err) {
    newKeyError.value = String((err as any)?.message || err || '新建密钥失败');
  } finally {
    newKeyBusy.value = false;
  }
}

/* ---------------------------- 第三步：选择模型 ---------------------------- */

const probeOptions = ref<string[]>([]);
const probeBusy = ref(false);
const probeError = ref('');
const probeHost = ref('');
const modelQuery = ref('');
const modelPopOpen = ref(false);
const modelFieldEl = ref<HTMLElement | null>(null);
const modelPopStyle = ref<Record<string, string>>({});

const modelCandidates = computed<string[]>(() => {
  const q = modelQuery.value.trim().toLowerCase();
  if (!q) return probeOptions.value;
  return probeOptions.value.filter((id) => id.toLowerCase().includes(q));
});

async function fetchRemoteModels(): Promise<void> {
  probeError.value = '';
  probeOptions.value = [];
  probeHost.value = '';
  const editId = props.mode === 'edit' && props.target ? String(props.target.id || '') : '';
  /* 表单填的优先；空则跟随提供方默认端点（官方提供方不需要用户手填） */
  const baseUrl = form.base_url.trim() || providerBaseUrl.value;
  if (!editId && !baseUrl) {
    probeError.value = '这家提供方没有默认端点：先填 Base URL，再拉模型列表';
    return;
  }
  probeBusy.value = true;
  try {
    const body: Record<string, unknown> = {};
    if (editId) {
      /* 已存配置：后端用库里那行的 base_url + 密钥去拉（空 base_url 由它自己判定） */
      body.model_id = editId;
    } else {
      body.base_url = baseUrl;
      /* 探测请求本身也要带对密钥，否则「跟随提供方默认密钥」的端点（豆包/火山方舟这类
         不公开模型列表的）一律 401：后端按 provider 解析默认密钥、按 key_id 解析指定密钥。 */
      if (form.provider.trim()) body.provider = form.provider.trim();
      if (keyMode.value === 'key' && keyChoice.value) body.key_id = keyChoice.value;
      if (keyMode.value === 'none') body.key_mode = 'none';
      if (form.secret_ref.trim()) body.secret_ref = form.secret_ref.trim();
    }
    const data = await api.post<{ items?: Array<{ id?: string }>; count?: number; host?: string }>(
      '/models/probe-models', body,
    );
    const ids = (data?.items || []).map((x) => String(x?.id || '')).filter(Boolean);
    probeOptions.value = ids;
    probeHost.value = String(data?.host || '');
    if (!ids.length) {
      probeError.value = '端点没有返回任何模型，直接手填 Model ID';
    } else {
      placeModelPop();
    }
  } catch (err) {
    const msg = String((err as any)?.message || err || '未知错误');
    if (editId && !form.base_url.trim() && providerBaseUrl.value && /base url/i.test(msg)) {
      probeError.value = '这条模型没存 Base URL（跟随提供方端点），列表接口用不上 —— 直接手填 Model ID 也能保存';
    } else {
      probeError.value = '拉取失败：' + msg;
    }
  } finally {
    probeBusy.value = false;
  }
}

/** 进第 3 步自动拉一次列表（同一家/同一端点只自动试一次，失败后交给「重试」）。 */
const autoProbeKey = ref('');
watch([step, () => form.provider, () => form.base_url], () => {
  if (step.value !== 3) { modelPopOpen.value = false; return; }
  if (probeBusy.value || probeOptions.value.length) return;
  const key = [props.mode, form.provider, form.base_url].join('|');
  if (autoProbeKey.value === key) return;
  autoProbeKey.value = key;
  void fetchRemoteModels();
}, { flush: 'post' });

/** 选中一条时：model_name 写 id；显示名称在用户没亲手改过时自动跟着这条 ID 走。 */
function pickModelId(id: string): void {
  form.model_name = id;
  syncNameFromModel(id);
  modelPopOpen.value = false;
  modelQuery.value = '';
}

/** 显示名称跟随 Model ID：没亲手改过、或者已经清空 → 自动填成 ID（换一条就跟着换）。 */
function syncNameFromModel(id: string): void {
  if (nameTouched.value && form.name.trim()) return;
  form.name = String(id || '').trim();
}

function placeModelPop(): void {
  const el = modelFieldEl.value;
  const vw = typeof window === 'undefined' ? 1024 : window.innerWidth;
  if (!el) { modelPopOpen.value = true; return; }
  const r = el.getBoundingClientRect();
  const width = Math.min(Math.max(r.width, 240), vw - 24);
  const left = Math.max(12, Math.min(r.left, vw - width - 12));
  modelPopStyle.value = {
    top: String(Math.round(r.bottom + 4)) + 'px',
    left: String(Math.round(left)) + 'px',
    width: String(Math.round(width)) + 'px',
  };
  modelPopOpen.value = true;
}

function toggleModelPop(): void {
  if (modelPopOpen.value) { modelPopOpen.value = false; return; }
  placeModelPop();
}

function onDocPointer(ev: MouseEvent): void {
  const t = ev.target as HTMLElement | null;
  if (!t || typeof t.closest !== 'function') return;
  if (t.closest('.model-pop') || t.closest('.model-field')) return;
  modelPopOpen.value = false;
}

/** 视口变化/滚动时收起下拉；但**在列表内部滚动**不算（否则一滚就没了）。 */
function onViewport(ev?: Event): void {
  if (!modelPopOpen.value) return;
  const t = ev?.target as HTMLElement | null;
  if (t && typeof t.closest === 'function' && t.closest('.model-pop')) return;
  modelPopOpen.value = false;
}

onMounted(() => {
  document.addEventListener('mousedown', onDocPointer);
  window.addEventListener('resize', onViewport);
  window.addEventListener('scroll', onViewport, true);
});

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocPointer);
  window.removeEventListener('resize', onViewport);
  window.removeEventListener('scroll', onViewport, true);
});

/* ---------------------------- 第四步 / 第五步 ---------------------------- */

const isDefaultHint = '现有默认模型不会被改动（不勾就是纯新增）';

const testState = ref<'idle' | 'running' | 'ok' | 'fail'>('idle');
const testMessage = ref('');

const testModelId = computed<string>(() => (
  props.mode === 'edit' && props.target ? String(props.target.id || '') : ''
));

async function testConnection(): Promise<void> {
  testMessage.value = '';
  if (!testModelId.value) {
    testState.value = 'idle';
    testMessage.value = '保存后可测试连接（还没有模型 id）';
    return;
  }
  testState.value = 'running';
  try {
    const body: Record<string, unknown> = {};
    if (keyMode.value === 'key' && keyChoice.value) body.key_id = keyChoice.value;
    const res = await api.post<any>('/models/' + encodeURIComponent(testModelId.value) + '/test', body);
    const ok = res?.ok !== false;
    testState.value = ok ? 'ok' : 'fail';
    const latency = res?.latency_ms ?? res?.latency;
    const msg = String(res?.message || res?.error || res?.detail || '').trim();
    const parts = ok
      ? ['连接成功', latency ? String(latency) + ' ms' : '', msg]
      : [msg || '连接失败'];
    testMessage.value = parts.filter(Boolean).join(' · ');
  } catch (err) {
    testState.value = 'fail';
    testMessage.value = String((err as any)?.message || err || '连接失败');
  }
}

/** 保存结果：父组件持请求，这里靠 busy 的起落判断「已提交但窗口没关」= 失败。 */
const saveState = ref<'idle' | 'saving' | 'failed' | 'saved'>('idle');
const saveMessage = ref('');

watch(() => props.busy, (now, before) => {
  if (now) {
    saveState.value = 'saving';
    return;
  }
  if (before && saveState.value === 'saving') {
    if (props.open) {
      saveState.value = 'failed';
      saveMessage.value = props.saveError || '保存失败（原因见上方提示），已填内容全部保留，可直接重试';
    } else {
      saveState.value = 'saved';
      saveMessage.value = '已保存';
    }
  }
});

/* ---------------------------- 高级配置（默认折叠） ---------------------------- */

const advanced = ref(false);
const BUILTIN_LEVELS = ['off', 'low', 'medium', 'high', 'max'];
const deviceValue = ref('');
const timeoutValue = ref('');

interface LevelRow { value: string; desc: string }

const levels = ref<LevelRow[]>([]);
const defaultLevel = ref('');
const paramsBroken = ref('');

function parseParamsSafe(text: string): Record<string, any> | null {
  const s = String(text ?? '').trim();
  if (!s) return {};
  try {
    const parsed = JSON.parse(s);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
  } catch { /* 落到最后统一返回 null */ }
  return null;
}

/** 那份 JSON 引起的 watch 不再反过来重读编辑器（避免自己覆盖自己） */
let writtenText = '';

function syncExtras(p: Record<string, any>): void {
  deviceValue.value = p.device == null ? '' : String(p.device);
  timeoutValue.value = p.timeout == null ? '' : String(p.timeout);
}

function syncReasoning(): void {
  const p = parseParamsSafe(form.parametersText);
  if (!p) {
    paramsBroken.value = '当前 parameters 不是合法 JSON，先修好再用这块编辑';
    levels.value = [];
    defaultLevel.value = '';
    deviceValue.value = '';
    timeoutValue.value = '';
    return;
  }
  paramsBroken.value = '';
  const raw = p.reasoning_levels;
  levels.value = Array.isArray(raw)
    ? raw.map((it: any) => ({ value: String(it?.value ?? ''), desc: String(it?.desc ?? '') }))
    : [];
  const eff = p.reasoning_effort;
  defaultLevel.value = eff === undefined || eff === null ? '' : String(eff);
  syncExtras(p);
}

function writeParams(mutate: (next: Record<string, any>) => void): void {
  if (paramsBroken.value) return;
  const p = parseParamsSafe(form.parametersText);
  if (!p) return;
  const next: Record<string, any> = { ...p };
  mutate(next);
  const text = Object.keys(next).length ? JSON.stringify(next, null, 2) : '{}';
  if (text === form.parametersText) return;
  writtenText = text;
  form.parametersText = text;
}

function writeReasoning(): void {
  writeParams((next) => {
    const rows = levels.value
      .map((r) => ({ value: r.value.trim(), desc: (r.desc || '').trim() }))
      .filter((r) => r.value)
      .map((r) => (r.desc ? r : { value: r.value }));
    if (rows.length) next.reasoning_levels = rows;
    else delete next.reasoning_levels;
    const eff = defaultLevel.value.trim();
    if (eff) next.reasoning_effort = eff;
    else delete next.reasoning_effort;
  });
}

/** 高级里的 device / 超时：写回 parameters 的 device / timeout 两个键，其余原样保留。 */
function writeExtras(): void {
  writeParams((next) => {
    const d = deviceValue.value.trim();
    if (d) next.device = d;
    else delete next.device;
    const t = timeoutValue.value.trim();
    if (t) next.timeout = /^\d+$/.test(t) ? Number(t) : t;
    else delete next.timeout;
  });
}

function addLevel(): void { levels.value.push({ value: '', desc: '' }); }
function removeLevel(i: number): void { levels.value.splice(i, 1); }

function moveLevel(i: number, delta: number): void {
  const j = i + delta;
  if (j < 0 || j >= levels.value.length) return;
  levels.value.splice(j, 0, ...levels.value.splice(i, 1));
}

function fillBuiltinLevels(): void {
  levels.value = BUILTIN_LEVELS.map((v) => ({ value: v, desc: '' }));
}

const levelValues = computed<string[]>(() => {
  const out: string[] = [];
  for (const r of levels.value) {
    const v = r.value.trim();
    if (v && !out.includes(v)) out.push(v);
  }
  const d = defaultLevel.value.trim();
  if (d && !out.includes(d)) out.push(d);
  return out;
});

function levelError(): string {
  const seen = new Set<string>();
  for (let i = 0; i < levels.value.length; i += 1) {
    const v = levels.value[i].value.trim();
    if (!v) return '推理等级第 ' + String(i + 1) + ' 行还没填值';
    if (seen.has(v)) return '推理等级「' + v + '」重复了，同一模型内的值要唯一';
    seen.add(v);
  }
  return '';
}

watch(() => form.parametersText, (t) => { if (t !== writtenText) syncReasoning(); });
watch(levels, writeReasoning, { deep: true });
watch(defaultLevel, writeReasoning);

/* 老接口的「密钥文件」写法（POST /models/secret）保留在高级里：迁移期仍然可用 */
const keyValue = ref('');
const keyBusy = ref(false);

const secretName = computed<string>(() => {
  const base = String(form.secret_ref || '').trim();
  if (base) return base;
  const prov = String(form.provider || '').trim().toLowerCase().replace(/[^a-z0-9_.-]/g, '');
  return (prov || 'custom') + '.key';
});

async function writeKey(): Promise<void> {
  const value = keyValue.value.trim();
  if (!value) { toast('先粘贴密钥内容', 'warn'); return; }
  keyBusy.value = true;
  try {
    const data = await api.post<{ bytes?: number }>('/models/secret', { name: secretName.value, value });
    form.secret_ref = secretName.value;
    keyValue.value = '';
    toast('密钥已写入本机' + (data?.bytes ? ' · ' + String(data.bytes) + ' 字节' : ''), 'ok');
  } catch (err) {
    reportError(err, '写入密钥失败');
  } finally {
    keyBusy.value = false;
  }
}

/* ---------------------------- 初始化 / 切换提供方 ---------------------------- */

function resetTransient(): void {
  formError.value = '';
  probeOptions.value = [];
  probeError.value = '';
  probeHost.value = '';
  probeBusy.value = false;
  autoProbeKey.value = '';
  modelQuery.value = '';
  modelPopOpen.value = false;
  keys.value = [];
  unassignedKeys.value = [];
  keysLoading.value = false;
  keysUnavailable.value = false;
  keyChoice.value = 'auto';
  newKeyOpen.value = false;
  newKeyLabel.value = '';
  newKeyValue.value = '';
  newKeyError.value = '';
  newKeyBusy.value = false;
  keyValue.value = '';
  keyBusy.value = false;
  connEditing.value = false;
  advanced.value = false;
  testState.value = 'idle';
  testMessage.value = '';
  saveState.value = 'idle';
  saveMessage.value = '';
  providerQuery.value = '';
  customMode.value = false;
  selectedProviderId.value = '';
  writtenText = '';
}

async function init(): Promise<void> {
  resetTransient();
  form.model_type = props.defaultType || 'agent';
  form.is_default = false;
  step.value = 1;
  reached.value = 1;

  const editing = props.mode === 'edit' && !!props.target;
  nameTouched.value = editing;
  if (editing && props.target) {
    const t = props.target as ModelConfig & {
      key_mode?: string | null;
      provider_key_id?: string | null;
      is_default?: boolean;
    };
    Object.assign(form, {
      name: t.name || '',
      model_type: t.model_type || props.defaultType || 'agent',
      provider: t.provider || '',
      model_name: t.model_name || '',
      base_url: t.base_url || '',
      secret_ref: t.secret_ref || '',
      provider_key_id: t.provider_key_id || '',
      parametersText: JSON.stringify(t.parameters || {}, null, 2),
      enabled: t.enabled !== false,
      is_default: t.is_default === true,
    });
    keyChoice.value = t.key_mode === 'none'
      ? 'none'
      : (t.key_mode === 'key' && t.provider_key_id ? String(t.provider_key_id) : 'auto');
  }

  await loadProviders();

  if (editing) {
    selectedProviderId.value = form.provider;
    authKind.value = selectedProvider.value?.auth === 'none' ? 'none' : 'bearer';
    /* 编辑一条已存在的模型：连接信息早已在库里，直接落在「选择模型」；
       步骤条仍可点回 1 / 2（reached 记到 3，别把回退拦掉）。 */
    step.value = 3;
    reached.value = 3;
    await loadKeys(form.provider);
    return;
  }

  if (props.seed) {
    const id = String(props.seed.id || '');
    form.provider = id;
    form.base_url = props.seed.base_url || '';
    authKind.value = props.seed.auth === 'none' ? 'none' : 'bearer';
    selectedProviderId.value = id;
    step.value = 2;
    reached.value = 2;
    await loadKeys(id);
    if (authKind.value === 'none' && !keys.value.length) keyChoice.value = 'none';
    return;
  }

  if (props.customEntry) {
    customMode.value = true;
    form.provider = '';
    form.base_url = '';
    authKind.value = 'bearer';
    connEditing.value = true;
    step.value = 2;
    reached.value = 2;
    return;
  }

  /* 预设体系已下线：不再有「从预设开在第二步」这条分支，一律从第一步选提供方开始。 */
}

watch(() => props.open, (v) => {
  if (!v) return;
  void init().then(() => syncReasoning());
}, { immediate: true });

/** 选一家提供方：清掉跨提供方的选择（密钥 + 已填 Model ID），再进第二步。 */
async function pickProvider(p: ProviderRow): Promise<void> {
  customMode.value = false;
  selectedProviderId.value = p.id;
  form.provider = p.id;
  form.base_url = p.base_url || '';
  authKind.value = p.auth === 'none' ? 'none' : 'bearer';
  connEditing.value = false;
  form.provider_key_id = '';
  keyChoice.value = 'auto';
  form.model_name = '';
  form.name = '';
  nameTouched.value = false;
  probeOptions.value = [];
  probeError.value = '';
  probeHost.value = '';
  modelQuery.value = '';
  newKeyOpen.value = false;
  newKeyLabel.value = '';
  newKeyValue.value = '';
  await loadKeys(p.id);
  if (authKind.value === 'none' && !keys.value.length) keyChoice.value = 'none';
  step.value = 2;
  reached.value = Math.max(reached.value, 2);
}

function pickCustom(): void {
  customMode.value = true;
  selectedProviderId.value = '';
  form.provider = '';
  form.base_url = '';
  authKind.value = 'bearer';
  connEditing.value = true;
  form.provider_key_id = '';
  keyChoice.value = 'none';
  form.model_name = '';
  form.name = '';
  keys.value = [];
  unassignedKeys.value = [];
  keysUnavailable.value = false;
  step.value = 2;
  reached.value = Math.max(reached.value, 2);
}

/* ---------------------------- 前进校验 ---------------------------- */

const baseUrlOk = computed<boolean>(() => /^https?:\/\/\S+/i.test(form.base_url.trim()));

/** 以 form.provider 为准找目录行（改过「提供方名称」后 selectedProviderId 会过期）。 */
const activeProvider = computed<ProviderRow | null>(() => {
  const id = form.provider.trim();
  if (!id) return null;
  return providers.value.find((p) => p.id === id) ?? null;
});

/** 这家提供方的默认端点；空 = 目录 / provider 表里没有可跟随的端点。 */
const providerBaseUrl = computed<string>(() => String(activeProvider.value?.base_url || '').trim());

/**
 * Base URL 空 = 合法：官方提供方由后端按提供方 / 协议解析默认端点（多模态向量走
 * DashScope 原生 host，写死兼容层路径反而会打到错端点）。只有查不到默认端点的
 * 自定义提供方才必填。
 */
const baseUrlRequired = computed<boolean>(() => {
  if (customMode.value) return true;
  const id = form.provider.trim();
  if (!id || id === 'custom') return true;
  const p = activeProvider.value;
  if (!p) return true;                       // 目录里没有这家 → 没有可跟随的端点
  return !providerBaseUrl.value && !p.has_base_url;
});

/** 真的缺 Base URL：必填且没填，或填了但不是 http(s) */
const baseUrlMissing = computed<boolean>(() => !baseUrlOk.value && baseUrlRequired.value);

const configFilled = computed<boolean>(() => (
  !!form.provider.trim() && !baseUrlMissing.value && !!form.model_name.trim() && !!form.name.trim()
));

const configText = computed<string>(() => {
  const missing: string[] = [];
  if (!form.provider.trim()) missing.push('提供方名称');
  if (baseUrlMissing.value) missing.push('Base URL');
  if (!form.model_name.trim()) missing.push('Model ID');
  if (!form.name.trim()) missing.push('显示名称');
  if (missing.length) return '还差：' + missing.join(' / ');
  const base = form.base_url.trim() ? 'Base URL' : 'Base URL（跟随提供方）';
  return '提供方 / ' + base + ' / Model ID / 显示名称都齐了';
});

const canNext = computed<boolean>(() => {
  if (step.value === 1) return !!selectedProviderId.value || customMode.value;
  if (step.value === 2) return !!form.provider.trim() && !baseUrlMissing.value;
  if (step.value === 3) return !!form.model_name.trim();
  return true;
});

function nextStep(): void {
  formError.value = '';
  if (step.value === 1) {
    if (!selectedProviderId.value && !customMode.value) { toast('先选一个提供方', 'warn'); return; }
    advance();
    return;
  }
  if (step.value === 2) {
    if (!form.provider.trim()) { formError.value = '提供方名称必填（写进 provider 字段，如 my-vendor）'; return; }
    if (baseUrlMissing.value) {
      formError.value = form.base_url.trim()
        ? 'Base URL 要以 http:// 或 https:// 开头'
        : 'Base URL 必填（这家提供方没有默认端点）';
      return;
    }
    advance();
    return;
  }
  if (step.value === 3) {
    if (!form.model_name.trim()) { formError.value = '先选或手填 Model ID'; return; }
    advance();
    return;
  }
  advance();
}

/* ---------------------------- 提交 ---------------------------- */

function parseParameters(text: string): Record<string, any> {
  const s = String(text ?? '').trim();
  if (!s) return {};
  let parsed: any;
  try {
    parsed = JSON.parse(s);
  } catch (err: any) {
    throw new Error('parameters 不是合法 JSON：' + String(err?.message || err));
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('parameters 必须是 JSON 对象');
  }
  return parsed;
}

function submit(): void {
  formError.value = '';
  saveMessage.value = '';
  const fail = (msg: string): void => {
    formError.value = msg;
    saveState.value = 'failed';
    saveMessage.value = msg;
    toast(msg, 'warn');
  };
  if (!form.name.trim()) { fail('显示名称必填'); return; }
  if (!form.model_name.trim()) { fail('Model ID 必填（先拉列表或手填）'); return; }
  if (!form.provider.trim()) { fail('提供方名称必填'); return; }
  if (baseUrlMissing.value) {
    fail(form.base_url.trim()
      ? 'Base URL 要以 http:// 或 https:// 开头'
      : 'Base URL 必填（这家提供方没有默认端点）');
    return;
  }
  writeReasoning();
  writeExtras();
  let parameters: Record<string, any>;
  try {
    parameters = parseParameters(form.parametersText);
  } catch (err: any) {
    advanced.value = true;
    fail(String(err?.message || err));
    return;
  }
  if (form.model_type === 'agent') {
    const bad = levelError();
    if (bad) { advanced.value = true; fail(bad); return; }
  }
  const payload: Record<string, unknown> = {
    name: form.name.trim(),
    provider: form.provider.trim(),
    model_type: form.model_type,
    model_name: form.model_name.trim() || null,
    base_url: form.base_url.trim() || null,
    secret_ref: form.secret_ref.trim() || null,
    key_mode: keyMode.value,
    provider_key_id: keyMode.value === 'key' ? keyChoice.value : null,
    is_default: !!form.is_default,
    parameters,
  };
  if (props.mode === 'edit') payload.enabled = !!form.enabled;
  saveState.value = 'saving';
  saveMessage.value = '提交中…';
  emit('submit', payload);
}

const advSummary = computed<string>(() => {
  const manual = form.base_url.trim();
  const parts = [manual
    ? (hostOf(manual) || manual)
    : (providerBaseUrl.value ? hostOf(providerBaseUrl.value) + '（跟随提供方）' : '未填 base_url')];
  if (keyMode.value === 'none') parts.push('无密钥');
  else if (keyMode.value === 'key') parts.push('指定密钥');
  else parts.push(form.secret_ref ? '密钥文件 ' + form.secret_ref : '跟随提供方默认密钥');
  return parts.join(' · ');
});
</script>

<template>
  <Modal
    :open="open"
    :title="mode === 'edit' ? '编辑模型' : '新建模型'"
    @close="emit('close')"
  >
    <div class="flow">
      <!-- 步骤条：一行文字 + 18px 序号圆，只有走过的步骤能点回去 -->
      <nav class="steps" aria-label="步骤">
        <template v-for="(s, i) in STEPS" :key="s.id">
          <span v-if="i > 0" class="st-line" aria-hidden="true"></span>
          <button
            type="button" class="st-step"
            :class="{ on: step === s.id, done: step > s.id }"
            :disabled="s.id > reached"
            :aria-current="step === s.id ? 'step' : undefined"
            :data-step-pill="s.id"
            @click="goStep(s.id)"
          >
            <span class="st-num">
              <AppIcon v-if="step > s.id" name="check" :size="11" />
              <template v-else>{{ s.id }}</template>
            </span>
            <span class="st-txt">{{ s.label }}</span>
          </button>
        </template>
      </nav>

      <!-- ① 选择提供方 -->
      <section v-if="step === 1" class="step-body" data-step="1">
        <div class="search-field prov-search">
          <AppIcon name="search" :size="14" />
          <input
            v-model="providerQuery" type="text" spellcheck="false"
            placeholder="搜索提供方（名称 / id / 说明）"
            data-provider-search
          />
        </div>

        <p class="hint-line warn" v-if="providersFallback" data-providers-unavailable>
          提供方目录接口暂不可用 —— 目前只能走「自定义提供方」（需重启后端进程才能列出厂商）。
        </p>
        <p class="hint-line" v-if="providersLoading">正在读取提供方…</p>

        <div class="prov-list">
          <button
            v-for="p in filteredProviders" :key="p.id"
            type="button" class="prov-item"
            :class="{ on: selectedProviderId === p.id }"
            :data-provider="p.id"
            @click="pickProvider(p)"
          >
            <img v-if="iconOf(p.id)" class="pv-icon" :src="iconOf(p.id)" :alt="p.label" />
            <span v-else class="pv-icon pv-icon-ph" aria-hidden="true"></span>
            <span class="pv-text">
              <span class="pv-label truncate">{{ p.label || p.id }}</span>
              <span class="pv-note truncate">{{ providerNote(p) || '自定义端点' }}</span>
            </span>
            <AppIcon name="chevron-right" :size="14" class="pv-go" />
          </button>

          <button type="button" class="prov-item custom" data-provider-custom @click="pickCustom">
            <span class="pv-icon pv-icon-ph" aria-hidden="true"></span>
            <span class="pv-text">
              <span class="pv-label">自定义提供方</span>
              <span class="pv-note">自己填名称 / Base URL / Model ID</span>
            </span>
            <AppIcon name="chevron-right" :size="14" class="pv-go" />
          </button>

          <p class="hint-line empty" v-if="!filteredProviders.length && !providersLoading">
            没有匹配的提供方，试试「自定义提供方」。
          </p>
        </div>
      </section>

      <!-- ② 连接与认证 -->
      <section v-else-if="step === 2" class="step-body" data-step="2">
        <div class="step-head">
          <h3 class="sh-title">
            <img v-if="iconOf(form.provider)" class="sh-icon" :src="iconOf(form.provider)" :alt="form.provider" />
            <span v-else class="sh-icon sh-icon-ph" aria-hidden="true"></span>
            {{ selectedProvider?.label || form.provider || '自定义提供方' }}
          </h3>
          <span class="spacer"></span>
          <!-- 连接信息默认只读（Base URL 留空 = 跟随提供方端点）；「连接设置」这一枚按钮就地展开成输入框。
               原来这里还有一枚「换提供方」，和底部「上一步」是同一件事（都回第 1 步），合并掉了。 -->
          <button
            v-if="!customMode && selectedProvider"
            type="button" class="lnk" data-conn-edit
            :aria-pressed="connEditing"
            @click="connEditing = !connEditing"
          >{{ connEditing ? '完成' : '连接设置' }}</button>
        </div>

        <div class="fields">
          <!-- 已配置：连接信息只读（Base URL 留空 = 跟随提供方端点），点右上角「连接设置」才变成输入框 -->
          <template v-if="connReadonly">
            <div class="fgroup">
              <div class="field-row">
                <span class="fr-k">Base URL</span>
                <span v-if="form.base_url.trim()" class="fr-v mono truncate" :title="form.base_url">{{ form.base_url }}</span>
                <span
                  v-else-if="providerBaseUrl" class="fr-v truncate" data-baseurl-auto
                  :title="'留空 = 跟随提供方端点；多模态向量等特殊协议由后端适配器解析'"
                >自动（跟随提供方：{{ hostOf(providerBaseUrl) }}）</span>
                <span v-else class="fr-v warn" data-baseurl-missing>未设置</span>
              </div>
              <p class="err-line" v-if="!form.base_url.trim() && !providerBaseUrl" data-baseurl-required>
                需要填 Base URL（这家提供方没有默认端点）
              </p>
            </div>
            <div class="field-row">
              <span class="fr-k">认证方式</span>
              <span class="fr-v">{{ authKind === 'none' ? '无需密钥' : 'Bearer（API Key）' }}</span>
            </div>
          </template>

          <template v-if="showConnFields">
            <label class="field">提供方名称
              <input v-model="form.provider" class="mono" spellcheck="false" placeholder="my-endpoint" data-conn-provider />
            </label>
            <div class="fgroup">
              <label class="field">Base URL
                <input v-model="form.base_url" class="mono" spellcheck="false" placeholder="https://api.example.com/v1" data-conn-baseurl />
              </label>
              <p class="hint-line" v-if="providerBaseUrl">留空 = 跟随提供方端点（{{ hostOf(providerBaseUrl) }}）</p>
              <p class="err-line" v-if="form.base_url.trim() && !baseUrlOk">Base URL 要以 http:// 或 https:// 开头</p>
            </div>
            <label class="field">认证方式
              <select v-model="authKind" data-conn-auth>
                <option value="bearer">Bearer（需要 API Key）</option>
                <option value="none">无需密钥</option>
              </select>
            </label>
          </template>

          <template v-if="!customMode">
            <div class="fgroup">
              <label class="field">API Key
                <select v-model="keyChoice" :disabled="keysLoading" data-key-select>
                  <option value="auto">{{ autoKeyLabel }}</option>
                  <option v-for="k in keys" :key="k.id" :value="k.id">{{ keyOptionLabel(k) }}</option>
                  <option v-if="authKind === 'none'" value="none">无需密钥</option>
                </select>
              </label>
              <p class="hint-line" v-if="keysLoading">正在读取密钥…</p>
              <p class="hint-line warn" v-else-if="keysUnavailable" data-keys-unavailable>
                密钥管理接口暂不可用 —— 先「跟随提供方默认密钥」或「无需密钥」即可继续。
              </p>
              <p class="hint-line" v-else-if="authKind === 'none'">
                这家不需要密钥（本机推理 / 免密端点）。
              </p>
            </div>

            <div class="inline-act" v-if="canCreateKey">
              <button type="button" class="btn sm ghost" data-key-new @click="newKeyOpen = !newKeyOpen">
                <AppIcon name="add" :size="13" />新建密钥
              </button>
            </div>

            <div class="key-new" v-if="newKeyOpen">
              <label class="field">密钥名称
                <input v-model="newKeyLabel" placeholder="如 主账号 / 测试用" data-key-label />
              </label>
              <label class="field">密钥内容
                <input
                  v-model="newKeyValue" type="password" autocomplete="off" spellcheck="false"
                  placeholder="sk-…（提交后立即清空）" data-key-value
                />
              </label>
              <p class="err-line" v-if="newKeyError" data-key-new-error>{{ newKeyError }}</p>
              <div class="key-new-foot">
                <button type="button" class="btn sm" @click="newKeyOpen = false; newKeyError = ''">取消</button>
                <button type="button" class="btn sm primary" :disabled="newKeyBusy" data-key-submit @click="createKey">
                  {{ newKeyBusy ? '写入中…' : '保存并选中' }}
                </button>
              </div>
            </div>
          </template>
          <p class="hint-line" v-else>
            自定义提供方还没有密钥库：密钥可在「高级配置」里用密钥文件引用，或先保存提供方后再管密钥。
          </p>

          <p class="err-line" v-if="formError" data-form-error>{{ formError }}</p>
        </div>
      </section>

      <!-- ③ 选择模型 -->
      <section v-else-if="step === 3" class="step-body" data-step="3">
        <p class="edit-target" v-if="mode === 'edit' && target" data-edit-target>
          正在编辑：{{ form.name || target.name || '未命名模型' }} ·
          {{ activeProvider?.label || form.provider || '自定义提供方' }} /
          {{ form.model_name || target.model_name || '未填 Model ID' }}
        </p>

        <div class="fields">
          <div class="fgroup">
            <label class="field">显示名称
              <input
                v-model="form.name" placeholder="界面上显示的名字" data-model-name
                @input="nameTouched = true"
              />
            </label>
          </div>

          <!-- .model-field 不是样式钩子：onDocPointer 靠它判断「点在字段里不算点外面」 -->
          <div class="fgroup model-field">
            <div class="mf-head">
              <span class="mf-label">Model ID</span>
              <span class="spacer"></span>
              <button type="button" class="btn sm ghost" :disabled="probeBusy" data-probe-models @click="fetchRemoteModels">
                <AppIcon name="refresh" :size="13" />{{ probeBusy ? '拉取中…' : '获取模型列表' }}
              </button>
            </div>

            <!-- 一个输入框两用：进第 3 步自动拉列表，也从这里直接手打 Model ID -->
            <div class="mf-input" ref="modelFieldEl">
              <input
                v-model="form.model_name" class="mono" spellcheck="false"
                placeholder="点右侧箭头从列表选，或直接手填" data-model-id-input
                @focus="placeModelPop"
                @input="syncNameFromModel(form.model_name)"
                @keydown.esc="modelPopOpen = false"
              />
              <button
                type="button" class="mf-caret"
                :class="{ busy: probeBusy }"
                :disabled="probeBusy"
                title="选择模型" data-model-pop-toggle @click="toggleModelPop"
              >
                <AppIcon name="chevron-down" :size="14" />
              </button>
            </div>

            <p class="hint-line" v-if="probeHost">列表来自 {{ probeHost }}（{{ probeOptions.length }} 个）</p>
          </div>
        </div>

        <p class="warn-line" v-if="probeError" data-probe-error>
          <AppIcon name="warning" :size="12" />
          <span class="wl-txt">{{ probeError }}</span>
          <button type="button" class="btn sm ghost" :disabled="probeBusy" data-probe-retry @click="fetchRemoteModels">
            重试
          </button>
        </p>

        <p class="tiny faint" v-if="form.model_name">当前 Model ID：<code class="mono">{{ form.model_name }}</code></p>
        <p class="err-line" v-if="formError" data-form-error>{{ formError }}</p>

        <!-- 模型下拉：fixed 定位，小屏也不出视口 -->
        <Teleport to="body">
          <div v-if="modelPopOpen" class="model-pop" :style="modelPopStyle" data-model-pop>
            <div class="mp-search">
              <AppIcon name="search" :size="13" />
              <input v-model="modelQuery" placeholder="搜索 Model ID" spellcheck="false" data-model-search />
            </div>
            <div class="mp-list">
              <button
                v-for="id in modelCandidates" :key="id" type="button"
                class="mp-opt" :class="{ on: form.model_name === id }"
                @click="pickModelId(id)"
              >
                <span class="mp-radio" aria-hidden="true"></span>
                <span class="mp-id mono truncate">{{ id }}</span>
              </button>
              <p class="mp-empty" v-if="!modelCandidates.length">没有匹配的模型 ID</p>
            </div>
          </div>
        </Teleport>
      </section>

      <!-- ④ 任务用途 -->
      <section v-else-if="step === 4" class="step-body" data-step="4">
        <p class="sh-sub">这个模型干什么用（决定它出现在哪个任务分类里）</p>
        <div class="seg types" role="group" aria-label="任务类型">
          <button
            v-for="t in typesList" :key="t" type="button"
            :class="{ active: form.model_type === t }"
            :data-type="t"
            @click="form.model_type = t"
          >{{ labelOf(t) }}</button>
        </div>

        <label class="check-row">
          <input type="checkbox" v-model="form.is_default" data-default-check />
          <span>设为该任务的默认模型</span>
        </label>
        <p class="hint-line">{{ isDefaultHint }}</p>

        <div class="reasoning" v-if="form.model_type === 'agent'">
          <button type="button" class="adv-toggle" @click="advanced = !advanced">
            <AppIcon :name="advanced ? 'chevron-up' : 'chevron-down'" :size="14" />
            <span>推理等级（问答模型专属）</span>
            <span class="spacer"></span>
            <span class="tiny faint">{{ levels.length ? levels.length + ' 档' : '未配置' }}</span>
          </button>
          <template v-if="advanced">
            <p class="err-line" v-if="paramsBroken">{{ paramsBroken }}</p>
            <template v-else>
              <div class="lv-row" v-for="(r, i) in levels" :key="i">
                <input v-model="r.value" class="mono" placeholder="值，如 high" />
                <input v-model="r.desc" placeholder="说明（可空）" />
                <button type="button" class="btn icon sm ghost" title="上移" :disabled="i === 0" @click="moveLevel(i, -1)">
                  <AppIcon name="chevron-up" :size="14" />
                </button>
                <button type="button" class="btn icon sm ghost" title="下移" :disabled="i === levels.length - 1" @click="moveLevel(i, 1)">
                  <AppIcon name="chevron-down" :size="14" />
                </button>
                <button type="button" class="btn icon sm ghost lv-del" title="删除这一档" @click="removeLevel(i)">
                  <AppIcon name="trash" :size="14" />
                </button>
              </div>
              <div class="lv-foot">
                <button type="button" class="btn sm ghost" @click="addLevel"><AppIcon name="add" :size="14" />添加档位</button>
                <button type="button" class="btn sm ghost" @click="fillBuiltinLevels">填入内置三档</button>
              </div>
              <label class="field">默认档
                <select v-model="defaultLevel">
                  <option value="">不设置</option>
                  <option v-for="v in levelValues" :key="v" :value="v">{{ v }}</option>
                </select>
              </label>
            </template>
          </template>
        </div>

        <p class="err-line" v-if="formError" data-form-error>{{ formError }}</p>
      </section>

      <!-- ⑤ 测试并保存 -->
      <section v-else class="step-body" data-step="5">
        <div class="check-grid">
          <div class="check-cell">
            <span class="cc-ico" :class="configFilled ? 'ok' : 'warn'">
              <AppIcon :name="configFilled ? 'check' : 'warning'" :size="11" />
            </span>
            <span class="cc-k">配置填写</span>
            <span class="cc-v clamp-2" :title="configText" data-status-config>{{ configText }}</span>
          </div>
          <div class="check-cell">
            <span class="cc-ico" :class="testState === 'ok' ? 'ok' : (testState === 'fail' ? 'bad' : 'idle')">
              <AppIcon :name="testState === 'ok' ? 'check' : (testState === 'fail' ? 'error' : 'info')" :size="11" />
            </span>
            <span class="cc-k">连接测试</span>
            <span
              class="cc-v clamp-2" :class="{ bad: testState === 'fail' }" :title="testMessage || '未测试'"
              data-status-test
            >{{ testState === 'running' ? '测试中…' : (testMessage || '未测试') }}</span>
            <button type="button" class="btn sm ghost" :disabled="testState === 'running'" data-test-conn @click="testConnection">
              {{ testState === 'running' ? '测试中…' : '测试连接' }}
            </button>
          </div>
          <div class="check-cell">
            <span
              class="cc-ico"
              :class="saveState === 'failed' ? 'bad' : (saveState === 'saved' ? 'ok' : 'idle')"
            >
              <AppIcon :name="saveState === 'failed' ? 'error' : (saveState === 'saved' ? 'check' : 'info')" :size="11" />
            </span>
            <span class="cc-k">保存结果</span>
            <span
              class="cc-v clamp-2" :class="{ bad: saveState === 'failed' }"
              :title="saveState === 'saved' ? '已保存' : (saveMessage || '还没保存')"
              data-status-save
            >{{ saveState === 'saved' ? '已保存' : (saveMessage || '还没保存') }}</span>
          </div>
        </div>

        <p class="err-line" v-if="formError" data-form-error>{{ formError }}</p>

        <!-- 高级配置（默认折叠） -->
        <button type="button" class="adv-toggle" @click="advanced = !advanced">
          <AppIcon :name="advanced ? 'chevron-up' : 'chevron-down'" :size="14" />
          <span>高级配置（可选）</span>
          <span class="spacer"></span>
          <span class="tiny faint truncate" v-if="!advanced">{{ advSummary }}</span>
        </button>
        <div class="adv" v-if="advanced">
          <div class="grid cols-2">
            <label class="field">device（可空）
              <input v-model="deviceValue" class="mono" placeholder="cpu / cuda:0" @change="writeExtras" />
            </label>
            <label class="field">超时（秒，可空）
              <input v-model="timeoutValue" class="mono" placeholder="30" @change="writeExtras" />
            </label>
          </div>
          <label class="field">parameters（JSON）
            <textarea v-model="form.parametersText" rows="5" class="mono" placeholder="{}"></textarea>
          </label>
          <label class="field">密钥文件（老接口，可空）
            <input v-model="form.secret_ref" class="mono" placeholder="provider.key" />
          </label>
          <div class="chips" v-if="secrets.length">
            <button
              type="button" class="btn sm" v-for="s in secrets" :key="s"
              :class="{ on: form.secret_ref === s }"
              @click="form.secret_ref = s"
            ><span class="mono tiny truncate">{{ s }}</span></button>
          </div>
          <div class="key-row">
            <label class="field key-field">写入密钥文件
              <input v-model="keyValue" type="password" autocomplete="off" spellcheck="false" placeholder="sk-…（写完即清空）" />
            </label>
            <button type="button" class="btn sm" :disabled="keyBusy" @click="writeKey">
              <AppIcon name="upload" :size="13" />{{ keyBusy ? '写入中' : '写入' }}
            </button>
          </div>
          <label class="row small" v-if="mode === 'edit'">
            <input type="checkbox" v-model="form.enabled" /> 启用
          </label>
        </div>
      </section>
    </div>

    <!-- 固定操作区：Modal 的 #footer 不参与主体滚动，所以按钮位置与步骤无关 -->
    <template #footer>
      <div class="flow-foot">
        <button class="btn" @click="emit('close')">取消</button>
        <button class="btn" v-if="step > 1" data-prev @click="prevStep">上一步</button>
        <button class="btn primary" v-if="step < 5" :disabled="!canNext" data-next @click="nextStep">下一步</button>
        <button class="btn primary" v-else :disabled="busy" data-save @click="submit">
          {{ busy ? '保存中…' : '保存' }}
        </button>
      </div>
    </template>
  </Modal>
</template>

<style scoped>
/* 单列流：宽 560。**不给 min-height** —— 弹窗高度 = min(86vh, 内容高)，底栏跟着内容走；
   硬撑高只会让短步骤在底部摊出一片死区（用户口径：主体内容少、周围留白过大）。
   内容超过 86vh 时由 .modal-body 收缩滚动，整窗不滚。 */
.flow {
  display: flex; flex-direction: column;
  max-width: 560px; margin: 0 auto; min-width: 0;
}
.flow > .steps { margin-top: 0; }

/* 步骤条：序号圆 + 12px 文字 + 1px 连接线，整条 26px（远小于 36 的上限） */
.steps { display: flex; align-items: center; flex: none; height: 26px; margin: 0 0 16px; min-width: 0; }
.st-line { flex: 1 1 auto; min-width: 6px; max-width: 24px; height: 1px; background: var(--divider); }
.st-step {
  display: inline-flex; align-items: center; gap: 6px; flex: 0 1 auto;
  min-width: 0; height: 26px; padding: 0; border: 0; background: transparent;
  color: var(--text-faint); font-size: calc(12px * var(--ts, 1)); cursor: default;
  transition: color var(--dur-fast) var(--ease);
}
.st-num {
  flex: none; display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 50%;
  border: 1px solid var(--border); color: var(--text-faint);
  font-size: calc(10.5px * var(--ts, 1)); font-weight: 650; font-variant-numeric: tabular-nums;
  transition: background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.st-txt { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.st-step.done { cursor: pointer; color: var(--text-dim); }
.st-step.done .st-num { border-color: transparent; background: var(--accent); color: var(--text-on-accent); }
.st-step.done:hover { color: var(--text); }
.st-step.on { color: var(--text); font-weight: 650; }
.st-step.on .st-num { border-color: var(--accent); color: var(--accent-text); font-weight: 650; }
.st-step:focus-visible { outline: none; box-shadow: var(--ring); border-radius: var(--r-pill); }

.step-body { display: flex; flex-direction: column; gap: 12px; min-width: 0; }

/* ① 提供方：搜索框 + 平铺列表（列表本身不滚动，避免弹窗里出现第二根滚动条） */
.prov-search { width: 100%; border-radius: var(--r-sm); }
.prov-search input { width: 100%; font-size: calc(13px * var(--ts, 1)); }
.prov-list { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.prov-item {
  display: flex; align-items: center; gap: 10px; width: 100%; min-height: 44px;
  padding: 6px 8px; border: 1px solid transparent; border-radius: var(--r-sm);
  background: transparent; color: var(--text); text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.prov-item:hover { background: var(--bg-hover); }
.prov-item.on { border-color: var(--accent); background: var(--accent-soft); }
.prov-item:focus-visible { outline: none; box-shadow: var(--ring); }
.prov-item.custom { margin-top: 4px; border-top-color: var(--divider); border-radius: 0 0 var(--r-sm) var(--r-sm); }
.pv-icon { width: 20px; height: 20px; flex: none; object-fit: contain; border-radius: 5px; }
.pv-icon-ph { display: block; background: var(--bg-sunken); border: 1px solid var(--divider); }
.pv-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 auto; }
.pv-label { font-size: calc(13px * var(--ts, 1)); font-weight: 600; }
.pv-note { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
.pv-go { flex: none; color: var(--text-faint); }

/* ② 连接与认证：所有项平铺成一列，只有字段本身有描边 */
.step-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.sh-title {
  display: flex; align-items: center; gap: 8px; margin: 0; min-width: 0;
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 650;
}
.sh-icon { width: 20px; height: 20px; object-fit: contain; flex: none; border-radius: 5px; }
.sh-icon-ph { display: block; background: var(--bg-sunken); border: 1px solid var(--divider); }
.sh-sub { margin: 0; font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.lnk {
  border: 0; background: transparent; padding: 0; white-space: nowrap;
  color: var(--accent-text); font-size: calc(12px * var(--ts, 1)); cursor: pointer;
}
.lnk:hover { text-decoration: underline; }
.lnk:focus-visible { outline: none; box-shadow: var(--ring); border-radius: var(--r-sm); }

.fields { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.fgroup { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.fr-k { flex: none; width: 64px; font-size: calc(12px * var(--ts, 1)); font-weight: 650; color: var(--text-dim); }
.fr-v { min-width: 0; font-size: calc(13px * var(--ts, 1)); }
.fr-v.warn { color: var(--warn); }

/* 字段：label 12/650，输入框吃满整列（全局 .field 把宽压到 340，这里放开） */
label.field { gap: 6px; font-weight: 650; }
label.field > input, label.field > select, label.field > textarea { max-width: none; }

.inline-act { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.key-new { display: flex; flex-direction: column; gap: 12px; padding-top: 12px; border-top: 1px solid var(--divider); }
.key-new-foot { display: flex; justify-content: flex-end; gap: 8px; }

.hint-line { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }
.hint-line.warn { color: var(--warn); }
.hint-line.empty { padding: 2px 0; }
.err-line { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--danger); }
.warn-line {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 0;
  font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--warn);
}
.wl-txt { flex: 1 1 200px; min-width: 0; overflow-wrap: anywhere; }

/* ③ 模型 */
.edit-target {
  margin: 0; padding: 4px 10px; border-left: 2px solid var(--border-strong);
  border-radius: 0 var(--r-sm) var(--r-sm) 0; background: var(--bg-subtle); color: var(--text-dim);
  font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6; overflow-wrap: anywhere;
}
.mf-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.mf-label { font-size: calc(12px * var(--ts, 1)); font-weight: 650; color: var(--text-dim); }
.mf-input { display: flex; align-items: center; gap: 6px; min-width: 0; }
.mf-input input { flex: 1 1 auto; min-width: 0; }
.mf-caret {
  flex: none; display: grid; place-items: center;
  width: var(--control-h); height: var(--control-h);
  border: 1px solid var(--border-strong); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text-dim); cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.mf-caret:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
.mf-caret:focus-visible { outline: none; box-shadow: var(--ring); }
.mf-caret:disabled { opacity: .5; cursor: default; }

/* 模型下拉：fixed 定位（见 placeModelPop），小屏也不出视口 */
.model-pop {
  position: fixed; z-index: 260; max-width: calc(100vw - 24px);
  display: flex; flex-direction: column; min-height: 0; padding: 6px;
  border: 1px solid var(--border-strong); border-radius: var(--r-md);
  background: var(--bg-elev); box-shadow: var(--shadow-2);
}
.mp-search {
  display: flex; align-items: center; gap: 7px; flex: none;
  height: var(--control-h); padding: 0 9px; margin-bottom: 4px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-sunken); color: var(--text-faint);
}
.mp-search:focus-within { border-color: var(--accent); box-shadow: var(--ring); }
.mp-search input {
  width: 100%; min-width: 0; height: 100%; padding: 0;
  border: 0; border-radius: 0; background: transparent; box-shadow: none;
  font-size: calc(12.5px * var(--ts, 1));
}
.mp-search input:hover, .mp-search input:focus { background: transparent; border: 0; box-shadow: none; }
.mp-list { flex: 1 1 auto; min-height: 0; max-height: 240px; overflow-y: auto; overscroll-behavior: contain; }
.mp-opt {
  display: flex; align-items: center; gap: 8px; width: 100%; height: 36px; padding: 0 8px;
  border: 0; border-radius: var(--r-sm); background: transparent; color: var(--text);
  font-size: calc(13px * var(--ts, 1)); text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.mp-opt:hover { background: var(--bg-hover); }
.mp-opt.on { background: var(--accent-soft); color: var(--accent-text); }
.mp-opt:focus-visible { outline: none; box-shadow: var(--ring); }
.mp-radio { flex: none; width: 14px; height: 14px; border-radius: 50%; border: 1px solid var(--border-strong); }
.mp-opt.on .mp-radio { border-color: var(--accent); border-width: 4px; }
.mp-id { flex: 1 1 auto; min-width: 0; }
.mp-empty { margin: 0; padding: 8px; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
/* ④ 任务用途 */
.types { height: auto; flex-wrap: wrap; gap: 2px; }
.types button { height: calc(var(--control-h) - 6px); padding: 0 11px; font-size: calc(12.5px * var(--ts, 1)); }
.check-row { display: flex; align-items: center; gap: 8px; font-size: calc(12.5px * var(--ts, 1)); color: var(--text); cursor: pointer; }
.reasoning { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.adv-toggle {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 8px 0; border: 0; border-top: 1px solid var(--divider);
  background: transparent; color: var(--text-dim); font-size: calc(12px * var(--ts, 1)); cursor: pointer;
}
.adv-toggle:hover { color: var(--text); }
.adv-toggle:focus-visible { outline: none; box-shadow: var(--ring); border-radius: var(--r-sm); }
.lv-row { display: grid; grid-template-columns: 96px minmax(0, 1fr) auto auto auto; gap: 6px; align-items: center; }
.lv-row input { width: 100%; height: var(--control-h-sm); min-width: 0; font-size: calc(12.5px * var(--ts, 1)); }
.lv-row .lv-del:hover:not(:disabled) { color: var(--danger); }
.lv-foot { display: flex; gap: 8px; flex-wrap: wrap; }

/* ⑤ 测试并保存：两列紧凑小行（图标 + 名称 + 状态），不堆成一长条 */
.check-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 16px; }
.check-cell {
  display: grid; grid-template-columns: 16px auto minmax(0, 1fr) auto; align-items: center; gap: 8px;
  min-width: 0; font-size: calc(12px * var(--ts, 1));
}
.cc-ico { display: grid; place-items: center; width: 16px; height: 16px; border-radius: 50%; color: var(--text-on-accent); }
.cc-ico.ok { background: var(--ok); }
.cc-ico.warn { background: var(--warn); }
.cc-ico.bad { background: var(--danger); }
.cc-ico.idle { background: var(--bg-hover); color: var(--text-faint); border: 1px solid var(--divider); }
.cc-k { color: var(--text-dim); font-weight: 650; white-space: nowrap; }
.cc-v { min-width: 0; color: var(--text); overflow-wrap: anywhere; }
.cc-v.bad { color: var(--danger); }

.adv { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.key-row { display: flex; align-items: flex-end; gap: 8px; min-width: 0; }
.key-field { flex: 1 1 auto; min-width: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; min-width: 0; }

/* 底部操作区：一根 1px 分隔线 + 10px 内边距（与主体 20px 下内边距相加 = 31px 到按钮）；宽度铺满 Modal 的 footer */
.flow-foot {
  display: flex; align-items: center; justify-content: flex-end; gap: 8px;
  width: 100%; padding-top: 10px; border-top: 1px solid var(--divider);
}

@media (max-width: 860px) {
  .flow { max-width: 100%; }
}

@media (max-width: 560px) {
  .flow { max-width: 100%; }
  /* 窄屏只留序号 + 当前步文字：换行与横向滚动都别出现 */
  .steps .st-step:not(.on) .st-txt { display: none; }
  .check-grid { grid-template-columns: minmax(0, 1fr); }
  .lv-row { grid-template-columns: minmax(0, 1fr) auto auto auto; }
  .lv-row input:nth-child(2) { grid-column: 1 / -1; }
  .fr-k { width: 56px; }
  /* 操作区两行：其余按钮一行，主按钮（DOM 里最后一个）独占一行 */
  .flow-foot { flex-wrap: wrap; }
  .flow-foot .btn { height: 34px; }
  .flow-foot .btn.primary { flex: 1 1 100%; }
}
</style>
