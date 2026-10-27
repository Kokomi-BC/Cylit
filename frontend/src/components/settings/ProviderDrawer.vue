<script setup lang="ts">
/**
 * ProviderDrawer —— 一家提供方的详情抽屉：**连接配置 / 密钥 / 关联模型** 三块。
 *
 * 为什么把页面底部那个独立「密钥」面板并进这里：密钥天然属于某一家提供方，
 * 放在页面底部会出现"两套密钥入口"，而且看不出这把钥匙是谁的、被哪些模型用着。
 * 现在唯一入口就是这家提供方的密钥列表（新增 / 改名 / 换值 / 设为默认 / 测试 / 删除）。
 *
 * 安全口径：**界面永远不出现明文**。后端只给 `masked`（`••••a1b2`），
 * 新增 / 换值时输入框只写不读，写完立刻清空。
 *
 * 对外契约（**签名别改**）：
 *   props { provider: ProviderRow | null, open: boolean }
 *   emit  close / changed（任何写操作后让父级刷新）
 * 另有两个可选扩展位（契约之外、都有默认值，不影响 C 阶段复用）：
 *   · `initialTab`：从「编辑」进来时直接落在连接配置；
 *   · `focus-type` 事件：关联模型里点「去配置」→ 父级滚到对应的任务分类卡。
 *
 * 版面口径：单列、只有一个滚动容器（`.pd-body`）。所有区块**默认展开**，
 * 不折叠成手风琴 —— 抽屉本身就是"按需出现的浮层"，再藏一层只会让人多点两下。
 */
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import type { ModelConfig } from '@/api/types';
import { loadModels, reportError, state, toast } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import AppIcon from '@/components/AppIcon.vue';
import Menu from '@/components/Menu.vue';
import { vendorColorIcon } from '@/assets/model-icons';
import type { KeyRow, ProviderRow, ProviderTestResult } from './modelTypes';

const props = withDefaults(defineProps<{
  provider: ProviderRow | null;
  open: boolean;
  /** 打开时落在哪一块（默认密钥，因为抽屉的主要用途是管密钥） */
  initialTab?: 'connect' | 'keys' | 'models';
}>(), { initialTab: 'keys' });

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'changed'): void;
  (e: 'focus-type', type: string): void;
  /* 同名多配置组的「批量配置」：把整组交给父级表单，一次写回每个配置（后端只支持逐个 PUT，
     所以父级循环调用既有接口，这里只负责"点了哪一组"）。 */
  (e: 'configure-group', models: ModelConfig[]): void;
}>();

function errText(err: unknown): string {
  return String((err as any)?.message ?? err ?? '未知错误');
}
function errStatus(err: unknown): number {
  return Number((err as any)?.status ?? 0);
}

const tab = ref<'connect' | 'keys' | 'models'>('keys');
/** 全抽屉共用的忙状态键（同一时刻只允许一个写操作） */
const busy = ref('');

function rowBusy(key: string): boolean {
  return busy.value === key;
}

/* ---------------------------- 连接配置 ---------------------------- */

type AuthKind = 'bearer' | 'none';

const conn = reactive({ label: '', base_url: '', auth: 'bearer' as AuthKind, note: '' });
const base = reactive({ label: '', base_url: '', auth: 'bearer' as AuthKind, note: '' });
const connError = ref('');

const connChanged = computed(() => conn.label !== base.label
  || conn.base_url !== base.base_url
  || conn.auth !== base.auth
  || conn.note !== base.note);

function fillConn(p: ProviderRow | null): void {
  base.label = p?.label ?? p?.id ?? '';
  base.base_url = p?.base_url ?? '';
  base.auth = p?.auth === 'none' ? 'none' : 'bearer';
  base.note = p?.note ?? '';
  conn.label = base.label;
  conn.base_url = base.base_url;
  conn.auth = base.auth;
  conn.note = base.note;
  connError.value = '';
}

async function saveConn(): Promise<void> {
  const p = props.provider;
  if (!p) return;
  connError.value = '';
  const label = conn.label.trim();
  if (!label) { connError.value = '名称必填'; return; }
  const url = conn.base_url.trim();
  if (url && !/^https?:\/\//i.test(url)) {
    connError.value = 'Base URL 要以 http:// 或 https:// 开头';
    return;
  }
  busy.value = 'save';
  try {
    await api.post('/models/providers', {
      id: p.id, label, base_url: url || undefined, auth: conn.auth, note: conn.note.trim() || undefined,
    });
    base.label = label;
    base.base_url = url;
    base.auth = conn.auth;
    base.note = conn.note.trim();
    toast('提供方已保存', 'ok');
    emit('changed');
  } catch (err) {
    connError.value = `保存失败：${errText(err)}`;
  } finally {
    busy.value = '';
  }
}

/* ---------------------------- 密钥 ---------------------------- */

const keys = ref<KeyRow[]>([]);
const keysLoading = ref(false);
const keysError = ref('');
/** 接口未上线（404）时的一行提示 + 写操作禁用：不自己造数据 */
const keysHint = ref('');
const keysWritable = ref(true);

async function loadKeys(): Promise<void> {
  const p = props.provider;
  if (!p) return;
  keysLoading.value = true;
  keysError.value = '';
  try {
    /* 后端实际回 `{items: [...], unassigned: [...]}`，冻结契约里写的是 `keys` —— 两种都认。 */
    const data = await api.get<{ keys?: KeyRow[]; items?: KeyRow[]; unassigned?: KeyRow[] }>(
      `/models/providers/${encodeURIComponent(p.id)}/keys`,
    );
    const list = data?.keys ?? data?.items ?? [];
    keys.value = Array.isArray(list) ? list : [];
    /* 同一响应里顺带给的「未归属」清单：有数组就直接用，省一次请求 */
    const un = data?.unassigned;
    if (Array.isArray(un)) unassigned.value = un;
    keysHint.value = '';
    keysWritable.value = true;
  } catch (err) {
    keys.value = [];
    if (errStatus(err) === 404) {
      keysWritable.value = false;
      keysHint.value = '后端的密钥接口还没上线（当前是旧版本），这一块暂时只读。';
    } else {
      keysError.value = `密钥列表读取失败：${errText(err)}`;
    }
  } finally {
    keysLoading.value = false;
  }
}

/** 密钥的展示名：后端允许 label 是空串（迁移出来的老密钥就是这样），界面不能因此留一片空白
 *  —— 列表、title、toast、确认框、409 现场统一兜底成「未命名密钥」。 */
function keyLabel(k: KeyRow | null | undefined): string {
  return String(k?.label ?? '').trim() || '未命名密钥';
}

function statusOf(k: KeyRow): { label: string; cls: string } {
  const s = String(k.status || '').toLowerCase();
  if (s === 'ok' || s === 'ready' || s === 'valid') return { label: '可用', cls: 'ok' };
  if (s === 'invalid' || s === 'error' || s === 'failed') return { label: '失效', cls: 'danger' };
  if (s === 'missing') return { label: '缺值', cls: 'warn' };
  return { label: '未测试', cls: '' };
}

/** 引用这条密钥的模型（后端 `provider_key_id` / `key_source.key_id` 两种字段都认）。 */
function refsOf(keyId: string): ModelConfig[] {
  return (state.models || []).filter((m: any) => String(m?.provider_key_id || m?.key_source?.key_id || '') === keyId);
}

/** 引用的一句话说法：列表正文与 title 共用，避免两处措辞走散。 */
function refsText(k: KeyRow): string {
  const refs = refsOf(k.id);
  if (!refs.length) return '还没有模型引用它';
  return `被 ${refs.length} 个模型引用：${refs.map((r) => r.name || r.id).join('、')}`;
}

/** `last_tested_at` 是 ISO 串；原样铺在行里会撑宽，统一压成 MM-DD HH:mm（解析不了就不显示）。 */
function timeText(iso?: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* 新增密钥：只写不读，保存后立刻清空输入 */
const newKey = reactive({ label: '', api_key: '', is_default: false });
const newKeyError = ref('');
const adding = ref(false);

function resetNewKey(): void {
  newKey.label = '';
  newKey.api_key = '';
  newKey.is_default = false;
  newKeyError.value = '';
}

async function addKey(): Promise<void> {
  const p = props.provider;
  if (!p || !keysWritable.value) return;
  const label = newKey.label.trim();
  if (!label) { newKeyError.value = '给这条密钥起个名字（比如「主账号」）'; return; }
  if (p.auth !== 'none' && !newKey.api_key.trim()) {
    newKeyError.value = '这家需要密钥，API Key 不能为空';
    return;
  }
  adding.value = true;
  newKeyError.value = '';
  try {
    await api.post(`/models/providers/${encodeURIComponent(p.id)}/keys`, {
      label,
      api_key: newKey.api_key.trim() || undefined,
      is_default: newKey.is_default || undefined,
    });
    resetNewKey();
    toast('密钥已保存（界面只显示掩码）', 'ok');
    await loadKeys();
    emit('changed');
  } catch (err) {
    newKeyError.value = `保存失败：${errText(err)}`;
  } finally {
    adding.value = false;
  }
}

/* 就地改名 */
const renamingId = ref('');
const renameText = ref('');

function startRename(k: KeyRow): void {
  renamingId.value = k.id;
  renameText.value = k.label || '';
}

function cancelRename(): void {
  renamingId.value = '';
}

async function commitRename(k: KeyRow): Promise<void> {
  if (renamingId.value !== k.id) return;
  const next = renameText.value.trim();
  renamingId.value = '';
  if (!next || next === k.label) return;
  busy.value = `rename:${k.id}`;
  try {
    await api.put(`/models/keys/${encodeURIComponent(k.id)}`, { label: next });
    toast('已改名', 'ok', 2400);
    await loadKeys();
    emit('changed');
  } catch (err) {
    reportError(err, '改名失败');
  } finally {
    busy.value = '';
  }
}

async function setDefaultKey(k: KeyRow): Promise<void> {
  busy.value = `default:${k.id}`;
  try {
    await api.put(`/models/keys/${encodeURIComponent(k.id)}`, { is_default: true });
    toast(`已把「${keyLabel(k)}」设为默认密钥`, 'ok');
    await loadKeys();
    emit('changed');
  } catch (err) {
    reportError(err, '设为默认失败');
  } finally {
    busy.value = '';
  }
}

/** 每条密钥的最近一次测试结果（只放在这一块里，不落库也不外传） */
const testMsg = ref<Record<string, ProviderTestResult>>({});

async function testKey(k: KeyRow): Promise<void> {
  busy.value = `test-key:${k.id}`;
  try {
    const res = await api.post<ProviderTestResult>(`/models/keys/${encodeURIComponent(k.id)}/test`);
    testMsg.value = { ...testMsg.value, [k.id]: res ?? { ok: false, message: '后端没有返回结果' } };
    toast(
      res?.ok ? '密钥可用' : `密钥不可用：${res?.message || '未知原因'}`,
      res?.ok ? 'ok' : 'error',
      res?.ok ? 3600 : 8000,
    );
    await loadKeys();
    emit('changed');
  } catch (err) {
    if (errStatus(err) === 404) testMsg.value = { ...testMsg.value, [k.id]: { ok: false, message: '测试接口还没上线' } };
    else reportError(err, '测试密钥失败');
  } finally {
    busy.value = '';
  }
}

/** 行内「更多操作」：原来一排三个按钮，收进溢出菜单行高才压得下来。 */
function keyMenuItems(k: KeyRow): { key: string; label: string; icon?: string; danger?: boolean; disabled?: boolean }[] {
  return [
    {
      key: 'test', label: rowBusy(`test-key:${k.id}`) ? '测试中…' : '测试',
      icon: 'bolt', disabled: rowBusy(`test-key:${k.id}`),
    },
    {
      key: 'default', label: '设为默认', icon: 'check_circle',
      disabled: k.is_default || rowBusy(`default:${k.id}`),
    },
    { key: 'rename', label: '重命名', icon: 'edit' },
    {
      key: 'delete', label: '删除', icon: 'trash', danger: true,
      disabled: rowBusy(`del-key:${k.id}`),
    },
  ];
}

function onKeyMenu(k: KeyRow, action: string): void {
  if (action === 'test') void testKey(k);
  else if (action === 'default') void setDefaultKey(k);
  else if (action === 'rename') startRename(k);
  else if (action === 'delete') void removeKey(k);
}

/** 删除被拒绝（409）时的现场：后端消息原样 + 一条"改成跟随默认密钥"的一键动作 */
const block = ref<{ keyId: string; keyLabel: string; message: string; models: ModelConfig[] } | null>(null);

async function removeKey(k: KeyRow): Promise<void> {
  const refs = refsOf(k.id);
  if (!(await askConfirm({
    title: '删除密钥',
    target: keyLabel(k),
    body: refs.length
      ? `还被 ${refs.length} 个模型引用，需先解除引用`
      : '删除后不可恢复',
    danger: true,
    confirmLabel: '删除',
  }))) return;
  busy.value = `del-key:${k.id}`;
  try {
    await api.del(`/models/keys/${encodeURIComponent(k.id)}`);
    toast('密钥已删除', 'ok');
    block.value = null;
    await loadKeys();
    emit('changed');
  } catch (err) {
    if (errStatus(err) === 409) {
      /* 后端的 409 消息里带引用数量与模型名：**原样**显示，不另编一句把人劝回去 */
      block.value = { keyId: k.id, keyLabel: keyLabel(k), message: errText(err), models: refs };
    } else {
      reportError(err, '删除密钥失败');
    }
  } finally {
    busy.value = '';
  }
}

/** 一键把引用改成「跟随提供方默认密钥」：逐条 PUT {key_mode:'auto'}，不静默换到别的密钥 */
async function detachRefs(): Promise<void> {
  const b = block.value;
  if (!b) return;
  busy.value = 'detach';
  try {
    const models = b.models.length ? b.models : refsOf(b.keyId);
    if (!models.length) {
      toast('没有找到引用它的模型（可能已被别处改掉）', 'warn');
      return;
    }
    for (const m of models) {
      await api.put(`/models/${m.id}`, { key_mode: 'auto' });
    }
    toast(`已把 ${models.length} 个模型改成跟随提供方默认密钥，可以再点删除`, 'ok', 5600);
    block.value = null;
    await loadModels();
    await loadKeys();
    emit('changed');
  } catch (err) {
    reportError(err, '改成跟随默认密钥失败');
  } finally {
    busy.value = '';
  }
}

/* ---------------------------- 未归属密钥 ---------------------------- */

const unassigned = ref<KeyRow[]>([]);
const unassignedError = ref('');
const unassignedHint = ref('');

async function loadUnassigned(): Promise<void> {
  try {
    /* 同上：`GET /models/keys?unassigned=1` 回的是 `{items: [...], unassigned: <计数>}`。
       ⚠️ 这里的 `unassigned` 是**数字**，绝不能当数组渲染（`Array.isArray` 挡住）。 */
    const data = await api.get<{ keys?: KeyRow[]; items?: KeyRow[]; unassigned?: unknown }>(
      '/models/keys', { unassigned: 1 },
    );
    const list = data?.keys ?? data?.items ?? (Array.isArray(data?.unassigned) ? data.unassigned : []);
    unassigned.value = Array.isArray(list) ? list : [];
    unassignedError.value = '';
    unassignedHint.value = '';
  } catch (err) {
    unassigned.value = [];
    if (errStatus(err) === 404) unassignedHint.value = '未归属密钥接口还没上线（当前是旧版本）。';
    else unassignedError.value = `未归属密钥读取失败：${errText(err)}`;
  }
}

async function claim(k: KeyRow): Promise<void> {
  const p = props.provider;
  if (!p) return;
  busy.value = `claim:${k.id}`;
  try {
    await api.post(`/models/providers/${encodeURIComponent(p.id)}/keys/${encodeURIComponent(k.id)}/assign`);
    toast('已认领到本提供方', 'ok');
    await Promise.all([loadKeys(), loadUnassigned()]);
    emit('changed');
  } catch (err) {
    reportError(err, '认领未归属密钥失败');
  } finally {
    busy.value = '';
  }
}

/* ---------------------------- 关联模型 / 连接测试 / 删除提供方 ---------------------------- */

const relatedModels = computed<ModelConfig[]>(() => {
  const id = String(props.provider?.id || '').toLowerCase();
  if (!id) return [];
  return (state.models || []).filter((m) => String(m.provider || '').toLowerCase() === id);
});

function keySourceOf(m: ModelConfig): string {
  const ks = (m as any).key_source;
  if (!ks || typeof ks !== 'object') return '密钥来源：—';
  if (ks.mode === 'provider') return '密钥来源：提供方默认';
  if (ks.mode === 'key') return `密钥来源：独立指定${ks.key_label ? `（${ks.key_label}）` : ''}`;
  if (ks.mode === 'none') return '密钥来源：无需密钥';
  if (ks.mode === 'legacy') return '密钥来源：旧版密钥文件';
  return '密钥来源：未配置密钥';
}

/** 关联模型行的状态点：缺密钥是硬错误，其次是后端连通性结果。 */
function modelDot(m: ModelConfig): string {
  if (m.has_secret === false && !m.is_mock) return 'danger';
  return m.status === 'ok' ? 'ok' : '';
}

function modelIcon(m: ModelConfig): string {
  return vendorColorIcon(m.provider);
}

/* ---- 同一提供方 + 同一自定义名称 → 合成一行 ----
 * 用户口径：「同一个厂商、同一个名称的模型能不能合并，而不是显示多个同名模型」。
 * 后端本来就允许同名不同用（同一底层模型既挂 generation 又挂 vision），
 * 于是抽屉里会出现两行一模一样的名字 —— 那不是在展示配置，是在制造重复。 */

/** 类型中文名：与 ModelPanel 的 TYPE_LABEL 同一套叫法，别自创第二套。 */
const REL_TYPE_LABEL: Record<string, string> = {
  generation: '文本生成',
  agent: '论文问答',
  embedding: '向量化',
  reranker: '重排',
  vision: '视觉读页',
  ocr: 'OCR',
};

function relTypeLabel(m: ModelConfig): string {
  const t = String(m.model_type || '');
  return REL_TYPE_LABEL[t] || (t || '未知');
}

interface RelGroup {
  /** 分组键：同名共用 n:<name>，空名各自 i:<id>（不合并） */
  key: string;
  name: string;
  models: ModelConfig[];
}

const relGroups = computed<RelGroup[]>(() => {
  const groups: RelGroup[] = [];
  const byKey = new Map<string, RelGroup>();
  for (const m of relatedModels.value) {
    const name = String(m.name || '').trim();
    /* 名称为空之间没有可比性：硬合会把不相干的配置绑成一行，所以各自独立 */
    const key = name ? `n:${name}` : `i:${m.id}`;
    const hit = byKey.get(key);
    if (hit) { hit.models.push(m); continue; }
    const g: RelGroup = { key, name, models: [m] };
    byKey.set(key, g);
    groups.push(g);
  }
  return groups;
});

/** 合并行的状态点取组内**最差**：任一条缺密钥/异常就 danger，全 ok 才 ok。 */
function groupDot(g: RelGroup): string {
  const dots = g.models.map(modelDot);
  if (dots.includes('danger')) return 'danger';
  return dots.every((d) => d === 'ok') ? 'ok' : '';
}

/** 组内密钥来源一致就照旧写那句；不一致只说「多个」，别挑一条冒充全体。 */
function groupKeySource(g: RelGroup): string {
  const first = keySourceOf(g.models[0]);
  return g.models.every((m) => keySourceOf(m) === first) ? first : '密钥来源：多个';
}

/** 组内类型中文名（去重、按出现顺序）：它是这一组"用在哪"的唯一线索。 */
function groupTypes(g: RelGroup): string[] {
  const out: string[] = [];
  for (const m of g.models) {
    const t = relTypeLabel(m);
    if (!out.includes(t)) out.push(t);
  }
  return out;
}

/** 组内真实 model_name（去重）：同一字段不重复写多次，类型由标签另列。 */
function groupModelIds(g: RelGroup): string {
  const out: string[] = [];
  for (const m of g.models) {
    const id = String(m.model_name || '').trim();
    if (id && !out.includes(id)) out.push(id);
  }
  return out.join(' · ');
}

/** 多配置组就地展开（按组键记，收起再点一次） */
const relExpanded = reactive(new Set<string>());

function toggleGroup(g: RelGroup): void {
  if (relExpanded.has(g.key)) relExpanded.delete(g.key);
  else relExpanded.add(g.key);
}

/** 右侧那枚「去配置」：单配置组直接跳它那一条，多配置组是**批量配置**入口。
 *  以前多配置组点它是"展开/收起"，什么也没配置（用户抱怨的正是这个）；展开移到点标题行后，
 *  这枚按钮才真的"去配置"，而且一次去配整组。 */
function groupGo(g: RelGroup): void {
  if (g.models.length === 1) goConfigure(g.models[0]);
}

/** 批量配置这一组：models.slice() 而不是引用 —— 父级表单会拿着这份清单逐条 PUT，
 *  抽屉这边随时可能因为 refresh 重建 relGroups，别让它跟着抖。 */
function groupBatch(g: RelGroup): void {
  emit('configure-group', g.models.slice());
}

/** 标题行的键盘入口：焦点落在行内那枚按钮上时**不抢**（否则回车会同时展开和批量配置）。 */
function onGroupKey(ev: KeyboardEvent, g: RelGroup): void {
  if (ev.target !== ev.currentTarget) return;
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  ev.preventDefault();
  toggleGroup(g);
}

/** 标题栏：M 个模型（N 个配置）—— 只有一个配置时括号没有信息量，省掉。 */
const relCountText = computed(() => {
  const total = relatedModels.value.length;
  const rows = relGroups.value.length;
  return total > 1 ? `${rows} 个模型（${total} 个配置）` : `${rows} 个模型`;
});

const provTest = ref<ProviderTestResult | null>(null);
const provError = ref('');

async function testProvider(): Promise<void> {
  const p = props.provider;
  if (!p) return;
  busy.value = 'test-provider';
  try {
    const res = await api.post<ProviderTestResult>(`/models/providers/${encodeURIComponent(p.id)}/test`, {});
    provTest.value = res ?? { ok: false, message: '后端没有返回结果' };
    toast(
      res?.ok ? `连接正常${res.count !== undefined ? ` · ${res.count} 个模型` : ''}`
        : `连接失败：${res?.message || '未知原因'}`,
      res?.ok ? 'ok' : 'error',
      res?.ok ? 3600 : 8000,
    );
    emit('changed');
  } catch (err) {
    if (errStatus(err) === 404) {
      provTest.value = { ok: false, message: '后端的提供方测试接口还没上线（当前是旧版本）。' };
    } else {
      reportError(err, '测试连接失败');
    }
  } finally {
    busy.value = '';
  }
}

async function removeProvider(): Promise<void> {
  const p = props.provider;
  if (!p) return;
  const n = relatedModels.value.length;
  if (!(await askConfirm({
    title: '删除提供方',
    target: p.label || p.id,
    body: n ? `还有 ${n} 个模型挂在它上面，需先改绑定` : '已存的密钥不跟着删',
    danger: true,
    confirmLabel: '删除',
  }))) return;
  busy.value = 'del-provider';
  try {
    await api.del(`/models/providers/${encodeURIComponent(p.id)}`);
    toast('提供方已删除', 'ok');
    emit('changed');
    emit('close');
  } catch (err) {
    /* 409（还被模型引用）的消息原样显示，绝不吞掉 */
    provError.value = `删除失败：${errText(err)}`;
  } finally {
    busy.value = '';
  }
}

function goConfigure(m: ModelConfig): void {
  emit('focus-type', String(m.model_type || ''));
  emit('close');
}

/* ---------------------------- 生命周期 ---------------------------- */

function refresh(): void {
  void loadKeys();
  void loadUnassigned();
  if (!(state.models || []).length) void loadModels();
}

function resetLocal(): void {
  provError.value = '';
  provTest.value = null;
  block.value = null;
  testMsg.value = {};
  renamingId.value = '';
  resetNewKey();
}

function onKeydown(ev: KeyboardEvent): void {
  if (ev.key === 'Escape' && props.open) emit('close');
}

watch(() => props.open, (v) => {
  if (typeof window !== 'undefined') {
    if (v) window.addEventListener('keydown', onKeydown);
    else window.removeEventListener('keydown', onKeydown);
  }
  if (!v) return;
  fillConn(props.provider);
  tab.value = props.initialTab;
  resetLocal();
  refresh();
});

watch(() => props.provider?.id, () => {
  if (!props.open) return;
  fillConn(props.provider);
  resetLocal();
  refresh();
});

onBeforeUnmount(() => {
  if (typeof window !== 'undefined') window.removeEventListener('keydown', onKeydown);
});

const ico = computed(() => vendorColorIcon(props.provider?.id));
const statusText = computed(() => {
  const s = String(props.provider?.status || 'unknown');
  if (s === 'ok') return '连接正常';
  if (s === 'error') return '连接异常';
  return '未测试';
});
const statusCls = computed(() => (props.provider?.status === 'ok' ? 'ok'
  : (props.provider?.status === 'error' ? 'danger' : '')));
</script>
<template>
  <Teleport to="body">
    <div v-if="open && provider" class="pd-mask" @click.self="emit('close')">
      <aside class="pd" role="dialog" aria-modal="true" :aria-label="'提供方：' + (provider.label || provider.id)">
        <header class="pd-head">
          <img v-if="ico" class="pd-logo" :src="ico" alt="">
          <span v-else class="pd-logo-fallback"><AppIcon name="model" :size="15" /></span>
          <div class="pd-head-text">
            <div class="pd-title truncate" :title="provider.label || provider.id">{{ provider.label || provider.id }}</div>
          </div>
          <span class="badge dim">{{ provider.kind === 'custom' ? '自定义' : '官方' }}</span>
          <span class="pd-dot" :class="statusCls" />
          <span class="pd-state">{{ statusText }}</span>
          <span class="spacer"></span>
          <button type="button" class="btn ghost icon sm" title="关闭" aria-label="关闭" @click="emit('close')">
            <AppIcon name="close" :size="15" />
          </button>
        </header>

        <div class="pd-tabs" role="tablist">
          <button
            type="button" role="tab" class="pd-tab" :class="{ on: tab === 'connect' }"
            :aria-selected="tab === 'connect'" @click="tab = 'connect'"
          >连接配置</button>
          <button
            type="button" role="tab" class="pd-tab" :class="{ on: tab === 'keys' }"
            :aria-selected="tab === 'keys'" @click="tab = 'keys'"
          >密钥<span class="pd-tab-n">{{ keys.length }}</span></button>
          <button
            type="button" role="tab" class="pd-tab" :class="{ on: tab === 'models' }"
            :aria-selected="tab === 'models'" @click="tab = 'models'"
          >关联模型<span class="pd-tab-n">{{ relatedModels.length }}</span></button>
        </div>

        <!-- 唯一的滚动容器 -->
        <div class="pd-body">
          <!-- ① 连接配置 -->
          <section class="pd-sec" v-if="tab === 'connect'">
            <div class="sec">
              <div class="sec-head">
                <h3>连接配置</h3>
                <span class="spacer"></span>
                <button type="button" class="link-btn" :disabled="rowBusy('test-provider')" @click="testProvider">
                  {{ rowBusy('test-provider') ? '测试中…' : '测试连接' }}
                </button>
              </div>
              <div class="conn-fields">
                <label class="field">名称
                  <input v-model="conn.label" placeholder="例如：我的 DeepSeek 账号" />
                </label>
                <label class="field">Base URL
                  <input v-model="conn.base_url" class="mono" spellcheck="false" placeholder="https://api.example.com/v1" />
                </label>
                <label class="field">认证方式
                  <select v-model="conn.auth">
                    <option value="bearer">Bearer（需要 API Key）</option>
                    <option value="none">无需密钥（本机推理 / 公开端点）</option>
                  </select>
                </label>
                <label class="field">备注
                  <input v-model="conn.note" placeholder="一句话说明这家是干什么的" />
                </label>
              </div>
              <p class="pd-err" v-if="connError">{{ connError }}</p>
              <div class="pd-result" v-if="provTest">
                <span class="badge" :class="provTest.ok ? 'ok' : 'danger'">
                  <AppIcon :name="provTest.ok ? 'check_circle' : 'error'" :size="12" />
                  {{ provTest.ok ? '连接正常' : '连接失败' }}
                </span>
                <span class="tiny faint" v-if="provTest.count !== undefined">{{ provTest.count }} 个模型</span>
                <span class="pd-result-msg" :class="{ bad: !provTest.ok }">{{ provTest.message }}</span>
              </div>
              <div class="sec-foot">
                <button class="btn sm" :disabled="rowBusy('save') || !connChanged" @click="fillConn(provider)">撤销</button>
                <button
                  class="btn primary sm" :disabled="rowBusy('save') || !connChanged"
                  data-provider-save @click="saveConn"
                >
                  <AppIcon name="check" :size="14" />{{ rowBusy('save') ? '保存中…' : '保存' }}
                </button>
              </div>
            </div>

            <div class="sec">
              <div class="sec-head"><h3>危险操作</h3></div>
              <div class="danger-row">
                <div class="dz-text">
                  <b>删除这家提供方</b>
                  <span class="tiny faint">还有模型挂在它上面时后端会拒绝删除，并把原因告诉你。</span>
                </div>
                <button class="btn sm danger" :disabled="rowBusy('del-provider')" @click="removeProvider">
                  <AppIcon name="trash" :size="14" />删除
                </button>
              </div>
              <p class="pd-err" v-if="provError">{{ provError }}</p>
            </div>
          </section>

          <!-- ② 密钥 -->
          <section class="pd-sec" v-else-if="tab === 'keys'">
            <p class="pd-hint" v-if="keysHint">{{ keysHint }}</p>
            <p class="pd-err" v-if="keysError">{{ keysError }}</p>

            <div class="pd-skel" v-if="keysLoading">
              <span class="sk sk-line" v-for="i in 3" :key="i" />
            </div>

            <template v-else>
              <div class="sec">
                <div class="sec-head">
                  <h3>密钥</h3>
                  <span class="spacer"></span>
                  <span class="tiny faint">{{ keys.length }} 条</span>
                </div>

                <ul class="key-list" v-if="keys.length">
                  <li class="key-row" v-for="k in keys" :key="k.id" :data-key-id="k.id">
                    <div class="kr-lines">
                      <div class="kr-top">
                        <input
                          v-if="renamingId === k.id" class="kr-rename" v-model="renameText" spellcheck="false"
                          @keydown.enter.prevent="commitRename(k)" @keydown.esc.stop="cancelRename"
                          @blur="commitRename(k)"
                        >
                        <button
                          v-else type="button" class="kr-name truncate"
                          :title="keyLabel(k) + '（点名字可改名）'" @click="startRename(k)"
                        >{{ keyLabel(k) }}</button>
                        <span
                          class="badge accent kr-def"
                          v-if="k.is_default || k.id === provider.default_key_id"
                        >默认</span>
                        <span class="spacer"></span>
                        <span class="kr-masked mono truncate">{{ k.masked || '（无掩码）' }}</span>
                      </div>
                      <div class="kr-sub">
                        <span class="kr-refs truncate" :title="refsText(k)">{{ refsText(k) }}</span>
                        <span class="kr-status" :class="statusOf(k).cls">{{ statusOf(k).label }}</span>
                        <span
                          v-if="testMsg[k.id]" class="kr-test truncate"
                          :class="testMsg[k.id].ok ? 'ok' : 'bad'"
                          :title="testMsg[k.id].message || ''"
                        >上次测试：{{ testMsg[k.id].ok ? '可用' : '不可用' }}<template v-if="testMsg[k.id].message"> · {{ testMsg[k.id].message }}</template></span>
                        <span v-if="timeText(k.last_tested_at)" class="kr-time">最近测试 {{ timeText(k.last_tested_at) }}</span>
                      </div>
                    </div>
                    <!-- 不传 `align`：走默认的右下角展开、紧贴触发按钮（用户口径 2026-12-27
                         「每个更多菜单都要紧挨着触发它的按钮或者位置」）。`align="end"` 是右对齐 =
                         往左长，会压住按钮本身。 -->
                    <Menu :items="keyMenuItems(k)" label="密钥操作" @select="onKeyMenu(k, $event)" />
                  </li>
                </ul>

                <div class="pd-empty" v-else-if="keysWritable">
                  <AppIcon name="document" :size="14" />
                  <span>这家还没有密钥。新增一条后，模型就能「跟随默认密钥」或单独指定它。</span>
                </div>
              </div>

              <!-- 删除被 409 拒绝：后端消息原样 + 一键改成跟随默认密钥 -->
              <div class="sec" v-if="block">
                <div class="pd-block">
                  <div class="blk-head">
                    <AppIcon name="warning" :size="14" />「{{ block.keyLabel }}」还在被引用
                  </div>
                  <p class="blk-msg">{{ block.message }}</p>
                  <div class="blk-models" v-if="block.models.length">
                    <span class="blk-model truncate" v-for="m in block.models" :key="m.id">{{ m.name || m.id }}</span>
                  </div>
                  <div class="blk-ops">
                    <button class="btn sm" :disabled="rowBusy('detach')" data-detach-refs @click="detachRefs">
                      <AppIcon name="link" :size="13" />{{ rowBusy('detach') ? '处理中…' : '把这些模型改成跟随默认密钥' }}
                    </button>
                    <button class="btn sm ghost" @click="block = null">知道了</button>
                  </div>
                </div>
              </div>

              <!-- 新增密钥：一行输入 + 按钮（只写不读） -->
              <div class="sec">
                <div class="sec-head"><h3>新增密钥</h3></div>
                <div class="ka-row">
                  <input
                    v-model="newKey.label"
                    placeholder="名称，如 主账号 / 备用 key" :disabled="!keysWritable"
                  />
                  <input
                    v-model="newKey.api_key" type="password" autocomplete="off" spellcheck="false"
                    :placeholder="provider.auth === 'none' ? '这家不需要密钥，可留空' : 'API Key（只写不回显）'"
                    :disabled="!keysWritable"
                  />
                  <label class="ka-def tiny">
                    <input type="checkbox" v-model="newKey.is_default" :disabled="!keysWritable" />设为默认
                  </label>
                  <button class="btn primary sm" :disabled="adding || !keysWritable" data-key-add @click="addKey">
                    <AppIcon name="add" :size="14" />{{ adding ? '保存中…' : '添加' }}
                  </button>
                </div>
                <p class="pd-err" v-if="newKeyError">{{ newKeyError }}</p>
                <p class="pd-hint" v-if="provider.auth === 'none' && keysWritable">这家是「无需密钥」的提供方，密钥字段可以留空。</p>
              </div>

              <!-- 未归属密钥（迁移出来、等人确认归属） -->
              <div class="sec" v-if="unassigned.length || unassignedError">
                <div class="sec-head">
                  <h3>未归属密钥</h3>
                  <span class="spacer"></span>
                  <span class="badge warn">待人工确认</span>
                </div>
                <p class="pd-err" v-if="unassignedError">{{ unassignedError }}</p>
                <ul class="key-list">
                  <li class="key-row" v-for="k in unassigned" :key="k.id">
                    <div class="kr-lines">
                      <div class="kr-top">
                        <span class="kr-name truncate" :title="keyLabel(k)">{{ keyLabel(k) }}</span>
                        <span class="spacer"></span>
                        <span class="kr-masked mono truncate">{{ k.masked || '（无掩码）' }}</span>
                      </div>
                    </div>
                    <button class="btn sm" :disabled="rowBusy('claim:' + k.id)" @click="claim(k)">
                      <AppIcon name="check" :size="13" />{{ rowBusy('claim:' + k.id) ? '认领中…' : '分配' }}
                    </button>
                  </li>
                </ul>
              </div>
              <p class="pd-hint" v-else-if="unassignedHint">{{ unassignedHint }}</p>
            </template>
          </section>

          <!-- ③ 关联模型 -->
          <section class="pd-sec" v-else>
            <div class="sec">
              <div class="sec-head">
                <h3>关联模型</h3>
                <span class="spacer"></span>
                <span class="tiny faint">{{ relCountText }}</span>
              </div>
              <ul class="rel" v-if="relGroups.length">
                <li
                  class="rel-row"
                  :class="{ merged: g.models.length > 1 }"
                  v-for="g in relGroups"
                  :key="g.key"
                  :data-rel-group="g.name || g.models[0].id"
                  :data-rel-group-count="g.models.length > 1 ? String(g.models.length) : null"
                >
                  <!-- 单配置：与合并前**一模一样**的一行（不为了统一而改掉已有的样子） -->
                  <template v-if="g.models.length === 1">
                    <span class="pd-dot" :class="modelDot(g.models[0])" />
                    <img v-if="modelIcon(g.models[0])" class="rel-ico" :src="modelIcon(g.models[0])" alt="">
                    <span v-else class="rel-ico-fallback"><AppIcon name="model" :size="13" /></span>
                    <span class="rel-name truncate" :title="g.name || g.models[0].id">{{ g.name || '（未命名）' }}</span>
                    <span class="rel-id mono truncate" :title="g.models[0].model_name || ''">{{ g.models[0].model_name || '—' }}</span>
                    <span class="rel-src truncate" :title="keySourceOf(g.models[0])">{{ keySourceOf(g.models[0]) }}</span>
                    <span class="spacer"></span>
                    <button class="btn sm ghost" data-rel-go @click="groupGo(g)">
                      <AppIcon name="edit" :size="13" />去配置
                    </button>
                  </template>

                  <!-- 多配置合并：一行说清「同一个名字被几处用着」，展开才逐条给详情 -->
                  <template v-else>
                    <!-- 标题行 = 展开/收起的开关（鼠标点、键盘 Enter/Space 都行）：展开这件事
                         以前挤在右侧那枚按钮上，而那枚按钮该干的是"去配置"。 -->
                    <div
                      class="rel-main"
                      role="button"
                      tabindex="0"
                      :aria-expanded="relExpanded.has(g.key)"
                      :title="relExpanded.has(g.key) ? '收起这组配置' : '展开这组配置'"
                      @click="toggleGroup(g)"
                      @keydown="onGroupKey($event, g)"
                    >
                      <span class="pd-dot" :class="groupDot(g)" />
                      <img v-if="modelIcon(g.models[0])" class="rel-ico" :src="modelIcon(g.models[0])" alt="">
                      <span v-else class="rel-ico-fallback"><AppIcon name="model" :size="13" /></span>
                      <span class="rel-name truncate" :title="g.name || g.models[0].id">{{ g.name || '（未命名）' }}</span>
                      <span class="rel-count">{{ g.models.length }} 个配置</span>
                      <span class="spacer"></span>
                      <button
                        class="btn sm ghost"
                        data-rel-go
                        data-rel-batch
                        :title="'批量配置这 ' + String(g.models.length) + ' 个配置'"
                        @click.stop="groupBatch(g)"
                      >
                        <AppIcon name="edit" :size="13" />批量配置
                      </button>
                    </div>
                    <div class="rel-meta">
                      <span class="rel-types">
                        <span class="rel-type" v-for="t in groupTypes(g)" :key="t">{{ t }}</span>
                      </span>
                      <span class="rel-id mono truncate" :title="groupModelIds(g)">{{ groupModelIds(g) || '—' }}</span>
                      <span class="rel-src truncate" :title="groupKeySource(g)">{{ groupKeySource(g) }}</span>
                    </div>
                    <ul class="rel-sub" v-if="relExpanded.has(g.key)">
                      <li class="rel-sub-row" v-for="m in g.models" :key="m.id" data-rel-config>
                        <span class="pd-dot" :class="modelDot(m)" />
                        <span class="rel-type">{{ relTypeLabel(m) }}</span>
                        <span class="rel-id mono truncate" :title="m.model_name || ''">{{ m.model_name || '—' }}</span>
                        <span class="rel-src truncate" :title="keySourceOf(m)">{{ keySourceOf(m) }}</span>
                        <span class="spacer"></span>
                        <button class="btn sm ghost" data-rel-go data-rel-config-go @click="goConfigure(m)">
                          <AppIcon name="edit" :size="13" />去配置
                        </button>
                      </li>
                    </ul>
                  </template>
                </li>
              </ul>
              <div class="pd-empty" v-else>
                <AppIcon name="model" :size="14" />
                <span>这家还没有配置任何模型 —— 到任务分类卡片里点「+」加一个。</span>
              </div>
            </div>
          </section>
        </div>
      </aside>
    </div>
  </Teleport>
</template><style scoped>
/* 遮罩只压暗：模糊留给真正的浮层菜单，抽屉本身不该"玻璃化" */
.pd-mask {
  position: fixed; inset: 0; z-index: 180;
  display: flex; justify-content: flex-end;
  background: var(--smoke);
}
.pd {
  display: flex; flex-direction: column;
  /* 用 100% 而不是 100vw：vw 含滚动条宽度，窄屏下有滚动条时会比可视区宽出来 */
  width: min(520px, 100%);
  max-width: 100%;
  height: 100dvh;
  background: var(--bg-elev);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow-2);
}

/* ---- 头部 ---- */
.pd-head {
  flex: none; display: flex; align-items: center; gap: 8px;
  height: 56px; padding: 0 20px;
  border-bottom: 1px solid var(--divider);
}
.pd-logo { flex: none; width: 24px; height: 24px; object-fit: contain; border-radius: var(--r-xs); }
.pd-logo-fallback {
  flex: none; display: inline-grid; place-items: center;
  width: 24px; height: 24px; border-radius: var(--r-xs);
  background: var(--bg-sunken); color: var(--text-faint);
}
.pd-head-text { min-width: 0; display: flex; }
.pd-title { min-width: 0; font-size: calc(15px * var(--ts, 1)); font-weight: 650; line-height: 1.3; }
.pd-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--text-faint); }
.pd-dot.ok { background: var(--ok); }
.pd-dot.danger { background: var(--danger); }
.pd-state { flex: none; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }

/* ---- tabs：下划线式，没有底色块 ---- */
.pd-tabs {
  flex: none; display: flex; align-items: flex-end; gap: 16px;
  padding: 0 20px; border-bottom: 1px solid var(--divider);
  overflow-x: auto; overflow-y: hidden; scrollbar-width: none;
}
.pd-tabs::-webkit-scrollbar { display: none; }
.pd-tab {
  flex: none; display: inline-flex; align-items: center; gap: 5px;
  height: 36px; padding: 0; border: 0; border-bottom: 2px solid transparent;
  background: transparent; color: var(--text-dim);
  font-family: inherit; font-size: calc(12.5px * var(--ts, 1)); font-weight: 500; line-height: 1;
  white-space: nowrap; cursor: pointer;
  transition: color var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.pd-tab:hover { color: var(--text); }
.pd-tab.on { color: var(--accent-text); border-bottom-color: var(--accent); font-weight: 600; }
.pd-tab:focus-visible { outline: none; box-shadow: var(--ring); border-radius: var(--r-xs); }
.pd-tab-n {
  font-size: calc(11px * var(--ts, 1)); font-weight: 600;
  color: var(--text-faint); font-variant-numeric: tabular-nums;
}
.pd-tab.on .pd-tab-n { color: var(--accent-text); }

/* ---- 主体：整块只有一个滚动容器 ---- */
.pd-body {
  flex: 1 1 auto; min-height: 0;
  overflow-y: auto; overflow-x: hidden;
  padding: 16px 20px 24px;
}
.pd-sec { display: flex; flex-direction: column; gap: 20px; min-width: 0; }
.sec { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.sec + .sec { border-top: 1px solid var(--divider); padding-top: 20px; }
.sec-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.sec-head h3 {
  margin: 0; font-size: calc(12px * var(--ts, 1)); font-weight: 650;
  color: var(--text-dim); letter-spacing: .01em;
}
.link-btn {
  flex: none; padding: 0; border: 0; background: transparent;
  color: var(--accent-text); font-family: inherit;
  font-size: calc(11.5px * var(--ts, 1)); font-weight: 600; line-height: 1.6; cursor: pointer;
  transition: color var(--dur-fast) var(--ease);
}
.link-btn:hover:not(:disabled) { color: var(--accent-hover); }
.link-btn:disabled { color: var(--text-faint); cursor: default; }
.link-btn:focus-visible { outline: none; box-shadow: var(--ring); border-radius: var(--r-xs); }

.pd-hint {
  margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.55; color: var(--text-faint);
  overflow-wrap: anywhere;
}
.pd-err {
  margin: 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.55; color: var(--danger);
  overflow-wrap: anywhere;
}
.pd-skel { display: flex; flex-direction: column; gap: 8px; }
.pd-empty {
  display: flex; align-items: flex-start; gap: 8px; padding: 12px;
  border: 1px dashed var(--divider); border-radius: var(--r-sm);
  color: var(--text-faint); font-size: calc(12px * var(--ts, 1)); line-height: 1.6;
}

/* ---- 连接配置 ---- */
.conn-fields { display: flex; flex-direction: column; gap: 14px; }
label.field { font-weight: 650; }
.field > input, .field > select { width: 100%; max-width: 100%; }
.pd-result { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.pd-result-msg {
  min-width: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.55;
  color: var(--text-faint); overflow-wrap: anywhere;
}
.pd-result-msg.bad { color: var(--danger); }
.sec-foot { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.danger-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.dz-text { display: flex; flex-direction: column; gap: 2px; flex: 1 1 180px; min-width: 0; }
.dz-text b { font-size: calc(12.5px * var(--ts, 1)); font-weight: 650; }

/* ---- 密钥行：行间只用 1px 分隔线，不套卡片 ---- */
.key-list { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
.key-row { display: flex; align-items: center; gap: 10px; min-width: 0; padding: 7px 0; }
.key-row + .key-row { border-top: 1px solid var(--divider); }
.kr-lines { display: flex; flex-direction: column; gap: 3px; flex: 1 1 auto; min-width: 0; }
.kr-top { display: flex; align-items: center; gap: 6px; min-width: 0; }
.kr-name {
  flex: 0 1 auto; min-width: 0; max-width: 62%; text-align: left;
  padding: 0; border: 0; background: transparent; color: var(--text);
  font-family: inherit; font-size: calc(13px * var(--ts, 1)); font-weight: 650; line-height: 1.45;
  cursor: text;
  transition: color var(--dur-fast) var(--ease);
}
button.kr-name:hover { color: var(--accent-text); }
.kr-name:focus-visible { outline: none; box-shadow: var(--ring); border-radius: var(--r-xs); }
.kr-rename {
  flex: 0 1 auto; min-width: 0; max-width: 62%; height: 26px;
  font-size: calc(12.5px * var(--ts, 1)); font-weight: 650;
}
.key-row .kr-def {
  flex: none; height: 17px; padding: 0 6px;
  font-size: calc(10.5px * var(--ts, 1)); font-weight: 600;
}
.kr-masked {
  flex: 0 1 auto; min-width: 0; max-width: 46%;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-faint);
}
.kr-sub {
  display: flex; align-items: center; gap: 6px; min-width: 0; overflow: hidden;
  white-space: nowrap; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5;
  color: var(--text-faint);
}
.kr-sub > span { flex: none; min-width: 0; }
.kr-sub > span + span::before { content: '·'; margin-right: 6px; color: var(--text-faint); }
.kr-sub > .kr-refs { flex: 0 1 auto; }
.kr-status.ok { color: var(--ok); }
.kr-status.warn { color: var(--warn); }
.kr-status.danger { color: var(--danger); }
.kr-test.ok { color: var(--ok); }
.kr-test.bad { color: var(--danger); }
.kr-time { font-variant-numeric: tabular-nums; }

/* ---- 409 现场 ---- */
.pd-block {
  display: flex; flex-direction: column; gap: 8px;
  padding: 12px; border-radius: var(--r-sm); background: var(--warn-soft);
}
.blk-head {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: calc(12.5px * var(--ts, 1)); font-weight: 650; color: var(--warn);
}
.blk-msg {
  margin: 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.6; color: var(--text);
  overflow-wrap: anywhere;
}
.blk-models { display: flex; flex-wrap: wrap; gap: 6px; min-width: 0; }
.blk-model {
  max-width: 100%; padding: 1px 8px; border-radius: var(--r-pill);
  background: var(--bg-elev); color: var(--text-dim);
  font-size: calc(11.5px * var(--ts, 1));
}
.blk-ops { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

/* ---- 新增密钥：一行输入 + 按钮 ---- */
.ka-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.ka-row > input:not([type="checkbox"]) { flex: 1 1 130px; min-width: 0; }
.ka-def {
  flex: none; display: inline-flex; align-items: center; gap: 5px;
  color: var(--text-dim); cursor: pointer;
}

/* ---- 关联模型 ----
   淡色圆角底只垫**列表内容**：「关联模型」标题（h3 + 右侧计数）留在卡外、透明底上；
   行本身无描边无底色，行间只有 1px 发丝线。 */
.rel {
  display: flex; flex-direction: column; margin: 0; padding: 3px 10px;
  list-style: none;
  background: var(--bg-sunken); border-radius: var(--r-sm);
}
.rel-row {
  display: flex; align-items: center; gap: 8px; min-width: 0;
  height: 40px; padding: 0 6px; border-radius: var(--r-sm);
  transition: background var(--dur-fast) var(--ease);
}
.rel-row + .rel-row { border-top: 1px solid var(--divider); }
.rel-row:hover { background: var(--bg-hover); }

/* ---- 合并行（同名多配置）：两行 —— 上面是"谁/几个/去哪"，下面是"什么类型/哪个真模型/密钥从哪来"。
   一条横排塞不下这些（520px 抽屉里必然挤到一起），所以合并行明确分两行，类型标签永远不换行。 */
.rel-row.merged { display: block; height: auto; padding: 5px 6px; }
.rel-main { display: flex; align-items: center; gap: 8px; min-width: 0; height: 28px; }
/* 合并行上面只有"名称 + 几个配置"，没有真实 id 抢地方 —— 名称可以吃满，别为了 40% 早早截断 */
.rel-row.merged .rel-main .rel-name { flex: 1 1 auto; max-width: none; }
/* 合并行的标题行本身是展开/收起开关：鼠标上是手型，键盘聚焦时给一圈 ring（和抽屉里其它控件一致） */
.rel-row.merged .rel-main { cursor: pointer; border-radius: var(--r-sm); }
.rel-row.merged .rel-main:focus-visible { outline: none; box-shadow: var(--ring); }
.rel-meta {
  display: flex; align-items: center; gap: 8px; min-width: 0;
  padding: 0 0 2px 14px; /* 14 = 点 + 间距：与上面名称左对齐 */
}
.rel-count {
  flex: none; white-space: nowrap; padding: 1px 6px; border-radius: 999px;
  background: var(--bg-elev); color: var(--text-faint);
  font-size: calc(11px * var(--ts, 1)); line-height: 1.5;
}
.rel-types { flex: none; display: inline-flex; align-items: center; gap: 4px; }
.rel-type {
  flex: none; white-space: nowrap; padding: 1px 6px; border-radius: 999px;
  background: var(--bg-elev); color: var(--text-dim);
  font-size: calc(11px * var(--ts, 1)); line-height: 1.5;
}
.rel-sub {
  display: flex; flex-direction: column; margin: 3px 0 0; padding: 0; list-style: none;
  border-top: 1px dashed var(--divider);
}
.rel-sub-row {
  display: flex; align-items: center; gap: 8px; min-width: 0;
  height: 32px; padding: 0 0 0 14px; border-radius: var(--r-sm);
}
.rel-sub-row:hover { background: var(--bg-hover); }
.rel-ico { flex: none; width: 16px; height: 16px; object-fit: contain; border-radius: var(--r-xs); }
.rel-ico-fallback {
  flex: none; display: inline-grid; place-items: center;
  width: 16px; height: 16px; border-radius: var(--r-xs);
  background: var(--bg-elev); color: var(--text-faint);
}
.rel-name {
  flex: 0 1 auto; min-width: 0; max-width: 40%;
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 600;
}
.rel-id, .rel-src {
  flex: 0 1 auto; min-width: 0;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}
.rel-src { max-width: 42%; }

/* ---- 窄屏：抽屉铺满，头部压一档；操作菜单始终在行内可达 ---- */
@media (max-width: 560px) {
  .pd { width: 100%; border-left: 0; }
  .pd-head { height: 52px; padding: 0 16px; }
  .pd-tabs { padding: 0 16px; gap: 14px; }
  .pd-body { padding: 14px 16px 20px; }
  .sec + .sec { padding-top: 16px; }
  .kr-name, .kr-rename { max-width: 52%; }
  .rel-src { max-width: 34%; }
}
</style>
