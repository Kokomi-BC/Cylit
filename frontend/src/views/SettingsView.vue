<script setup lang="ts">
/**
 * 设置：**分区**的参数编辑 + 只读系统信息与健康检查。
 *
 * 版式（移植自参照项目 retain-pdf 的设置弹窗）：**左导航 + 右内容窗格**，一次只渲染一区；
 * 左侧导航吸顶，深链（`/app/settings#kb`）只负责切到对应窗格。
 *
 * 分区：
 * 1. **常规参数** —— 任务线程数 / 渲染 / RAG 参数 / 其他；**只有这一区有"保存设置"**
 *    （数据面 `PUT /settings`，仅提交变更项）。
 * 2. **显示主题** —— 皮肤注册表驱动的换肤（`src/theme/registry.ts` + `styles/themes/*.css`），
 *    选择写 localStorage，立即生效。
 * 3. **索引管理** —— 概览统计、论文索引表、索引状态与对账、chunk 元数据。
 *    这些原本长在「知识库」页上，用户口径是"那页全是设置"，于是整体搬到这里；
 *    它们都是**即时操作**，没有"保存"这一步。
 * 4. **在线解析（MinerU）** —— 第三方联网解析服务的 token 与默认参数：
 *    token **只写不回显**（后端只给掩码），配置后可就地「验证」（真的打一次 MinerU 接口）。
 * 5. **搜索记录** —— 知识库搜索历史的本地管理（localStorage）。
 * 6. **系统信息** —— 只读信息 + 健康检查（`GET /settings/health`）。
 *
 * 数据面：`GET /settings`（values + stored + editable 白名单）、`PUT /settings`、
 * `GET /settings/health`；MinerU 另走 `/mineru/config`（GET/PUT）与 `/mineru/verify`（POST）。
 * 设置写入 setting 表，重启后仍生效。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api } from '@/api/client';
import type { HealthPayload, MineruConfig, MineruVerifyResult, ParseEngine } from '@/api/types';
import {
  loadHealth, loadSettings, reportError, state, toast, withLoading,
} from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';
import KbIndexPanel from '@/components/settings/KbIndexPanel.vue';
import SearchHistoryPanel from '@/components/settings/SearchHistoryPanel.vue';
import ThemeAppearancePanel from '@/components/settings/ThemeAppearancePanel.vue';

/* ---------------------------- 表单结构定义 ---------------------------- */

/** 可编辑项键（必须落在后端 EDITABLE 白名单内）。 */
type EditableKey =
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
    key: 'task',
    title: '任务',
    hint: '后台任务的工作线程数。翻译本身固定串行（没有并发开关）：串行才能保住跨页前文与前缀缓存。',
    fields: [
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
/* ---------------------------- 分区导航 ---------------------------- */

/** 分区：`id` 同时是锚点（`/app/settings#kb` 直接落在「索引管理」）。
 *
 * 版式是**左导航 + 右内容窗格**（移植自参照项目 retain-pdf 的设置弹窗）：一次只渲染一区，
 * 于是不需要"滚到第几屏"的锚点对齐 —— 深链只负责切区，不再负责滚动。
 * `desc` 是窗格抬头的说明（下放到 SECTIONS 里，避免每个 section 各写一遍标题）。 */
const SECTIONS = [
  { id: 'general', label: '常规参数', icon: 'settings', desc: '后台任务线程数、页面与公式渲染 dpi、RAG 检索条数。改完要按「保存设置」才落库。' },
  { id: 'appearance', label: '显示主题', icon: 'sparkle', desc: '界面皮肤：色块就是效果预览，点一下立即换肤，选择记在本机（localStorage）。' },
  { id: 'kb', label: '索引管理', icon: 'knowledge', desc: '知识库概览、论文索引与 chunk 对账。都是即时操作，没有"保存"这一步。' },
  { id: 'mineru', label: '在线解析', icon: 'bolt', desc: 'MinerU 云端解析的 token 与默认参数。token 只写不回显（后端只给掩码）。' },
  { id: 'history', label: '搜索记录', icon: 'search', desc: '知识库检索的历史记录，存在本机（localStorage），不上传后端。' },
  { id: 'about', label: '系统信息', icon: 'server', desc: '只读配置与后端健康检查（数据库 / 向量库 / 任务池）。' },
] as const;

const route = useRoute();
const router = useRouter();
const active = ref<string>(SECTIONS[0].id);

/** 当前分区的标题与说明：窗格抬头用它，section 里不再重复写标题。 */
const activeSection = computed(() => SECTIONS.find((s) => s.id === active.value) ?? SECTIONS[0]);

/** 点分区：换窗格 + 把 hash 写进地址栏（用 replace，不堆历史，也不触发任何滚动）。 */
function go(id: string) {
  active.value = id;
  if (route.hash !== `#${id}`) void router.replace({ hash: `#${id}` });
}

/** 深链与浏览器前进/后退：只切区（滚动交给浏览器自己的恢复策略）。 */
watch(() => route.hash, (hash) => {
  const id = String(hash || '').replace('#', '');
  if (SECTIONS.some((s) => s.id === id)) active.value = id;
});

/* ---------------------------- 表单状态 ---------------------------- */

/** 当前编辑值与「当前生效值」，用于变更检测与撤销。 */
const form = reactive<SettingsForm>({
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

/* ============================ 在线解析（MinerU） ============================
 * MinerU 是**联网的第三方解析服务**：导入时把 PDF 交给它，公式/表格比本地解析更强。
 * 它要一个 token（官方每 90 天过期），所以这一区的东西分两类：
 * - **凭证**：token 输入框 + 保存 / 验证 / 清除（token 只写不回显：后端只给掩码）；
 * - **默认参数**：模型版本、语言、默认解析引擎（导入弹窗的初值）。
 *
 * 与上面「常规参数」的区别：这些**不进 `PUT /settings`**，而是各自的 `/mineru/config`。
 * 原因是 token 属于敏感值，不该混在通用设置白名单里（也方便后端单独做权限与掩码）。
 * ========================================================================== */

interface MineruForm {
  /** 新 token：**空 = 本次不改**（后端拿不到 token 就不动已存的那个） */
  token: string;
  engine_default: ParseEngine;
  model_version: 'pipeline' | 'vlm';
  language: string;
}

const MINERU_MODELS: ReadonlyArray<{ value: 'pipeline' | 'vlm'; label: string }> = [
  { value: 'pipeline', label: 'pipeline（默认；版面 + 公式更稳）' },
  { value: 'vlm', label: 'vlm（VLM 大模型；复杂版面更强）' },
];

const MINERU_LANGS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'en', label: 'en（英文论文，默认）' },
  { value: 'ch', label: 'ch（中文文献）' },
];

const mineruForm = reactive<MineruForm>({
  token: '', engine_default: 'local', model_version: 'pipeline', language: 'en',
});
/** 服务端**当前生效**的那一份：用来算"改了没有"，以及撤销 / 保存成功后回填。 */
const mineruBase = reactive<MineruConfig>({
  has_token: false, token_masked: null, engine_default: 'local',
  model_version: 'pipeline', language: 'en',
});
const mineruBusy = ref('');
const mineruError = ref('');
const mineruVerify = ref<MineruVerifyResult | null>(null);

const mineruTokenText = computed(
  () => mineruBase.token_masked || (mineruBase.has_token ? '已保存（掩码不可用）' : ''),
);
/** 只在这些东西真的变了（或填了新 token）时才允许保存，避免无意义请求。 */
const mineruChanged = computed(() => !!mineruForm.token
  || mineruForm.engine_default !== mineruBase.engine_default
  || mineruForm.model_version !== mineruBase.model_version
  || mineruForm.language !== mineruBase.language);

/** 把后端返回的配置同时灌进"表单值"与"服务端权威值"（保存成功后也走它）。 */
function fillMineru(cfg: MineruConfig) {
  mineruBase.has_token = cfg?.has_token === true;
  mineruBase.token_masked = cfg?.token_masked ?? null;
  mineruBase.token_source = cfg?.token_source ?? null;
  mineruBase.engine_default = cfg?.engine_default === 'mineru' ? 'mineru' : 'local';
  mineruBase.model_version = cfg?.model_version === 'vlm' ? 'vlm' : 'pipeline';
  mineruBase.language = cfg?.language ? String(cfg.language) : 'en';
  mineruBase.base_url = cfg?.base_url ?? '';
  mineruBase.token_file = cfg?.token_file ?? null;
  mineruBase.online_ready = cfg?.online_ready === true;
  mineruForm.token = '';
  mineruForm.engine_default = mineruBase.engine_default;
  mineruForm.model_version = mineruBase.model_version;
  mineruForm.language = mineruBase.language;
}

async function loadMineru() {
  mineruError.value = '';
  try {
    fillMineru(await api.get<MineruConfig>('/mineru/config'));
  } catch (err) {
    // 后端还没上 `/mineru/*` 时这里必然失败：给一句明确的话，而不是让区块空白
    mineruError.value = `MinerU 配置读取失败：${(err as Error)?.message ?? String(err)}`;
  }
}

/** 后端 `envelope(message=…)` 的文案（client 挂在 `__meta` 上，与论文库同一写法）。 */
function metaMessage(res: any): string {
  return String(res?.__meta?.message ?? '');
}

/** 保存 token 与默认参数。**只提交变更项**（后端按字段可选处理）。 */
async function saveMineru() {
  if (!mineruChanged.value) { toast('没有需要保存的改动', 'warn'); return; }
  const patch: Record<string, unknown> = {};
  if (mineruForm.token) patch.token = mineruForm.token;
  if (mineruForm.engine_default !== mineruBase.engine_default) {
    patch.engine_default = mineruForm.engine_default;
  }
  if (mineruForm.model_version !== mineruBase.model_version) {
    patch.model_version = mineruForm.model_version;
  }
  if (mineruForm.language !== mineruBase.language) patch.language = mineruForm.language;
  mineruBusy.value = 'save';
  try {
    const res = await api.put<MineruConfig>('/mineru/config', patch);
    fillMineru(res);
    mineruVerify.value = null;         // 换了 token / 参数：上次的验证结论已经不作数了
    toast(metaMessage(res) || 'MinerU 配置已保存', 'ok', 3600);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 6000);
  } finally {
    mineruBusy.value = '';
  }
}

/**
 * 验证 token：后端会真的打一次 MinerU 接口。
 *
 * ⚠️ **失败时 HTTP 仍然是 200**（`ok:false` + `state`），所以这里按 `ok`/`state` 判成败，
 * 绝不按 HTTP 状态码；`message` 是后端给的一句可执行中文提示，原样展示（加前缀会毁掉指引）。
 */
async function verifyMineru() {
  mineruBusy.value = 'verify';
  mineruVerify.value = null;
  try {
    const res = await api.post<MineruVerifyResult>('/mineru/verify', {});
    mineruVerify.value = res;
    toast(res?.message || (res?.ok ? 'Token 有效' : 'Token 验证未通过'),
      res?.ok ? 'ok' : 'error', res?.ok ? 3600 : 7000);
    await loadMineru();                // 验证顺手刷新掩码与 online_ready
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 6000);
    mineruVerify.value = { ok: false, state: 'network', message: (err as Error)?.message };
  } finally {
    mineruBusy.value = '';
  }
}

/** 清除 token：不可逆（要重新申请/粘贴），用原生 confirm 二次确认。 */
async function clearMineruToken() {
  if (!window.confirm('清除已保存的 MinerU token？\n\n清除后「在线解析」不可用，需要重新填入并验证。')) return;
  mineruBusy.value = 'clear';
  try {
    const res = await api.put<MineruConfig>('/mineru/config', { clear_token: true });
    fillMineru(res);
    mineruVerify.value = null;
    toast(metaMessage(res) || '已清除 MinerU token', 'ok', 3200);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 6000);
  } finally {
    mineruBusy.value = '';
  }
}

onMounted(async () => {
  await load();
  await loadHealthCard();
  await loadMineru();
  // 深链（/app/settings#kb）：只切到对应窗格 —— 一次只渲染一区，没有"滚到第几屏"要纠偏。
  const id = String(route.hash || '').replace('#', '');
  if (id && SECTIONS.some((s) => s.id === id)) active.value = id;
});
</script>

<template>
  <!-- 版式：左导航 + 右内容窗格（与参照项目 retain-pdf 的设置弹窗同构）。
       一次只渲染一区：切区不滚动、不堆叠，深链只负责选窗格。 -->
  <div class="settings-layout">
    <aside class="settings-rail">
      <h1 class="settings-rail-title">设置</h1>
      <nav class="settings-nav" aria-label="设置分类">
        <button
          v-for="s in SECTIONS"
          :key="s.id"
          type="button"
          class="settings-nav-item"
          :class="{ active: active === s.id }"
          @click="go(s.id)"
        >
          <AppIcon :name="s.icon" :size="15" /><span class="settings-nav-label">{{ s.label }}</span>
        </button>
      </nav>
    </aside>

    <div class="settings-pane">
      <header class="pane-head">
        <h2>{{ activeSection.label }}</h2>
        <p>{{ activeSection.desc }}</p>
      </header>

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
      <!-- ① 常规参数：唯一需要"保存"的一区 -->
      <section id="general" class="sec" v-if="active === 'general'">
        <div class="sec-actions">
          <span class="badge warn" v-if="changed.length">有 {{ changed.length }} 项未保存</span>
          <span class="spacer"></span>
          <button class="btn sm" :disabled="busy || !changed.length" @click="reset">
            <AppIcon name="refresh" :size="15" />撤销改动
          </button>
          <button class="btn primary sm" :disabled="!canSave" @click="save">
            <AppIcon name="check" :size="15" />{{ busy ? '保存中…' : '保存设置' }}
          </button>
        </div>

        <div class="card" v-for="g in GROUPS" :key="g.key">
          <div class="card-head">{{ g.title }}</div>
          <div class="card-body">
            <div class="grid cols-2">
              <template v-for="f in g.fields" :key="f.key">
                <label class="field" v-if="f.type === 'number'">
                  <span class="field-top">{{ f.label }}<code class="field-key">{{ f.key }}</code></span>
                  <input type="number" :min="f.min" v-model.number="form[f.key]" />
                </label>
                <label class="field" v-else-if="f.type === 'text'">
                  <span class="field-top">{{ f.label }}<code class="field-key">{{ f.key }}</code></span>
                  <input type="text" v-model="form[f.key]" placeholder="留空 = mock provider" />
                </label>
                <label class="field" v-else>
                  <span class="field-top inline">
                    <input type="checkbox" v-model="form[f.key]" />
                    <span>{{ f.label }}</span>
                    <code class="field-key">{{ f.key }}</code>
                  </span>
                </label>
              </template>
            </div>
            <p class="tiny faint hint">{{ g.hint }}</p>
          </div>
        </div>
      </section>

      <!-- ③ 索引管理（管理/诊断，即时生效，没有"保存"） -->
      <section id="kb" class="sec" v-if="active === 'kb'">
        <KbIndexPanel />
      </section>

      <!-- ② 显示主题：注册表驱动的皮肤网格，点一下立即换肤 -->
      <section id="appearance" class="sec" v-if="active === 'appearance'">
        <ThemeAppearancePanel />
      </section>

      <!-- ③ 在线解析（MinerU）：token 与默认参数，即时保存（没有"保存设置"那一步） -->
      <section id="mineru" class="sec" v-if="active === 'mineru'">
        <div class="sec-actions">
          <span class="badge accent" v-if="mineruBase.online_ready">已就绪</span>
          <span class="badge warn" v-else-if="mineruBase.has_token">已存 token（未验证）</span>
          <span class="badge" v-else>未配置 token</span>
          <span class="spacer"></span>
          <button class="btn sm" :disabled="!!mineruBusy || !mineruChanged" @click="saveMineru">
            <AppIcon name="check" :size="15" />{{ mineruBusy === 'save' ? '保存中…' : '保存' }}
          </button>
          <button
            class="btn sm"
            :disabled="!!mineruBusy || !mineruBase.has_token"
            :title="mineruBase.has_token ? '真的打一次 MinerU 接口，确认 token 还能用' : '还没有 token 可验证'"
            @click="verifyMineru"
          >
            <AppIcon name="bolt" :size="15" />{{ mineruBusy === 'verify' ? '验证中…' : '验证' }}
          </button>
        </div>

        <div class="card">
          <div class="card-head">凭证</div>
          <div class="card-body">
            <label class="field">
              <span class="field-top">
                API token
                <span class="spacer"></span>
                <span class="tiny faint" v-if="mineruTokenText">已保存 {{ mineruTokenText }}</span>
              </span>
              <input
                type="password"
                autocomplete="off"
                spellcheck="false"
                v-model="mineruForm.token"
                :placeholder="mineruBase.has_token
                  ? `已保存 ${mineruTokenText}（留空 = 不修改）`
                  : '粘贴 mineru.net 申请到的 token'"
              />
            </label>
            <div class="row row-gap">
              <button
                class="btn sm danger"
                :disabled="!!mineruBusy || !mineruBase.has_token"
                @click="clearMineruToken"
              >
                <AppIcon name="trash" :size="14" />{{ mineruBusy === 'clear' ? '清除中…' : '清除 token' }}
              </button>
              <span class="spacer"></span>
              <span class="tiny faint">token 每 90 天过期，过期后需到 mineru.net 重新申请</span>
            </div>
            <!-- 验证结论：有效 / 无效 / 过期 / 连不上，各自给不同的语义色与可执行提示 -->
            <div class="row row-gap" v-if="mineruVerify">
              <span class="badge" :class="mineruVerify.ok ? 'ok' : 'danger'">
                <AppIcon :name="mineruVerify.ok ? 'check_circle' : 'error'" :size="13" />
                {{ mineruVerify.ok ? 'Token 有效' : 'Token 不可用' }}
              </span>
              <span class="tiny faint" v-if="mineruVerify.latency_ms !== null
                && mineruVerify.latency_ms !== undefined">
                延迟 {{ mineruVerify.latency_ms }} ms
              </span>
              <span class="tiny faint" v-if="mineruVerify.http_status">
                HTTP {{ mineruVerify.http_status }}<template v-if="mineruVerify.code !== null
                  && mineruVerify.code !== undefined"> / code {{ mineruVerify.code }}</template>
              </span>
            </div>
            <p
              class="tiny verify-msg"
              :class="mineruVerify?.ok ? 'ok' : 'bad'"
              v-if="mineruVerify?.message"
            >{{ mineruVerify.message }}</p>
            <p class="tiny faint">
              token 文件：<span class="mono">{{ mineruBase.token_file || '（后端未返回）' }}</span>
            </p>
            <p class="tiny faint" v-if="mineruBase.base_url">
              服务地址：<span class="mono">{{ mineruBase.base_url }}</span>
            </p>
            <p class="load-error tiny" v-if="mineruError">{{ mineruError }}</p>
          </div>
        </div>

        <div class="card">
          <div class="card-head">默认参数</div>
          <div class="card-body">
            <div class="grid cols-2">
              <label class="field">
                模型版本
                <select v-model="mineruForm.model_version">
                  <option v-for="m in MINERU_MODELS" :key="m.value" :value="m.value">{{ m.label }}</option>
                </select>
              </label>
              <label class="field">
                语言（影响 OCR）
                <select v-model="mineruForm.language">
                  <option v-for="l in MINERU_LANGS" :key="l.value" :value="l.value">{{ l.label }}</option>
                </select>
              </label>
            </div>
            <label class="field field-block">
              默认解析引擎（导入弹窗的初始值）
              <select v-model="mineruForm.engine_default">
                <option value="local">本地解析（离线、快）</option>
                <option value="mineru">在线解析（MinerU 云端）</option>
              </select>
            </label>
            <p class="tiny faint hint">
              这里只是<b>导入弹窗的初值</b>：真要让某一篇走联网解析，还得在导入时把「解析引擎」
              选成在线解析（弹窗里可以逐篇改）。模型版本选 pipeline 时版面与公式更稳；
              选 vlm 时复杂版面更好，但更慢。
            </p>
          </div>
        </div>
      </section>

      <!-- ⑤ 搜索记录（本机 localStorage） -->
      <section id="history" class="sec" v-if="active === 'history'">
        <SearchHistoryPanel />
      </section>

      <!-- ⑥ 系统信息（只读） -->
      <section id="about" class="sec" v-if="active === 'about'">
        <div class="grid cols-2">
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
              <button class="btn icon" title="重新检测" @click="loadHealthCard">
                <AppIcon name="refresh" :size="15" />
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
      </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 设置页私有细节（其余样式复用 base.css 的通用类） */
.load-error { color: var(--danger); }
.hint { margin: 10px 0 0; }
.health-row { margin-top: 8px; }

/**
 * 版式：左导航 + 右内容窗格（参照 retain-pdf 的设置弹窗）。
 *
 * 导航 208px 固定宽、**吸顶**：设置页很长，滚到索引表下面还要能换区；
 * 底 色必须不透明（`--bg-elev`），否则正文会从卡片下面透出来。
 */
.settings-layout {
  display: grid; gap: 20px; align-items: start;
  grid-template-columns: 208px minmax(0, 1fr);
}
.settings-rail {
  position: sticky; top: calc(var(--header-h) + 12px);
  display: flex; flex-direction: column; gap: 10px;
  padding: 14px 12px;
  background: var(--bg-elev); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-1);
}
.settings-rail-title { margin: 0; padding: 0 8px; font-size: 15px; }
.settings-nav { display: flex; flex-direction: column; gap: 2px; }
.settings-nav-item {
  display: flex; align-items: center; gap: 9px; width: 100%;
  height: 34px; padding: 0 10px; border-radius: var(--r-sm);
  background: transparent; color: var(--text-dim);
  font-family: inherit; font-size: 13px; font-weight: 500; text-align: left; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.settings-nav-item:hover { background: var(--bg-hover); color: var(--text); }
.settings-nav-item.active { background: var(--accent-soft); color: var(--accent-text); font-weight: 600; }
.settings-nav-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.settings-pane { min-width: 0; display: flex; flex-direction: column; gap: 14px; }
.pane-head { display: grid; gap: 4px; }
.pane-head h2 { margin: 0; font-size: 17px; }
.pane-head p { margin: 0; max-width: 76ch; font-size: 12.5px; line-height: 1.6; color: var(--text-dim); }

/* 窄屏：导航折到内容上方、横排可滚（不挤成两列，也不把内容压到 200px 宽） */
@media (max-width: 860px) {
  .settings-layout { grid-template-columns: minmax(0, 1fr); }
  .settings-rail {
    position: static; flex-direction: row; align-items: center;
    gap: 10px; padding: 10px 12px; overflow-x: auto;
  }
  .settings-rail-title { flex: none; }
  .settings-nav { flex-direction: row; gap: 4px; }
  .settings-nav-item { width: auto; white-space: nowrap; }
}

/* 分区：只放内容（标题在窗格抬头）。卡片间距交给 base.css 的 `.card + .card`，
   这里不叠 gap —— 两处都管会出现 22px 的"缝"，看着像两组内容。 */
.sec { scroll-margin-top: calc(var(--header-h) + 62px); }
.sec-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }

/* 表单项：标签与"后端的键名"同一行，键名右对齐靠灰，不再占一整行高度 */
.field-top { display: flex; align-items: baseline; gap: 8px; width: 100%; }
.field-top.inline { align-items: center; }
.field-key {
  margin-left: auto; font-family: var(--mono); font-size: 11px; font-weight: 400;
  color: var(--text-faint);
}

/* 只读值 / 健康信息的长路径：占一半宽度并省略 */
.ro-value { max-width: 56%; }

/* 在线解析（MinerU）：卡片内的行间距与验证结论的语义色 */
.row-gap { margin-top: 10px; }
/* token 输入框不跟着 `.field > input` 的 340px 上限走：token 很长，宽一点更好核对 */
.field input[type="password"] { max-width: 100%; width: 100%; font-family: var(--mono); }
.field-block { margin-top: 12px; }
/* 验证结论：成功走 --ok、失败走 --danger（与徽章同一套语义色，不用新色） */
.verify-msg { margin: 8px 0 0; line-height: 1.6; }
.verify-msg.ok { color: var(--ok); }
.verify-msg.bad { color: var(--danger); }
</style>
