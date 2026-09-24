<script setup lang="ts">
/**
 * 模型管理。
 *
 * 页面只围绕 `model_type`（generation / embedding / reranker / vision / ocr）组织，
 * 模型名由用户输入或预设带出，默认路由由后端 `types_summary()` 提供。
 * 密钥只写入 `data/secrets/` 文件，界面只列文件名，不回显内容。
 */

import { computed, onMounted, reactive, ref } from 'vue';
import { api } from '@/api/client';
import type { ModelConfig, ModelPreset, ModelTypeDefault } from '@/api/types';
import {
  loadModelTypes, loadModels, reportError, state, toast, withLoading,
} from '@/stores/app';
import { fmtDate, fmtRelative, roundMs, truncate } from '@/utils/format';
import Modal from '@/components/Modal.vue';
import Menu from '@/components/Menu.vue';
import AppIcon from '@/components/AppIcon.vue';

/** 模型类型中文标签（类型是产品概念，不是硬编码的模型名）。 */
const TYPE_LABEL: Record<string, string> = {
  generation: '文本生成',
  embedding: '向量化',
  reranker: '重排',
  vision: '视觉读页',
  ocr: 'OCR',
};

/** 类型卡的一行：类型 + 该类型当前默认模型（可能未配置）。 */
interface TypeRow {
  type: string;
  entry: ModelTypeDefault | null;
}

const activeType = ref('');            // '' = 全部
const filterType = ref('');            // 与 activeType 同步，供表单默认值复用
const busyKey = ref('');               // 行级 / 操作级忙碌标记
const typeRows = ref<TypeRow[]>([]);   // 各类型当前默认（界面按存在与否渲染）
const loadError = ref('');             // 列表 / 类型默认加载失败的可见提示
const secretForm = reactive({ name: '', value: '' });
const testResults = ref<Record<string, any>>({});   // model_id -> 最近一次测试结果

/* ------------------------------ 表单状态 ------------------------------ */

/** 同 Menu 组件的 items 结构（结构类型，无需导出）。 */
interface RowAction {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
}

/** 行内只留「测试」；设为默认 / 编辑 / 删除收进「⋯」菜单。 */
function rowMenu(m: ModelConfig): RowAction[] {
  return [
    {
      key: 'default',
      label: '设为默认',
      icon: 'check_circle',
      disabled: !!m.is_default || rowBusy('def:' + m.id),
    },
    { key: 'edit', label: '编辑配置', icon: 'edit' },
    { key: 'delete', label: '删除模型', icon: 'trash', danger: true, disabled: rowBusy('del:' + m.id) },
  ];
}

function onRowAction(m: ModelConfig, key: string) {
  if (key === 'default') { setDefault(m); return; }
  if (key === 'edit') { openEdit(m); return; }
  if (key === 'delete') removeModel(m);
}

const formOpen = ref(false);
const formMode = ref<'create' | 'edit'>('create');
const formTarget = ref<ModelConfig | null>(null);
const form = reactive({
  name: '', model_type: 'generation', provider: '', model_name: '',
  base_url: '', secret_ref: '', parametersText: '{}', enabled: true,
});

/* ------------------------------ 工具函数 ------------------------------ */

function typeLabel(t?: string | null): string {
  const key = String(t ?? '');
  return TYPE_LABEL[key] || key || '未知类型';
}

/** 类型下拉选项：优先用后端 /models/types 返回的类型，回退到内置类型表。 */
const allTypes = computed<string[]>(() => {
  const norm = new Set(typeRows.value.map((r) => r.type));
  if (norm.size) return [...norm];
  const apiTypes = state.modelTypes?.types;
  const fromApi = Array.isArray(apiTypes) ? apiTypes : [];
  if (fromApi.length) return [...new Set(fromApi)];
  return Object.keys(TYPE_LABEL);
});

/**
 * /models/types 的返回结构按实际形态兼容：
 * 既可能是 `{types:[...], defaults:{...}}`，也可能是直接 `{generation:{...}}`。
 * 后端是动态 JSON，边界处按 any 处理，再收敛成强类型 TypeRow[]。
 */
function normalizeTypeRows(payload: any): TypeRow[] {
  const raw: Record<string, any> = payload && typeof payload === 'object' ? payload : {};
  const defaults = (raw.defaults && typeof raw.defaults === 'object') ? raw.defaults : null;
  const declared: string[] | null = Array.isArray(raw.types) ? raw.types : null;
  const keys: string[] = declared?.length ? declared : Object.keys(defaults || raw);
  return keys
    .filter((k) => k !== 'types' && k !== 'defaults')
    .map((k) => {
      const item = defaults ? defaults[k] : raw[k];
      return {
        type: k,
        entry: (item && typeof item === 'object') ? item as ModelTypeDefault : null,
      };
    });
}

function defaultFor(type: string): ModelTypeDefault | null {
  const row = typeRows.value.find((r) => r.type === type);
  return row ? row.entry : null;
}

/** 类型卡上的完整记录（用于补 base_url 一类只在 /models 返回的字段）。 */
function modelFull(entry: ModelTypeDefault | null): ModelConfig | null {
  if (!entry) return null;
  return (state.models || []).find((m) => m.id === entry.id) || null;
}

function typeSummary(t: string): string {
  if (t === 'generation') return '翻译 / 问答 / 分析';
  if (t === 'embedding') return '索引与检索';
  if (t === 'reranker') return '检索结果重排';
  if (t === 'vision') return '扫描件读页';
  if (t === 'ocr') return '纯 OCR 端点';
  return '';
}

/* ------------------------------ 数据加载 ------------------------------ */

async function refresh() {
  try {
    // withLoading 内部已经 reportError，这里不再重复弹错，避免一次失败两条提示。
    await withLoading('models', async () => {
      await loadModels();
      await loadModelTypes();
    });
    typeRows.value = normalizeTypeRows(state.modelTypes);
    loadError.value = '';
  } catch (err) {
    // 页面保持可用：把失败原因固定显示在表头区，不静默
    loadError.value = `模型配置加载失败：${(err as any)?.message ?? err}`;
  }
}

async function loadSecrets() {
  try {
    const data = await api.get<{ secrets?: string[]; dir?: string }>('/models/secrets/list');
    state.secrets = data?.secrets || [];
    state.secretsDir = data?.dir || '';
  } catch (err) {
    reportError(err, '读取密钥列表失败');
  }
}

async function loadPresets() {
  try {
    const data = await api.get<{ presets?: ModelPreset[] }>('/models/presets');
    state.presets = Array.isArray(data?.presets) ? data.presets : [];
  } catch (err) {
    reportError(err, '读取预设失败');
  }
}

onMounted(() => {
  refresh();
  loadPresets();
  loadSecrets();
});

/** 顶部「刷新」：把模型列表、类型默认、预设一起重新拉一遍。 */
async function refreshAll() {
  await refresh();
  await loadPresets();
}

/* ------------------------------ 列表与筛选 ------------------------------ */

const filtered = computed<ModelConfig[]>(() => {
  const all = state.models || [];
  return activeType.value ? all.filter((m) => m.model_type === activeType.value) : all;
});

function setType(t: string) {
  activeType.value = t;
  filterType.value = t;
}

function rowBusy(key: string): boolean {
  return busyKey.value === key;
}

function statusInfo(m: ModelConfig): { label: string; cls: string } {
  const s = String(m?.status || '').toLowerCase();
  if (s === 'ok' || s === 'ready' || s === 'healthy') return { label: '可用', cls: 'ok' };
  if (s === 'error' || s === 'failed' || s === 'bad') return { label: '异常', cls: 'danger' };
  if (s === 'unknown' || s === '') return { label: '未测试', cls: '' };
  return { label: String(m.status), cls: '' };
}

function providerCell(m: ModelConfig): string {
  const p = m?.provider || '—';
  return m?.is_mock ? `${p}（占位）` : p;
}

function latencyOf(m: ModelConfig): string {
  const r = testResults.value[m.id];
  // 后端动态 JSON：旧接口可能在模型记录上直接回 latency_ms，这里按 any 兜底读
  const ms = r?.latency_ms ?? r?.latency ?? (m as any)?.latency_ms;
  return ms === undefined || ms === null ? '—' : `${roundMs(ms)} ms`;
}

function parametersPreview(m: ModelConfig): string {
  const p = m?.parameters;
  if (!p || typeof p !== 'object') return '';
  const keys = Object.keys(p);
  if (!keys.length) return '';
  const parts = keys.map((k) => `${k}=${JSON.stringify(p[k])}`);
  return truncate(parts.join(' · '), 120);
}

/** 预设的结构由后端决定（可能带 generation / embedding / reranker / vision 字段），按动态 JSON 读。 */
function presetTypes(p: any): string {
  const out: string[] = [];
  if (p?.generation) out.push(TYPE_LABEL.generation);
  if (p?.embedding) out.push(TYPE_LABEL.embedding);
  if (p?.reranker) out.push(TYPE_LABEL.reranker);
  if (p?.vision) out.push(TYPE_LABEL.vision);
  return out.length ? out.join(' / ') : '可能无可导入项';
}

/* ------------------------------ 测试 / 默认 / 删除 ------------------------------ */

async function testModel(m: ModelConfig) {
  busyKey.value = `test:${m.id}`;
  try {
    const res = await withLoading('test', () => api.post<any>(`/models/${m.id}/test`), { silent: true });
    testResults.value = { ...testResults.value, [m.id]: res };
    const timing = res?.latency_ms === undefined || res?.latency_ms === null
      ? '' : ` · ${roundMs(res.latency_ms)} ms`;
    if (res?.ok) {
      toast(`测试通过：${m.name || m.id}${timing}`, 'ok', 4200);
    } else {
      toast(`测试失败：${m.name || m.id} · ${truncate(res?.detail || '未知原因', 160)}`, 'error', 8000);
    }
    await refresh();
  } catch (err) {
    reportError(err, '测试请求失败');
  } finally {
    busyKey.value = '';
  }
}

async function setDefault(m: ModelConfig) {
  busyKey.value = `def:${m.id}`;
  try {
    await api.put(`/models/${m.id}/default`);
    toast(`已设为「${typeLabel(m.model_type)}」默认模型`, 'ok');
    await refresh();
  } catch (err) {
    reportError(err, '设为默认失败');
  } finally {
    busyKey.value = '';
  }
}

async function removeModel(m: ModelConfig) {
  // 内置占位模型（离线兜底）删掉后**不会再自动重建** —— 这一点必须说清楚，
  // 因为以前它是"删了下次重启又回来"（用户报的"删不掉的占位模型"）。
  const tip = m.builtin
    ? `确认删除内置占位模型「${m.name || m.id}」？\n\n`
      + '它只是离线兜底（没有可用模型时才会用到）；删除后**不会**再自动重建，'
      + '需要时可以在「模型配置」卡头点「恢复内置占位」找回来。'
    : `确认删除模型配置「${m.name || m.id}」？此操作不可撤销。`;
  if (!window.confirm(tip)) return;
  busyKey.value = `del:${m.id}`;
  try {
    await api.del(`/models/${m.id}`);
    toast(m.builtin ? '已删除内置占位模型（不会再自动重建）' : '已删除模型配置', 'ok');
    await refresh();
  } catch (err) {
    reportError(err, '删除失败');
  } finally {
    busyKey.value = '';
  }
}

/** 把删过的内置占位模型找回来（清掉"已删除"记录，缺谁补谁）。 */
async function restoreBuiltin() {
  busyKey.value = 'restore-builtin';
  try {
    const res = await withLoading('restore-builtin',
      () => api.post<{ created?: number }>('/models/builtin/restore'), { silent: true });
    const n = res?.created ?? 0;
    toast(n ? `已恢复 ${n} 个内置占位模型` : '内置占位模型都在，无需恢复', 'ok', 3600);
    await refresh();
  } catch (err) {
    reportError(err, '恢复内置占位模型失败');
  } finally {
    busyKey.value = '';
  }
}

/* ------------------------------ 预设导入 ------------------------------ */

const importBaseUrl = ref('');

async function importPreset(p: ModelPreset) {
  busyKey.value = `preset:${p.id}`;
  try {
    const res = await api.post<any>('/models/import-presets', {
      provider: p.id,
      base_url: importBaseUrl.value ? importBaseUrl.value : undefined,
    });
    const created = Array.isArray(res?.created) ? res.created : [];
    const failed = created.filter((x: any) => x && x.error);
    const ok = created.length - failed.length;
    if (!created.length) {
      toast('该预设没有可导入的配置', 'warn');
    } else if (failed.length) {
      toast(`预设已导入：成功 ${ok} 项、失败 ${failed.length} 项（详见控制台）`, 'warn', 6000);
      console.warn('预设导入失败明细', failed);
    } else {
      toast(`预设已导入 ${ok} 项，请在「密钥」区放入密钥后逐个测试`, 'ok', 5600);
    }
    await refresh();
    await loadSecrets();
  } catch (err) {
    reportError(err, '导入预设失败');
  } finally {
    busyKey.value = '';
  }
}

/* ------------------------------ 密钥（不落库、不回显） ------------------------------ */

async function writeSecret() {
  const name = secretForm.name.trim();
  const value = secretForm.value;
  if (!name) { toast('请填写密钥文件名（如 provider.key）', 'warn'); return; }
  if (!value) { toast('请填写密钥内容', 'warn'); return; }
  busyKey.value = 'secret';
  try {
    const data = await withLoading('secret', () => api.post<any>('/models/secret', { name, value }), { silent: true });
    const bytes = data?.bytes;
    toast(`密钥已写入（仅本机）${bytes ? ` · ${bytes} 字节` : ''}`, 'ok');
    secretForm.name = '';
    secretForm.value = '';   // 写入后立即清空，界面不留任何密钥内容
    await loadSecrets();
  } catch (err) {
    reportError(err, '写入密钥失败');
  } finally {
    busyKey.value = '';
  }
}

/* ------------------------------ 新建 / 编辑 ------------------------------ */

function parseParameters(text: string | null | undefined): Record<string, any> {
  const s = String(text ?? '').trim();
  if (!s) return {};
  let parsed: any;
  try {
    parsed = JSON.parse(s);
  } catch (err: any) {
    throw new Error(`参数不是合法 JSON：${err.message}（示例：{"temperature": 0.3}）`);
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('参数必须是 JSON 对象，例如 {"temperature": 0.3}');
  }
  return parsed;
}

function openCreate() {
  formMode.value = 'create';
  formTarget.value = null;
  Object.assign(form, {
    name: '', model_type: activeType.value || 'generation', provider: '',
    model_name: '', base_url: '', secret_ref: '',
    parametersText: '{}', enabled: true,
  });
  formOpen.value = true;
}

function openEdit(m: ModelConfig) {
  formMode.value = 'edit';
  formTarget.value = m;
  Object.assign(form, {
    name: m.name || '',
    model_type: m.model_type || 'generation',
    provider: m.provider || '',
    model_name: m.model_name || '',
    base_url: m.base_url || '',
    secret_ref: m.secret_ref || '',
    parametersText: JSON.stringify(m.parameters || {}, null, 2),
    enabled: m.enabled !== false,
  });
  formOpen.value = true;
}

async function submitForm() {
  if (!form.name.trim()) { toast('请填写模型名称（界面显示用）', 'warn'); return; }
  let parameters: Record<string, any>;
  try {
    parameters = parseParameters(form.parametersText);
  } catch (err: any) {
    toast(err.message, 'error', 8000);
    return;
  }
  const payload: Record<string, any> = {
    name: form.name.trim(),
    model_type: form.model_type,
    provider: form.provider.trim() || 'openai_compatible',
    model_name: form.model_name.trim() || null,
    base_url: form.base_url.trim() || null,
    secret_ref: form.secret_ref.trim() || null,
    parameters,
  };
  if (formMode.value === 'edit') payload.enabled = !!form.enabled;
  busyKey.value = 'submit';
  try {
    if (formMode.value === 'edit' && formTarget.value) {
      await api.put(`/models/${formTarget.value.id}`, payload);
      toast('模型配置已更新', 'ok');
    } else {
      await api.post('/models', payload);
      toast('模型配置已创建，建议点「测试」验证连通性', 'ok', 4800);
    }
    formOpen.value = false;
    await refresh();
  } catch (err) {
    reportError(err, '保存失败');
  } finally {
    busyKey.value = '';
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>模型管理</h1>
      </div>
      <div class="grow"></div>
      <div class="page-actions">
        <button class="btn sm" @click="refreshAll" :disabled="state.loading.models">
          <AppIcon name="refresh" :size="14" />{{ state.loading.models ? '刷新中…' : '刷新' }}
        </button>
        <button class="btn primary sm" @click="openCreate">
          <AppIcon name="add" :size="14" />新建模型
        </button>
      </div>
    </div>

    <div class="load-error" v-if="loadError">{{ loadError }}</div>

    <div class="grid cols-3">
      <div class="card stat" v-for="row in typeRows" :key="row.type">
        <div class="row small">
          <b>{{ typeLabel(row.type) }}</b>
          <span class="spacer"></span>
          <span class="badge" :class="row.entry && row.entry.is_mock ? 'warn' : (row.entry ? 'ok' : '')">
            {{ row.entry ? (row.entry.is_mock ? '占位' : '已接入') : '未配置' }}
          </span>
        </div>
        <div class="k tiny">{{ typeSummary(row.type) }}</div>
        <template v-if="row.entry">
          <div class="small truncate gap-6">{{ row.entry.name || '（未命名）' }}</div>
          <div class="tiny faint truncate">
            <span class="mono">{{ row.entry.provider || '—' }}</span>
            · {{ row.entry.model_name || '—' }}
            <span v-if="row.entry.builtin"> · 内置</span>
          </div>
          <div class="tiny faint truncate" v-if="modelFull(row.entry)?.base_url">
            {{ modelFull(row.entry)?.base_url }}
          </div>
        </template>
        <div class="small muted gap-6" v-else>未配置该类型默认模型</div>
      </div>
      <div class="card stat" v-if="!typeRows.length">
        <div class="k">各类型默认</div>
        <div class="small muted">暂无数据。</div>
      </div>
    </div>

    <div class="card gap-top">
      <div class="card-head">
        <span>模型配置</span>
        <span class="badge">{{ filtered.length }} 项</span>
        <span class="spacer"></span>
        <button class="btn sm ghost" :disabled="rowBusy('restore-builtin')" @click="restoreBuiltin"
                title="把删过的内置占位模型（离线兜底）找回来：删掉它们不会再自动重建，点这里恢复">
          <AppIcon name="refresh" :size="14" />恢复内置占位
        </button>
        <div class="seg">
          <button :class="{ active: activeType === '' }" @click="setType('')">全部</button>
          <button v-for="t in allTypes" :key="t" :class="{ active: activeType === t }" @click="setType(t)">
            {{ t }}
          </button>
        </div>
      </div>
      <div class="card-body tight">
        <div class="empty" v-if="state.loading.models && !filtered.length">正在加载模型配置…</div>
        <table class="data" v-else-if="filtered.length">
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>Provider</th>
              <th>模型名</th>
              <th>状态</th>
              <th>延迟</th>
              <th>默认</th>
              <th>最后测试</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in filtered" :key="m.id">
              <td>
                <div>
                  {{ m.name || '（未命名）' }}
                  <span v-if="m.builtin" class="badge dim" style="margin-left: 6px"
                        title="内置占位模型（离线兜底）：删掉后不会再自动重建">内置</span>
                </div>
                <div class="tiny faint mono">{{ m.id }}</div>
                <div class="tiny faint truncate" v-if="m.base_url">{{ m.base_url }}</div>
                <div class="tiny faint" v-if="m.secret_ref">密钥：{{ m.secret_ref }}</div>
                <div class="tiny faint" v-else>未配置密钥</div>
                <div class="tiny faint truncate" v-if="parametersPreview(m)">{{ parametersPreview(m) }}</div>
              </td>
              <td><span class="badge accent">{{ typeLabel(m.model_type) }}</span></td>
              <td><span class="mono small">{{ providerCell(m) }}</span></td>
              <td><span class="mono small">{{ m.model_name || '—' }}</span></td>
              <td>
                <span class="badge" :class="statusInfo(m).cls">{{ statusInfo(m).label }}</span>
              </td>
              <td class="num small">{{ latencyOf(m) }}</td>
              <td>
                <span class="badge ok" v-if="m.is_default">默认</span>
                <span class="badge" v-else>—</span>
              </td>
              <td class="small">
                <div>{{ fmtDate(m.last_tested_at) }}</div>
                <div class="tiny faint" v-if="m.last_tested_at">{{ fmtRelative(m.last_tested_at) }}</div>
              </td>
              <td class="ops">
                <div class="row nowrap" style="gap: 6px">
                  <button class="btn sm" :disabled="rowBusy('test:' + m.id)" @click="testModel(m)">
                    <AppIcon name="bolt" :size="14" />{{ rowBusy('test:' + m.id) ? '测试中' : '测试' }}
                  </button>
                  <Menu :items="rowMenu(m)" up :label="`更多操作：${m.name || m.id}`"
                        @select="onRowAction(m, $event)" />
                </div>
                <div class="tiny gap-4" v-if="testResults[m.id]">
                  <template v-if="testResults[m.id].ok">
                    <span class="badge ok">测试通过</span>
                    <span class="muted" v-if="testResults[m.id].latency_ms !== undefined && testResults[m.id].latency_ms !== null">
                      延迟 {{ roundMs(testResults[m.id].latency_ms) }} ms
                    </span>
                    <span class="muted" v-if="testResults[m.id].dim">维度 {{ testResults[m.id].dim }}</span>
                    <span class="muted" v-if="testResults[m.id].device">{{ testResults[m.id].device }}</span>
                  </template>
                  <template v-else>
                    <span class="badge danger">测试失败</span>
                    <span class="muted">{{ truncate(testResults[m.id].detail || '未知原因', 160) }}</span>
                  </template>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="empty" v-else>
          <p v-if="activeType">该类型下还没有模型配置。</p>
          <p v-else>还没有任何模型配置。</p>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span>一键导入预设</span>
        <span class="spacer"></span>
        <input v-model="importBaseUrl" class="preset-input" placeholder="可选：覆盖 base_url（留空用预设默认）" />
        <button class="btn sm ghost" @click="loadPresets">
          <AppIcon name="refresh" :size="14" />刷新
        </button>
      </div>
      <div class="card-body">
        <div class="row wrap" v-if="state.presets.length">
          <button class="btn sm" v-for="p in state.presets" :key="p.id"
                  :title="'含：' + presetTypes(p)"
                  :disabled="rowBusy('preset:' + p.id)" @click="importPreset(p)">
            {{ rowBusy('preset:' + p.id) ? '导入中…' : (p.label || p.id) }}
          </button>
        </div>
        <div class="small faint" v-else>没有可用预设。</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span>密钥</span>
        <span class="badge warn">不落库</span>
        <span class="spacer"></span>
        <span class="tiny faint mono" v-if="state.secretsDir">{{ state.secretsDir }}</span>
        <button class="btn sm ghost" @click="loadSecrets">
          <AppIcon name="refresh" :size="14" />刷新
        </button>
      </div>
      <div class="card-body">
        <div class="row wrap" v-if="state.secrets.length">
          <span class="badge mono" v-for="name in state.secrets" :key="name">{{ name }}</span>
        </div>
        <div class="small faint" v-else>data/secrets/ 下还没有密钥文件。</div>

        <div class="grid cols-2 gap-top">
          <label class="field">密钥文件名（如 deepseek.key）
            <input v-model="secretForm.name" placeholder="provider.key" />
          </label>
          <label class="field">密钥内容（写入后不回显）
            <input v-model="secretForm.value" type="password" placeholder="sk-…" />
          </label>
        </div>
        <div class="row gap-10">
          <button class="btn primary sm" :disabled="rowBusy('secret')" @click="writeSecret">
            <AppIcon name="upload" :size="14" />{{ rowBusy('secret') ? '写入中…' : '写入密钥' }}
          </button>
        </div>
      </div>
    </div>

    <Modal :open="formOpen" :title="formMode === 'edit' ? '编辑模型配置' : '新建模型配置'"
           wide @close="formOpen = false">
      <div class="grid cols-2">
        <label class="field">名称（界面显示用）<input v-model="form.name" /></label>
        <label class="field">类型（默认路由按此分组）
          <select v-model="form.model_type">
            <option v-for="t in allTypes" :key="t" :value="t">{{ typeLabel(t) }}（{{ t }}）</option>
          </select>
        </label>
        <label class="field">Provider（如 deepseek / dashscope / ollama / local）
          <input v-model="form.provider" class="mono" placeholder="openai_compatible" />
        </label>
        <label class="field">模型名（对应 provider 的 model 字段）
          <input v-model="form.model_name" class="mono" placeholder="由用户 / 预设带出" />
        </label>
        <label class="field">base_url
          <input v-model="form.base_url" class="mono" placeholder="https://…/v1" />
        </label>
        <label class="field">secret_ref（密钥文件名，可留空）
          <input v-model="form.secret_ref" class="mono" placeholder="provider.key" />
        </label>
      </div>
      <label class="field gap-10">parameters（JSON 对象）
        <textarea v-model="form.parametersText" rows="6" class="mono"></textarea>
      </label>
      <label class="row small gap-10" v-if="formMode === 'edit'">
        <input type="checkbox" v-model="form.enabled" /> 启用该模型
      </label>
      <p class="small" v-if="formMode === 'edit'">
        当前模型 id：<span class="mono">{{ formTarget ? formTarget.id : '—' }}</span>
      </p>
      <template #footer>
        <button class="btn" @click="formOpen = false">取消</button>
        <button class="btn primary" :disabled="rowBusy('submit')" @click="submitForm">
          {{ rowBusy('submit') ? '保存中…' : '保存' }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* 加载失败时的可见提示（失败不静默） */
.load-error {
  margin-bottom: 12px;
  padding: 8px 12px;
  border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--border));
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 12.5px;
}

.card.gap-top { margin-top: 14px; }
.gap-4 { margin-top: 4px; }
.gap-6 { margin-top: 6px; }
.gap-10 { margin-top: 10px; }
.preset-input { width: 300px; }

/* 表格：窄列不换行（否则「未测试」「文本生成」会被拆成两行），
   整表在窄屏下横向滚动而不是挤压每个单元格。 */
.card-body.tight { overflow-x: auto; }
table.data { min-width: 940px; }
table.data td:not(:first-child), table.data th:not(:first-child) { white-space: nowrap; }
table.data td.ops { width: 132px; }
/* 名称列：id / base_url / secret_ref / 参数都是长字符串，必须能截断，
   否则整张表会被这一列撑出横向滚动条。 */
table.data td:first-child { max-width: 340px; }
table.data td:first-child > div {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>
