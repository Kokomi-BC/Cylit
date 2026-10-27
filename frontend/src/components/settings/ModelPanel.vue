<script setup lang="ts">
/**
 * 设置页「模型」面板。
 *
 * 版式（用户口径）：**标题 + 说明** → **限额** → **用量统计** → **任务分类与模型配置** → **模型提供方管理**。
 *
 * 三处结构性约定（都是改版时踩过的坑，留着当护栏）：
 * ① 页面上**不出现密钥内容**：密钥天然属于某一家提供方，唯一入口是提供方详情抽屉。
 * ② 任务分类卡只放「当前模型」一行，换模型走 ModelSwitcher 浮层。原来那个
 *    「这一类模型配置」弹窗是同一批操作的第二套路径，已整套下线：测试 / 设默认 /
 *    编辑 / 重命名 / 删除收进浮层行尾的「⋯」。卡片高度因此由内容决定 ——
 *    旧版靠 min-height + 卡内滚动对齐，模型少的那几张底部会留一大片空白。
 * ③ 提供方管理用 1px 细分隔线组织成一张表：常用操作只留「管理密钥」，
 *    测试 / 编辑 / 删除收进「⋯」，上移 / 下移只在 hover 或焦点进入时出现。
 *
 * 数据面全部走既有封装（api.get/post/put/del），后端契约见 modelTypes.ts 的注释。
 * 新接口（密钥 / 未归属 / 提供方测试）在后端还没上线时按 404 优雅降级：
 * 抽屉里给一行提示并禁用写操作，**不自己造数据**。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import type { ModelConfig, ModelLimitsSummary } from '@/api/types';
import {
  loadModelTypes, loadModels, reportError, state, toast, withLoading,
} from '@/stores/app';
import { roundMs, truncate } from '@/utils/format';
import { askConfirm } from '@/stores/confirm';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import Menu from '@/components/Menu.vue';
import { categoryIcon, vendorColorIcon } from '@/assets/model-icons';
import ModelForm from '@/components/settings/ModelForm.vue';
import ModelSwitcher from '@/components/settings/ModelSwitcher.vue';
import UsageChart from '@/components/settings/UsageChart.vue';
import LimitCard from '@/components/settings/LimitCard.vue';
import LimitBanner from '@/components/settings/LimitBanner.vue';
import ProviderDrawer from '@/components/settings/ProviderDrawer.vue';
import type { ProviderRow } from '@/components/settings/modelTypes';

/** 模型类型中文标签（类型是产品概念，不是硬编码的模型名）。 */
const TYPE_LABEL: Record<string, string> = {
  generation: '文本生成',
  agent: '论文问答',
  embedding: '向量化',
  reranker: '重排',
  vision: '视觉读页',
  ocr: 'OCR',
};

/** 类型 → 图标（与 categoryIcon 同源；认不出来的新类型仍可回落 AppIcon 字形） */
const TYPE_ICON: Record<string, string> = {
  generation: 'sparkle',
  agent: 'chat',
  embedding: 'layers',
  reranker: 'list',
  vision: 'image',
  ocr: 'document',
};
/** 卡片顺序：问答放第二（最常换），生成第一（别处功能都靠它兜底），其余按离正文的远近排 */
const TYPE_ORDER = ['generation', 'agent', 'embedding', 'reranker', 'vision', 'ocr'];

/** 一句话说明「这个类型是给谁用的」—— 都是对着后端真实取用点写的，不是猜的。 */
const TYPE_DESC: Record<string, string> = {
  generation: '翻译 / 解读 / 摘要',
  agent: '正文里的问答对话',
  embedding: '知识库检索的向量化',
  reranker: '检索结果的重排',
  vision: '扫描件整页识别',
  ocr: '当前无调用方',
};

/** 页面上**不列**的类型：后端枚举里还留着，但没有任何代码取用它。
 *  只在「这一类一条模型都没有」时才隐藏：万一库里真有 ocr 配置，列出来才看得见、删得掉。 */
const HIDDEN_TYPES = new Set(['ocr']);

/** GET /models 每行新增的密钥来源字段（后端算，前端只画；老后端没有，全部可选）。 */
interface KeySource {
  mode: 'provider' | 'key' | 'none' | 'legacy';
  key_id?: string | null;
  key_label?: string | null;
  status?: string | null;
}
type ModelRow = ModelConfig & {
  key_mode?: 'auto' | 'key' | 'none';
  provider_key_id?: string | null;
  key_source?: KeySource | null;
};

const loadError = ref('');
const busyKey = ref('');
const testResults = ref<Record<string, any>>({});   // model_id -> 最近一次测试结果
const highlightId = ref('');                        // 刚建好的那一行，短暂提亮
/** 「去配置」跳过来的那一类：卡片短暂高亮，并滚进可视区 */
const focusKey = ref('');

const usageRef = ref<InstanceType<typeof UsageChart> | null>(null);

/* ---------------------------- 限额（/models/limits） ----------------------------
 * 横幅与限额块**共用这一份** summary：横幅只读 most_urgent，限额块读 items。
 * 一次请求出两处渲染，改完限额后各自不必再拉一遍（也就不会出现两处数字不一致）。 */
const limitSummary = ref<ModelLimitsSummary | null>(null);
const limitLoading = ref(false);
let limitSeq = 0;

async function loadLimits(): Promise<void> {
  const token = ++limitSeq;
  limitLoading.value = true;
  try {
    const data = await api.get<ModelLimitsSummary>('/models/limits/summary');
    if (token === limitSeq) limitSummary.value = data ?? null;
  } catch {
    /* 限额是旁路：读不到就按"没有限额"渲染（横幅不出现、块里是空态），绝不挡住模型列表 */
    if (token === limitSeq) limitSummary.value = null;
  } finally {
    if (token === limitSeq) limitLoading.value = false;
  }
}

/* ---------------------------- 按类型分组 ----------------------------
 * 分类语义照旧（text generation / 论文问答 agent / 向量化 embedding / 重排 reranker /
 * 视觉 vision；ocr 保持隐藏规则），每组只取「当前模型」一行给卡片画。 */

const allTypes = computed<string[]>(() => {
  const fromApi = state.modelTypes?.types;
  if (Array.isArray(fromApi) && fromApi.length) return [...new Set(fromApi)];
  return Object.keys(TYPE_LABEL);
});

const panelTypes = computed(() => allTypes.value.filter((t) => !HIDDEN_TYPES.has(t)
  || (state.models || []).some((m) => m.model_type === t)));

function typeLabel(t?: string | null): string {
  const key = String(t ?? '');
  return TYPE_LABEL[key] || key || '未知类型';
}

/** 厂商图标（彩色，来自 assets/model-icons；认不出来给空串，模板里回落统一占位方块） */
function vendorIco(provider?: string | null): string {
  return vendorColorIcon(provider);
}

/** 分类图标走 CSS mask（素材是 currentColor 的单色 svg，直接 img 会变黑、深色主题下看不见） */
function catStyle(type: string): Record<string, string> {
  const url = categoryIcon(type);
  if (!url) return {};
  const value = 'url("' + url + '")';
  return { maskImage: value, WebkitMaskImage: value };
}

function hostOf(url?: string | null): string {
  if (!url) return '';
  try { return new URL(url).host; } catch { return String(url); }
}

interface TypeGroup {
  key: string;
  title: string;
  icon: string;
  rows: ModelRow[];
  /** 这一类**当前在用**的那个（后端 is_default，每种类型各一个） */
  current: ModelRow | null;
}

const typeGroups = computed<TypeGroup[]>(() => {
  const known = panelTypes.value;
  const order = [...TYPE_ORDER.filter((t) => known.includes(t)), ...known.filter((t) => !TYPE_ORDER.includes(t))];
  const all = (state.models || []) as ModelRow[];
  return order.map((t) => {
    const rows = all.filter((m) => m.model_type === t);
    return {
      key: t,
      title: typeLabel(t),
      icon: TYPE_ICON[t] || 'model',
      rows,
      current: rows.find((m) => m.is_default) || null,
    };
  });
});

/* ---------------------------- 状态 / 密钥来源 ----------------------------
 * 一个模型的状态要能立刻分三种：正常 / 未配置密钥 / 连接异常（外加"还没测过"这个中性态）。
 *
 * 判定的**唯一真源是 key_source**（后端已经把「跟随提供方默认密钥 / 独立指定 / 无需密钥」
 * 三种来源合并算完），不能再看老字段 has_secret：老字段只回答"有没有自己的 secret_ref"，
 * 于是「跟随提供方默认密钥」（secret_ref=null）会被误判成"没配密钥" —— 同一张卡上就会
 * 一边橙字「未配置密钥」一边写「密钥来源：提供方默认」，自相矛盾。
 * has_secret 只在**后端没返回 key_source**（老接口）时兜底。 */

/** 密钥状态：ok / missing / invalid；null = 后端没说（或占位模型，不该报密钥问题）。 */
function keyStateOf(m: ModelRow): 'ok' | 'missing' | 'invalid' | null {
  if (m?.is_mock) return null;                    // 占位模型一律不报密钥问题
  const ks = m?.key_source;
  if (ks && typeof ks === 'object') {
    const s = String(ks.status || '').toLowerCase();
    if (s === 'invalid') return 'invalid';        // 契约里只出现在 mode==='key'（独立指定的那把失效/被删）
    if (s === 'missing') return 'missing';
    if (s === 'ok') return 'ok';
    return 'ok';                                  // key_source 在但状态缺省：不冤枉用户
  }
  /* 老接口：没有 key_source，只能回落到 has_secret 这个旧字段 */
  return m?.has_secret === false ? 'missing' : null;
}

/** 密钥问题（没有就是 null）：优先级高于连接状态 —— 它更能解释"为什么这个模型用不了"。 */
function keyProblem(m: ModelRow): { label: string; cls: string } | null {
  const st = keyStateOf(m);
  if (st === 'invalid') return { label: '密钥失效', cls: 'danger' };
  if (st === 'missing') return { label: '未配置密钥', cls: 'warn' };
  return null;
}

function statusInfo(m: ModelRow): { label: string; cls: string } {
  const key = keyProblem(m);
  if (key) return key;
  const s = String(m?.status || '').toLowerCase();
  if (s === 'ok' || s === 'ready' || s === 'healthy') return { label: '正常', cls: 'ok' };
  if (s === 'error' || s === 'failed' || s === 'bad') return { label: '连接异常', cls: 'danger' };
  return { label: '未测试', cls: '' };
}

/** 「密钥来源：提供方默认 / 独立指定 / 无需密钥 / 未配置密钥」（后端 key_source）。 */
function keySourceInfo(m: ModelRow): { text: string; cls: string } {
  const ks = m?.key_source;
  if (!ks || typeof ks !== 'object') {
    /* 老后端不返回 key_source：不猜，直接说"没有这个信息" */
    return { text: '密钥来源：—', cls: '' };
  }
  if (ks.mode === 'provider') {
    return { text: '密钥来源：提供方默认', cls: ks.status === 'ok' ? '' : 'warn' };
  }
  if (ks.mode === 'key') {
    const name = ks.key_label ? '（' + ks.key_label + '）' : '';
    return { text: '密钥来源：独立指定' + name, cls: ks.status === 'ok' ? '' : 'warn' };
  }
  if (ks.mode === 'none') return { text: '密钥来源：无需密钥', cls: '' };
  if (ks.mode === 'legacy') return { text: '密钥来源：旧版密钥文件', cls: 'warn' };
  return { text: '密钥来源：未配置密钥', cls: 'warn' };
}

function providerCell(m: ModelRow): string {
  const p = m?.provider || '—';
  return m?.is_mock ? p + '（占位）' : p;
}

function itemTitle(m: ModelRow): string {
  const host = hostOf(m.base_url);
  const extra = [m.model_name || '', providerCell(m), host].filter(Boolean).join(' · ');
  /* 后缀走同一套判定：只有真的缺密钥/密钥失效才补一句，别再用 has_secret 误报 */
  const key = keyProblem(m);
  return key ? extra + '（' + key.label + '）' : extra;
}

/** 卡片上那一行的 tooltip：完整展示名 + Model ID（名字本身在窄列里会被截断） */
function rowTitle(m: ModelRow): string {
  return (m.name || '（未命名）') + ' · ' + (m.model_name || '—');
}

function latencyOf(m: ModelRow): string {
  const r = testResults.value[m.id];
  const ms = r?.latency_ms ?? r?.latency ?? (m as any)?.latency_ms;
  return ms === undefined || ms === null ? '—' : roundMs(ms) + ' ms';
}

function rowBusy(key: string): boolean {
  return busyKey.value === key;
}

/* ---------------------------- 数据加载 ---------------------------- */

async function refresh(): Promise<void> {
  try {
    // withLoading 内部已经 reportError，这里不再重复弹错，避免一次失败两条提示。
    await withLoading('models', async () => {
      await loadModels();
      await loadModelTypes();
    });
    loadError.value = '';
  } catch (err) {
    // 面板保持可用：失败原因固定显示在抬头区，不静默
    loadError.value = '模型配置加载失败：' + String((err as any)?.message ?? err);
  }
}

/** 密钥文件名列表：**只为 ModelForm 的 secret_ref 下拉**留着（写密钥的入口已经搬进提供方抽屉）。
 *  接口没上线 / 读失败都不打扰用户 —— 这一页不再提供密钥管理。 */
async function loadSecretNames(): Promise<void> {
  try {
    const data = await api.get<{ secrets?: string[]; dir?: string }>('/models/secrets/list');
    state.secrets = Array.isArray(data?.secrets) ? data.secrets : [];
    state.secretsDir = data?.dir || '';
  } catch {
    state.secrets = [];
    state.secretsDir = '';
  }
}

/* ---------------------------- 提供方（目录 + 密钥数 + 状态） ----------------------------
 * 形状严格照后端契约：providers 里每行带 configured / key_count / default_key_* / status，
 * 另有 custom 一份"自定义入口"。目录读不到时给一行明确的错误与重试，不伪造列表。 */
const providers = ref<ProviderRow[]>([]);
const providersLoading = ref(false);
const providersError = ref('');
async function loadProviders(): Promise<void> {
  providersLoading.value = true;
  try {
    const data = await api.get<{ providers?: ProviderRow[] }>('/models/providers');
    providers.value = Array.isArray(data?.providers) ? data.providers : [];
    providersError.value = '';
  } catch (err) {
    providers.value = [];
    providersError.value = '提供方目录读取失败：' + String((err as any)?.message ?? err);
  } finally {
    providersLoading.value = false;
  }
}

/* ---------------------------- 「模型提供方」展示顺序 ----------------------------
 * 默认顺序（没手动排过时）：DeepSeek → Claude(anthropic) → ChatGPT(openai) → Gemini，
 * 其余按后端返回顺序（后端已按 sort 升序返回），自定义入口永远排最后。
 * 手动排序走后端 PUT /models/providers/order（body { ids: [...] } 是整个顺序）。
 * **优雅降级**：行里没有 sort 字段（排序接口还没上线）就按默认顺序只读展示，
 * 拖动与上移/下移禁用并在区块抬头给一行提示 —— 不伪造顺序、不发注定 404 的请求、不白屏。
 * 这一块的顺序**只影响本区展示**：表单第 1 步的提供方列表按后端返回顺序，不受这里拖动影响。 */
const PROVIDER_PINNED = ['deepseek', 'anthropic', 'openai', 'gemini'];

/** 本地顺序覆盖（松手/上移下移后立刻生效，失败回滚）；空 = 跟着后端返回的顺序 */
const orderOverride = ref<string[]>([]);
const orderBusy = ref(false);

type ProviderSorted = ProviderRow & { sort?: number };

/** 后端有没有给出 sort：没有就说明排序接口还没上线，整块降级 */
const canOrder = computed(() => (providers.value as ProviderSorted[])
  .some((p) => typeof p.sort === 'number'));

const providerList = computed<ProviderSorted[]>(() => {
  const list = providers.value as ProviderSorted[];
  if (orderOverride.value.length) {
    const pos = (id: string) => { const i = orderOverride.value.indexOf(id); return i < 0 ? 1e9 : i; };
    return [...list].sort((a, b) => pos(a.id) - pos(b.id));
  }
  if (canOrder.value) return list;   // 后端已按 sort 升序返回，原样展示
  /* 没有 sort：四家常用的按产品口径排最前，其余保持后端顺序（sort 稳定） */
  const rank = (id: string) => {
    const i = PROVIDER_PINNED.indexOf(String(id).toLowerCase());
    return i < 0 ? PROVIDER_PINNED.length : i;
  };
  return [...list].sort((a, b) => rank(a.id) - rank(b.id));
});

function providerIds(list: ProviderSorted[]): string[] { return list.map((p) => p.id); }

/** 提交顺序：失败**回滚到 prev** 并把原因说出来（不静默吞掉） */
async function submitOrder(ids: string[], prev: string[]): Promise<void> {
  orderBusy.value = true;
  orderOverride.value = ids;
  try {
    await api.put('/models/providers/order', { ids });
    toast('提供方顺序已保存', 'ok', 2400);
    await loadProviders();           // 用后端的新 sort 复核，成功后才放下本地覆盖
    orderOverride.value = [];
  } catch (err: any) {
    orderOverride.value = prev;
    if (Number(err?.status) === 404) {
      toast('排序接口还没上线（后端是旧版本），顺序没保存', 'warn', 6000);
    } else {
      reportError(err, '保存提供方顺序失败');
    }
  } finally {
    orderBusy.value = false;
  }
}

/** 不用鼠标的兜底：上移 / 下移，与拖动同一条接口 */
function moveProvider(id: string, dir: -1 | 1): void {
  if (!canOrder.value || orderBusy.value) return;
  const list = [...providerList.value];
  const i = list.findIndex((p) => p.id === id);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= list.length) return;
  const prev = providerIds(list);
  const [row] = list.splice(i, 1);
  list.splice(j, 0, row);
  void submitOrder(providerIds(list), prev);
}

/* 拖动：dragover 只记落点（不发请求），松手才提交一次 */
const dragId = ref('');
const dropId = ref('');

function onRowDragStart(p: ProviderRow, ev: DragEvent): void {
  if (!canOrder.value) { ev.preventDefault(); return; }
  dragId.value = p.id;
  ev.dataTransfer?.setData('text/plain', p.id);
  if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'move';
}

function onRowDragOver(p: ProviderRow, ev: DragEvent): void {
  if (!canOrder.value || !dragId.value) return;
  ev.preventDefault();
  if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move';
  if (p.id !== dragId.value) dropId.value = p.id;
}

function onRowDragEnd(): void { dragId.value = ''; dropId.value = ''; }

/** 行的键盘入口：焦点落在行内按钮上时交给按钮自己（不抢回车/空格） */
function onRowKey(ev: KeyboardEvent, p: ProviderRow): void {
  if (ev.target !== ev.currentTarget) return;
  ev.preventDefault();
  openDrawer(p, 'keys');
}

function onRowDrop(target: ProviderRow): void {
  const from = dragId.value;
  dragId.value = '';
  dropId.value = '';
  if (!canOrder.value || !from || from === target.id) return;
  const list = [...providerList.value];
  const fi = list.findIndex((p) => p.id === from);
  const ti = list.findIndex((p) => p.id === target.id);
  if (fi < 0 || ti < 0) return;
  const prev = providerIds(list);
  const [moved] = list.splice(fi, 1);
  list.splice(ti, 0, moved);
  void submitOrder(providerIds(list), prev);
}

/** 默认密钥那一格的文案。
 *  后端迁移时给默认密钥起的名字就是「默认密钥」，直接拼会读成「默认密钥：默认密钥」——
 *  所以名字与固定文案相同时只报「已设置」；没有默认密钥时仍报「未设置」。 */
const DEFAULT_KEY_WORD = '默认密钥';

function defaultKeyText(p: ProviderRow): string {
  const label = String(p.default_key_label ?? '').trim();
  if (!label) return DEFAULT_KEY_WORD + '：未设置';
  if (label === DEFAULT_KEY_WORD) return DEFAULT_KEY_WORD + ' 已设置';
  return DEFAULT_KEY_WORD + '：' + label;
}

/** 窄屏那一档（≤560）与 CSS 断点同值：行内只留 ⋯，「管理密钥」进菜单、元信息让位给 title */
const narrow = ref(false);
let narrowMq: MediaQueryList | null = null;

function onNarrow(ev: MediaQueryListEvent | MediaQueryList): void {
  narrow.value = !!ev.matches;
}

/** 行第二行的完整文案（窄屏只画 base_url，其余靠这条 tooltip 兜住，不丢信息） */
function providerSubTitle(p: ProviderRow): string {
  return [
    p.base_url || '未设置',
    (p.configured || 0) + ' 个模型',
    (p.key_count || 0) + ' 条密钥',
    defaultKeyText(p),
  ].join(' · ');
}

function providerStatusText(p: ProviderRow): string {
  const s = String(p.status || 'unknown');
  if (s === 'ok') return '连接正常';
  if (s === 'error') return '连接异常';
  return '未测试';
}

function providerStatusCls(p: ProviderRow): string {
  const s = String(p.status || 'unknown');
  if (s === 'ok') return 'ok';
  if (s === 'error') return 'danger';
  return '';
}

/** 行尾「⋯」：常用只留「管理密钥」，其余三个次要动作收进这一层。
 *  窄屏（≤560）行内放不下那颗按钮 —— 放进去整行会被撑成三行（实测 99px），
 *  所以「管理密钥」在窄屏也收进菜单，桌面档仍是行内按钮 + 三个次要项。 */
function providerMenuItems(p: ProviderRow) {
  const testing = rowBusy('ptest:' + p.id);
  const keys = { key: 'keys', label: '管理密钥', icon: 'document' };
  const rest = [
    { key: 'test', label: testing ? '测试中…' : '测试连接', icon: 'bolt', disabled: testing },
    { key: 'edit', label: '编辑配置', icon: 'edit' },
    { key: 'delete', label: '删除', icon: 'trash', danger: true, disabled: rowBusy('pdel:' + p.id) },
  ];
  return narrow.value ? [keys, ...rest] : rest;
}

function onProviderAction(key: string, p: ProviderRow): void {
  if (key === 'keys') openDrawer(p, 'keys');
  else if (key === 'test') void testProvider(p);
  else if (key === 'edit') openDrawer(p, 'connect');
  else if (key === 'delete') void removeProvider(p);
}

/* ---------------------------- 提供方抽屉 ---------------------------- */

const drawerOpen = ref(false);
const drawerProvider = ref<ProviderRow | null>(null);
const drawerTab = ref<'connect' | 'keys' | 'models'>('keys');
let focusTimer: ReturnType<typeof setTimeout> | null = null;

function openDrawer(p: ProviderRow, tab: 'connect' | 'keys' | 'models' = 'keys'): void {
  drawerProvider.value = p;
  drawerTab.value = tab;
  drawerOpen.value = true;
}

function closeDrawer(): void {
  drawerOpen.value = false;
}

/** 抽屉里任何写操作之后：模型列表、提供方目录、用量一起刷新 */
async function onDrawerChanged(): Promise<void> {
  await refresh();
  await loadProviders();
  usageRef.value?.reload();
}

/** 关联模型里点「去配置」：关掉抽屉、滚到那一类的卡片并高亮一下 */
async function onFocusType(type: string): Promise<void> {
  if (!type) return;
  focusKey.value = type;
  await nextTick();
  document.getElementById('type-card-' + type)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  if (focusTimer) clearTimeout(focusTimer);
  focusTimer = setTimeout(() => { focusKey.value = ''; focusTimer = null; }, 1800);
}

/* ---------------------------- 提供方操作 ---------------------------- */

async function testProvider(p: ProviderRow): Promise<void> {
  busyKey.value = 'ptest:' + p.id;
  try {
    const res = await api.post<any>('/models/providers/' + encodeURIComponent(p.id) + '/test', {});
    if (res?.ok) {
      const count = res.count !== undefined ? ' · ' + res.count + ' 个模型' : '';
      toast('「' + (p.label || p.id) + '」连接正常' + count, 'ok');
    } else {
      toast('「' + (p.label || p.id) + '」连接失败：' + truncate(res?.message || '未知原因', 160), 'error', 8000);
    }
    await loadProviders();
  } catch (err: any) {
    if (Number(err?.status) === 404) {
      toast('后端的提供方测试接口还没上线（当前是旧版本），暂时测不了', 'warn', 6000);
    } else {
      reportError(err, '测试连接失败');
    }
  } finally {
    busyKey.value = '';
  }
}

async function removeProvider(p: ProviderRow): Promise<void> {
  if (!(await askConfirm({
    title: '删除提供方',
    target: p.label || p.id,
    body: '还有模型挂在它上面时不能删',
    danger: true,
    confirmLabel: '删除',
  }))) return;
  busyKey.value = 'pdel:' + p.id;
  try {
    await api.del('/models/providers/' + encodeURIComponent(p.id));
    toast('提供方已删除', 'ok');
    await loadProviders();
    await refresh();
  } catch (err: any) {
    /* 409（还被模型引用）的消息原样显示，不自己吞掉也不要另编一句 */
    toast(String(err?.message ?? err), 'error', 9000);
  } finally {
    busyKey.value = '';
  }
}

/* ---------------------------- 新建提供方 ---------------------------- */
const newProvOpen = ref(false);
const newProvError = ref('');
const newProv = reactive({ id: '', label: '', base_url: '', auth: 'bearer' as 'bearer' | 'none', note: '' });

function openNewProvider(): void {
  newProv.id = '';
  newProv.label = '';
  newProv.base_url = '';
  newProv.auth = 'bearer';
  newProv.note = '';
  newProvError.value = '';
  newProvOpen.value = true;
}

async function submitNewProvider(): Promise<void> {
  newProvError.value = '';
  const id = newProv.id.trim().toLowerCase();
  const label = newProv.label.trim();
  const url = newProv.base_url.trim();
  if (!id) { newProvError.value = '提供方 ID 必填（小写字母 / 数字 / 连字符）'; return; }
  if (!/^[a-z0-9][a-z0-9_-]*$/.test(id)) { newProvError.value = 'ID 只能用小写字母、数字、下划线和连字符'; return; }
  if (!label) { newProvError.value = '名称必填'; return; }
  if (url && !/^https?:\/\//i.test(url)) { newProvError.value = 'Base URL 要以 http:// 或 https:// 开头'; return; }
  busyKey.value = 'newprov';
  try {
    await api.post('/models/providers', {
      id, label, base_url: url || undefined, auth: newProv.auth, note: newProv.note.trim() || undefined,
    });
    toast('提供方已创建', 'ok');
    newProvOpen.value = false;
    await loadProviders();
  } catch (err: any) {
    newProvError.value = '保存失败：' + String(err?.message ?? err);
  } finally {
    busyKey.value = '';
  }
}

/* ---------------------------- 按类型添加（卡片右上那枚 +） ----------------------------
 * 直接开表单：提供方列表是表单的第 1 步（可搜索、自带「自定义提供方」一行），
 * 中间那层「先选提供方」的弹窗是**同一件事的第二套界面**，已删除（用户口径 2026-12-02）。 */
function openAdd(type: string): void {
  openCreate(type);
}

/* ---------------------------- 切换模型浮层 ----------------------------
 * 卡片只有一颗按钮（整张卡），点 / 回车就把这张卡当锚点开浮层 —— 锚点同时是
 * 「关掉后焦点还给谁」，所以必须是卡本身（卡内那一行不可聚焦）。
 * 浮层自己负责定位、键盘与点外关闭，这里只管状态与动作分发。 */
const swOpen = ref(false);
const swType = ref('');
const swAnchor = ref<HTMLElement | null>(null);

/** 浮层看的是**当前的**这一类：测试 / 设默认 / 删除刷新后这里跟着变（computed，不做快照） */
const swGroup = computed<TypeGroup | null>(
  () => typeGroups.value.find((g) => g.key === swType.value) || null,
);
const swTitle = computed(() => (swGroup.value ? swGroup.value.title : '选择模型'));

/** 正在被测试/删除/改名的那个模型交给浮层置灰（busyKey 是 "kind:id" 的形状） */
const swBusyIds = computed<string[]>(() => {
  const raw = String(busyKey.value || '');
  const i = raw.indexOf(':');
  if (i < 0) return [];
  const kind = raw.slice(0, i);
  return ['test', 'def', 'del', 'name'].includes(kind) ? [raw.slice(i + 1)] : [];
});

function openSwitcher(type: string, anchor: HTMLElement | null): void {
  swType.value = type;
  swAnchor.value = anchor;
  swOpen.value = true;
}

function closeSwitcher(): void {
  swOpen.value = false;
}

function onCardClick(type: string, ev: MouseEvent): void {
  /* 同一个分类再点一次 = 收起；换一个分类就直接换内容 */
  if (swOpen.value && swType.value === type) { closeSwitcher(); return; }
  openSwitcher(type, ev.currentTarget as HTMLElement | null);
}

/** 卡片的键盘入口：焦点在卡内按钮上时**不抢**（否则回车会同时开浮层和点按钮） */
function onCardKey(ev: KeyboardEvent, type: string): void {
  if (ev.target !== ev.currentTarget) return;
  ev.preventDefault();
  openSwitcher(type, ev.currentTarget as HTMLElement);
}

function onSwitchSelect(m: ModelRow): void {
  const current = swGroup.value?.current;
  closeSwitcher();
  if (current && current.id === m.id) return;   // 选的就是当前这个：只收浮层
  void setDefault(m);                           // 「切换」= 把这一类的默认指过去
}

/** 「⋯」的五个动作：接口调用全用下面已有的函数，浮层只负责转发。
 *  编辑 / 重命名 / 删除都会再开一层弹窗，先把浮层收掉，免得两层叠着、Esc 一次关两个。 */
function onSwitchAction(payload: { key: string; row: ModelRow }): void {
  const m = payload.row;
  if (payload.key === 'test') { void testModel(m); return; }
  if (payload.key === 'default') { if (!m.is_default) void setDefault(m); return; }
  closeSwitcher();
  if (payload.key === 'edit') openEdit(m);
  else if (payload.key === 'rename') void openRename(m);
  else if (payload.key === 'delete') void removeModel(m);
}

/** 浮层底部的「+ 添加模型」：和卡片右上那枚 + 同一条路（先选提供方，再进表单） */
function onSwitchAdd(): void {
  const type = swType.value;
  closeSwitcher();
  openAdd(type);
}

/* ---------------------------- 重命名 ----------------------------
 * 原来在卡片里做行内 input（卡片一收就没了）。现在卡片只显示当前模型，
 * 改名收进浮层「⋯」→ 一个窄弹窗，提交的仍是同一个 PUT /models/{id}（只发 name）。 */

const renameOpen = ref(false);
const renameTarget = ref<ModelRow | null>(null);
const renameText = ref('');
const renameInput = ref<HTMLInputElement | null>(null);

async function openRename(m: ModelRow): Promise<void> {
  renameTarget.value = m;
  renameText.value = m.name || '';
  renameOpen.value = true;
  await nextTick();
  renameInput.value?.focus();
  renameInput.value?.select();
}

function closeRename(): void {
  renameOpen.value = false;
  renameTarget.value = null;
}

async function submitRename(): Promise<void> {
  const m = renameTarget.value;
  if (!m) return;
  const next = renameText.value.trim();
  const prev = m.name || '';
  closeRename();
  if (!next || next === prev) return;
  busyKey.value = 'name:' + m.id;
  try {
    await api.put('/models/' + m.id, { name: next });
    toast('已改名', 'ok', 2400);
    await refresh();
  } catch (err) {
    reportError(err, '改名失败');
  } finally {
    busyKey.value = '';
  }
}

/* ---------------------------- 测试 / 默认 / 删除模型 ---------------------------- */

async function testModel(m: ModelRow): Promise<void> {
  busyKey.value = 'test:' + m.id;
  try {
    const res = await withLoading('test', () => api.post<any>('/models/' + m.id + '/test'), { silent: true });
    testResults.value = { ...testResults.value, [m.id]: res };
    const timing = res?.latency_ms === undefined || res?.latency_ms === null
      ? '' : ' · ' + roundMs(res.latency_ms) + ' ms';
    if (res?.ok) {
      toast('测试通过：' + (m.name || m.id) + timing, 'ok', 4200);
    } else {
      toast('测试失败：' + (m.name || m.id) + ' · ' + truncate(res?.detail || '未知原因', 160), 'error', 8000);
    }
    await refresh();
  } catch (err) {
    reportError(err, '测试请求失败');
  } finally {
    busyKey.value = '';
  }
}

async function setDefault(m: ModelRow): Promise<void> {
  busyKey.value = 'def:' + m.id;
  try {
    await api.put('/models/' + m.id + '/default');
    toast('已设为「' + typeLabel(m.model_type) + '」默认模型', 'ok');
    await refresh();
  } catch (err) {
    reportError(err, '设为默认失败');
  } finally {
    busyKey.value = '';
  }
}

async function removeModel(m: ModelRow): Promise<void> {
  /* 删之前先看它是不是某一类的默认：后端会自动把默认顺延到下一条，但用户得先知道。 */
  const sameType = ((state.models || []) as ModelRow[]).filter((x) => x.model_type === m.model_type && x.id !== m.id);
  const next = sameType.find((x) => x.enabled !== false) || sameType[0] || null;
  const routeNote = m.is_default
    ? '这是「' + typeLabel(m.model_type) + '」当前默认模型 —— 删掉后会自动顺延到「'
      + (next?.name || next?.id || '（没有可用的了，该类型将不可用）') + '」。'
    : '';
  if (!(await askConfirm({
    title: '删除模型配置',
    target: m.name || m.id,
    body: [routeNote, '删除后不可恢复'].filter(Boolean).join(' '),
    danger: true,
    confirmLabel: '删除',
  }))) return;
  busyKey.value = 'del:' + m.id;
  try {
    await api.del('/models/' + m.id);
    toast('已删除模型配置', 'ok');
    await refresh();
    await loadProviders();
    usageRef.value?.reload();
  } catch (err) {
    reportError(err, '删除失败');
  } finally {
    busyKey.value = '';
  }
}

/* ---------------------------- 新建 / 编辑（统一走 ModelForm） ---------------------------- */

const formOpen = ref(false);
const formMode = ref<'create' | 'edit'>('create');
const formTarget = ref<ModelConfig | null>(null);
/** 同名多配置的批量清单：长度 > 1 时 submitForm 走「一条表单写回 N 个配置」那条路。
 *  单个配置一律是 []，老路径一个字都不动。只在 openBatch 里被填满，其余入口都要清空，
 *  否则上一次的批量标记会漏到下一次单条编辑上。 */
const formBatch = ref<ModelRow[]>([]);
const formDefaultType = ref('agent');
/** 保存失败的文案：透给表单第五步的「保存结果」那一行（saveError），每次打开表单时清空 */
const formError = ref('');
/** 时段保存成功但后端带了 warning（"默认价未配置…"）：表单里原样黄字提示 */
const formWarning = ref('');
/** 时段保存失败时后端 400 的 message：**原样**贴在时段那块里（前端校验只是省一次往返） */
const formRulesError = ref('');
/** 新建成功后拿到的模型 id：时段保存失败时重试走 PUT，不会 POST 出第二份 */
const formSavedId = ref('');
/** 打开时直接停在第几步（0 = 不指定）：只有「编辑高峰价」会传 4 */
const formInitialStep = ref(0);
/** 配合上一条：顺手展开「计费」折叠块，让分时段单价列表一眼可见 */
const formFocusPrice = ref(false);
let highlightTimer: ReturnType<typeof setTimeout> | null = null;

function openCreate(type = 'agent'): void {
  formMode.value = 'create';
  formTarget.value = null;
  formBatch.value = [];
  formDefaultType.value = type;
  formError.value = '';
  formWarning.value = '';
  formRulesError.value = '';
  formSavedId.value = '';
  formInitialStep.value = 0;
  formFocusPrice.value = false;
  formOpen.value = true;
}

function openEdit(m: ModelRow): void {
  formMode.value = 'edit';
  formTarget.value = m;
  formBatch.value = [];        // 单条编辑：把上一次批量留下的标记清掉
  formDefaultType.value = m.model_type || 'agent';
  formError.value = '';
  formWarning.value = '';
  formRulesError.value = '';
  formSavedId.value = '';
  formInitialStep.value = 0;
  formFocusPrice.value = false;
  formOpen.value = true;
}

/**
 * 抽屉里同名多配置那一行的「批量配置」：同一份表单依次写回这一组的每个配置。
 *
 * 表单的初始值取**第一条**：同名 + 同提供方，用户想统一改的正是那些共同字段（类型不写回，见下）。
 * 类型（任务用途）不进批量：这一组之所以成组就是因为同名被多处用着，抹平类型等于删配置。
 */
function openBatch(models: ModelRow[]): void {
  if (!models.length) return;
  formMode.value = 'edit';
  formTarget.value = models[0];
  formBatch.value = models;
  /* 与 openEdit 同一套复位：只多了一条「本次是批量」，其余字段的初值语义不变 */
  formDefaultType.value = models[0].model_type || 'agent';
  formError.value = '';
  formWarning.value = '';
  formRulesError.value = '';
  formSavedId.value = '';
  formInitialStep.value = 0;
  formFocusPrice.value = false;
  formOpen.value = true;
}

/**
 * 限额卡片上的「编辑高峰价」：直接打开这个配置的编辑表单，并停在放单价的那一步
 * （第 4 步里就是「计费（单价与币种）」+ 分时段单价那一块）。
 */
function openEditPrice(id: string): void {
  const m = ((state.models || []) as ModelRow[]).find((x) => x.id === id);
  if (!m) return;
  openEdit(m);                  // openEdit 内部已经把 formBatch 清空，这里不必再来一次
  formInitialStep.value = 4;
  formFocusPrice.value = true;
}

function flash(id: string): void {
  highlightId.value = id;
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = setTimeout(() => {
    if (highlightId.value === id) highlightId.value = '';
    highlightTimer = null;
  }, 2800);
}

/**
 * 批量写回：把同一份表单内容依次写到这一组的每个配置上。
 *
 * 为什么循环而不是新加批量接口：后端只有 PUT /models/{id}（逐个），前端不该为界面便利造第二套语义。
 * 单条失败**不中断**：写成功的就留着（回滚只是骗自己），失败清单一次报全，表单不关，改完可直接重试
 * —— 与限额卡片的批量 report 是同一种写法（一行总账 + 每行「名称 — 后端 message」）。
 *
 * 类型（model_type）**不写回**：同名多配置之所以成组，就是同一个名字被不同用途用着，
 * 抹平类型等于删配置。表单横幅也是这么跟用户说的。
 */
async function submitBatch(
  payload: Record<string, any>,
  rules: { items: Record<string, any>[] } | null,
): Promise<void> {
  const list = formBatch.value.slice();
  const body: Record<string, any> = { ...payload };
  delete body.model_type;
  const ok: string[] = [];
  const fails: Array<{ name: string; message: string }> = [];
  for (const m of list) {
    const id = String(m.id || '');
    const label = String(m.name || m.model_name || id || '（未命名）');
    if (!id) { fails.push({ name: label, message: '缺少配置 id，跳过' }); continue; }
    try {
      await api.put('/models/' + id, body);
      if (rules) await api.put('/models/' + id + '/price-rules', rules);
    } catch (err) {
      fails.push({ name: label, message: String((err as any)?.message ?? err) });
      continue;                     // 单条失败不中断：后面的配置照写
    }
    ok.push(label);
  }
  if (fails.length) {
    formError.value = [
      String(ok.length) + ' 个成功 / ' + String(fails.length) + ' 个失败',
      ...fails.map((f) => f.name + ' — ' + f.message),
    ].join('\n');
    toast('批量配置有 ' + String(fails.length) + ' 个失败', 'error', 6000);
    await refresh();
    return;                         // 表单留着：失败清单和已填内容都在，改完直接重试
  }
  formBatch.value = [];
  formOpen.value = false;
  await refresh();
  toast('已写入 ' + String(ok.length) + ' 个配置', 'ok');
  /* 逐个 flash：highlightId 只有一个，最终亮着的是最后一个，但刷新后每一行都点过一次
     （宁可这样，也不引入"多行同时高亮"的第二种状态） */
  for (const m of list) flash(String(m.id));
}

/**
 * 保存模型：**两个请求，顺序固定**。
 *
 * 1. PUT /models/{id}（或 POST /models）—— 默认价 / 币种 / 配置，逻辑与以前一致；
 * 2. PUT /models/{id}/price-rules —— 分时段单价，**整批替换**（空数组 = 清空全部时段）。
 *
 * 为什么第二个失败不回滚、也不关表单：模型已经真的在库里了，回滚只是骗自己；
 * 界面上留一句「模型已保存，时段保存失败：<后端 message>」，把已经填好的时段留在屏幕上，
 * 用户改完直接再点保存即可（新建时记住拿到的 id，重试走 PUT，不会 POST 出第二份）。
 */
async function submitForm(
  payload: Record<string, any>,
  rules: { items: Record<string, any>[] } | null,
): Promise<void> {
  busyKey.value = 'submit';
  formError.value = '';
  formWarning.value = '';
  formRulesError.value = '';
  try {
    /* 同名多配置批量：一条表单写回 N 个配置（各自 PUT，互不牵连），与单条路径完全分开 */
    if (formBatch.value.length > 1) {
      await submitBatch(payload, rules);
      return;
    }
    const editId = formMode.value === 'edit' && formTarget.value ? String(formTarget.value.id) : '';
    let modelId = editId || formSavedId.value;
    let created: any = null;
    try {
      if (!modelId) {
        created = await api.post<any>('/models', payload);
        modelId = String(created?.id || created?.model?.id || '');
        if (modelId) formSavedId.value = modelId;
      } else {
        await api.put('/models/' + modelId, payload);
      }
    } catch (err) {
      /* 两条路都留着：toast 给人即时反馈，formError 让表单第五步自己留一行原因（关掉弹窗就没了） */
      formError.value = '保存失败：' + String((err as any)?.message ?? err);
      reportError(err, '保存失败');
      return;
    }

    if (rules && modelId) {
      try {
        const saved = await api.put<any>('/models/' + modelId + '/price-rules', rules);
        /* 没配默认价也允许存时段：后端用 warning 说清"时段之外仍不计费"，原样转达 */
        const warning = String(saved?.warning || '');
        if (warning) {
          formWarning.value = warning;
          toast(warning, 'warn', 6000);
          await refresh();
          return;                       /* 表单留着：这条 warning 要能被看见 */
        }
      } catch (err) {
        /* 后端 message **原样**放两处：时段那块里（改完就地重试）+ 保存结果那一行（不吞错） */
        formRulesError.value = String((err as any)?.message ?? err);
        formError.value = '模型已保存，时段保存失败：' + String((err as any)?.message ?? err);
        reportError(err, '时段单价保存失败');
        await refresh();
        return;                         /* 表单不关：时段还在，改完可直接重试 */
      }
    }

    formOpen.value = false;
    await refresh();
    if (created) {
      await loadProviders();
      const id = modelId || String(payload.name || '');
      /* 保存成功 ≠ 连接可用：存完立刻用既有 POST /models/{id}/test 真打一次端点。 */
      if (id) setTimeout(() => { void testModel(created as ModelRow); }, 300);
      toast('已创建，建议点「测试」验证连通性', 'ok', 4800);
    } else {
      toast('模型配置已更新', 'ok');
    }
    if (modelId) flash(modelId);
  } finally {
    busyKey.value = '';
  }
}

async function refreshAll(): Promise<void> {
  await refresh();
  await loadProviders();
  usageRef.value?.reload();
  void loadLimits();
}

onMounted(() => {
  narrowMq = window.matchMedia('(max-width: 560px)');
  onNarrow(narrowMq);
  narrowMq.addEventListener('change', onNarrow);
  void refreshAll();          // 里面已经带上了 loadLimits()
  void loadSecretNames();
});

onBeforeUnmount(() => {
  narrowMq?.removeEventListener('change', onNarrow);
  narrowMq = null;
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = null;
  if (focusTimer) clearTimeout(focusTimer);
  focusTimer = null;
});
</script>

<template>
  <div class="model-panel">
    <!-- ① 顶部工具行：页面标题与一句说明在窗格抬头（那里已经有了），这里只放计数与刷新，
           不再重复第二个「模型」标题 —— 两行同名字在 390px 上会白占一屏。 -->
    <div class="panel-bar">
      <span class="cap">{{ state.models.length }} 个配置</span>
      <span class="spacer"></span>
      <button class="btn icon sm" :disabled="state.loading.models" title="重新读取模型 / 提供方" @click="refreshAll">
        <AppIcon name="refresh" :size="15" />
      </button>
    </div>

    <!-- 页头顶着「模型」二字，横幅就贴在它下面、第一个内容块之前；
         只在「设置 · 模型」这一页出现（这个面板只在这一页挂载）。
         warn / exceeded 三态由后端 summary.level 决定，这里不自算。 -->
    <LimitBanner :summary="limitSummary" />

    <div class="load-error" v-if="loadError">{{ loadError }}</div>

    <!-- ② 限额：裸块（区块标题 + 留白），与「用量」同一观感，不套 .card。
         「新增限额」那一行也在这块里，所以整块一起上移：先读"会不会被拦住"，再看花掉多少 -->
    <LimitCard
      :summary="limitSummary" :loading="limitLoading"
      @refresh="loadLimits" @edit-price="openEditPrice"
    />

    <!-- ②' 用量统计（口径不变，只压缩留白与层级） -->
    <UsageChart ref="usageRef" />

    <!-- ③ 任务分类与模型配置：每类一张卡，卡上只有当前模型那一行 -->
    <section class="block types-block">
      <header class="block-head">
        <h3 class="bh-title"><AppIcon name="grid" :size="14" />任务分类与模型配置</h3>
        <span class="bh-desc">点卡片换这一类的模型；右上角的 + 往这一类里加</span>
      </header>

      <div class="types-row">
        <!-- 整张卡是一颗按钮：点 / 回车开这一类的模型浮层，浮层里再做测试、设默认、编辑、删除。
             卡内的 + 与空态的添加按钮都 stop，别误触。 -->
        <section
          class="type-col" v-for="g in typeGroups" :key="g.key"
          :id="'type-card-' + g.key"
          :class="{ focus: focusKey === g.key }"
          role="button" tabindex="0"
          :data-type-card="g.key"
          :aria-expanded="swOpen && swType === g.key"
          :aria-label="g.title + '：' + (g.current ? (g.current.name || '未命名') : '未配置') + '，点击换模型'"
          :title="g.title + '：' + (TYPE_DESC[g.key] || '—')"
          @click="onCardClick(g.key, $event)"
          @keydown.enter="onCardKey($event, g.key)"
          @keydown.space="onCardKey($event, g.key)"
          @keydown.esc="closeSwitcher"
        >
          <header class="tc-head">
            <span class="tc-ico">
              <span v-if="categoryIcon(g.key)" class="cat-mask" :style="catStyle(g.key)" />
              <AppIcon v-else :name="g.icon" :size="12" />
            </span>
            <span class="tc-title truncate">{{ g.title }}</span>
            <span class="tc-count" v-if="g.rows.length > 1">{{ g.rows.length }}</span>
            <span class="spacer"></span>
            <button
              type="button" class="tc-add"
              :title="'添加' + g.title + '模型'" :aria-label="'添加' + g.title + '模型'"
              :data-add-type="g.key" @click.stop="openAdd(g.key)"
            >
              <AppIcon name="add" :size="14" />
            </button>
          </header>

          <!-- 当前模型行：整行就是卡片的可点区域，右侧箭头指示"能展开" -->
          <div
            class="tc-cur" v-if="g.current"
            :class="{ fresh: highlightId === g.current.id }"
            :title="rowTitle(g.current)"
          >
            <img v-if="vendorIco(g.current.provider)" class="pv-ico" :src="vendorIco(g.current.provider)" alt="">
            <span v-else class="pv-fallback" :title="providerCell(g.current)"><AppIcon name="model" :size="12" /></span>
            <span class="tc-name truncate">{{ g.current.name || '（未命名）' }}</span>
            <AppIcon class="tc-chev" name="chevron-down" :size="14" />
          </div>
          <!-- 未配置态：不放空行 —— 一行引导文字 + 一枚小按钮，卡不会塌下去也不留空 -->
          <div class="tc-cur is-empty" v-else>
            <span class="tc-hint truncate">这一类还没有模型</span>
            <button type="button" class="btn sm" @click.stop="openAdd(g.key)">
              <AppIcon name="add" :size="13" />添加模型
            </button>
          </div>

          <div class="tc-meta is-status" v-if="g.current">
            <span class="stat-dot" :class="statusInfo(g.current).cls" />
            <span class="tc-status" :class="statusInfo(g.current).cls">{{ statusInfo(g.current).label }}</span>
            <span class="sep">·</span>
            <span class="tc-keysrc truncate" :class="keySourceInfo(g.current).cls" :title="keySourceInfo(g.current).text">{{ keySourceInfo(g.current).text }}</span>
          </div>
          <div class="tc-meta" v-else>
            <span class="tc-keysrc truncate">{{ TYPE_DESC[g.key] || '这一类还没登记用途' }}</span>
          </div>
        </section>
      </div>
    </section>

    <!-- ④ 模型提供方管理 -->
    <section class="block providers-block">
      <header class="block-head">
        <h3 class="bh-title"><AppIcon name="server" :size="14" />模型提供方</h3>
        <span class="bh-desc">连接配置与密钥都在这家的详情里；点整行打开，按住行可拖动排序</span>
        <span class="prov-order-note" v-if="!canOrder" :title="'后端 /models/providers 没返回 sort 字段'">
          <AppIcon name="warning" :size="12" />排序接口暂不可用
        </span>
        <span class="spacer"></span>
        <button class="btn sm" data-provider-new @click="openNewProvider">
          <AppIcon name="add" :size="14" />新增提供方
        </button>
      </header>

      <div class="prov-skel" v-if="providersLoading">
        <span class="sk sk-line" v-for="i in 3" :key="i" />
      </div>

      <div class="prov-error" v-else-if="providersError">
        <p>{{ providersError }}</p>
        <button class="btn sm" @click="loadProviders"><AppIcon name="refresh" :size="14" />重试</button>
      </div>

      <div class="prov-empty" v-else-if="!providers.length">
        <AppIcon name="server" :size="14" />
        <span>还没有可用的提供方目录 —— 后端目录接口没返回内容，或还没配置任何一家。</span>
      </div>

      <div class="card prov-card" v-else>
        <div
          class="prov-row" v-for="(p, i) in providerList" :key="p.id"
          role="button" tabindex="0" :data-provider-row="p.id"
          :draggable="canOrder ? 'true' : 'false'"
          :class="{ dragging: dragId === p.id, 'drop-target': dropId === p.id }"
          @click="openDrawer(p, 'keys')"
          @keydown.enter="onRowKey($event, p)"
          @keydown.space="onRowKey($event, p)"
          @dragstart="onRowDragStart(p, $event)"
          @dragover="onRowDragOver(p, $event)"
          @drop.prevent="onRowDrop(p)"
          @dragend="onRowDragEnd"
        >
          <span class="pr-grip" :class="{ off: !canOrder }" title="按住拖动排序" aria-hidden="true">
            <AppIcon name="list" :size="14" />
          </span>
          <img v-if="vendorIco(p.id)" class="pv-ico lg" :src="vendorIco(p.id)" alt="">
          <span v-else class="pv-fallback lg" :title="p.label || p.id"><AppIcon name="server" :size="15" /></span>

          <div class="pr-main">
            <div class="pr-top">
              <span class="pr-name truncate" :title="p.label || p.id">{{ p.label || p.id }}</span>
              <span class="badge tiny dim">{{ p.kind === 'custom' ? '自定义' : '官方' }}</span>
              <span class="stat-dot" :class="providerStatusCls(p)" :title="providerStatusText(p)" />
              <span class="pr-status" :class="providerStatusCls(p)">{{ providerStatusText(p) }}</span>
            </div>
            <div class="pr-sub" :title="providerSubTitle(p)">
              <span class="mono truncate" :class="{ unset: !p.base_url }">{{ p.base_url || '未设置' }}</span>
              <!-- 窄屏把这一段藏掉（只留 base_url），否则行会被撑成三行 -->
              <span class="pr-extra">
                <span class="sep">·</span>
                <span>{{ p.configured || 0 }} 个模型</span>
                <span class="sep">·</span>
                <span>{{ p.key_count || 0 }} 条密钥</span>
                <span class="sep">·</span>
                <span class="truncate" :class="{ unset: !p.default_key_label }" :title="p.default_key_label || ''">{{ defaultKeyText(p) }}</span>
              </span>
            </div>
          </div>

          <!-- 上移/下移：不用鼠标时的兜底，平时藏起来，hover 或焦点进来才显形 -->
          <div class="pr-move" @click.stop>
            <button
              type="button" class="btn icon sm ghost" title="上移" aria-label="上移"
              :disabled="!canOrder || orderBusy || i === 0" @click="moveProvider(p.id, -1)"
            ><AppIcon name="chevron-up" :size="14" /></button>
            <button
              type="button" class="btn icon sm ghost" title="下移" aria-label="下移"
              :disabled="!canOrder || orderBusy || i === providerList.length - 1" @click="moveProvider(p.id, 1)"
            ><AppIcon name="chevron-down" :size="14" /></button>
          </div>

          <div class="pr-ops" @click.stop>
            <button v-if="!narrow" type="button" class="btn sm" title="打开密钥列表" @click="openDrawer(p, 'keys')">
              <AppIcon name="document" :size="13" />管理密钥
            </button>
            <Menu
              :items="providerMenuItems(p)" :label="(p.label || p.id) + ' 的更多操作'" size="sm"
              @select="(key: string) => onProviderAction(key, p)"
            />
          </div>
        </div>
      </div>
    </section>

    <!-- 重命名：卡片上不再有行内输入框，改名从浮层「⋯」进来，提交的仍只有 name -->
    <Modal :open="renameOpen" title="重命名模型" narrow @close="closeRename">
      <label class="field">
        展示名
        <input
          ref="renameInput" v-model="renameText" spellcheck="false"
          placeholder="给这个配置起个名字" @keydown.enter.prevent="submitRename"
        >
      </label>
      <template #footer>
        <button type="button" class="btn sm" @click="closeRename">取消</button>
        <button
          type="button" class="btn primary sm"
          :disabled="rowBusy('name:' + (renameTarget?.id || ''))" @click="submitRename"
        >{{ rowBusy('name:' + (renameTarget?.id || '')) ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>

    <!-- 新建提供方（官方那几家的身份由后端目录给，这里用于加自己的端点） -->
    <Modal :open="newProvOpen" title="新增提供方" narrow @close="newProvOpen = false">
      <div class="add-picker">
        <label class="field">提供方 ID
          <input v-model="newProv.id" class="mono" spellcheck="false" placeholder="my-endpoint" />
        </label>
        <label class="field">名称
          <input v-model="newProv.label" placeholder="我的私有端点" />
        </label>
        <label class="field">Base URL
          <input v-model="newProv.base_url" class="mono" spellcheck="false" placeholder="https://api.example.com/v1" />
        </label>
        <label class="field">认证方式
          <select v-model="newProv.auth">
            <option value="bearer">Bearer（需要 API Key）</option>
            <option value="none">无需密钥</option>
          </select>
        </label>
        <label class="field">备注
          <input v-model="newProv.note" placeholder="选填" />
        </label>
        <p class="err" v-if="newProvError">{{ newProvError }}</p>
      </div>
      <template #footer>
        <button type="button" class="btn sm" @click="newProvOpen = false">取消</button>
        <button type="button" class="btn primary sm" :disabled="rowBusy('newprov')" @click="submitNewProvider">
          {{ rowBusy('newprov') ? '保存中…' : '保存' }}
        </button>
      </template>
    </Modal>

    <!-- 换模型浮层（Teleport + fixed，锚点是整张卡：关闭后焦点也回到这张卡） -->
    <ModelSwitcher
      :open="swOpen" :anchor="swAnchor" :title="swTitle"
      :rows="swGroup?.rows ?? []" :current-id="swGroup?.current?.id ?? ''"
      :loading="state.loading.models" :busy-ids="swBusyIds"
      @close="closeSwitcher" @select="onSwitchSelect" @action="onSwitchAction" @add="onSwitchAdd"
    >
      <template #meta="{ model }">
        <span class="sw-meta" :title="itemTitle(model)">
          <span class="stat-dot" :class="statusInfo(model).cls" />
          <span class="tc-status" :class="statusInfo(model).cls">{{ statusInfo(model).label }}</span>
          <span class="faint" v-if="testResults[model.id]">{{ testResults[model.id].ok ? latencyOf(model) : '失败' }}</span>
        </span>
      </template>
    </ModelSwitcher>

    <ProviderDrawer
      :provider="drawerProvider" :open="drawerOpen" :initial-tab="drawerTab"
      @close="closeDrawer" @changed="onDrawerChanged" @focus-type="onFocusType"
      @configure-group="openBatch"
    />

    <ModelForm
      :open="formOpen" :mode="formMode" :target="formTarget"
      :default-type="formDefaultType"
      :types="panelTypes" :type-labels="TYPE_LABEL"
      :secrets="state.secrets" :busy="rowBusy('submit')" :save-error="formError"
      :rules-warning="formWarning" :rules-error="formRulesError"
      :initial-step="formInitialStep" :focus-price="formFocusPrice"
      :batch-count="formBatch.length"
      @close="formOpen = false" @submit="submitForm"
    />
  </div>
</template>

<style scoped>
/* 面板本身是一列「块」，块间距 24px —— 四段结构一眼分得开 */
.model-panel { display: flex; flex-direction: column; gap: var(--sp-5); min-width: 0; }

/* ① 顶部工具行 */
.panel-bar { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.cap {
  font-size: calc(12px * var(--ts, 1)); color: var(--text-faint);
  letter-spacing: .01em; line-height: 1.5; min-width: 0;
}

.load-error {
  padding: 8px 12px;
  border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--border));
  border-radius: var(--r-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: calc(12.5px * var(--ts, 1));
}

/* ③④ 两个「块」：抬头一行 + 内容；不再用卡片包一层，免得和内部卡片抢边框 */
.block { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.block-head { display: flex; align-items: baseline; gap: var(--sp-2); flex-wrap: wrap; }
.bh-title {
  display: inline-flex; align-items: center; gap: 7px; margin: 0;
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 650; color: var(--text);
}
.bh-desc { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }

/* ---------------- 任务分类卡 ----------------
 * 列数**只看承载块自己的宽度**，不挂视口媒体查询：视口与可用宽之间隔着应用侧栏 +
 * 设置二级导航 + 内边距（实测差 478px，窄档还会变），拿视口去猜已经翻过两次车
 * （auto-fit 在 962 排 4 列留洞；视口断点在 1280 排出 151px 的窄卡）。
 * 阈值按 .types-block 的内联宽度：≥920 → 5 列（1440 的容器 962 → 卡 183）；
 * 620~919 → 3 列（1280 容器 802 → 260，1200 容器 722 → 233）；400~619 → 2 列；<400 → 1 列。
 * 列再窄也只截断模型名（truncate + title 看全名），不换行、不撑破卡片。
 * 同一行等高靠 grid 的 stretch + 卡片自身 flex 列，高度完全由内容决定：
 * 模型少的卡不留空，模型多的卡也不会把整页拉长（卡上只有一行当前模型）。 */
.types-block { container-type: inline-size; }
.types-row {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--sp-3);
  align-items: stretch;
}
@container (max-width: 919px) {
  .types-row { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@container (max-width: 619px) {
  .types-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@container (max-width: 399px) {
  .types-row { grid-template-columns: minmax(0, 1fr); }
}
.type-col {
  display: flex; flex-direction: column; gap: 6px; min-width: 0;
  /* 辅助行按卡宽决策（见 .tc-meta 的 @container） */
  container-type: inline-size;
  padding: 12px 14px;
  /* 淡色圆角底是全站统一口径（--bg-sunken + --r-sm，标题留在卡外透明）：
     原来这里是 --bg-elev 白底 + 1px 四边描边 + --r-md + --shadow-1，五个白盒子压在
     「限额 / 用量」两块裸块上，同一页不是一套语言 —— 所以去壳，只留淡底 */
  background: var(--bg-sunken);
  border: 0; box-shadow: none;
  border-radius: var(--r-sm);
  cursor: pointer;
  /* 没有描边可换了，悬停只让底色略深一点（深色主题下同样是"变一点"，不闪） */
  transition: background-color var(--dur) var(--ease);
}
.type-col:hover { background-color: color-mix(in srgb, var(--bg-sunken) 93%, var(--text) 7%); }
.type-col:focus-visible { outline: none; box-shadow: var(--ring); }
/* 「去配置」跳过来的那一类、以及浮层打开时：描边没了，同一种提示改成底色偏主题色 */
.type-col.focus,
.type-col[aria-expanded='true'] { background-color: color-mix(in srgb, var(--accent) 10%, var(--bg-sunken)); }

.tc-head { display: flex; align-items: center; gap: 7px; min-width: 0; }
.tc-ico {
  display: inline-grid; place-items: center; flex: none;
  width: 22px; height: 22px; border-radius: 6px;
  background: var(--accent-soft); color: var(--accent-text);
}
/* 分类图标素材是 currentColor 的单色 svg：用 mask 染色，深色主题下也看得见 */
.cat-mask {
  width: 14px; height: 14px; background-color: currentColor;
  -webkit-mask-size: contain; mask-size: contain;
  -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat;
  -webkit-mask-position: center; mask-position: center;
}
.tc-title { font-size: calc(12.5px * var(--ts, 1)); font-weight: 650; color: var(--text); min-width: 0; }
/* 模型条数从徽章降成次要文字：卡上已经有「当前模型」，数字只是补充 */
.tc-count {
  flex: none; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
/* 「+」：幽灵图标钮，平时不抢眼，hover 才显色 */
.tc-add {
  display: inline-flex; align-items: center; justify-content: center; flex: none;
  width: 22px; height: 22px; padding: 0;
  border: 0; border-radius: var(--r-pill);
  background: transparent; color: var(--text-faint); cursor: pointer;
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.tc-add:hover, .type-col:hover .tc-add { background: var(--bg-hover); color: var(--accent-text); }
.tc-add:focus-visible { outline: none; box-shadow: var(--ring); }

/* 当前模型行：整行固定 40px，模型名必须单行截断（名字再长也不许撑破卡片） */
.tc-cur {
  display: flex; align-items: center; gap: 8px; min-width: 0;
  height: 40px; padding: 0 6px; margin: 0 -6px;
  border-radius: var(--r-sm);
  transition: background var(--dur-fast) var(--ease);
}
.type-col:hover .tc-cur { background: var(--bg-hover); }
/* 刚建好的那条：极轻底色标一下，不闪不跳 */
.tc-cur.fresh { background: var(--accent-soft); }
.tc-name {
  flex: 1 1 auto; min-width: 0;
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 600; color: var(--text);
}
.tc-chev { flex: none; color: var(--text-faint); transition: transform var(--dur) var(--ease); }
.type-col[aria-expanded='true'] .tc-chev { transform: rotate(180deg); }
/* 未配置态：一行引导文字 + 一枚小按钮，占同样的行高，卡不会塌下去也不留空 */
.tc-cur.is-empty { gap: 8px; }
.tc-hint { flex: 1 1 auto; min-width: 0; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint); }
.tc-cur .btn { flex: none; }

/* 辅助行：状态点 + 状态文字 + · + 密钥来源。这一行**必须恒为一行** —— 卡高不齐就是从
 * 这里开始的（实测 232px 卡里「正常」被折成上下两个字）。所以状态点 / 状态文字 / 分隔点
 * 一律 nowrap 且不参与收缩，宽度不够只让密钥来源截断；再窄（卡 < 210px）密钥来源整段让位，
 * 只留状态 + 一个 ·。全文都在 title 里。 */
.tc-meta {
  display: flex; align-items: center; gap: 5px; min-width: 0; flex-wrap: nowrap;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}
.tc-meta > .stat-dot,
.tc-meta > .tc-status,
.tc-meta > .sep { flex: none; white-space: nowrap; }
.tc-meta > .tc-keysrc { flex: 0 1 auto; min-width: 0; }
/* 容器查询按**卡自身**的宽度判断（五列时每张卡只有 180 上下，不能拿视口宽度猜）。
   密钥来源连同它前面那枚分隔点一起收起 —— 否则卡底会悬着一个孤零零的「·」。 */
@container (max-width: 209px) {
  .tc-meta.is-status > .sep,
  .tc-meta.is-status > .tc-keysrc { display: none; }
}
.tc-status.ok { color: var(--ok); }
.tc-status.warn { color: var(--warn); }
.tc-status.danger { color: var(--danger); }
.tc-keysrc { min-width: 0; color: var(--text-faint); }
.tc-keysrc.warn { color: var(--warn); }
/* 浮层行尾那组状态由调用方的插槽渲染，样式跟着插槽留在本文件 */
.sw-meta { display: inline-flex; align-items: center; gap: 4px; min-width: 0; }
.sep { flex: none; color: var(--text-faint); opacity: .7; }

/* 状态点：只有一个点该有颜色 */
.stat-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--text-faint); }
.stat-dot.ok { background: var(--ok); }
.stat-dot.warn { background: var(--warn); }
.stat-dot.danger { background: var(--danger); }

/* ---------------- 提供方列表 ----------------
 * 一整块 .card 外壳，内部用 1px 细分隔线分行：行本身不再有描边、不再有行间空隙，
 * 一屏读得完，也不会看起来像"一叠卡片"。 */
.prov-card { padding: 2px 14px; overflow: hidden; }
.prov-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  min-height: 58px; padding: 10px 4px; min-width: 0;
  cursor: pointer;
  /* 圆角写在行自己身上、不写在 :hover 里：hover 底色是一整块矩形，圆角只挂在悬停态上
     会让"悬停时才变圆"；与抽屉里 .rel-row 同一处口径，两处才不会一圆一方。 */
  border-radius: var(--r-sm);
  transition: background var(--dur) var(--ease);
}
.prov-row + .prov-row { border-top: 1px solid var(--divider); }
.prov-row:hover { background: var(--bg-hover); }
/* 行在 .prov-card 的 overflow:hidden 里，外扩的 focus 环会被裁掉一半 —— 用内侧 outline */
.prov-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.pr-main { display: flex; flex-direction: column; gap: 3px; flex: 1 1 240px; min-width: 0; }
.pr-top { display: flex; align-items: center; gap: 7px; min-width: 0; flex-wrap: wrap; }
.pr-name { font-size: calc(13px * var(--ts, 1)); font-weight: 600; min-width: 0; }
.pr-status { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.pr-status.ok { color: var(--ok); }
.pr-status.danger { color: var(--danger); }
.pr-sub {
  display: flex; align-items: center; gap: 5px; min-width: 0;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
  white-space: nowrap; overflow: hidden;
}
/* base_url 是最长的不可断词（例如 https://generativelanguage.googleapis.com/v1beta/openai）。
   flex 子项默认 min-width:auto = min-content，会把它撑出容器、顶出横向滚动条，
   所以这里必须允许收缩后省略，完整地址看 title。 */
.pr-sub .mono { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.pr-sub .unset { color: var(--text-faint); }
/* 「N 个模型 · N 条密钥 · 默认密钥…」整段可整体收起（窄屏那行留给 base_url） */
.pr-extra { display: inline-flex; align-items: center; gap: 5px; min-width: 0; }
.pr-ops { display: flex; align-items: center; gap: 6px; flex: none; }

/* 拖动排序：手柄、拖动中的行、目标位置的插入线，以及键盘兜底的上移/下移 */
.pr-grip { flex: none; display: inline-flex; align-items: center; color: var(--text-faint); cursor: grab; }
.pr-grip.off { opacity: .35; cursor: not-allowed; }
.prov-row.dragging { opacity: .6; }
.prov-row.drop-target { box-shadow: inset 0 2px 0 var(--accent); }
.pr-move { display: flex; align-items: center; gap: 2px; opacity: 0; transition: opacity var(--dur-fast) var(--ease); }
.prov-row:hover .pr-move, .prov-row:focus-within .pr-move { opacity: 1; }
@media (hover: none) { .pr-move { opacity: 1; } }
.prov-order-note { display: inline-flex; align-items: center; gap: 5px; font-size: calc(11.5px * var(--ts, 1)); color: var(--warn); }

/* 厂商图标：统一视觉框、保持比例；缺图标用占位方块（绝不渲染空 src） */
.pv-ico { flex: none; object-fit: contain; border-radius: 3px; width: 20px; height: 20px; }
.pv-ico.lg { width: 24px; height: 24px; border-radius: 5px; }
.pv-fallback {
  flex: none; display: inline-grid; place-items: center;
  width: 20px; height: 20px; border-radius: 3px;
  background: var(--bg-sunken); color: var(--text-faint);
}
.pv-fallback.lg { width: 24px; height: 24px; border-radius: 5px; }

.prov-skel { display: flex; flex-direction: column; gap: 10px; }
.prov-error, .prov-empty {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 12px; border: 1px dashed var(--divider); border-radius: var(--r-md);
  color: var(--text-faint); font-size: calc(12px * var(--ts, 1)); line-height: 1.5;
}
.prov-error { border-color: color-mix(in srgb, var(--danger) 40%, var(--divider)); color: var(--danger); }
.prov-error p { margin: 0; flex: 1 1 200px; min-width: 0; overflow-wrap: anywhere; }

/* 「新增提供方」弹窗里的字段竖排（原来「+」的提供方选择器已删，那套 .add-note 随之下线） */
.add-picker { display: flex; flex-direction: column; gap: 14px; }
.err { color: var(--danger); margin: 0; line-height: 1.5; }

/* 断点从宽到窄递减排列：同一元素命中的最后一条生效（列宽由 1fr 均分，绝不出现横向滚动） */
/* 下面两条是容器查询不被支持时的兜底：写在容器查询之后，窄档同时命中时由它们定稿
   （860 → 2×377、560 → 1×530，与实测一致；容器查询更强时按容器宽度算） */
@media (max-width: 860px) {
  .types-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .types-row { grid-template-columns: minmax(0, 1fr); }
  /* 这一档行内只留「⋯」：元信息收进 title、管理密钥进菜单，
     否则「管理密钥」会被挤到第三行，一行 99px（实测）九家铺出去近 900px */
  .pr-extra { display: none; }
  .pr-ops { flex: none; margin-left: auto; }
  /* 名称那一行也不许折：折了同样会变成第三行 —— 名字截断，徽章/状态保持原宽 */
  .pr-top { flex-wrap: nowrap; }
  .pr-top > .badge, .pr-top > .stat-dot, .pr-top > .pr-status { flex: none; }
}
</style>
