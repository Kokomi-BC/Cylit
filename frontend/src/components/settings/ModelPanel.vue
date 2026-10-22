<script setup lang="ts">
/**
 * 设置页「模型」面板。
 *
 * 自上而下三块：**消耗统计**（UsageChart）→ **模型列表** → 密钥 / 一键导入（默认收起）。
 * 列表里问答模型（`model_type === "agent"`）排最前：它是对话选择器的数据源，
 * 其余类型是别处功能在用，收进折叠块，免得一屏二十行里找不着今天要换的那一个。
 * 新建 / 编辑走 ModelForm 的两步式表单；密钥只写 `data/secrets/` 下的文件，界面只列文件名。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
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
import ModelForm from '@/components/settings/ModelForm.vue';
import UsageChart from '@/components/settings/UsageChart.vue';

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
const secretOpen = ref(false);
const presetOpen = ref(false);
const highlightId = ref('');                         // 刚建好的那一行，短暂提亮

const secretForm = reactive({ name: '', value: '' });
const importBaseUrl = ref('');

const usageRef = ref<InstanceType<typeof UsageChart> | null>(null);

/* ---------------------------- 按类型分组 ----------------------------
 * 用户口径 2026-12-07：列表改成**一种类型一张卡** —— 以前只有「问答模型 / 其他模型」两张，
 * 后者还把向量化、重排、视觉、OCR 混在一张折叠卡里，既看不出在用什么、也没地方往这一类里加。
 * 现在每张卡的抬头就是「类型图标 + 名字 + 当前在用的那一个 + 一枚加号」，行内是名称与配置。 */

/** 类型 → 图标（新类型没登记就用 model） */
const TYPE_ICON: Record<string, string> = {
  generation: 'sparkle',   // 文本生成：翻译 / 解读这些写作任务
  agent: 'chat',           // 论文问答
  embedding: 'layers',     // 向量化：一层层向量
  reranker: 'list-check',  // 重排：按相关度重排名次
  vision: 'image',         // 视觉读页
  ocr: 'document',         // OCR：从扫描件里读字
};
/** 卡片顺序：问答放第二（最常换），生成第一（别处功能都靠它兜底），其余按离正文的远近排 */
const TYPE_ORDER = ['generation', 'agent', 'embedding', 'reranker', 'vision', 'ocr'];

function hostOf(url?: string | null): string {
  if (!url) return '';
  try { return new URL(url).host; } catch { return String(url); }
}

interface TypeGroup {
  key: string; title: string; icon: string;
  rows: ModelConfig[];
  /** 这一类**当前在用**的那个（后端 `is_default`，每种类型各一个） */
  current: ModelConfig | null;
  empty: string;
}

const typeGroups = computed<TypeGroup[]>(() => {
  const known = allTypes.value;
  const order = [...TYPE_ORDER.filter((t) => known.includes(t)), ...known.filter((t) => !TYPE_ORDER.includes(t))];
  return order.map((t) => {
    const rows = (state.models || []).filter((m) => m.model_type === t);
    return {
      key: t,
      title: typeLabel(t),
      icon: TYPE_ICON[t] || 'model',
      rows,
      current: rows.find((m) => m.is_default) || null,
      empty: `还没有${typeLabel(t)}模型`,
    };
  });
});

/* ---------------------------- 按类型添加（抬头那枚 +） ----------------------------
 * 用户口径：每个类型的右上角一枚加号，点开**先选**、再进表单。
 * 候选就是这套预设里**带这一类档位**的那几个（`presets[i][type]`）—— 选中即把 provider /
 * base_url / 密钥文件名 / 参数一起填好，表单直接开在第二步；没有合适的预设就走「手动新建」。 */
const addOpen = ref(false);
const addType = ref('agent');

interface AddCandidate { id: string; label: string; model: string; host: string }

const addCandidates = computed<AddCandidate[]>(() => {
  const t = addType.value;
  const out: AddCandidate[] = [];
  for (const pre of state.presets || []) {
    const spec = (pre as any)[t];
    if (!spec || typeof spec !== 'object') continue;
    out.push({
      id: pre.id,
      label: pre.label || pre.id,
      model: String(spec.model_name || ''),
      host: hostOf(spec.base_url) || pre.provider || '',
    });
  }
  return out;
});

function openAdd(type: string) {
  addType.value = type;
  addOpen.value = true;
}

/** 选中一个预设：关掉选择器，表单带着预设直接开在第二步 */
function addFromPreset(id: string) {
  addOpen.value = false;
  openCreate(addType.value, id);
}

function addManual() {
  addOpen.value = false;
  openCreate(addType.value);
}

/** 类型胶囊：优先后端 `/models/types`（新类型不用改前端），回退到内置标签表。 */
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
  const p = m.parameters || {};
  const effort = p.reasoning_effort;
  const count = Array.isArray(p.reasoning_levels) ? p.reasoning_levels.length : 0;
  // 没配档位就是老样子（只有默认档，连默认档都没有就是「—」）
  if (!count) return effort ? { text: String(effort), cls: '' } : { text: '—', cls: '' };
  // 配了档位就把"默认档 + 几档"一起报出来：一眼看得出这个模型的思考深度有几档可选
  return { text: effort ? `默认 ${effort} · ${count} 档` : `${count} 档`, cls: '' };
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
  usageRef.value?.reload();
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
    /* 列很窄，行内只剩「名称 + 默认 + 状态」；测试挪到这里（一列放不下第二枚按钮） */
    { key: 'test', label: '测试连通性', icon: 'bolt', disabled: rowBusy('test:' + m.id) },
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
  if (key === 'test') { void testModel(m); return; }
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
    usageRef.value?.reload();
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

/* ---------------------------- 新建 / 编辑（统一走 ModelForm） ---------------------------- */

const formOpen = ref(false);
const formMode = ref<'create' | 'edit'>('create');
const formTarget = ref<ModelConfig | null>(null);
const formDefaultType = ref('agent');
/** 从「+」里选中了哪个预设：表单据此直接开在第二步并填好（空 = 从零填） */
const formPreset = ref('');
let highlightTimer: ReturnType<typeof setTimeout> | null = null;

function openCreate(type = 'agent', preset = '') {
  formMode.value = 'create';
  formTarget.value = null;
  formDefaultType.value = type;
  formPreset.value = preset;
  formOpen.value = true;
}

function openEdit(m: ModelConfig) {
  formMode.value = 'edit';
  formTarget.value = m;
  formDefaultType.value = m.model_type || 'agent';
  formPreset.value = '';
  formOpen.value = true;
}

function flash(id: string) {
  highlightId.value = id;
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = setTimeout(() => {
    if (highlightId.value === id) highlightId.value = '';
    highlightTimer = null;
  }, 2800);
}

async function submitForm(payload: Record<string, any>) {
  busyKey.value = 'submit';
  try {
    if (formMode.value === 'edit' && formTarget.value) {
      await api.put(`/models/${formTarget.value.id}`, payload);
      toast('模型配置已更新', 'ok');
      formOpen.value = false;
      await refresh();
      flash(formTarget.value.id);
    } else {
      const created = await api.post<any>('/models', payload);
      formOpen.value = false;
      await refresh();
      const id = String(created?.id || created?.model?.id || payload.name || '');
      if (id) flash(id);
      toast('已创建，建议点「测试」验证连通性', 'ok', 4800);
    }
  } catch (err) {
    reportError(err, '保存失败');
  } finally {
    busyKey.value = '';
  }
}

onBeforeUnmount(() => {
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = null;
});
</script>

<template>
  <div class="model-panel grid cards">
    <p class="panel-note">加模型、测连通、看用量，都在这一块。</p>

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

    <UsageChart ref="usageRef" />

    <!-- 六个类型并成一排、每类一列（用户口径 2026-12-07 二轮）：抬头是「图标 + 名字 + 个数 + 加号」，
         下面先报**当前在用的那个**，再列这一类里的模型。列只有 ~180px，所以行内只留
         「状态点 + 名称 + 默认」，其余动作全收进 ⋯（测试也在里面）。 -->
    <div class="types-row">
      <section class="type-col" v-for="g in typeGroups" :key="g.key">
        <div class="tc-head">
          <span class="tc-title"><AppIcon :name="g.icon" :size="13" />{{ g.title }}</span>
          <span class="hd-count">{{ g.rows.length }}</span>
          <span class="spacer"></span>
          <button
            type="button" class="card-add"
            :title="`添加${g.title}模型`" :aria-label="`添加${g.title}模型`"
            :data-add-type="g.key"
            @click="openAdd(g.key)"
          >
            <AppIcon name="add" :size="14" />
          </button>
        </div>

        <!-- 当前使用的那个：一行胶囊，名字长了省略号（列宽固定，不能让它把列撑开） -->
        <div
          class="tc-cur" v-if="g.current"
          :title="`当前使用的${g.title}模型：${g.current.name || g.current.id}`"
        >
          <AppIcon name="check" :size="11" />
          <span class="truncate">{{ g.current.name || g.current.model_name || g.current.id }}</span>
        </div>
        <div class="tc-cur none" v-else>未设默认</div>

        <div class="tc-list">
          <div class="tc-item" v-for="m in g.rows" :key="m.id" :class="{ fresh: highlightId === m.id }">
            <div class="tc-line">
              <span class="stat-dot" :class="statusInfo(m).cls" :title="statusInfo(m).label" />
              <input
                v-if="renamingId === m.id"
                class="rename" :ref="bindRename" v-model="renameText"
                spellcheck="false"
                @keydown.enter.prevent="commitRename(m)"
                @keydown.esc.stop="cancelRename"
                @blur="commitRename(m)"
              />
              <button
                v-else type="button" class="tc-name truncate" :title="`${m.id}（点击改名）`"
                @click="startRename(m)"
              >{{ m.name || '（未命名）' }}</button>
              <!-- 「默认」不写字：上面那枚胶囊已经报了「当前用的就是它」，
                   这里只用一枚对勾示意 —— 狭列里 30px 的徽章会把模型名挤成三个字 -->
              <span class="tc-default" v-if="m.is_default" title="当前使用">
                <AppIcon name="check" :size="11" />
              </span>
            </div>
            <div class="tc-sub mono truncate">{{ m.model_name || '—' }}</div>
            <div class="tc-sub truncate">
              {{ providerCell(m) }}
              <template v-if="hostOf(m.base_url)"> · {{ hostOf(m.base_url) }}</template>
              <span class="miss" v-if="m.has_secret === false && !m.is_mock"> · 缺密钥</span>
            </div>
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
            <!-- 行操作压成右上角一枚 ⋯：hover / 键盘聚焦才浮现（触摸设备常显） -->
            <Menu
              class="tc-more" :items="rowMenu(m)" up
              :label="`更多操作：${m.name || m.id}`"
              @select="onRowAction(m, $event)"
            />
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

    <!-- 密钥 + 一键导入预设：两张都是"收起时只有一行抬头"的短卡，并排占同一行 -->
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

    <!-- 「+」的选择器（用户口径 2026-12-07）：先挑这一类的现成配置，挑中就带着预设进表单；
         没有合适的再手动填。用一个窄弹窗而不是就地菜单 —— 候选可能七八个（每家一个），
         就地弹层会把设置页顶长，而且这页本来就在滚动容器里。 -->
    <Modal
      :open="addOpen"
      :title="`添加${typeLabel(addType)}模型`"
      @close="addOpen = false"
    >
      <div class="add-picker">
        <p class="tiny faint add-note">
          选一家现成的（base_url / 密钥文件名 / 参数会一起填好），或自己填一份。
        </p>
        <div class="add-grid" v-if="addCandidates.length">
          <button
            class="add-card" v-for="c in addCandidates" :key="c.id" type="button"
            :data-add-preset="c.id" @click="addFromPreset(c.id)"
          >
            <span class="add-name truncate">{{ c.label }}</span>
            <span class="add-sub mono truncate">{{ c.model || '—' }}</span>
            <span class="add-host tiny truncate">{{ c.host }}</span>
          </button>
        </div>
        <div class="tiny faint" v-else>
          没有适用于「{{ typeLabel(addType) }}」的现成预设，直接手动填一份。
        </div>
      </div>
      <template #footer>
        <button class="btn sm" @click="addManual">
          <AppIcon name="edit" :size="14" />手动新建配置
        </button>
      </template>
    </Modal>

    <ModelForm
      :open="formOpen" :mode="formMode" :target="formTarget"
      :default-type="formDefaultType" :default-preset="formPreset"
      :types="allTypes" :type-labels="TYPE_LABEL"
      :presets="state.presets" :secrets="state.secrets" :busy="rowBusy('submit')"
      @close="formOpen = false" @submit="submitForm"
    />
  </div>
</template>

<style scoped>
/* 卡片两列版式：短卡（密钥 / 预设这类表单、开关组）并排，长内容（用量图、模型列表）整宽。
   ≤1100px 回落单列；卡片间距只由这里的 gap 给（base.css 的 `.card + .card` 在 `.grid` 里已清零，
   不再叠第二份 margin，否则同排卡片会一高一低）。 */
.cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  align-items: start;
}
.cards > .card { min-width: 0; }
.cards > .wide { grid-column: 1 / -1; }
@media (max-width: 1100px) { .cards { grid-template-columns: minmax(0, 1fr); } }

/* 顶部一句话：说清这块能干什么，不讲实现 */
.panel-note {
  grid-column: 1 / -1; margin: 0;
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
}

/* 面板工具条与失败条：不属于任何一张卡，永远整宽 */
.sec-actions { grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

/* 加载失败时的可见提示（失败不静默） */
.load-error {
  grid-column: 1 / -1;
  padding: 8px 12px;
  border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--border));
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: calc(12.5px * var(--ts, 1));
}

/* 折叠块的抬头：仍是 .card-head 的位置与分割线，只是整条可点 */
.card-head.fold {
  width: 100%; border: 0; border-bottom: 1px solid var(--divider);
  border-radius: 0; background: transparent; color: var(--text);
  font: inherit; font-weight: 600; text-align: left; cursor: pointer;
}
.card-head.fold:hover { color: var(--accent-text); }

.gap-top { margin-top: 10px; }
.cap { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); letter-spacing: .01em; }

/* 抬头：标题（图标+文字）与计数分离 —— 计数是"说明"，不该跟标题抢字重 */
.hd-title { display: inline-flex; align-items: center; gap: 7px; }
.hd-count {
  min-width: 20px; padding: 0 6px; height: 18px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 999px; background: var(--bg-sunken);
  color: var(--text-faint); font-size: calc(11px * var(--ts, 1)); font-weight: 600;
  font-variant-numeric: tabular-nums;
}

/* 抬头那枚加号：与行内 .btn.icon.sm 同一档尺寸，但常显、带主题色的 hover */
.card-add {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; padding: 0;
  border: 1px solid var(--divider); border-radius: 999px;
  background: transparent; color: var(--text-dim); cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.card-add:hover {
  border-color: color-mix(in srgb, var(--accent) 50%, var(--divider));
  background: var(--bg-hover); color: var(--accent-text);
}

/* 「+」的选择器：一格一家的卡片，两列自适应；太窄就一列 */
.add-picker { display: flex; flex-direction: column; gap: 10px; }
.add-note { margin: 0; line-height: 1.5; }
.add-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; }
.add-card {
  display: flex; flex-direction: column; align-items: flex-start; gap: 2px; min-width: 0;
  padding: 9px 11px; text-align: left; cursor: pointer;
  border: 1px solid var(--divider); border-radius: var(--radius-sm);
  background: var(--bg-layer); color: var(--text); font: inherit;
  transition: border-color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.add-card:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--divider)); background: var(--bg-hover); }
.add-name { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; max-width: 100%; }
.add-sub { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim); max-width: 100%; }
.add-host { color: var(--text-faint); max-width: 100%; }

/* ---------------------------- 六个类型并成一排 ----------------------------
 * 用户口径 2026-12-07 二轮：「六张类型卡并成一排（每类一列）」。
 * 每列只有 ~180px，所以列内版式是**竖着排的三小行**（名称 / 模型名 / 服务商·域名），
 * 动作压到右上角一枚 ⋯（测试也进去）—— 行内再放按钮就会把名字挤成一个字。
 * 窗口窄了依次降为 3 列 / 2 列 / 1 列：宁可换行，也不要 6 个 90px 的夹缝。 */
.types-row {
  grid-column: 1 / -1;
  display: grid; grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px; align-items: start;
}
@media (max-width: 1239px) { .types-row { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 820px) { .types-row { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .types-row { grid-template-columns: minmax(0, 1fr); } }

/* 列的外框：与 .card 同一门语言（同圆角、同边框、同阴影），但不吃 `.card + .card` 的间距 */
.type-col {
  display: flex; flex-direction: column; min-width: 0;
  background: var(--bg-layer); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-1);
  overflow: hidden;
}
.tc-head {
  display: flex; align-items: center; gap: 5px; min-width: 0;
  padding: 8px 8px 7px; border-bottom: 1px solid var(--divider);
}
.tc-title {
  display: inline-flex; align-items: center; gap: 5px; min-width: 0;
  font-size: calc(12px * var(--ts, 1)); font-weight: 600; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 当前在用的那个（用户口径「放置当前使用的模型名称」）：一枚淡色胶囊，名字长了省略 */
.tc-cur {
  display: flex; align-items: center; gap: 4px; min-width: 0;
  margin: 7px 8px 0; padding: 0 7px; height: 20px;
  border-radius: 999px; background: var(--accent-soft); color: var(--accent-text);
  font-size: calc(11px * var(--ts, 1)); font-weight: 500;
}
.tc-cur.none { background: var(--bg-sunken); color: var(--text-faint); }

.tc-list { display: flex; flex-direction: column; padding: 4px 0 6px; }
/* 一条模型：三小行 + 右上角那枚 ⋯ */
.tc-item { position: relative; padding: 6px 8px; min-width: 0; }
.tc-item:hover { background: var(--bg-hover); }
/* 刚建好 / 刚保存的那一条：提亮一下再退回去（不用自己找「哪个是我刚建的」） */
.tc-item.fresh { background: var(--accent-soft); box-shadow: inset 2px 0 0 var(--accent); }
.tc-line { display: flex; align-items: center; gap: 5px; min-width: 0; }
/* 点名字就地改名：文字按钮而不是普通按钮（cursor: text = 「点进输入框」的手感） */
.tc-name {
  flex: 1 1 auto; min-width: 0; padding: 0; border: 0; background: transparent;
  color: var(--text); font: inherit; font-size: calc(12px * var(--ts, 1)); font-weight: 600;
  text-align: left; cursor: text; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tc-name:hover { color: var(--accent-text); }
.tc-line .rename { flex: 1 1 auto; min-width: 0; font-size: calc(12px * var(--ts, 1)); font-weight: 600; }
.tc-default { flex: none; display: inline-flex; color: var(--ok); }
.tc-sub {
  margin-top: 1px; padding-left: 11px;
  font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint);
}
.tc-sub.mono { font-family: var(--mono); }
.tc-sub .miss { color: var(--warn); }
.tc-test { display: flex; align-items: center; gap: 4px; margin-top: 3px; padding-left: 11px; min-width: 0; }

/* 状态点：只有一个点该有颜色（三种状态各自的色） */
.stat-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--text-faint); }
.stat-dot.ok { background: var(--ok); }
.stat-dot.danger { background: var(--danger); }

/* 列里的 ⋯：压在这一条的右上角，hover / 键盘聚焦才浮现（触摸设备常显） */
.tc-more { position: absolute; top: 2px; right: 2px; opacity: 0; transition: opacity var(--dur-fast) var(--ease); }
.tc-item:hover .tc-more, .tc-item:focus-within .tc-more { opacity: 1; }
@media (hover: none) { .tc-more { opacity: 1; } }

/* 空态：一行说明 + 「添加」按钮（抬头那枚加号之外的第二处入口） */
.tc-empty {
  display: flex; align-items: center; gap: 6px;
  padding: 4px 8px 2px; color: var(--text-faint);
  font-size: calc(11px * var(--ts, 1));
}
.tc-empty .btn { flex: none; }
/* 密钥文件：等宽小签，一眼看出"有没有、叫什么"，内容永远不回显 */
.secrefs { display: flex; flex-wrap: wrap; gap: 6px; }
.secref {
  padding: 2px 8px; border-radius: 999px;
  background: var(--bg-sunken); color: var(--text-dim); font-size: calc(11.5px * var(--ts, 1));
}

/* 预设：一排胶囊 chips（原先是一排 .btn，7 个就糊成按钮墙） */
.presets { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.preset {
  height: 28px; padding: 0 11px;
  border: 1px solid var(--divider); border-radius: 999px;
  background: transparent; color: var(--text-dim);
  font: inherit; font-size: calc(12px * var(--ts, 1)); cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.preset:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--divider));
  background: var(--bg-hover); color: var(--text);
}
.preset:disabled { opacity: .45; cursor: default; }
</style>
