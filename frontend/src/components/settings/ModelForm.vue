<script setup lang="ts">
/**
 * 新建 / 编辑模型的**纵向五步流**（弹窗内，单列、最多 560px 宽）。
 *
 * 1 选择提供方  → 可搜索下拉（图标 + 名称 + 一句说明），取代原来的厂商卡片墙
 * 2 连接与认证  → 已配置的提供方只读展示 Base URL / 认证方式（点右上角「连接设置」才可编辑）；
 *                 默认密钥自动选中（key_mode=auto），可换某一把或「+ 新建密钥」
 * 3 选择模型    → 先拉 POST /models/probe-models 再搜索选 Model ID；拉不到就手填。
 *                 **显示名称 name 与 Model ID model_name 是两个字段**，不互相猜
 * 4 参数与计费  → **任务用途只读**（用户口径 2026-12：配置是按模型分类走的，从哪张卡进来就是哪一类，
 *                 表单再问一遍只会让人把配置挪到别的分类里）；「设为该分类默认模型」单独勾选、默认不勾
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
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import type { ModelConfig, ModelPreset } from '@/api/types';
import { api } from '@/api/client';
import { reportError, toast } from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import { vendorColorIcon } from '@/assets/model-icons';
import { BUILTIN_EFFORTS, EFFORT_VALUE_PLACEHOLDER } from '@/utils/reasoning';
import PriceRuleEditor from '@/components/settings/PriceRuleEditor.vue';
import {
  draftOf as draftOfRule,
  ruleDraftError,
  toRuleItem,
  type PriceRuleDraft,
} from '@/utils/priceRule';
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
  /** 后端 PUT 分时段单价返回的 warning（"默认价未配置…"），原样展示 */
  rulesWarning?: string;
  /** 时段保存失败时后端 400 的 message（原样展示在时段那块里，不当成"保存失败"） */
  rulesError?: string;
  /** 打开时直接停在第几步（0 = 不指定，按原流程走）：只影响"打开那一刻"，之后翻页照旧 */
  initialStep?: number;
  /** 配合 initialStep：顺手把「计费」折叠块展开，让分时段单价列表一眼可见 */
  focusPrice?: boolean;
  /** 已有的「显示名称」清单（父级从当前所有模型配置去重后传进来）：同名会被抽屉归成一组，
   *  所以这里要能"选一个已有名字"而不是每次都手打一遍。
   *  **带 provider**：这份"记忆"是按供应商分开的（用户口径）—— 换一家提供方，看到的就该是那一家用过的名字，
   *  不然选了个属于别家的名字，落进抽屉只会多出一个孤零零的单条组。 */
  existingNames?: Array<{ name: string; count: number; provider: string }>;
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
  rulesWarning: '',
  rulesError: '',
  initialStep: 0,
  focusPrice: false,
  existingNames: () => [],
});

const emit = defineEmits<{
  (e: 'close'): void;
  /**
   * 第二个参数是分时段单价（PUT /models/{id}/price-rules 的 body）：
   * `null` = 这次不提交时段（新建且没填 / 后端没有这个接口），父组件据此并发请求的时机。
   */
  (e: 'submit', payload: Record<string, unknown>, rules: { items: Record<string, unknown>[] } | null): void;
}>();

/* ---------------------------- 步骤 ---------------------------- */

const STEPS = [
  { id: 1, label: '选择提供方' },
  { id: 2, label: '连接与认证' },
  { id: 3, label: '选择模型' },
  { id: 4, label: '参数与计费' },
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
  /* 计费：单价**每百万 Token**，币种空串 = 跟随厂商（'' / 'CNY' / 'USD'） */
  currency: '',
  price_in: '',
  price_out: '',
  price_cache_read: '',
  price_cache_write: '',
});

/* typesList 删掉了：任务用途不再由表单选（见第 4 步模板里的说明）。需要"全部类型"的地方仍在父级。 */

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

/** 「显示名称」的已有名称下拉：状态与 modelPop **各自独立** —— 两个下拉是两种用途，
 *  共用一份 state 会互相收掉（打开一个就把另一个关了）。 */
const namePopOpen = ref(false);
const nameFieldEl = ref<HTMLElement | null>(null);
const namePopStyle = ref<Record<string, string>>({});

/** 下拉里的搜索词：**独立于**「显示名称」输入框 —— 名字常常很长（带 · 后缀），
 *  一边看着输入框一边筛太别扭，所以下拉自带一个搜索框，打开即聚焦、直接打字。 */
const nameQuery = ref('');
const nameSearchEl = ref<HTMLInputElement | null>(null);

/** 当前提供方用过的名字（用户口径：每个供应商分别记忆）。换提供方就换一份，互不串。 */
const namesOfProvider = computed<Array<{ name: string; count: number; provider: string }>>(() => {
  const provider = String(form.provider || '').trim().toLowerCase();
  if (!provider) return [];
  return props.existingNames.filter((n) => String(n.provider || '').toLowerCase() === provider);
});

/** 搜索框再收窄一次（大小写不敏感，空词全列） */
const filteredNames = computed<Array<{ name: string; count: number; provider: string }>>(() => {
  const q = nameQuery.value.trim().toLowerCase();
  if (!q) return namesOfProvider.value;
  return namesOfProvider.value.filter((n) => n.name.toLowerCase().includes(q));
});

/** 显示名称是下游「同名批量配置」的分组键：手打一个字不一样就成不了组，所以要能挑已有名字。 */
function pickName(name: string): void {
  form.name = name;
  nameTouched.value = true;
  namePopOpen.value = false;
}

/**
 * 在「显示名称」里**打字就自动弹候选**（用户口径：输入模型名称时自动弹出候选框）。
 *
 * 两个细节：
 * - 输入框的文本就是筛选词（`nameQuery` 跟着它走），所以打「思考」就直接收窄到那两条；
 * - **一条都匹配不上就不弹 / 收起**：用户常要手打一个全新的名字，那种时候弹一张
 *   "没有匹配的已有名称"的空面板，只会挡在输入框下面碍事。
 * - 不抢焦点：这条路是"边打字边看"，焦点必须留在输入框里（点箭头那条才聚焦搜索框）。
 */
function syncNamePopWhileTyping(): void {
  if (!String(form.provider || '').trim()) return;      // 还没定提供方：没有可记忆的清单
  nameQuery.value = form.name;
  if (!filteredNames.value.length) { namePopOpen.value = false; return; }
  placeNamePop();
}

/** 手打过就置 touched：之后换 Model ID 不再自动改写它（原来的 @input 语义）。 */
function onNameInput(): void {
  nameTouched.value = true;
  syncNamePopWhileTyping();
}

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
  if (step.value !== 3) { modelPopOpen.value = false; namePopOpen.value = false; return; }
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

/** 与 placeModelPop 同一套算法（贴字段下沿 + 视口内收边），只是写另一份 style。 */
function placeNamePop(): void {
  const el = nameFieldEl.value;
  const vw = typeof window === 'undefined' ? 1024 : window.innerWidth;
  if (!el) { namePopOpen.value = true; return; }
  const r = el.getBoundingClientRect();
  const width = Math.min(Math.max(r.width, 240), vw - 24);
  const left = Math.max(12, Math.min(r.left, vw - width - 12));
  namePopStyle.value = {
    top: String(Math.round(r.bottom + 4)) + 'px',
    left: String(Math.round(left)) + 'px',
    width: String(Math.round(width)) + 'px',
  };
  namePopOpen.value = true;
}

function toggleNamePop(): void {
  if (namePopOpen.value) { namePopOpen.value = false; return; }
  /* 每次打开都是一次新查询：上一次搜的词留着，看起来像"这家只有这几条" */
  nameQuery.value = '';
  placeNamePop();
  /* 打开就聚焦搜索框（点箭头的人下一步一定是打字或扫一眼列表）。
     nextTick 是必须的：弹层还没挂上时 ref 是 null。 */
  void nextTick(() => nameSearchEl.value?.focus());
}

function onDocPointer(ev: MouseEvent): void {
  const t = ev.target as HTMLElement | null;
  if (!t || typeof t.closest !== 'function') return;
  /* 两个下拉各收各的：点在 A 里只影响 A，不要把 B 一起收掉（mousedown 早于 click）。
     ⚠️ 认的是 **data-* 属性**不是类名：两个下拉的类名是同一套（.model-pop，用户口径"样式完全一致"），
     再用类名判断就分不出点的是哪一个，会互相收掉。 */
  if (!t.closest('.model-field') && !t.closest('[data-model-pop]')) modelPopOpen.value = false;
  if (!t.closest('.name-field') && !t.closest('[data-name-pop]')) namePopOpen.value = false;
}

/** 视口变化/滚动时收起下拉；但**在列表内部滚动**不算（否则一滚就没了）。 */
function onViewport(ev?: Event): void {
  const t = ev?.target as HTMLElement | null;
  const inside = (sel: string): boolean => !!(t && typeof t.closest === 'function' && t.closest(sel));
  if (modelPopOpen.value && !inside('[data-model-pop]')) modelPopOpen.value = false;
  if (namePopOpen.value && !inside('[data-name-pop]')) namePopOpen.value = false;
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
  levels.value = BUILTIN_EFFORTS.map((v) => ({ value: v, desc: '' }));
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
    if (!v) return '推理强度第 ' + String(i + 1) + ' 行还没填值';
    if (seen.has(v)) return '推理强度「' + v + '」重复了，同一模型内的值要唯一';
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

/* ---------------------------- 计费（单价 / 币种） ----------------------------
 * 单价**每百万 Token**，只认模型级（后端 billing.price_of 也不做厂商级继承：
 * 猜出来的价会让账看起来对、其实错）；币种链是 模型 → 厂商 → CNY，所以"跟随厂商"
 * 时要显示**解析出来的实际币种**，而不是让用户自己猜链子。
 *
 * 空的单价一律是 null（= 未计费），**绝不写成 0** —— 0 的含义是"这次免费"，
 * 那是错账；未计费的含义是"不知道多少钱"，会单独进 unpriced 计数。
 * 只配输入价或只配输出价时后端整体按未计费处理（priced 要求两个都有），
 * 所以这里也不做"半算"，只给一句黄色提示。
 */

type PriceField = 'price_in' | 'price_out' | 'price_cache_read' | 'price_cache_write';
const PRICE_FIELDS: PriceField[] = ['price_in', 'price_out', 'price_cache_read', 'price_cache_write'];
const PRICE_LABEL: Record<PriceField, string> = {
  price_in: '输入 Token 单价',
  price_out: '输出 Token 单价',
  price_cache_read: '缓存读单价',
  price_cache_write: '缓存写单价',
};

const billingOpen = ref(false);

/** 「计费」总开关：关 = 这个模型不计费（四个单价一律传 null、时段一并清空）。
 *  库里**没有**这个字段，它是纯界面口径 —— 「没配价」本来就已经等价于未计费
 *  （billing 的 priced 语义不动），开关只是把「空着四个框」这件容易漏的事说成人话。 */
const billingOn = ref(true);

const CURRENCY_SYMBOL: Record<string, string> = { CNY: '¥', USD: '$', EUR: '€' };

/** 厂商的默认币种：编辑时后端给的 currency_effective 最准（它已经把链子算完了） */
const vendorCurrency = computed<string>(() => {
  const providerCur = (selectedProvider.value as { currency?: string } | null)?.currency;
  const target = props.mode === 'edit' ? (props.target as ModelConfig | null) : null;
  return String(providerCur || target?.currency_effective || 'CNY').toUpperCase();
});

const effectiveCurrency = computed<string>(() => (form.currency || vendorCurrency.value).toUpperCase());
const currencySymbol = computed<string>(() => CURRENCY_SYMBOL[effectiveCurrency.value] || effectiveCurrency.value + ' ');

/** 单价字符串 → 数字：空 = 未配置（null），非法（非有限数 / 负数）= 报错。 */
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

const priceErrors = computed<string[]>(() =>
  /* 不计费态：四个价比一律按 null 提交（见 submit），框本身也 disabled —— 这里不再校验。
     否则「先填了半截再关掉开关」会被一条自己已经改不了的报错卡在保存上。 */
  !billingOn.value
    ? []
    : PRICE_FIELDS
      .filter((key) => !parsePrice(form[key]).ok)
      .map((key) => PRICE_LABEL[key] + '必须是非负数字（可以填小数，如 0.28）'));

const priceError = computed<string>(() => priceErrors.value[0] || '');

const priceIn = computed<number | null>(() => parsePrice(form.price_in).value);
const priceOut = computed<number | null>(() => parsePrice(form.price_out).value);

/** 只配了一半：后端 priced 要求输入价 + 输出价都在，缺一个整体算未计费。
 *  非法输入（负数 / 非数字）先走红字报错，不再叠加这句"只配了一半"。 */
const priceHalf = computed<boolean>(() => {
  if (priceError.value) return false;
  return (priceIn.value == null) !== (priceOut.value == null);
});
const priceConfigured = computed<boolean>(() => priceIn.value != null && priceOut.value != null);

function moneyText(v: number): string {
  return currencySymbol.value + Number(v || 0).toFixed(2);
}

/** 实时算式：输入 1M + 输出 0.5M 的样例调用，跟着输入框变。 */
const priceCalc = computed<{ text: string; warn: boolean }>(() => {
  if (priceConfigured.value) {
    const pin = Number(priceIn.value || 0);
    const pout = Number(priceOut.value || 0);
    const cost = pin * 1 + pout * 0.5;   // （输入 1M × 输入价 + 输出 0.5M × 输出价）÷ 1M
    const text = '（输入 1M × ' + moneyText(pin) + ' + 输出 0.5M × ' + moneyText(pout) + '）÷ 1M = ' + moneyText(cost);
    return { text, warn: false };
  }
  if (priceHalf.value) return { text: '只配了一半 → 仍按未计费处理', warn: true };
  return { text: '', warn: false };
});

/** 折叠状态下的一句话摘要（时段条数也挂在这一行上：折叠着也要看得出"这个模型有峰谷价"） */
const billingSummary = computed<string>(() => {
  /* 折叠着也要看得出「这个模型不算钱」：开关关掉的摘要不提时段（提交时会被清空） */
  if (!billingOn.value) return '不计费';
  const rules = ruleCount.value ? ruleCount.value + ' 个时段' : '无时段';
  if (priceError.value) return '单价非法 · ' + rules;
  if (priceConfigured.value) {
    return '入 ' + moneyText(priceIn.value || 0) + ' · 出 ' + moneyText(priceOut.value || 0) + ' / 1M · ' + rules;
  }
  if (priceHalf.value) return '只配了一半 · ' + rules;
  return '未配置单价 · ' + rules;
});

/* ---------------------------- 分时段单价（时段规则） ----------------------------
 * 口径（别写反）：模型上的四个默认单价 = **时段之外**的价（DeepSeek 口径里就是空闲价）；
 * 时段规则是**例外覆盖**，且**只对工作日（周一–周五）生效**，周末一律落到默认价。
 * 同一模型的时段之间不许重叠 —— 这条集合级约束只有后端能最终判定（整批替换），
 * 本地先算一遍只是为了省一次往返；后端 400 的 message 一律原样展示。
 *
 * ⚠️ 读取失败时**绝不提交空数组**：PUT 是整批替换，发 [] 会把已有规则清空。
 */
const priceRules = ref<PriceRuleDraft[]>([]);
/** 既有规则读到了没有：没读到就不发 PUT（否则等于把已有规则清空） */
const priceRulesLoaded = ref(false);
/** 后端没有这个接口（404）：老后端也能存默认价 —— 整块降级，别把整个表单卡死 */
const priceRulesUnsupported = ref(false);
const priceRulesError = ref('');

/** 条数：读回来之前先用列表行带的 price_rule_count（GET /models 每行都有） */
const ruleCount = computed<number>(() => (
  priceRulesLoaded.value
    ? priceRules.value.length
    : Number((props.target as ModelConfig | null)?.price_rule_count || 0)
));

const rulesSectionTitle = computed<string>(() => (
  '分时段单价 · ' + (ruleCount.value ? ruleCount.value + ' 个时段' : '未配置（全部时刻按默认单价）')
));

/** 没配默认价却配了时段：不是错误（允许保存），但得说清"时段之外仍不计费" */
const rulesWithoutDefault = computed<boolean>(() => priceRules.value.length > 0 && !priceConfigured.value);

async function loadPriceRules(modelId: string): Promise<void> {
  priceRules.value = [];
  priceRulesLoaded.value = false;
  priceRulesUnsupported.value = false;
  priceRulesError.value = '';
  if (!modelId) return;
  try {
    const data = await api.get<{ items?: Record<string, any>[] }>(
      '/models/' + encodeURIComponent(modelId) + '/price-rules',
    );
    const items = Array.isArray(data?.items) ? data.items : [];
    priceRules.value = items.map((row) => draftOfRule(row));
    priceRulesLoaded.value = true;
  } catch (err: any) {
    if (Number(err?.status) === 404) {
      priceRulesUnsupported.value = true;
      return;
    }
    priceRulesError.value = '时段单价读取失败：' + String(err?.message ?? err);
  }
}

function retryPriceRules(): void {
  const id = String((props.target as ModelConfig | null)?.id || '');
  if (id) void loadPriceRules(id);
}

/** 提交给父组件的时段 body；null = 这次不碰时段 */
function composeRules(): { items: Record<string, unknown>[] } | null {
  if (priceRulesUnsupported.value) return null;
  /* 「不计费」开关关掉 = 这次就是把时段清空（PUT 整批替换的空数组写法），**草稿一律不发**：
     草稿是用户关开关之前填的东西，发出去等于「嘴上说不计费、库里还留着峰谷价」，
     而 priced 只看默认价，那种半死状态没人看得出来。新建模式没有 id，返回 null。 */
  if (!billingOn.value) return props.mode === 'edit' ? { items: [] } : null;
  if (props.mode === 'edit' && !priceRulesLoaded.value) return null;
  /* 编辑时**空数组也要发**：那正是"清空全部时段"的唯一写法（PUT 是整批替换） */
  if (!priceRules.value.length) return props.mode === 'edit' ? { items: [] } : null;
  return { items: priceRules.value.map((row) => toRuleItem(row)) };
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
  namePopOpen.value = false;
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
  priceRules.value = [];
  priceRulesLoaded.value = false;
  priceRulesUnsupported.value = false;
  priceRulesError.value = '';
  connEditing.value = false;
  advanced.value = false;
  billingOpen.value = false;
  billingOn.value = true;          // 初值 = 开；编辑已有模型时 init() 读到价再改（见下）
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
      /* 后端回 '' = 跟随厂商；单价回 null = 没配过（显示成空框，绝不写成 0） */
      currency: t.currency || '',
      price_in: priceText(t.price_in),
      price_out: priceText(t.price_out),
      price_cache_read: priceText(t.price_cache_read),
      price_cache_write: priceText(t.price_cache_write),
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
    await loadPriceRules(String((props.target as ModelConfig).id || ''));
    /* 「计费」开关的初值要等时段读完才算得出：库里已经有价（默认价齐全 或 有分时段）
       才开着；两样都没有 = 这个模型本来就不计费，别默认替用户打开 —— 不然界面在撒谎，
       而用户一保存就把「我压根没配价」当成「我选了不计费」。 */
    billingOn.value = priceConfigured.value || priceRules.value.length > 0;
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
  void init().then(() => { syncReasoning(); applyInitialStep(); });
}, { immediate: true });

/**
 * 「编辑高峰价」这类入口的落点：**只在打开那一刻**生效，之后用户自己翻页、折叠都不受影响。
 */
function applyInitialStep(): void {
  const n = Number(props.initialStep) || 0;
  if (n >= 1 && n <= STEPS.length) {
    step.value = n;
    /* 步骤条只允许点回走过的步；跳过去的那几步按"已到达"记，否则 goStep 会把人拦回来 */
    reached.value = Math.max(reached.value, n);
  }
  /* 「编辑高峰价」这类入口是奔着配价来的：折叠块和计费开关都先打开，
     否则从「未计费」的模型点进来会看到一块空面板。 */
  if (props.focusPrice) { billingOpen.value = true; billingOn.value = true; }
}

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
  if (priceError.value) {
    billingOpen.value = true;
    fail(priceError.value);
    return;
  }
  /* 时段规则读不到就别发 PUT：整批替换，空数组 = 清空已有规则。
     不计费态例外：那时无论读没读到都要清空（composeRules 直接给 {items: []}）。 */
  if (billingOn.value && props.mode === 'edit' && !priceRulesLoaded.value && !priceRulesUnsupported.value) {
    billingOpen.value = true;
    fail(priceRulesError.value || '时段单价还没读到，先点「重新读取」再保存（时段是整批替换，发空数组会清空已有规则）');
    return;
  }
  /* 不计费态不校验草稿：那些行根本不会发出去（见 composeRules） */
  const ruleError = billingOn.value ? ruleDraftError(priceRules.value) : '';
  if (ruleError) {
    billingOpen.value = true;
    fail(ruleError);
    return;
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
    /* 计费：五个键**每次都发**——后端"不传 = 不动"，显式 null 才代表"清空"。
       空框在这里就是 null（未计费），不会变成 0（0 是"免费"，两回事）。
       不计费开关关掉时四个价一律 null（哪怕框里还留着旧文本：那是关开关前的残留，
       不该偷偷变成价格）；币种照旧发，它跟计不计费无关，换回来时还得是原来那个。 */
    currency: form.currency || null,
    price_in: billingOn.value ? parsePrice(form.price_in).value : null,
    price_out: billingOn.value ? parsePrice(form.price_out).value : null,
    price_cache_read: billingOn.value ? parsePrice(form.price_cache_read).value : null,
    price_cache_write: billingOn.value ? parsePrice(form.price_cache_write).value : null,
  };
  if (props.mode === 'edit') payload.enabled = !!form.enabled;
  saveState.value = 'saving';
  saveMessage.value = '提交中…';
  /* 默认价（上面的 payload）与时段（第二个参数）分两个请求：先 PUT /models/{id} 再 PUT price-rules */
  emit('submit', payload, composeRules());
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
          <!-- .name-field 不是样式钩子（与 .model-field 同理）：onDocPointer 靠它判断「点在字段里不算点外面」 -->
          <div class="fgroup name-field">
            <div class="mf-head"><span class="mf-label">显示名称</span><span class="spacer"></span></div>
            <!-- 观感照抄同一列的 Model ID：同高、同圆角、同 hover，只是右侧箭头开的是「已有名称」 -->
            <div class="mf-input" ref="nameFieldEl">
              <input
                v-model="form.name" placeholder="界面上显示的名字" data-model-name
                autocomplete="off" spellcheck="false"
                @input="onNameInput" @focus="syncNamePopWhileTyping"
                @keydown.esc="namePopOpen = false"
              />
              <button type="button" class="mf-caret" title="选择已有名称" data-name-pop-toggle @click="toggleNamePop">
                <AppIcon name="chevron-down" :size="14" />
              </button>
            </div>
            <p class="hint-line" v-if="existingNames.length" data-name-hint>
              同名会归成一组，便于「批量配置」；也可以直接手打一个新名字。
            </p>
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

          <!-- 已有名称下拉：定位方式同 .model-pop（fixed），但状态独立 —— 两个下拉互不干扰 -->
          <!-- Esc 也挂在这里：焦点 Tab 到选项上时，输入框那个 @keydown.esc 已经收不到了 -->
          <!-- 与上面那个 Model ID 下拉**同一套类**（.model-pop / .mp-search / .mp-list / .mp-opt）：
               用户口径「和底部的下拉框样式完全一致」—— 不写第二份样式，要改就一起改。
               区分两个下拉靠 data-name-pop / .name-field，不靠类名（类名一合并它们会互相收掉）。 -->
          <div v-if="namePopOpen" class="model-pop" :style="namePopStyle" data-name-pop @keydown.esc="namePopOpen = false">
            <div class="mp-search">
              <AppIcon name="search" :size="13" />
              <input
                ref="nameSearchEl" v-model="nameQuery" type="text" spellcheck="false"
                placeholder="搜索已有名称" data-name-search
                @keydown.esc.stop="namePopOpen = false"
              />
            </div>
            <div class="mp-list">
              <button
                v-for="n in filteredNames" :key="n.name" type="button"
                class="mp-opt" :class="{ on: (form.name || '').trim() === n.name }"
                data-name-option @click="pickName(n.name)"
              >
                <span class="mp-radio" aria-hidden="true"></span>
                <span class="mp-id truncate">{{ n.name }}</span>
                <span class="mp-count">{{ n.count }} 个配置</span>
              </button>
              <p class="mp-empty" v-if="!filteredNames.length" data-name-pop-empty>
                {{ namesOfProvider.length ? '没有匹配的已有名称' : '这家提供方还没有同名的配置' }}
              </p>
            </div>
          </div>
        </Teleport>
      </section>

      <!-- ④ 参数与计费 -->
      <!-- 任务用途**不给改**（用户口径 2026-12）：配置是按模型分类来的 —— 从哪张卡进来就是哪一类，
           在这里再选一次，等于允许把配置挪进别的分类，而抽屉/卡片的"同名同组"正是按分类排的。
           所以这里只把既成事实写出来，选择权留给"从哪里进来"。 -->
      <section v-else-if="step === 4" class="step-body" data-step="4">
        <p class="sh-sub">这一类的参数与计费（任务用途由进入的分类卡决定）</p>
        <p class="edit-target" data-type-readonly>
          任务用途：{{ labelOf(form.model_type) }}
        </p>

        <label class="check-row">
          <input type="checkbox" v-model="form.is_default" data-default-check />
          <span>设为该分类的默认模型</span>
        </label>
        <p class="hint-line">{{ isDefaultHint }}</p>

        <!-- 推理强度**每个走 chat/completions 的模型都该能自己定**（生成 / 问答 / 视觉读页）：
             厂商档位名并不统一（minimal…high、off、甚至 xhigh），所以档位逐模型配 ——
             embedding / reranker 没有推理这回事，不给它们这块。 -->
        <div class="reasoning" v-if="['generation', 'agent', 'vision'].includes(form.model_type)">
          <button type="button" class="adv-toggle" @click="advanced = !advanced">
            <AppIcon :name="advanced ? 'chevron-up' : 'chevron-down'" :size="14" />
            <span>推理强度（该模型支持的档位）</span>
            <span class="spacer"></span>
            <span class="tiny faint">{{ levels.length ? levels.length + ' 档' : '未配置' }}</span>
          </button>
          <template v-if="advanced">
            <p class="err-line" v-if="paramsBroken">{{ paramsBroken }}</p>
            <template v-else>
              <div class="lv-row" v-for="(r, i) in levels" :key="i">
                <input v-model="r.value" class="mono" :placeholder="EFFORT_VALUE_PLACEHOLDER" />
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
                <button type="button" class="btn sm ghost" data-fill-levels @click="fillBuiltinLevels">填入内置档位</button>
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


        <!-- 计费：单价**每百万 Token**，默认折叠（大多数人第一次进来不配价）。
             币种空串 = 跟随厂商，所以要把解析出来的实际币种写在选项里。 -->
        <div class="billing">
          <button
            type="button" class="adv-toggle" data-billing-collapse
            :aria-expanded="billingOpen" @click="billingOpen = !billingOpen"
          >
            <AppIcon :name="billingOpen ? 'chevron-up' : 'chevron-down'" :size="14" />
            <span>计费（单价与币种）</span>
            <span class="spacer"></span>
            <span class="tiny faint" data-billing-summary>{{ billingSummary }}</span>
          </button>
          <template v-if="billingOpen">
            <!-- 「计费」总开关折进折叠体里（用户口径：默认折叠在计费菜单内）——折叠按钮右侧那行
                 摘要仍然会说「不计费」，所以收起时也不会看不出这个模型算不算钱。
                 关掉不是隐藏，是明说「这个模型不计费」—— 提交时四个单价 null、时段清空。
                 造型是 switch：真身仍是原生 checkbox（键盘、焦点、无障碍全白拿），轨道与圆点是纯装饰。 -->
            <label class="sw-row">
              <span class="sw">
                <input
                  type="checkbox" role="switch" v-model="billingOn" data-billing-toggle
                  :aria-checked="billingOn"
                />
                <span class="sw-track" aria-hidden="true"><span class="sw-knob"></span></span>
              </span>
              <span>该模型计费（关闭 = 不计费，调用记为未计费）</span>
            </label>
            <p class="hint-line" v-if="!billingOn" data-billing-off>
              该模型不计费：调用记成未计费（cost 为空），也不能给它设限额
            </p>
            <label class="field billing-cur">
              <span>币种</span>
              <select v-model="form.currency" data-billing-currency>
                <option value="">跟随厂商（当前 {{ vendorCurrency }}）</option>
                <option value="CNY">人民币 CNY</option>
                <option value="USD">美元 USD</option>
              </select>
            </label>

            <div class="billing-grid">
              <label class="field">
                <span>输入 Token 单价<em class="unit">每百万 Token</em></span>
                <input v-model="form.price_in" inputmode="decimal" data-price="price_in" :disabled="!billingOn" placeholder="如 2（算得出钱的前提）" />
              </label>
              <label class="field">
                <span>输出 Token 单价<em class="unit">每百万 Token</em></span>
                <input v-model="form.price_out" inputmode="decimal" data-price="price_out" :disabled="!billingOn" placeholder="如 8（算得出钱的前提）" />
              </label>
              <label class="field">
                <span>缓存读<em class="unit">每百万 Token · 可选</em></span>
                <input v-model="form.price_cache_read" inputmode="decimal" data-price="price_cache_read" :disabled="!billingOn" placeholder="可选，服务商提供该计价规则时填写" />
              </label>
              <label class="field">
                <span>缓存写<em class="unit">每百万 Token · 可选</em></span>
                <input v-model="form.price_cache_write" inputmode="decimal" data-price="price_cache_write" :disabled="!billingOn" placeholder="可选，服务商提供该计价规则时填写" />
              </label>
            </div>

            <p class="calc-line" :class="{ warn: priceCalc.warn }" v-if="priceCalc.text" data-billing-calc>{{ priceCalc.text }}</p>
            <p class="err-line" v-if="priceError" data-billing-error>{{ priceError }}</p>
            <p class="warn-line" v-else-if="priceHalf" data-billing-half>只配了一半 → 仍按未计费处理：费用统计里不会出现金额。</p>
            <!-- 不计费态上面已经有一句专门的说法，这里不再叠一句「未配置单价」 -->
            <p class="hint-line" v-else-if="billingOn && !priceConfigured" data-billing-none>未配置单价：该模型的调用会被记为未计费，费用统计为 0 条。</p>

            <!-- 分时段单价：上面四个价是**时段之外**的价，这里配的是例外覆盖（只对工作日生效）。
                 不计费态整块不渲染：草稿反正不会发出去（见 composeRules），摆着只会让人以为能存。 -->
            <div class="price-rules" v-if="billingOn">
              <div class="pr-head">
                <span class="pr-title" data-price-rules-title>{{ rulesSectionTitle }}</span>
                <span class="spacer"></span>
                <button
                  type="button" class="lnk" v-if="priceRulesError"
                  data-price-rules-retry @click="retryPriceRules"
                >重新读取</button>
              </div>
              <p class="hint-line" data-price-rules-note>
                默认单价用于时段之外的时刻（含周末）；时段规则只在工作日（周一–周五）生效，周末一律按默认单价。
              </p>
              <p class="hint-line" data-price-rules-example>
                DeepSeek 的用法：默认价填空闲价，再把工作日高峰 09:00–12:00、14:00–18:00 添成两段。
              </p>
              <p class="err-line" v-if="priceRulesError" data-price-rules-error>{{ priceRulesError }}</p>
              <p class="err-line" v-else-if="rulesError" data-price-rules-save-error>{{ rulesError }}</p>
              <p class="hint-line" v-else-if="priceRulesUnsupported" data-price-rules-unsupported>
                后端没有分时段单价接口（404）：这次保存只写默认价，已有时段保持原样。
              </p>
              <PriceRuleEditor v-model:rows="priceRules" :symbol="currencySymbol" />
              <p class="warn-line" v-if="rulesWithoutDefault" data-price-rules-nodefault>
                默认价未配置：时段内会按规则价计费，时段之外仍记为未计费（也因此不能给这个模型设限额）
              </p>
              <p class="warn-line" v-if="rulesWarning" data-price-rules-warning>{{ rulesWarning }}</p>
            </div>
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
        <!-- 时段保存成功但后端带了 warning：保存完就停在这一步，提示得在这儿，不能藏在第 4 步里 -->
        <p class="warn-line" v-if="rulesWarning" data-rules-warning>{{ rulesWarning }}</p>

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
/* pre-line：批量配置的失败清单是多行（一行总账 + 每行「名称 — 后端 message」），
   既有单行文案里没有换行符，所以这个改动对它们没有可见影响。 */
.err-line { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--danger); white-space: pre-line; }
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

/* 「显示名称」的已有名称下拉**复用 .model-pop 那一整套**（用户口径「和底部的下拉框样式完全一致」）：
   容器、搜索行、选项行、hover/选中态一律不另写，要改就一起改。
   所以这里只剩一条它自己需要的东西：右侧「N 个配置」的淡色标注。 */
.mp-count { flex: none; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.mp-opt.on .mp-count { color: inherit; }
/* ④ 任务用途 */
.types { height: auto; flex-wrap: wrap; gap: 2px; }
.types button { height: calc(var(--control-h) - 6px); padding: 0 11px; font-size: calc(12.5px * var(--ts, 1)); }
.check-row { display: flex; align-items: center; gap: 8px; font-size: calc(12.5px * var(--ts, 1)); color: var(--text); cursor: pointer; }
/* 切换开关：原生 checkbox 铺满整块并透明（键盘 Tab / 空格照旧可用），
   .sw-track 画胶囊、.sw-knob 画圆点，选中态用 --accent —— 不引入新颜色。 */
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

/* ④-b 计费：与「推理强度」并列的一块，折叠时只占一行 */
.billing { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.billing-cur { max-width: 320px; }
.billing-cur select { max-width: none; }
.billing-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 12px; min-width: 0; }
.billing-grid .field { min-width: 0; }
.billing-grid .field > span { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.billing-grid .unit {
  font-style: normal; font-weight: 500; font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint);
}
.billing-grid input { max-width: none; }
.calc-line {
  margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5;
  color: var(--text-dim); font-variant-numeric: tabular-nums; overflow-wrap: anywhere;
}
.calc-line.warn { color: var(--warn); }

/* ④-c 分时段单价：与默认价同一块，用一条分隔线切开（默认价 = 时段之外的价） */
.price-rules { display: flex; flex-direction: column; gap: 8px; min-width: 0; padding-top: 10px; border-top: 1px solid var(--divider); }
.pr-head { display: flex; align-items: baseline; gap: 8px; min-width: 0; flex-wrap: wrap; }
.pr-title {
  font-size: calc(12px * var(--ts, 1)); font-weight: 650; color: var(--text-dim);
  overflow-wrap: anywhere;
}


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
