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
 */
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import type { ModelConfig } from '@/api/types';
import { loadModels, reportError, state, toast } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import AppIcon from '@/components/AppIcon.vue';
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
    const data = await api.get<{ keys?: KeyRow[] }>(`/models/providers/${encodeURIComponent(p.id)}/keys`);
    keys.value = Array.isArray(data?.keys) ? data.keys : [];
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

/** 这家有没有默认密钥：以后端算的 `default_key_id` 为准，回退到列表里的 `is_default`。 */
const defaultKeyLabel = computed(() => {
  const p = props.provider;
  if (p?.default_key_label) return p.default_key_label;
  const k = keys.value.find((x) => x.id === p?.default_key_id) ?? keys.value.find((x) => x.is_default);
  return k?.label ?? '';
});

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

/** 删除被拒绝（409）时的现场：后端消息原样 + 一条"改成跟随默认密钥"的一键动作 */
const block = ref<{ keyId: string; keyLabel: string; message: string; models: ModelConfig[] } | null>(null);

async function removeKey(k: KeyRow): Promise<void> {
  const refs = refsOf(k.id);
  if (!(await askConfirm({
    title: `删除密钥「${keyLabel(k)}」？`,
    body: refs.length
      ? `被 ${refs.length} 个模型引用：${refs.map((r) => r.name || r.id).join('、')}。被引用时后端会拒绝删除。`
      : '删除后这条密钥就没了，不可撤销。',
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
    const data = await api.get<{ keys?: KeyRow[] }>('/models/keys', { unassigned: 1 });
    unassigned.value = Array.isArray(data?.keys) ? data.keys : [];
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
    title: `删除提供方「${p.label || p.id}」？`,
    body: n
      ? `还有 ${n} 个模型挂在这家，后端会拒绝删除并告诉你还剩几个。`
      : '删除后这条提供方记录就没了（已存的密钥不跟着删）。',
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
      <aside class="pd" role="dialog" aria-modal="true" :aria-label="`提供方：${provider.label || provider.id}`">
        <header class="pd-head">
          <img v-if="ico" class="pv-ico lg" :src="ico" alt="">
          <span v-else class="pv-fallback lg"><AppIcon name="model" :size="16" /></span>
          <div class="pd-head-text">
            <div class="pd-title truncate" :title="provider.label || provider.id">{{ provider.label || provider.id }}</div>
            <div class="pd-sub">
              <span class="badge tiny dim">{{ provider.kind === 'custom' ? '自定义' : '官方' }}</span>
              <span class="stat-dot" :class="statusCls" />
              <span class="tiny faint">{{ statusText }}</span>
            </div>
          </div>
          <span class="spacer"></span>
          <button type="button" class="pd-x" title="关闭" @click="emit('close')">
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
          >密钥<span class="hd-count">{{ keys.length }}</span></button>
          <button
            type="button" role="tab" class="pd-tab" :class="{ on: tab === 'models' }"
            :aria-selected="tab === 'models'" @click="tab = 'models'"
          >关联模型<span class="hd-count">{{ relatedModels.length }}</span></button>
        </div>

        <div class="pd-body">
          <!-- ① 连接配置 -->
          <section class="pd-sec" v-if="tab === 'connect'">
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
            <p class="pd-err" v-if="connError">{{ connError }}</p>
            <div class="row">
              <button class="btn primary sm" :disabled="rowBusy('save') || !connChanged" data-provider-save @click="saveConn">
                <AppIcon name="check" :size="14" />{{ rowBusy('save') ? '保存中…' : '保存' }}
              </button>
              <button class="btn sm" :disabled="rowBusy('save') || !connChanged" @click="fillConn(provider)">
                撤销
              </button>
              <span class="spacer"></span>
              <button class="btn sm" :disabled="rowBusy('test-provider')" @click="testProvider">
                <AppIcon name="bolt" :size="14" />{{ rowBusy('test-provider') ? '测试中…' : '测试连接' }}
              </button>
            </div>
            <div class="pd-result" v-if="provTest">
              <span class="badge" :class="provTest.ok ? 'ok' : 'danger'">
                <AppIcon :name="provTest.ok ? 'check_circle' : 'error'" :size="12" />
                {{ provTest.ok ? '连接正常' : '连接失败' }}
              </span>
              <span class="tiny faint" v-if="provTest.count !== undefined">{{ provTest.count }} 个模型</span>
              <span class="tiny" :class="provTest.ok ? 'faint' : 'bad'">{{ provTest.message }}</span>
            </div>
            <div class="pd-danger-zone">
              <div class="dz-text">
                <b>删除这家提供方</b>
                <span class="tiny faint">还有模型挂在它上面时后端会拒绝删除，并把原因告诉你。</span>
              </div>
              <button class="btn sm danger" :disabled="rowBusy('del-provider')" @click="removeProvider">
                <AppIcon name="trash" :size="14" />删除
              </button>
            </div>
            <p class="pd-err" v-if="provError">{{ provError }}</p>
          </section>

          <!-- ② 密钥 -->
          <section class="pd-sec" v-else-if="tab === 'keys'">
            <p class="pd-hint" v-if="keysHint">{{ keysHint }}</p>
            <p class="pd-err" v-if="keysError">{{ keysError }}</p>

            <div class="pd-skel" v-if="keysLoading">
              <span class="sk sk-line" v-for="i in 3" :key="i" />
            </div>

            <div class="key-list" v-else-if="keys.length">
              <div class="key-row" v-for="k in keys" :key="k.id">
                <div class="key-top">
                  <span class="key-name" v-if="renamingId !== k.id" :title="`${keyLabel(k)}（点名字可改名）`">
                    <button type="button" class="name-btn truncate" @click="startRename(k)">{{ keyLabel(k) }}</button>
                  </span>
                  <input
                    v-else class="key-rename" v-model="renameText" spellcheck="false"
                    @keydown.enter.prevent="commitRename(k)" @keydown.esc.stop="cancelRename"
                    @blur="commitRename(k)"
                  >
                  <span class="key-flag" v-if="k.is_default || k.id === provider.default_key_id">默认</span>
                  <span class="key-status" :class="statusOf(k).cls">{{ statusOf(k).label }}</span>
                  <span class="spacer"></span>
                  <span class="key-masked mono">{{ k.masked || '（无掩码）' }}</span>
                </div>
                <div class="key-refs tiny faint" :title="refsOf(k.id).map((r) => r.name || r.id).join('、')">
                  <template v-if="refsOf(k.id).length">
                    被 {{ refsOf(k.id).length }} 个模型引用：{{ refsOf(k.id).map((r) => r.name || r.id).join('、') }}
                  </template>
                  <template v-else>还没有模型引用它</template>
                </div>
                <div class="key-ops">
                  <button class="btn sm" :disabled="rowBusy(`test-key:${k.id}`)" @click="testKey(k)">
                    <AppIcon name="bolt" :size="13" />{{ rowBusy(`test-key:${k.id}`) ? '测试中…' : '测试' }}
                  </button>
                  <button
                    class="btn sm" :disabled="k.is_default || rowBusy(`default:${k.id}`)"
                    :title="k.is_default ? '已经是默认密钥' : '设为这家提供方的默认密钥'"
                    @click="setDefaultKey(k)"
                  >
                    <AppIcon name="check_circle" :size="13" />设为默认
                  </button>
                  <button class="btn sm danger" :disabled="rowBusy(`del-key:${k.id}`)" @click="removeKey(k)">
                    <AppIcon name="trash" :size="13" />删除
                  </button>
                  <span class="tiny faint" v-if="testMsg[k.id]">
                    {{ testMsg[k.id].ok ? '上次测试：可用' : '上次测试：不可用' }}
                    <template v-if="testMsg[k.id].message"> · {{ testMsg[k.id].message }}</template>
                  </span>
                </div>
              </div>
            </div>

            <div class="pd-empty" v-else-if="!keysLoading && keysWritable">
              <AppIcon name="document" :size="14" />
              <span>这家还没有密钥。新增一条后，模型就能「跟随默认密钥」或单独指定它。</span>
            </div>

            <!-- 删除被 409 拒绝：后端消息原样 + 一键改成跟随默认密钥 -->
            <div class="pd-block" v-if="block">
              <div class="blk-title">
                <AppIcon name="warning" :size="14" />「{{ block.keyLabel }}」还在被引用
              </div>
              <p class="blk-msg">{{ block.message }}</p>
              <div class="row">
                <button class="btn sm" :disabled="rowBusy('detach')" data-detach-refs @click="detachRefs">
                  <AppIcon name="link" :size="13" />{{ rowBusy('detach') ? '处理中…' : '把这些模型改成跟随默认密钥' }}
                </button>
                <button class="btn sm ghost" @click="block = null">知道了</button>
              </div>
            </div>

            <!-- 新增密钥：只写不读 -->
            <div class="key-add">
              <div class="ka-title">新增密钥</div>
              <div class="ka-grid">
                <label class="field">名称
                  <input v-model="newKey.label" placeholder="主账号 / 备用 key" :disabled="!keysWritable" />
                </label>
                <label class="field">API Key
                  <input
                    v-model="newKey.api_key" type="password" autocomplete="off" spellcheck="false"
                    :placeholder="provider.auth === 'none' ? '这家不需要密钥，可留空' : '只写不回显，保存后只显示掩码'"
                    :disabled="!keysWritable"
                  />
                </label>
              </div>
              <div class="row">
                <label class="check-line tiny">
                  <input type="checkbox" v-model="newKey.is_default" :disabled="!keysWritable" />
                  设为默认密钥
                </label>
                <span class="spacer"></span>
                <button class="btn primary sm" :disabled="adding || !keysWritable" data-key-add @click="addKey">
                  <AppIcon name="add" :size="14" />{{ adding ? '保存中…' : '添加密钥' }}
                </button>
              </div>
              <p class="pd-err" v-if="newKeyError">{{ newKeyError }}</p>
              <p class="pd-hint" v-if="provider.auth === 'none' && keysWritable">这家是「无需密钥」的提供方，密钥字段可以留空。</p>
            </div>

            <!-- 未归属密钥（迁移出来、等人确认归属） -->
            <div class="unassigned" v-if="unassigned.length || unassignedError">
              <div class="ka-title">未归属密钥 <span class="badge warn tiny">待人工确认</span></div>
              <p class="pd-err" v-if="unassignedError">{{ unassignedError }}</p>
              <div class="ua-row" v-for="k in unassigned" :key="k.id">
                <span class="ua-name truncate" :title="keyLabel(k)">{{ keyLabel(k) }}</span>
                <span class="ua-masked mono tiny">{{ k.masked || '（无掩码）' }}</span>
                <span class="spacer"></span>
                <button class="btn sm" :disabled="rowBusy(`claim:${k.id}`)" @click="claim(k)">
                  <AppIcon name="check" :size="13" />{{ rowBusy(`claim:${k.id}`) ? '认领中…' : '认领到本提供方' }}
                </button>
              </div>
            </div>
            <p class="pd-hint" v-else-if="unassignedHint">{{ unassignedHint }}</p>
          </section>

          <!-- ③ 关联模型 -->
          <section class="pd-sec" v-else>
            <div class="rel" v-if="relatedModels.length">
              <div class="rel-row" v-for="m in relatedModels" :key="m.id">
                <span class="stat-dot" :class="m.has_secret === false && !m.is_mock ? 'danger' : (m.status === 'ok' ? 'ok' : '')" />
                <div class="rel-main">
                  <span class="rel-name truncate" :title="m.name || m.id">{{ m.name || '（未命名）' }}</span>
                  <span class="rel-sub tiny faint truncate" :title="m.model_name || ''">
                    <span class="mono">{{ m.model_name || '—' }}</span> · {{ keySourceOf(m) }}
                  </span>
                </div>
                <button class="btn sm" @click="goConfigure(m)">
                  <AppIcon name="edit" :size="13" />去配置
                </button>
              </div>
            </div>
            <div class="pd-empty" v-else>
              <AppIcon name="model" :size="14" />
              <span>这家还没有配置任何模型 —— 到任务分类卡片里点「+」加一个。</span>
            </div>
          </section>
        </div>
      </aside>
    </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩 + 右侧抽屉：360px 宽时抽屉铺满可视宽度（不溢出、不横向滚） */
.pd-mask {
  position: fixed; inset: 0; z-index: 180;
  display: flex; justify-content: flex-end;
  background: color-mix(in srgb, var(--bg-sunken) 40%, rgba(12, 16, 24, .45));
  backdrop-filter: blur(3px);
  -webkit-backdrop-filter: blur(3px);
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
.pd-head {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 14px; border-bottom: 1px solid var(--divider);
}
.pd-head-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.pd-title { font-size: calc(14px * var(--ts, 1)); font-weight: 650; }
.pd-sub { display: flex; align-items: center; gap: 6px; }
.pd-x {
  flex: none; display: inline-grid; place-items: center; width: 26px; height: 26px;
  padding: 0; border: 0; border-radius: var(--radius-sm);
  background: transparent; color: var(--text-faint); cursor: pointer;
}
.pd-x:hover { background: var(--bg-hover); color: var(--text); }

.pd-tabs {
  display: flex; gap: 4px; padding: 8px 14px 0; border-bottom: 1px solid var(--divider);
  min-width: 0; overflow-x: auto; scrollbar-width: none;
}
.pd-tabs::-webkit-scrollbar { display: none; }
.pd-tab {
  display: inline-flex; align-items: center; gap: 5px; flex: none;
  padding: 6px 10px; border: 0; border-bottom: 2px solid transparent; border-radius: 0;
  background: transparent; color: var(--text-dim);
  font: inherit; font-size: calc(12.5px * var(--ts, 1)); cursor: pointer;
}
.pd-tab:hover { color: var(--text); }
.pd-tab.on { color: var(--accent-text); border-bottom-color: var(--accent); font-weight: 600; }
.pd-tab:focus-visible { outline: none; box-shadow: var(--ring); }

.pd-body { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 12px 14px 20px; }
.pd-sec { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.pd-hint {
  margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.55; color: var(--text-faint);
  overflow-wrap: anywhere;
}
.pd-err {
  margin: 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.55; color: var(--danger);
  overflow-wrap: anywhere;
}
.pd-result { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pd-result .bad { color: var(--danger); }
.pd-skel { display: flex; flex-direction: column; gap: 8px; }
.pd-empty {
  display: flex; align-items: flex-start; gap: 8px; padding: 12px;
  border: 1px dashed var(--divider); border-radius: var(--radius-sm);
  color: var(--text-faint); font-size: calc(12px * var(--ts, 1)); line-height: 1.6;
}
.hd-count {
  min-width: 18px; padding: 0 5px; height: 16px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 999px; background: var(--bg-sunken); color: var(--text-faint);
  font-size: calc(10px * var(--ts, 1)); font-weight: 600; font-variant-numeric: tabular-nums;
}
.stat-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--text-faint); }
.stat-dot.ok { background: var(--ok); }
.stat-dot.danger { background: var(--danger); }

/* 密钥行：名称 + 默认/状态 + 掩码，第二行引用，第三行动作（窄屏自动折行） */
.key-list { display: flex; flex-direction: column; gap: 8px; }
.key-row {
  display: flex; flex-direction: column; gap: 5px;
  padding: 9px 10px; border: 1px solid var(--divider); border-radius: var(--radius-sm);
  background: var(--bg-layer); min-width: 0;
}
.key-top { display: flex; align-items: center; gap: 6px; min-width: 0; flex-wrap: wrap; }
.key-name { min-width: 0; display: inline-flex; }
.name-btn {
  padding: 0; border: 0; background: transparent; color: var(--text);
  font: inherit; font-size: calc(12.5px * var(--ts, 1)); font-weight: 600;
  cursor: text; min-width: 0; max-width: min(180px, 100%);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.name-btn:hover { color: var(--accent-text); }
.key-rename { height: 24px; font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; max-width: 180px; }
.key-flag {
  flex: none; padding: 0 5px; height: 16px; display: inline-flex; align-items: center;
  border-radius: 999px; background: var(--accent-soft); color: var(--accent-text);
  font-size: calc(10px * var(--ts, 1)); font-weight: 600;
}
.key-status { flex: none; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
.key-status.ok { color: var(--ok); }
.key-status.warn { color: var(--warn); }
.key-status.danger { color: var(--danger); }
.key-masked {
  flex: 0 1 auto; min-width: 0; max-width: 100%;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.key-refs {
  overflow: hidden; line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.key-ops { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }

/* 409 现场 */
.pd-block {
  display: flex; flex-direction: column; gap: 7px;
  padding: 10px; border-radius: var(--radius-sm);
  border: 1px solid color-mix(in srgb, var(--warn) 45%, var(--divider));
  background: color-mix(in srgb, var(--warn) 10%, transparent);
}
.blk-title { display: inline-flex; align-items: center; gap: 6px; font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; color: var(--warn); }
.blk-msg {
  margin: 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.6; color: var(--text);
  word-break: break-word; overflow-wrap: anywhere;
}

/* 新增密钥 */
.key-add {
  display: flex; flex-direction: column; gap: 8px;
  padding: 10px; border: 1px solid var(--divider); border-radius: var(--radius-sm);
  background: var(--bg-sunken);
}
.ka-title { display: flex; align-items: center; gap: 6px; font-size: calc(12px * var(--ts, 1)); font-weight: 600; color: var(--text-dim); }
.ka-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.check-line { display: inline-flex; align-items: center; gap: 6px; color: var(--text-dim); }

/* 未归属密钥 */
.unassigned {
  display: flex; flex-direction: column; gap: 7px;
  padding: 10px; border: 1px solid var(--divider); border-radius: var(--radius-sm);
}
.ua-row { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.ua-name { min-width: 0; max-width: 160px; font-size: calc(12px * var(--ts, 1)); font-weight: 600; }
.ua-masked {
  color: var(--text-dim); min-width: 0; max-width: 100%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 关联模型 */
.rel { display: flex; flex-direction: column; gap: 2px; }
.rel-row {
  display: flex; align-items: center; gap: 8px; min-width: 0;
  padding: 7px 6px; border-radius: var(--radius-sm);
}
.rel-row:hover { background: var(--bg-hover); }
.rel-main { display: flex; flex-direction: column; gap: 1px; flex: 1 1 auto; min-width: 0; }
.rel-name { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; min-width: 0; }
.rel-sub { min-width: 0; overflow: hidden; }
.rel-sub .mono { min-width: 0; }

/* 危险区 */
.pd-danger-zone {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-top: 4px; padding-top: 10px; border-top: 1px solid var(--divider);
}
.dz-text { display: flex; flex-direction: column; gap: 1px; flex: 1 1 160px; min-width: 0; }
.dz-text b { font-size: calc(12.5px * var(--ts, 1)); }

.pv-ico { flex: none; object-fit: contain; border-radius: 4px; }
.pv-ico.lg { width: 30px; height: 30px; }
.pv-fallback {
  flex: none; display: inline-grid; place-items: center;
  border-radius: 4px; background: var(--bg-sunken); color: var(--text-faint);
}
.pv-fallback.lg { width: 30px; height: 30px; }

@media (max-width: 420px) {
  .pd { width: 100%; border-left: 0; }
  .pd-head { padding: 10px 12px; }
  .pd-body { padding: 10px 12px 16px; }
}
</style>
