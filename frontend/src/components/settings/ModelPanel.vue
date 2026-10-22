<script setup lang="ts">
/**
 * 设置页「模型」面板。
 *
 * 版式（用户口径）：**标题 + 说明** → **用量统计** → **任务分类与模型配置** → **模型提供方管理**。
 *
 * 相比上一版的三处结构性改动：
 * ① 页面底部那个独立的「密钥」折叠区**删掉了** —— 密钥天然属于某一家提供方，
 *    现在唯一的入口是提供方详情抽屉（新增 / 改名 / 换值 / 设为默认 / 测试 / 删除 / 看引用）。
 *    页面任何地方都不再出现密钥内容，连输入框都没有（写密钥只在抽屉里做）。
 * ② 任务分类卡不再是一条窄列：默认模型单独一块（比其它行高一档、带「默认」标签），
 *    其余模型是紧凑列表，列数用 auto-fit 跟着宽度走。
 * ③ 提供方管理成了独立一块：列表行有图标、官方/自定义、Base URL、模型数、密钥数、
 *    默认密钥、连接状态与四项操作，点整行进抽屉。
 *
 * 数据面全部走既有封装（api.get/post/put/del），后端契约见 modelTypes.ts 的注释。
 * 新接口（密钥 / 未归属 / 提供方测试）在后端还没上线时按 404 优雅降级：
 * 抽屉里给一行提示并禁用写操作，**不自己造数据**。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import type { ModelConfig } from '@/api/types';
import {
  loadModelTypes, loadModels, reportError, state, toast, withLoading,
} from '@/stores/app';
import { roundMs, truncate } from '@/utils/format';
import { askConfirm } from '@/stores/confirm';
import AppIcon from '@/components/AppIcon.vue';
import Menu from '@/components/Menu.vue';
import Modal from '@/components/Modal.vue';
import { categoryIcon, vendorColorIcon } from '@/assets/model-icons';
import ModelForm from '@/components/settings/ModelForm.vue';
import UsageChart from '@/components/settings/UsageChart.vue';
import ProviderSelector from '@/components/settings/ProviderSelector.vue';
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

/** `GET /models` 每行新增的密钥来源字段（后端算，前端只画；老后端没有，全部可选）。 */
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

interface RowAction {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
}

const loadError = ref('');
const busyKey = ref('');
const testResults = ref<Record<string, any>>({});   // model_id -> 最近一次测试结果
const highlightId = ref('');                        // 刚建好的那一行，短暂提亮
/** 「去配置」跳过来的那一类：卡片短暂高亮，并滚进可视区 */
const focusKey = ref('');

const usageRef = ref<InstanceType<typeof UsageChart> | null>(null);

/* ---------------------------- 按类型分组 ----------------------------
 * 分类语义照旧（text generation / 论文问答 agent / 向量化 embedding / 重排 reranker /
 * 视觉 vision；ocr 保持隐藏规则），只是每张卡的版式换了（默认模型突出 + 其余紧凑）。 */

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
  /** 默认之外的那几条（紧凑列表） */
  others: ModelRow[];
  empty: string;
}

const typeGroups = computed<TypeGroup[]>(() => {
  const known = panelTypes.value;
  const order = [...TYPE_ORDER.filter((t) => known.includes(t)), ...known.filter((t) => !TYPE_ORDER.includes(t))];
  const all = (state.models || []) as ModelRow[];
  return order.map((t) => {
    const rows = all.filter((m) => m.model_type === t);
    const current = rows.find((m) => m.is_default) || null;
    return {
      key: t,
      title: typeLabel(t),
      icon: TYPE_ICON[t] || 'model',
      rows,
      current,
      others: current ? rows.filter((m) => m.id !== current.id) : rows,
      empty: '还没有' + typeLabel(t) + '模型',
    };
  });
});

/* ---------------------------- 状态 / 密钥来源 ----------------------------
 * 一个模型的状态要能立刻分三种：正常 / 未配置密钥 / 连接异常（外加"还没测过"这个中性态）。
 * has_secret === false 来自后端，比 status 更早能看出"这把钥匙还没填"。 */
function statusInfo(m: ModelRow): { label: string; cls: string } {
  if (m?.has_secret === false && !m?.is_mock) return { label: '未配置密钥', cls: 'warn' };
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

/** ⋯ 里那条信息行的文字：服务商 · 域名 */
function infoLabel(m: ModelRow): string {
  const host = hostOf(m.base_url);
  return [providerCell(m), host].filter(Boolean).join(' · ') || '—';
}

function itemTitle(m: ModelRow): string {
  const host = hostOf(m.base_url);
  const extra = [m.model_name || '', providerCell(m), host].filter(Boolean).join(' · ');
  return m.has_secret === false && !m.is_mock ? extra + '（缺密钥）' : extra;
}

function latencyOf(m: ModelRow): string {
  const r = testResults.value[m.id];
  const ms = r?.latency_ms ?? r?.latency ?? (m as any)?.latency_ms;
  return ms === undefined || ms === null ? '—' : roundMs(ms) + ' ms';
}

function rowBusy(key: string): boolean {
  return busyKey.value === key;
}

/** ⋯ 菜单：信息行（服务商·域名）→ 密钥来源（只读）→ 测试 / 设为默认 / 编辑 / 删除 */
function rowMenu(m: ModelRow): RowAction[] {
  return [
    { key: 'info', label: infoLabel(m), icon: 'server', disabled: true },
    { key: 'keysrc', label: keySourceInfo(m).text, icon: 'document', disabled: true },
    { key: 'test', label: '测试连通性', icon: 'bolt', disabled: rowBusy('test:' + m.id) },
    { key: 'default', label: '设为默认', icon: 'check_circle', disabled: !!m.is_default || rowBusy('def:' + m.id) },
    { key: 'edit', label: '编辑配置', icon: 'edit' },
    { key: 'delete', label: '删除模型', icon: 'trash', danger: true, disabled: rowBusy('del:' + m.id) },
  ];
}

function onRowAction(m: ModelRow, key: string): void {
  if (key === 'info' || key === 'keysrc') return;
  if (key === 'test') { void testModel(m); return; }
  if (key === 'default') { void setDefault(m); return; }
  if (key === 'edit') { openEdit(m); return; }
  if (key === 'delete') void removeModel(m);
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
const customEntry = ref<ProviderRow>({
  id: 'custom', kind: 'custom', label: '自定义提供方', icon: '',
  note: '填名称 / Base URL / API Key / Model ID',
});

async function loadProviders(): Promise<void> {
  providersLoading.value = true;
  try {
    const data = await api.get<{ providers?: ProviderRow[]; custom?: ProviderRow }>('/models/providers');
    providers.value = Array.isArray(data?.providers) ? data.providers : [];
    if (data?.custom && data.custom.id) customEntry.value = data.custom;
    providersError.value = '';
  } catch (err) {
    providers.value = [];
    providersError.value = '提供方目录读取失败：' + String((err as any)?.message ?? err);
  } finally {
    providersLoading.value = false;
  }
}

/** 选择器里的候选 = 目录里的官方/自定义那几家 + 末尾的「自定义提供方」入口 */
const providerOptions = computed<ProviderRow[]>(() => [...providers.value, customEntry.value]);

/** 默认密钥那一格的文案。
 *  后端迁移时给默认密钥起的名字就是「默认密钥」，直接拼会读成「默认密钥：默认密钥」——
 *  所以名字与固定文案相同时只报「已设置」；没有默认密钥时仍报「未设置」。 */
const DEFAULT_KEY_WORD = '默认密钥';

function defaultKeyText(p: ProviderRow): string {
  const label = String(p.default_key_label ?? '').trim();
  if (!label) return `${DEFAULT_KEY_WORD}：未设置`;
  if (label === DEFAULT_KEY_WORD) return `${DEFAULT_KEY_WORD} 已设置`;
  return `${DEFAULT_KEY_WORD}：${label}`;
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

/* ---------------------------- 提供方操作（列表行上的四个动作） ---------------------------- */

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
    title: '删除提供方「' + (p.label || p.id) + '」？',
    body: '还有模型挂在它上面时后端会拒绝删除，并把原因告诉你。',
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

/* ---------------------------- 按类型添加（卡片抬头那枚 +） ----------------------------
 * 只有一步：先选提供方（ProviderSelector，可搜索、不溢出视口），选中就直接开表单，
 * 表单按这家的 base_url / auth 预填。原先「先挑预设、再进表单」那一级已经**整套下线**
 * （用户口径第二轮：后端连 /models/presets 都不再提供），这里既不请求预设，也不再提"套预设"。 */
const addOpen = ref(false);
const addType = ref('agent');
const addProviderId = ref('');

function openAdd(type: string): void {
  addType.value = type;
  addProviderId.value = '';
  addOpen.value = true;
  if (!providers.value.length) void loadProviders();
}

/** 给 ModelForm 的 seed：只用它认得的四个字段（provider / base_url / 密钥名预填） */
function seedOf(p: ProviderRow | null): { id: string; label?: string; base_url?: string; auth?: string } | null {
  if (!p) return null;
  return { id: p.id, label: p.label, base_url: p.base_url || '', auth: p.auth };
}

/** 选中提供方：官方 → 带着这家的身份与端点进表单（开在第二步）；
 *  「自定义提供方」→ 不带 seed、不带 customEntry，**照样从第一步进**（自定义也在第一步的列表里）。 */
watch(addProviderId, (id) => {
  if (!id || !addOpen.value) return;
  addOpen.value = false;
  if (id === customEntry.value.id) {
    openCreate(addType.value, false, null);
    return;
  }
  const picked = providerOptions.value.find((x) => x.id === id) ?? null;
  openCreate(addType.value, false, picked);
});

/** 底部的「手动新建配置」：第一步就是选提供方（自定义提供方也在那一步的列表里） */
function addManual(): void {
  addOpen.value = false;
  openCreate(addType.value, false, null);
}

/* ---------------------------- 行内改名 ---------------------------- */

const renamingId = ref('');
const renameText = ref('');
let renameInput: HTMLInputElement | null = null;

function bindRename(el: any): void {
  if (el) renameInput = el as HTMLInputElement;
}

async function startRename(m: ModelRow): Promise<void> {
  renamingId.value = m.id;
  renameText.value = m.name || '';
  await nextTick();
  renameInput?.focus();
  renameInput?.select();
}

function cancelRename(): void {
  renamingId.value = '';
}

/** 回车 / 失焦保存：只提交 name 一个字段，别的一律不动。 */
async function commitRename(m: ModelRow): Promise<void> {
  if (renamingId.value !== m.id) return;   // Esc 已取消，卸载触发的 blur 不算保存
  const next = renameText.value.trim();
  renamingId.value = '';
  if (!next || next === (m.name || '')) return;
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
    title: '删除模型配置「' + (m.name || m.id) + '」？',
    body: [routeNote, '删除后这个配置就没了，不可撤销。'].filter(Boolean).join(' '),
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
const formDefaultType = ref('agent');
/** 恒为 false：`customEntry` 会让表单跳过第一步，而「手动新建 / 自定义」都要求先在第
 *  一步选提供方（自定义提供方本身就是第一步列表里的一行）。prop 保留给将来真需要跳步的场景。 */
const formCustom = ref(false);
/** 保存失败的文案：透给表单第五步的「保存结果」那一行（`saveError`），每次打开表单时清空 */
const formError = ref('');
/** 官方提供方带进来的厂商身份与端点 */
const formSeed = ref<{ id: string; label?: string; base_url?: string; auth?: string } | null>(null);
let highlightTimer: ReturnType<typeof setTimeout> | null = null;

function openCreate(
  type = 'agent',
  custom = false,
  provider: ProviderRow | null = null,
): void {
  formMode.value = 'create';
  formTarget.value = null;
  formDefaultType.value = type;
  formCustom.value = custom;
  formSeed.value = seedOf(provider);
  formError.value = '';
  formOpen.value = true;
}

function openEdit(m: ModelRow): void {
  formMode.value = 'edit';
  formTarget.value = m;
  formDefaultType.value = m.model_type || 'agent';
  formCustom.value = false;
  formSeed.value = null;
  formError.value = '';
  formOpen.value = true;
}

function flash(id: string): void {
  highlightId.value = id;
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = setTimeout(() => {
    if (highlightId.value === id) highlightId.value = '';
    highlightTimer = null;
  }, 2800);
}

async function submitForm(payload: Record<string, any>): Promise<void> {
  busyKey.value = 'submit';
  formError.value = '';
  try {
    if (formMode.value === 'edit' && formTarget.value) {
      await api.put('/models/' + formTarget.value.id, payload);
      toast('模型配置已更新', 'ok');
      formOpen.value = false;
      await refresh();
      flash(formTarget.value.id);
    } else {
      const created = await api.post<any>('/models', payload);
      formOpen.value = false;
      await refresh();
      await loadProviders();
      const id = String(created?.id || created?.model?.id || payload.name || '');
      /* 保存成功 ≠ 连接可用：存完立刻用既有 POST /models/{id}/test 真打一次端点。 */
      if (id) setTimeout(() => { void testModel(created as ModelRow); }, 300);
      if (id) flash(id);
      toast('已创建，建议点「测试」验证连通性', 'ok', 4800);
    }
  } catch (err) {
    /* 两条路都留着：toast 给人即时反馈，formError 让表单第五步自己留一行原因（关掉弹窗就没了） */
    formError.value = '保存失败：' + String((err as any)?.message ?? err);
    reportError(err, '保存失败');
  } finally {
    busyKey.value = '';
  }
}

async function refreshAll(): Promise<void> {
  await refresh();
  await loadProviders();
  usageRef.value?.reload();
}

onMounted(() => {
  void refreshAll();
  void loadSecretNames();
});

onBeforeUnmount(() => {
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
      <span class="cap">{{ state.models.length }} 个配置 · 加模型、测连通、看用量都在这；密钥在各提供方详情里</span>
      <span class="spacer"></span>
      <button class="btn icon sm" :disabled="state.loading.models" title="重新读取模型 / 提供方" @click="refreshAll">
        <AppIcon name="refresh" :size="15" />
      </button>
    </div>

    <div class="load-error" v-if="loadError">{{ loadError }}</div>

    <!-- ② 用量统计（口径不变，只压缩留白与层级） -->
    <UsageChart ref="usageRef" />

    <!-- ③ 任务分类与模型配置：每类一张卡，默认模型单独一块 -->
    <section class="block types-block">
      <header class="block-head">
        <h3 class="bh-title"><AppIcon name="grid" :size="14" />任务分类与模型配置</h3>
        <span class="bh-desc">每类一个默认模型 + 若干备选；点卡片右上角的 + 往这一类里加</span>
      </header>

      <div class="types-row">
        <section
          class="type-col" v-for="g in typeGroups" :key="g.key"
          :id="'type-card-' + g.key"
          :class="{ focus: focusKey === g.key }"
        >
          <header class="tc-head">
            <span class="tc-ico">
              <span v-if="categoryIcon(g.key)" class="cat-mask" :style="catStyle(g.key)" />
              <AppIcon v-else :name="g.icon" :size="12" />
            </span>
            <span class="tc-title truncate" :title="g.title + '：' + (TYPE_DESC[g.key] || '—')">{{ g.title }}</span>
            <span class="hd-count">{{ g.rows.length }}</span>
            <span class="spacer"></span>
            <button
              type="button" class="card-add"
              :title="'添加' + g.title + '模型'" :aria-label="'添加' + g.title + '模型'"
              :data-add-type="g.key" @click="openAdd(g.key)"
            >
              <AppIcon name="add" :size="14" />
            </button>
          </header>
          <p class="tc-desc">{{ TYPE_DESC[g.key] || '这一类还没登记用途' }}</p>

          <!-- 默认模型：比其它行高一档（底色 + 描边 + 徽章 + 状态文字） -->
          <div class="tc-default" v-if="g.current" :title="itemTitle(g.current)">
            <div class="td-top">
              <img v-if="vendorIco(g.current.provider)" class="pv-ico" :src="vendorIco(g.current.provider)" alt="">
              <span v-else class="pv-fallback" :title="providerCell(g.current)"><AppIcon name="model" :size="12" /></span>
              <span class="td-name" :title="g.current.name || g.current.id">{{ g.current.name || '（未命名）' }}</span>
              <Menu
                class="tc-more" :items="rowMenu(g.current)" up
                :label="'更多操作：' + (g.current.name || g.current.id)"
                @select="onRowAction(g.current, $event)"
              />
            </div>
            <div class="td-meta">
              <span class="badge accent tiny">默认</span>
              <span class="stat-dot" :class="statusInfo(g.current).cls" />
              <span class="td-status" :class="statusInfo(g.current).cls">{{ statusInfo(g.current).label }}</span>
              <span class="td-sep">·</span>
              <span class="mono td-model truncate" :title="g.current.model_name || ''">{{ g.current.model_name || '—' }}</span>
            </div>
            <div class="td-keysrc tiny" :class="keySourceInfo(g.current).cls">{{ keySourceInfo(g.current).text }}</div>
          </div>
          <div class="tc-nodefault" v-else>
            <AppIcon name="warning" :size="12" />还没设这一类默认模型
          </div>

          <!-- 其它模型：紧凑列表（状态点 + 厂商图标 + 展示名） -->
          <div class="tc-list">
            <div class="tc-item" v-for="m in g.others" :key="m.id" :class="{ fresh: highlightId === m.id }">
              <div class="tc-line">
                <span class="stat-dot" :class="statusInfo(m).cls" :title="statusInfo(m).label" />
                <img v-if="vendorIco(m.provider)" class="pv-ico" :src="vendorIco(m.provider)" alt="">
                <span v-else class="pv-fallback" :title="providerCell(m)"><AppIcon name="model" :size="11" /></span>
                <input
                  v-if="renamingId === m.id"
                  class="rename" :ref="bindRename" v-model="renameText"
                  spellcheck="false"
                  @keydown.enter.prevent="commitRename(m)"
                  @keydown.esc.stop="cancelRename"
                  @blur="commitRename(m)"
                >
                <button
                  v-else type="button" class="tc-name truncate" :title="(m.name || m.id) + '（点击改名）'"
                  @click="startRename(m)"
                >{{ m.name || '（未命名）' }}</button>
                <Menu
                  class="tc-more" :items="rowMenu(m)" up
                  :label="'更多操作：' + (m.name || m.id)"
                  @select="onRowAction(m, $event)"
                />
              </div>
              <div class="tc-sub truncate" :title="itemTitle(m)">
                <span class="mono truncate">{{ m.model_name || '—' }}</span>
                <span class="td-sep">·</span>
                <span class="tc-status" :class="statusInfo(m).cls">{{ statusInfo(m).label }}</span>
              </div>
              <div class="tc-keysrc tiny" :class="keySourceInfo(m).cls">{{ keySourceInfo(m).text }}</div>
              <div class="tc-test tiny" v-if="testResults[m.id]">
                <template v-if="testResults[m.id].ok">
                  <span class="badge ok tiny">通过</span>
                  <span class="faint">{{ latencyOf(m) }}</span>
                </template>
                <template v-else>
                  <span class="badge danger tiny">失败</span>
                  <span class="faint truncate">{{ truncate(testResults[m.id].detail || '未知原因', 60) }}</span>
                </template>
              </div>
            </div>

            <div class="tc-empty" v-if="!g.others.length && g.current">
              <span class="truncate">这一类只有默认这一个</span>
            </div>
            <div class="tc-empty" v-if="!g.rows.length">
              <span class="truncate">{{ g.empty }}</span>
              <button class="btn sm" @click="openAdd(g.key)">
                <AppIcon name="add" :size="13" />添加
              </button>
            </div>
          </div>
        </section>
      </div>
    </section>

    <!-- ④ 模型提供方管理 -->
    <section class="block providers-block">
      <header class="block-head">
        <h3 class="bh-title"><AppIcon name="server" :size="14" />模型提供方</h3>
        <span class="bh-desc">连接配置与密钥都在这家的详情里；点整行打开</span>
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

      <div class="prov-rows" v-else>
        <div
          class="prov-row" v-for="p in providers" :key="p.id"
          role="button" tabindex="0" :data-provider-row="p.id"
          @click="openDrawer(p, 'keys')"
          @keydown.enter.prevent="openDrawer(p, 'keys')"
          @keydown.space.prevent="openDrawer(p, 'keys')"
        >
          <img v-if="vendorIco(p.id)" class="pv-ico lg" :src="vendorIco(p.id)" alt="">
          <span v-else class="pv-fallback lg" :title="p.label || p.id"><AppIcon name="server" :size="15" /></span>

          <div class="pr-main">
            <div class="pr-top">
              <span class="pr-name truncate" :title="p.label || p.id">{{ p.label || p.id }}</span>
              <span class="badge tiny dim">{{ p.kind === 'custom' ? '自定义' : '官方' }}</span>
              <span class="stat-dot" :class="providerStatusCls(p)" :title="providerStatusText(p)" />
              <span class="pr-status tiny" :class="providerStatusCls(p)">{{ providerStatusText(p) }}</span>
            </div>
            <div class="pr-sub tiny faint" :title="p.base_url || '未设置'">
              <span class="mono" :class="{ unset: !p.base_url }">{{ p.base_url || '未设置' }}</span>
              <span class="td-sep">·</span>
              <span>{{ p.configured || 0 }} 个模型</span>
              <span class="td-sep">·</span>
              <span>{{ p.key_count || 0 }} 条密钥</span>
              <span class="td-sep">·</span>
              <span :class="{ unset: !p.default_key_label }" :title="p.default_key_label || ''">{{ defaultKeyText(p) }}</span>
            </div>
          </div>

          <div class="pr-ops" @click.stop>
            <button class="btn sm" title="打开密钥列表" @click="openDrawer(p, 'keys')">
              <AppIcon name="document" :size="13" />管理密钥
            </button>
            <button class="btn sm" :disabled="rowBusy('ptest:' + p.id)" title="真的打一次这家端点" @click="testProvider(p)">
              <AppIcon name="bolt" :size="13" />{{ rowBusy('ptest:' + p.id) ? '测试中…' : '测试连接' }}
            </button>
            <button class="btn sm" @click="openDrawer(p, 'connect')">
              <AppIcon name="edit" :size="13" />编辑
            </button>
            <button class="btn sm danger" :disabled="rowBusy('pdel:' + p.id)" @click="removeProvider(p)">
              <AppIcon name="trash" :size="13" />删除
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- 「+」的选择器：选一家提供方（可搜索）→ 直接进表单；不再有「预设」这一级 -->
    <Modal :open="addOpen" :title="'添加' + typeLabel(addType) + '模型'" @close="addOpen = false">
      <div class="add-picker">
        <p class="tiny faint add-note">
          先选厂商（官方提供方会自动填好 Base URL），选中后填自己的 API Key、从厂商拉模型。
        </p>
        <ProviderSelector
          v-model="addProviderId"
          :providers="providerOptions"
          :loading="providersLoading"
          :error="providersError"
        />
      </div>
      <template #footer>
        <button class="btn sm" @click="addManual">
          <AppIcon name="edit" :size="14" />手动新建配置
        </button>
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
        <p class="tiny err" v-if="newProvError">{{ newProvError }}</p>
      </div>
      <template #footer>
        <button class="btn sm" @click="newProvOpen = false">取消</button>
        <button class="btn primary sm" :disabled="rowBusy('newprov')" @click="submitNewProvider">
          {{ rowBusy('newprov') ? '保存中…' : '保存' }}
        </button>
      </template>
    </Modal>

    <ProviderDrawer
      :provider="drawerProvider" :open="drawerOpen" :initial-tab="drawerTab"
      @close="closeDrawer" @changed="onDrawerChanged" @focus-type="onFocusType"
    />

    <ModelForm
      :open="formOpen" :mode="formMode" :target="formTarget"
      :default-type="formDefaultType" :custom-entry="formCustom" :seed="formSeed"
      :types="panelTypes" :type-labels="TYPE_LABEL"
      :secrets="state.secrets" :busy="rowBusy('submit')" :save-error="formError"
      @close="formOpen = false" @submit="submitForm"
    />
  </div>
</template>

<style scoped>
/* 面板本身是一列「块」，块间距 16px —— 比卡片间距大一点，四段结构一眼分得开 */
.model-panel { display: flex; flex-direction: column; gap: 16px; min-width: 0; }

/* ① 顶部工具行 */
.panel-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.cap {
  font-size: calc(12px * var(--ts, 1)); color: var(--text-faint);
  letter-spacing: .01em; line-height: 1.5; min-width: 0;
}

.load-error {
  padding: 8px 12px;
  border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--border));
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: calc(12.5px * var(--ts, 1));
}

/* ③④ 两个「块」：抬头一行 + 内容；不再用卡片包一层，免得和内部卡片抢边框 */
.block { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.block-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.bh-title {
  display: inline-flex; align-items: center; gap: 7px; margin: 0;
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 650; color: var(--text);
}
.bh-desc { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }

/* 任务分类：列数跟着宽度走（桌面充分展开，窄屏 1~2 列），绝不靠缩字号 */
.types-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
  gap: 10px; align-items: start;
}
.type-col {
  display: flex; flex-direction: column; min-width: 0;
  background: var(--bg-layer); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-1);
  overflow: hidden;
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.type-col.focus { border-color: var(--accent); box-shadow: var(--ring); }

.tc-head { display: flex; align-items: center; gap: 7px; min-width: 0; padding: 9px 10px 7px; }
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
.hd-count {
  min-width: 18px; padding: 0 5px; height: 16px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 999px; background: var(--bg-sunken); color: var(--text-faint);
  font-size: calc(10.5px * var(--ts, 1)); font-weight: 600; font-variant-numeric: tabular-nums;
}
.card-add {
  display: inline-flex; align-items: center; justify-content: center; flex: none;
  width: 22px; height: 22px; padding: 0;
  border: 1px solid var(--divider); border-radius: 999px;
  background: transparent; color: var(--text-dim); cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.card-add:hover { border-color: color-mix(in srgb, var(--accent) 50%, var(--divider)); background: var(--bg-hover); color: var(--accent-text); }
.card-add:focus-visible { outline: none; box-shadow: var(--ring); }
.tc-desc { margin: -2px 10px 0; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }

/* 默认模型：比其它行高一档（浅底 + 描边 + 更大的名字 + 状态文字） */
.tc-default {
  position: relative;
  margin: 8px 8px 0; padding: 8px 9px;
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--divider));
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  display: flex; flex-direction: column; gap: 4px; min-width: 0;
}
.td-top { display: flex; align-items: flex-start; gap: 6px; min-width: 0; padding-right: 18px; }
/* 默认模型的名字**不截断**：最多两行（宽列一行放得下，窄列换第二行也读得完） */
.td-name {
  font-size: calc(13px * var(--ts, 1)); font-weight: 650; color: var(--text); min-width: 0;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  line-height: 1.35;
}
.td-meta { display: flex; align-items: center; gap: 5px; min-width: 0; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
.td-status.ok { color: var(--ok); }
.td-status.warn { color: var(--warn); }
.td-status.danger { color: var(--danger); }
.td-sep { color: var(--text-faint); opacity: .7; }
.td-model { min-width: 0; font-size: calc(11px * var(--ts, 1)); }
.td-keysrc { color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.td-keysrc.warn { color: var(--warn); }
.tc-nodefault {
  display: flex; align-items: center; gap: 6px;
  margin: 8px 8px 0; padding: 7px 9px;
  border: 1px dashed var(--divider); border-radius: var(--radius-sm);
  color: var(--text-faint); font-size: calc(11.5px * var(--ts, 1));
}

/* 其它模型：紧凑三行（名称 / 模型名·状态 / 密钥来源） */
.tc-list { display: flex; flex-direction: column; padding: 6px 0 6px; }
.tc-item { position: relative; padding: 6px 10px; min-width: 0; }
.tc-item + .tc-item { border-top: 1px solid var(--divider); }
.tc-item:hover { background: var(--bg-hover); }
.tc-item.fresh { background: var(--accent-soft); box-shadow: inset 2px 0 0 var(--accent); }
.tc-line { display: flex; align-items: center; gap: 6px; min-width: 0; padding-right: 18px; }
.tc-name {
  flex: 1 1 auto; min-width: 0; padding: 0; border: 0; background: transparent;
  color: var(--text); font: inherit; font-size: calc(12px * var(--ts, 1)); font-weight: 600;
  text-align: left; cursor: text; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tc-name:hover { color: var(--accent-text); }
.rename { flex: 1 1 auto; min-width: 0; height: 22px; font-size: calc(12px * var(--ts, 1)); font-weight: 600; }
.tc-sub { display: flex; align-items: center; gap: 4px; min-width: 0; margin-top: 1px; padding-left: 12px; font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); }
.tc-status.ok { color: var(--ok); }
.tc-status.warn { color: var(--warn); }
.tc-status.danger { color: var(--danger); }
/* 任何 mono 片段都不许当"不可断的长词"把卡片撑宽 */
.tc-sub .mono, .td-model { min-width: 0; max-width: 100%; }
.tc-keysrc { padding-left: 12px; margin-top: 1px; color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tc-keysrc.warn { color: var(--warn); }
.tc-test { display: flex; align-items: center; gap: 4px; margin-top: 3px; padding-left: 12px; min-width: 0; }

/* 状态点：只有一个点该有颜色 */
.stat-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--text-faint); }
.stat-dot.ok { background: var(--ok); }
.stat-dot.danger { background: var(--danger); }

/* 列里的 ⋯：压在右上角，hover / 键盘聚焦才浮现（触摸设备常显） */
.tc-more { position: absolute; top: 3px; right: 3px; opacity: 0; transition: opacity var(--dur-fast) var(--ease); }
.tc-default .tc-more { opacity: .7; }
.tc-item:hover .tc-more, .tc-item:focus-within .tc-more { opacity: 1; }
@media (hover: none) { .tc-more { opacity: 1; } }

.tc-empty {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 4px 10px 2px; color: var(--text-faint);
  font-size: calc(11px * var(--ts, 1));
}
.tc-empty .btn { flex: none; }

/* 厂商图标：统一视觉框、保持比例；缺图标用占位方块（绝不渲染空 src） */
.pv-ico { flex: none; object-fit: contain; border-radius: 3px; width: 15px; height: 15px; }
.pv-ico.lg { width: 30px; height: 30px; border-radius: 5px; }
.pv-fallback {
  flex: none; display: inline-grid; place-items: center;
  width: 15px; height: 15px; border-radius: 3px;
  background: var(--bg-sunken); color: var(--text-faint);
}
.pv-fallback.lg { width: 30px; height: 30px; border-radius: 5px; }

/* ④ 提供方列表：一行一家（窄屏折成两行，绝不横向溢出） */
.prov-rows { display: flex; flex-direction: column; gap: 8px; }
.prov-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 10px 12px; min-width: 0;
  background: var(--bg-layer); border: 1px solid var(--border); border-radius: var(--radius);
  cursor: pointer;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.prov-row:hover { border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); background: var(--bg-hover); }
.prov-row:focus-visible { outline: none; box-shadow: var(--ring); }
.pr-main { display: flex; flex-direction: column; gap: 3px; flex: 1 1 240px; min-width: 0; }
.pr-top { display: flex; align-items: center; gap: 7px; min-width: 0; flex-wrap: wrap; }
.pr-name { font-size: calc(12.5px * var(--ts, 1)); font-weight: 650; min-width: 0; }
.pr-status { color: var(--text-faint); }
.pr-status.ok { color: var(--ok); }
.pr-status.danger { color: var(--danger); }
.pr-sub { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; min-width: 0; }
/* Base URL 是最长的不可断词（例如 https://generativelanguage.googleapis.com/v1beta/openai）。
   flex 子项默认 min-width:auto = min-content，会把 .pr-main 撑到 312px，
   在 320/360 下直接把整页顶出 23px 的横向滚动条 —— 所以这里必须允许它收缩后省略，
   完整地址看 title。 */
.pr-sub .mono {
  font-family: var(--mono);
  flex: 0 1 auto; min-width: 0; max-width: 100%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pr-sub .unset { color: var(--text-faint); }
.pr-ops { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }

.prov-skel { display: flex; flex-direction: column; gap: 8px; }
.prov-error, .prov-empty {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 12px; border: 1px dashed var(--divider); border-radius: var(--radius);
  color: var(--text-faint); font-size: calc(12px * var(--ts, 1)); line-height: 1.5;
}
.prov-error { border-color: color-mix(in srgb, var(--danger) 40%, var(--divider)); color: var(--danger); }
.prov-error p { margin: 0; flex: 1 1 200px; min-width: 0; overflow-wrap: anywhere; }

/* 「+」里只剩提供方选择器（预设卡那一套样式随功能一起下线） */
.add-picker { display: flex; flex-direction: column; gap: 10px; }
.add-note { margin: 0; line-height: 1.5; }
.err { color: var(--danger); margin: 0; line-height: 1.5; }

@media (max-width: 520px) {
  .pr-ops { flex: 1 0 100%; }
  .pr-ops .btn { flex: 1 1 auto; justify-content: center; }
}
</style>
