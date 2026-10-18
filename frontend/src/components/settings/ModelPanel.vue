<script setup lang="ts">
/**
 * 设置页「模型」面板。
 *
 * 问答模型（`model_type === "agent"`）单独排在最前：它是对话选择器的数据源，
 * 其余类型（生成 / 向量 / 重排 / 视觉 / OCR）是别处功能在用，收进折叠块，
 * 免得一屏二十行里找不着今天要换的那一个。
 * 密钥只写 `data/secrets/` 下的文件，界面只列文件名、不回显内容。
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue';
import { api } from '@/api/client';
import type { ModelConfig, ModelPreset } from '@/api/types';
import {
  loadModelTypes, loadModels, reportError, state, toast, withLoading,
} from '@/stores/app';
import { roundMs, truncate } from '@/utils/format';
import { askConfirm } from '@/stores/confirm';
import AppIcon from '@/components/AppIcon.vue';
import Menu from '@/components/Menu.vue';
import Modal from '@/components/Modal.vue';

/** 模型类型中文标签（类型是产品概念，不是硬编码的模型名）。 */
const TYPE_LABEL: Record<string, string> = {
  generation: '文本生成',
  agent: '论文问答',
  embedding: '向量化',
  reranker: '重排',
  vision: '视觉读页',
  ocr: 'OCR',
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
const otherOpen = ref(false);                       // 「其他模型」默认收起
const secretOpen = ref(false);
const presetOpen = ref(false);

const secretForm = reactive({ name: '', value: '' });
const importBaseUrl = ref('');

/* ---------------------------- 列表分组 ---------------------------- */

const agentRows = computed(() => (state.models || []).filter((m) => m.model_type === 'agent'));
const otherRows = computed(() => (state.models || []).filter((m) => m.model_type !== 'agent'));

/** 两张卡同一套行版式，只有抬头与折叠状态不同。 */
const groups = computed(() => [
  { key: 'agent', title: '问答模型', rows: agentRows.value, empty: '还没有问答模型配置，对话会回落到生成模型。' },
  { key: 'other', title: '其他模型', rows: otherRows.value, empty: '没有其它类型的模型配置。' },
]);

/** 类型下拉：优先后端 `/models/types`（新类型不用改前端），回退到内置标签表。 */
const allTypes = computed<string[]>(() => {
  const fromApi = state.modelTypes?.types;
  if (Array.isArray(fromApi) && fromApi.length) return [...new Set(fromApi)];
  return Object.keys(TYPE_LABEL);
});

function typeLabel(t?: string | null): string {
  const key = String(t ?? '');
  return TYPE_LABEL[key] || key || '未知类型';
}

function statusInfo(m: ModelConfig): { label: string; cls: string } {
  const s = String(m?.status || '').toLowerCase();
  if (s === 'ok' || s === 'ready' || s === 'healthy') return { label: '可用', cls: 'ok' };
  if (s === 'error' || s === 'failed' || s === 'bad') return { label: '异常', cls: 'danger' };
  return { label: '未测试', cls: '' };
}

function providerCell(m: ModelConfig): string {
  const p = m?.provider || '—';
  return m?.is_mock ? `${p}（占位）` : p;
}

/** 行的第四格：问答模型看推理等级，其余类型看它是什么类型（后者没有这一项）。 */
function tagOf(m: ModelConfig): { text: string; cls: string } {
  if (m.model_type !== 'agent') return { text: typeLabel(m.model_type), cls: 'accent' };
  const effort = (m.parameters || {}).reasoning_effort;
  return effort ? { text: String(effort), cls: '' } : { text: '—', cls: '' };
}

function latencyOf(m: ModelConfig): string {
  const r = testResults.value[m.id];
  // 后端动态 JSON：旧接口可能在模型记录上直接回 latency_ms，这里按 any 兜底读
  const ms = r?.latency_ms ?? r?.latency ?? (m as any)?.latency_ms;
  return ms === undefined || ms === null ? '—' : `${roundMs(ms)} ms`;
}

/* ---------------------------- 数据加载 ---------------------------- */

async function refresh() {
  try {
    // withLoading 内部已经 reportError，这里不再重复弹错，避免一次失败两条提示。
    await withLoading('models', async () => {
      await loadModels();
      await loadModelTypes();
    });
    loadError.value = '';
  } catch (err) {
    // 面板保持可用：失败原因固定显示在表头区，不静默
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
  void refresh();
  void loadPresets();
  void loadSecrets();
});

async function refreshAll() {
  await refresh();
  await loadPresets();
}

function rowBusy(key: string): boolean {
  return busyKey.value === key;
}

/* ---------------------------- 行内改名 ---------------------------- */

const renamingId = ref('');
const renameText = ref('');
let renameInput: HTMLInputElement | null = null;

function bindRename(el: any) {
  if (el) renameInput = el as HTMLInputElement;
}

async function startRename(m: ModelConfig) {
  renamingId.value = m.id;
  renameText.value = m.name || '';
  await nextTick();
  renameInput?.focus();
  renameInput?.select();
}

function cancelRename() {
  renamingId.value = '';
}

/** 回车 / 失焦保存：只提交 name 一个字段，别的一律不动（后端按字段可选处理）。 */
async function commitRename(m: ModelConfig) {
  if (renamingId.value !== m.id) return;   // Esc 已取消，卸载触发的 blur 不算保存
  const next = renameText.value.trim();
  renamingId.value = '';
  if (!next || next === (m.name || '')) return;
  busyKey.value = `name:${m.id}`;
  try {
    await api.put(`/models/${m.id}`, { name: next });
    toast('已改名', 'ok', 2400);
    await refresh();
  } catch (err) {
    reportError(err, '改名失败');
  } finally {
    busyKey.value = '';
  }
}

/* ---------------------------- 测试 / 默认 / 删除 ---------------------------- */

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
  if (key === 'default') { void setDefault(m); return; }
  if (key === 'edit') { openEdit(m); return; }
  if (key === 'delete') void removeModel(m);
}

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
  if (!(await askConfirm({
    title: `删除模型配置「${m.name || m.id}」？`,
    body: '删除后这个配置就没了，不可撤销。',
    danger: true,
    confirmLabel: '删除',
  }))) return;
  busyKey.value = `del:${m.id}`;
  try {
    await api.del(`/models/${m.id}`);
    toast('已删除模型配置', 'ok');
    await refresh();
  } catch (err) {
    reportError(err, '删除失败');
  } finally {
    busyKey.value = '';
  }
}

/* ---------------------------- 预设导入 ---------------------------- */

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
      toast(`预设已导入 ${ok} 项，放入密钥后逐个测试`, 'ok', 5600);
    }
    await refresh();
    await loadSecrets();
  } catch (err) {
    reportError(err, '导入预设失败');
  } finally {
    busyKey.value = '';
  }
}

/* ---------------------------- 密钥（不落库、不回显） ---------------------------- */

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

/* ---------------------------- 新建 / 编辑 ---------------------------- */

const formOpen = ref(false);
const formMode = ref<'create' | 'edit'>('create');
const formTarget = ref<ModelConfig | null>(null);
const form = reactive({
  name: '', model_type: 'agent', provider: '', model_name: '',
  base_url: '', secret_ref: '', parametersText: '{}', enabled: true,
});

function parseParameters(text: string | null | undefined): Record<string, any> {
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

function openCreate(type = 'agent') {
  formMode.value = 'create';
  formTarget.value = null;
  Object.assign(form, {
    name: '', model_type: type, provider: '', model_name: '',
    base_url: '', secret_ref: '', parametersText: '{}', enabled: true,
  });
  formOpen.value = true;
}

function openEdit(m: ModelConfig) {
  formMode.value = 'edit';
  formTarget.value = m;
  Object.assign(form, {
    name: m.name || '',
    model_type: m.model_type || 'agent',
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
  if (!form.name.trim()) { toast('请填写名称', 'warn'); return; }
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
  <div class="model-panel">
    <div class="sec-actions">
      <span class="cap">{{ state.models.length }} 个配置</span>
      <span class="spacer"></span>
      <button class="btn icon sm" :disabled="state.loading.models" title="重新读取模型配置"
              @click="refreshAll"><AppIcon name="refresh" :size="15" /></button>
      <button class="btn primary sm" @click="openCreate()">
        <AppIcon name="add" :size="15" />新建模型
      </button>
    </div>

    <div class="load-error" v-if="loadError">{{ loadError }}</div>

    <!-- 两张卡共用一套行版式：「其他模型」只是默认收起，不是另一套写法 -->
    <section class="card" v-for="g in groups" :key="g.key">
      <button
        v-if="g.key === 'other'"
        type="button" class="card-head fold" :aria-expanded="otherOpen"
        @click="otherOpen = !otherOpen"
      >
        <span class="hd-title"><AppIcon name="layers" :size="14" />{{ g.title }}</span>
        <span class="hd-count">{{ g.rows.length }}</span>
        <span class="spacer"></span>
        <AppIcon :name="otherOpen ? 'chevron-up' : 'chevron-down'" :size="15" />
      </button>
      <div class="card-head" v-else>
        <span class="hd-title"><AppIcon name="chat" :size="14" />{{ g.title }}</span>
        <span class="hd-count">{{ g.rows.length }}</span>
      </div>

      <!-- 空态：直接给"现在能做的事"，不留一整张空卡片 -->
      <div class="card-body" v-if="!g.rows.length && (g.key === 'agent')">
        <div class="mempty">
          <AppIcon name="chat" :size="15" />
          <span class="mempty-text">{{ g.empty }}</span>
          <button class="btn sm" @click="openCreate('agent')">
            <AppIcon name="add" :size="14" />新建
          </button>
        </div>
      </div>
      <div class="card-body tight" v-else-if="g.key === 'agent' || otherOpen">
        <div class="mlist">
          <div class="mrow" v-for="m in g.rows" :key="m.id">
            <div class="mmain">
              <input
                v-if="renamingId === m.id"
                class="rename" :ref="bindRename" v-model="renameText"
                spellcheck="false"
                @keydown.enter.prevent="commitRename(m)"
                @keydown.esc.stop="cancelRename"
                @blur="commitRename(m)"
              />
              <button
                v-else type="button" class="mname" :title="`${m.id}（点击改名）`"
                @click="startRename(m)"
              >{{ m.name || '（未命名）' }}</button>
              <div class="msub">
                <span class="mono truncate">{{ m.model_name || '—' }}</span>
                <span class="msep">·</span>
                <span class="truncate">{{ providerCell(m) }}</span>
              </div>
            </div>

            <div class="mtags">
              <span class="badge dim tiny">{{ tagOf(m).text }}</span>
              <span class="badge ok tiny" v-if="m.is_default">默认</span>
              <span class="stat" :class="statusInfo(m).cls"><i class="stat-dot" />{{ statusInfo(m).label }}</span>
            </div>

            <!-- 行操作只有 hover / 键盘聚焦时才浮现：静止时一行只留"它是谁、能不能用" -->
            <div class="m-ops">
              <button class="btn sm" :disabled="rowBusy('test:' + m.id)" @click="testModel(m)">
                <AppIcon name="bolt" :size="14" />{{ rowBusy('test:' + m.id) ? '测试中' : '测试' }}
              </button>
              <Menu
                :items="rowMenu(m)" up :label="`更多操作：${m.name || m.id}`"
                @select="onRowAction(m, $event)"
              />
            </div>

            <!-- 测试结论就地一行：通过 / 失败 + 延迟或原因 -->
            <div class="m-test tiny" v-if="testResults[m.id]">
              <template v-if="testResults[m.id].ok">
                <span class="badge ok tiny">通过</span>
                <span class="faint">延迟 {{ latencyOf(m) }}</span>
                <span class="faint" v-if="testResults[m.id].dim">维度 {{ testResults[m.id].dim }}</span>
              </template>
              <template v-else>
                <span class="badge danger tiny">失败</span>
                <span class="faint truncate">{{ truncate(testResults[m.id].detail || '未知原因', 120) }}</span>
              </template>
            </div>
          </div>

          <div class="empty tiny faint" v-if="!g.rows.length">{{ g.empty }}</div>
        </div>
      </div>
    </section>

    <section class="card">
      <button type="button" class="card-head fold" :aria-expanded="secretOpen" @click="secretOpen = !secretOpen">
        <span class="hd-title"><AppIcon name="document" :size="14" />密钥</span>
        <span class="hd-count">{{ state.secrets.length }}</span>
        <span class="spacer"></span>
        <AppIcon :name="secretOpen ? 'chevron-up' : 'chevron-down'" :size="15" />
      </button>
      <div class="card-body" v-if="secretOpen">
        <div class="secrefs" v-if="state.secrets.length">
          <span class="secref mono" v-for="name in state.secrets" :key="name">{{ name }}</span>
        </div>
        <div class="tiny faint" v-else>还没有密钥文件。</div>

        <div class="grid cols-2 gap-top">
          <label class="field">文件名<input v-model="secretForm.name" class="mono" placeholder="provider.key" /></label>
          <label class="field">内容
            <input v-model="secretForm.value" type="password" autocomplete="off" placeholder="sk-…" />
          </label>
        </div>
        <div class="row gap-top">
          <button class="btn primary sm" :disabled="rowBusy('secret')" @click="writeSecret">
            <AppIcon name="upload" :size="14" />{{ rowBusy('secret') ? '写入中…' : '写入密钥' }}
          </button>
          <span class="spacer"></span>
          <span class="tiny faint mono truncate" v-if="state.secretsDir">{{ state.secretsDir }}</span>
        </div>
      </div>
    </section>

    <section class="card">
      <button type="button" class="card-head fold" :aria-expanded="presetOpen" @click="presetOpen = !presetOpen">
        <span class="hd-title"><AppIcon name="sparkle" :size="14" />一键导入预设</span>
        <span class="hd-count">{{ state.presets.length }}</span>
        <span class="spacer"></span>
        <AppIcon :name="presetOpen ? 'chevron-up' : 'chevron-down'" :size="15" />
      </button>
      <div class="card-body" v-if="presetOpen">
        <label class="field">
          base_url
          <input v-model="importBaseUrl" class="mono" placeholder="留空 = 用预设默认" />
        </label>
        <div class="presets" v-if="state.presets.length">
          <button
            class="preset" v-for="p in state.presets" :key="p.id"
            :disabled="rowBusy('preset:' + p.id)" @click="importPreset(p)"
          >{{ rowBusy('preset:' + p.id) ? '导入中…' : (p.label || p.id) }}</button>
        </div>
        <div class="tiny faint gap-top" v-else>没有可用预设。</div>
      </div>
    </section>

    <Modal
      :open="formOpen" :title="formMode === 'edit' ? '编辑模型' : '新建模型'"
      wide @close="formOpen = false"
    >
      <div class="grid cols-2">
        <label class="field">名称<input v-model="form.name" placeholder="界面显示用" /></label>
        <label class="field">类型
          <select v-model="form.model_type">
            <option v-for="t in allTypes" :key="t" :value="t">{{ typeLabel(t) }}</option>
          </select>
        </label>
        <label class="field">提供方<input v-model="form.provider" class="mono" placeholder="deepseek / dashscope / ollama" /></label>
        <label class="field">模型名<input v-model="form.model_name" class="mono" placeholder="provider 侧的 model" /></label>
        <label class="field">base_url<input v-model="form.base_url" class="mono" placeholder="https://…/v1" /></label>
        <label class="field">密钥文件名<input v-model="form.secret_ref" class="mono" placeholder="provider.key" /></label>
      </div>
      <label class="field gap-top">parameters
        <textarea v-model="form.parametersText" rows="6" class="mono" placeholder='{"reasoning_effort": "high"}'></textarea>
      </label>
      <label class="row small gap-top" v-if="formMode === 'edit'">
        <input type="checkbox" v-model="form.enabled" /> 启用该模型
      </label>
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
.model-panel { display: flex; flex-direction: column; }
.sec-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }

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

/* 折叠块的抬头：仍是 .card-head 的位置与分割线，只是整条可点 */
.card-head.fold {
  width: 100%; border: 0; border-bottom: 1px solid var(--divider);
  border-radius: 0; background: transparent; color: var(--text);
  font: inherit; font-weight: 600; text-align: left; cursor: pointer;
}
.card-head.fold:hover { color: var(--accent-text); }

.gap-top { margin-top: 10px; }
.cap { font-size: 12px; color: var(--text-faint); letter-spacing: .01em; }

/* 抬头：标题（图标+文字）与计数分离 —— 计数是"说明"，不该跟标题抢字重 */
.hd-title { display: inline-flex; align-items: center; gap: 7px; }
.hd-count {
  min-width: 20px; padding: 0 6px; height: 18px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 999px; background: var(--bg-sunken);
  color: var(--text-faint); font-size: 11px; font-weight: 600;
  font-variant-numeric: tabular-nums;
}

/* 行：左边「它是谁」、右边「能不能用」，操作收进 hover。
   两段式而不是五列：名称长短不一时五列会把右边的徽章推得忽左忽右。 */
.mlist { display: flex; flex-direction: column; }
.mrow {
  display: grid; align-items: center; gap: 4px 12px;
  grid-template-columns: minmax(0, 1fr) auto auto;
  padding: 8px 14px;
  border-bottom: 1px solid var(--divider);
  transition: background var(--dur-fast) var(--ease);
}
.mrow:last-child { border-bottom: 0; }
.mrow:hover { background: var(--bg-hover); }

.mmain { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
/* 点名字就地改名：非编辑态是**文字按钮**（cursor: text 而不是 pointer：它就是"点进输入框"的手感） */
.mname {
  padding: 0; border: 0; background: transparent; color: var(--text);
  font: inherit; font-size: 13px; font-weight: 600; text-align: left; cursor: text;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mname:hover { color: var(--accent-text); }
.rename { width: 100%; font-size: 13px; font-weight: 600; }
.msub {
  display: flex; align-items: center; gap: 5px; min-width: 0;
  font-size: 11.5px; color: var(--text-faint);
}
.msep { opacity: .55; }

.mtags { display: flex; align-items: center; gap: 7px; white-space: nowrap; }
.m-ops { display: flex; align-items: center; gap: 4px; opacity: 0; transition: opacity var(--dur-fast) var(--ease); }
.mrow:hover .m-ops, .mrow:focus-within .m-ops { opacity: 1; }
/* 触摸设备没有 hover：常显，否则「测试 / ⋯」永远点不到 */
@media (hover: none) { .m-ops { opacity: 1; } }

/* 状态用"点 + 字"而不是彩底徽章：一行里三枚彩块会打架，只有一个点该有颜色 */
.stat {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11.5px; color: var(--text-faint);
}
.stat-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-faint); }
.stat.ok { color: var(--text-dim); }
.stat.ok .stat-dot { background: var(--ok); }
.stat.danger { color: var(--danger); }
.stat.danger .stat-dot { background: var(--danger); }

.m-test { grid-column: 1 / -1; display: flex; align-items: center; gap: 6px; min-width: 0; }

/* 空态：一行说明 + 一个"现在就能做"的按钮，不占一整张白卡 */
.mempty {
  display: flex; align-items: center; gap: 8px;
  padding: 2px 0; color: var(--text-faint); font-size: 12.5px;
}
.mempty-text { flex: 1; min-width: 0; }

/* 密钥文件：等宽小签，一眼看出"有没有、叫什么"，内容永远不回显 */
.secrefs { display: flex; flex-wrap: wrap; gap: 6px; }
.secref {
  padding: 2px 8px; border-radius: 999px;
  background: var(--bg-sunken); color: var(--text-dim); font-size: 11.5px;
}

/* 预设：一排胶囊 chips（原先是一排 .btn，7 个就糊成按钮墙） */
.presets { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.preset {
  height: 28px; padding: 0 11px;
  border: 1px solid var(--divider); border-radius: 999px;
  background: transparent; color: var(--text-dim);
  font: inherit; font-size: 12px; cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.preset:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--divider));
  background: var(--bg-hover); color: var(--text);
}
.preset:disabled { opacity: .45; cursor: default; }
</style>
