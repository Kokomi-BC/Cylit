<script setup lang="ts">
/**
 * 设置：并发 / 渲染 / RAG 参数的白名单编辑 + 只读系统信息与健康检查。
 *
 * 数据面 `GET /settings`（values + stored + editable 白名单）、`PUT /settings`（仅提交变更项）、
 * `GET /settings/health`（库 / 向量库 / 任务池）。设置写入 setting 表，重启后仍生效。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { api } from '@/api/client';
import type { HealthPayload } from '@/api/types';
import {
  loadHealth, loadSettings, reportError, state, toast, withLoading,
} from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';

/* ---------------------------- 表单结构定义 ---------------------------- */

/** 可编辑项键（必须落在后端 EDITABLE 白名单内）。 */
type EditableKey =
  | 'translate_concurrency' | 'translate_max_concurrency' | 'translate_min_concurrency'
  | 'task_worker_count' | 'render_dpi' | 'crop_dpi'
  | 'rag_top_k_vector' | 'rag_top_k_lexical' | 'rag_top_k_fused' | 'rag_top_n_rerank'
  | 'allow_general_knowledge' | 'default_generation_model';

/** 表单值：数字项未填时为 ''，开关为布尔，文本项为字符串。 */
type FormValue = number | string | boolean;

type SettingsForm = Record<EditableKey, FormValue>;

interface FieldDef {
  key: EditableKey;
  label: string;
  type: 'number' | 'text' | 'bool';
  min?: number;
}

interface GroupDef {
  key: string;
  title: string;
  hint: string;
  fields: FieldDef[];
}

interface ReadonlyDef {
  key: string;
  label: string;
}

/** 分组表单：键必须在后端白名单内。 */
const GROUPS: GroupDef[] = [
  {
    key: 'concurrency',
    title: '并发',
    hint: '翻译并发（AIMD 自适应上下限）与任务工作线程数；改小更省额度，改大更快但更易触发限流。',
    fields: [
      { key: 'translate_concurrency', label: '翻译并发（初始 N）', type: 'number', min: 1 },
      { key: 'translate_max_concurrency', label: '翻译并发上限', type: 'number', min: 1 },
      { key: 'translate_min_concurrency', label: '翻译并发下限', type: 'number', min: 1 },
      { key: 'task_worker_count', label: '任务工作线程数', type: 'number', min: 1 },
    ],
  },
  {
    key: 'render',
    title: '渲染',
    hint: '页面图用于复核与降级；公式高倍裁图 dpi 建议为页面 dpi 的 3–4 倍。',
    fields: [
      { key: 'render_dpi', label: '页面渲染 dpi', type: 'number', min: 72 },
      { key: 'crop_dpi', label: '公式裁图 dpi', type: 'number', min: 72 },
    ],
  },
  {
    key: 'rag',
    title: 'RAG 检索',
    hint: '向量 / 词法召回条数与融合、重排条数。',
    fields: [
      { key: 'rag_top_k_vector', label: '向量召回 top_k', type: 'number', min: 1 },
      { key: 'rag_top_k_lexical', label: '词法召回 top_k', type: 'number', min: 1 },
      { key: 'rag_top_k_fused', label: '融合后 top_k', type: 'number', min: 1 },
      { key: 'rag_top_n_rerank', label: '重排条数 n', type: 'number', min: 0 },
    ],
  },
  {
    key: 'misc',
    title: '其他',
    hint: '通用知识开关仅影响 RAG 未命中时的兜底回答；默认生成模型留空则使用 mock provider。',
    fields: [
      { key: 'allow_general_knowledge', label: '允许通用知识（RAG 兜底）', type: 'bool' },
      { key: 'default_generation_model', label: '默认生成模型（留空 = mock）', type: 'text' },
    ],
  },
];

/** 编辑表单涉及的全部键（与后端 EDITABLE 白名单一致）。 */
const EDITABLE_KEYS: EditableKey[] = GROUPS.flatMap((g) => g.fields.map((f) => f.key));

/** 只读信息卡片。 */
const READONLY_KEYS: ReadonlyDef[] = [
  { key: 'data_dir', label: '数据目录' },
  { key: 'db_backend', label: '业务库后端' },
  { key: 'api_prefix', label: 'API 前缀' },
  { key: 'secrets_dir', label: '密钥目录' },
  { key: 'user_id', label: '用户 ID' },
];

/** 当前编辑值与「当前生效值」，用于变更检测与撤销。 */
const form = reactive<SettingsForm>({
  translate_concurrency: '',
  translate_max_concurrency: '',
  translate_min_concurrency: '',
  task_worker_count: '',
  render_dpi: '',
  crop_dpi: '',
  rag_top_k_vector: '',
  rag_top_k_lexical: '',
  rag_top_k_fused: '',
  rag_top_n_rerank: '',
  allow_general_knowledge: false,
  default_generation_model: '',
});
const base = reactive<SettingsForm>({ ...form });

const busy = ref(false);
const loadError = ref('');
const health = ref<HealthPayload | null>(null);
const healthError = ref('');

/** 首次进入用骨架提示，加载失败给明确错误而不是空白页。 */
const loading = computed(() => !!state.loading.settings && !state.settings && !loadError.value);

/** PUT /settings 的返回形状：逐键生效 / 被拒 + 服务端权威 values。 */
interface SettingsApplyResult {
  applied?: Record<string, unknown>;
  rejected?: Record<string, string>;
  values?: Record<string, unknown>;
}

/* ---------------------------- 载入 ---------------------------- */

function fillFrom(values?: Record<string, unknown> | null) {
  for (const key of EDITABLE_KEYS) {
    const v = values?.[key];
    if (key === 'allow_general_knowledge') {
      form[key] = v === true || v === 'true' || v === 1;
    } else if (key === 'default_generation_model') {
      form[key] = v === null || v === undefined ? '' : String(v);
    } else {
      form[key] = v === null || v === undefined || v === '' ? '' : Number(v);
    }
    base[key] = form[key];
  }
}

async function load() {
  loadError.value = '';
  try {
    const data = await withLoading('settings', () => loadSettings(), { silent: true });
    fillFrom(data?.values || {});
  } catch (err) {
    loadError.value = `设置加载失败：${(err as Error)?.message ?? String(err)}`;
    reportError(err, '设置加载失败');
  }
}

async function loadHealthCard() {
  healthError.value = '';
  try {
    health.value = await loadHealth();
    if (health.value?.error) healthError.value = health.value.error;
  } catch (err) {
    healthError.value = (err as Error)?.message ?? String(err);
  }
}

/* ---------------------------- 变更检测 ---------------------------- */

function changedKeys(): EditableKey[] {
  return EDITABLE_KEYS.filter((k) => form[k] !== base[k]);
}

const changed = computed<EditableKey[]>(() => changedKeys());
const canSave = computed(() => changed.value.length > 0 && !busy.value);

async function save() {
  const keys = changedKeys();
  if (!keys.length) { toast('没有需要保存的改动', 'warn'); return; }
  const payload: Partial<Record<EditableKey, FormValue>> = {};
  for (const k of keys) {
    const v = form[k];
    if (v === '') {
      toast(`「${k}」不能为空，请填入数字或文本后再保存`, 'warn');
      return;
    }
    payload[k] = v;
  }
  busy.value = true;
  try {
    const res = await api.put<SettingsApplyResult>('/settings', payload);
    const rejected = res?.rejected || {};
    const applied = Object.keys(res?.applied || {});
    if (res?.values) fillFrom(res.values);      // 以服务端生效值为准（含被拒项回滚）
    if (applied.length) {
      toast(`已保存（重启后仍生效）：${applied.join('、')}`, 'ok', 4200);
    }
    for (const [k, reason] of Object.entries(rejected)) {
      toast(`「${k}」未生效：${reason}`, 'warn', 5200);
    }
    if (!applied.length && !Object.keys(rejected).length) {
      toast('服务端未返回任何生效项，请检查请求内容', 'warn');
    }
    await loadHealthCard();
  } catch (err) {
    reportError(err, '保存设置失败');
  } finally {
    busy.value = false;
  }
}

function reset() {
  for (const k of EDITABLE_KEYS) form[k] = base[k];
  toast('已还原为当前生效值（未保存）', 'info', 2400);
}

/* ---------------------------- 健康展示 ---------------------------- */

/* 健康信息是后端动态 JSON，在边界处用 any 收敛 */
const db = computed<Record<string, any>>(() => health.value?.database ?? {});
const vector = computed<Record<string, any>>(() => health.value?.vector_store ?? {});
const runner = computed<Record<string, any>>(() => health.value?.task_runner ?? {});

const dbOk = computed(() => db.value.ok === true);
const vectorOk = computed(() => vector.value.available === true);

const queueText = computed(() => {
  const q = runner.value.queues;
  if (!q) return '—';
  if (Array.isArray(q)) return q.length ? q.join(', ') : '—';
  if (typeof q === 'object') {
    const entries = Object.entries(q);
    return entries.length ? entries.map(([k, v]) => `${k}:${v}`).join(', ') : '—';
  }
  return String(q);
});

const runningText = computed(() => {
  const r = runner.value.running;
  if (Array.isArray(r)) return `${r.length} 个`;
  if (r === null || r === undefined) return '—';
  return `${r} 个`;
});

/** 只读信息取值（values 是后端动态 JSON，这里在边界处收敛为展示字符串）。 */
function valueOf(key: string): string {
  const v = state.settings?.values?.[key];
  return v === null || v === undefined || v === '' ? '—' : String(v);
}

onMounted(async () => {
  await load();
  await loadHealthCard();
});
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>设置</h1>
      </div>
      <div class="grow"></div>
      <div class="page-actions">
        <span class="badge" :class="changed.length ? 'warn' : ''">
          {{ changed.length ? ('有 ' + changed.length + ' 项未保存') : '无改动' }}
        </span>
        <button class="btn sm" :disabled="busy || !changed.length" @click="reset">
          <AppIcon name="refresh" :size="14" />撤销改动
        </button>
        <button class="btn primary sm" :disabled="!canSave" @click="save">
          <AppIcon name="check" :size="14" />{{ busy ? '保存中…' : '保存设置' }}
        </button>
      </div>
    </div>

    <!-- 首次加载 -->
    <div class="empty card" v-if="loading">正在加载设置…</div>

    <!-- 加载失败（可重试） -->
    <div class="empty card" v-else-if="loadError">
      <p class="load-error">{{ loadError }}</p>
      <button class="btn sm" @click="load">
        <AppIcon name="refresh" :size="14" />重试
      </button>
    </div>

    <template v-else>
      <div class="card" v-for="g in GROUPS" :key="g.key">
        <div class="card-head">{{ g.title }}</div>
        <div class="card-body">
          <div class="grid cols-2">
            <template v-for="f in g.fields" :key="f.key">
              <label class="field" v-if="f.type === 'number'">
                {{ f.label }}
                <input type="number" :min="f.min" v-model.number="form[f.key]" />
                <span class="tiny faint mono">{{ f.key }}</span>
              </label>
              <label class="field" v-else-if="f.type === 'text'">
                {{ f.label }}
                <input type="text" v-model="form[f.key]" placeholder="留空 = mock provider" />
                <span class="tiny faint mono">{{ f.key }}</span>
              </label>
              <label class="field" v-else>
                <span class="row">
                  <input type="checkbox" v-model="form[f.key]" />
                  <span>{{ f.label }}</span>
                </span>
                <span class="tiny faint mono">{{ f.key }}</span>
              </label>
            </template>
          </div>
          <p class="tiny faint hint">{{ g.hint }}</p>
        </div>
      </div>

      <div class="grid cols-2" style="margin-top: 14px">
        <div class="card">
          <div class="card-head">只读信息（不可编辑）</div>
          <div class="card-body">
            <div class="row small" v-for="r in READONLY_KEYS" :key="r.key">
              <span class="muted">{{ r.label }}</span>
              <span class="spacer"></span>
              <span class="mono tiny truncate ro-value" :title="valueOf(r.key)">{{ valueOf(r.key) }}</span>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            健康检查
            <span class="spacer"></span>
            <button class="btn sm ghost" @click="loadHealthCard">
              <AppIcon name="refresh" :size="14" />重新检测
            </button>
          </div>
          <div class="card-body">
            <div class="empty" v-if="!health">检测中…</div>
            <div class="empty load-error" v-else-if="healthError">健康检查失败：{{ healthError }}</div>
            <template v-else>
              <div class="row small">
                <span class="badge" :class="dbOk ? 'ok' : 'danger'">{{ dbOk ? '数据库正常' : '数据库异常' }}</span>
                <span class="mono tiny">{{ db.backend || '—' }}</span>
                <span class="spacer"></span>
                <span class="mono tiny truncate ro-value" :title="String(db.database ?? db.error ?? '')">
                  {{ db.database || db.error || '' }}
                </span>
              </div>
              <div class="row small health-row">
                <span class="badge" :class="vectorOk ? 'ok' : 'danger'">
                  {{ vectorOk ? '向量库可用' : '向量库不可用' }}
                </span>
                <span class="tiny faint">chunk {{ vector.chunks ?? '—' }}</span>
                <span class="spacer"></span>
                <span class="mono tiny truncate ro-value" :title="String(vector.path ?? vector.error ?? '')">
                  {{ vector.path || vector.error || '' }}
                </span>
              </div>
              <div class="row small health-row">
                <span class="badge">任务池</span>
                <span class="tiny faint">workers {{ runner.workers ?? '—' }}</span>
                <span class="tiny faint">运行中 {{ runningText }}</span>
                <span class="spacer"></span>
                <span class="mono tiny truncate ro-value" :title="queueText">队列 {{ queueText }}</span>
              </div>
              <div class="row small health-row">
                <span class="badge">数据目录</span>
                <span class="spacer"></span>
                <span class="mono tiny truncate ro-value" :title="health.data_dir">{{ health.data_dir || '—' }}</span>
              </div>
            </template>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 设置页私有细节（其余样式复用 base.css 的通用类） */
.load-error { color: var(--danger); }
.hint { margin: 8px 0 0; }
.health-row { margin-top: 8px; }

/* 只读值 / 健康信息的长路径：占一半宽度并省略 */
.ro-value { max-width: 56%; }
</style>
