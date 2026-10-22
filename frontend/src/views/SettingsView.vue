<script setup lang="ts">
/**
 * 设置：**分区**的参数编辑 + 只读系统信息与健康检查。
 *
 * 版式（模仿 Edge 设置页）：**贴左的导航栏 + 自己滚的内容窗格**，一次只渲染一区；
 * 整页不滚窗口 —— 分区高度不等，交给窗口滚的话滚动条会随切区出现/消失、整页宽度跟着变，
 * 左栏就横向抖一下。深链（`/app/settings#kb`）只负责切窗格。
 *
 * 分区按主题分成四组（左边栏顺序，组内即"改行为 → 改模型 → 改数据 → 改服务"的动线）：
 * · **通用**：1. 显示主题（皮肤注册表驱动，写 localStorage 立即生效）。
 * · **智能与模型**：2. 模型（问答模型 + 其他类型、密钥、预设导入，全是即时操作）、
 *   3. 论文问答（技能、对话风格、运行限额；模型本体在「模型」区）。
 * · **内容与索引**：4. 索引管理（检索规模 / 概览 / 论文索引表 / 对账 / chunk 元数据）、
 *   5. 搜索记录（本地搜索历史）、回收站（软删论文的恢复 / 彻底删除；原来的独立页 /trash 已并到这里）。
 * · **服务与系统**：6. 在线解析（MinerU token 与默认参数，token **只写不回显**，可就地验证）、
 *   7. 系统信息（只读信息 + `GET /settings/health`）。
 *
 * 原来的「常规参数」整区**已拆掉**（用户口径）：真生效的检索参数搬进「索引管理」的检索规模小节，
 * 任务工作线程数 / 页面渲染 dpi / 公式裁图 dpi / 默认生成模型四项直接删除 —— 前两个的读取点
 * 读的是环境变量、设置表里的值没人读；末一个已被模型系统的「默认模型」取代。
 * 老深链 `/settings#general` 由路由守卫改写到 `#kb`（见 `router/index.ts`）。
 *
 * 数据面：`GET /settings`（values + stored + editable 白名单）、`PUT /settings`、
 * `GET /settings/health`；MinerU 另走 `/mineru/config`（GET/PUT）与 `/mineru/verify`（POST）。
 * 设置写入 setting 表，重启后仍生效。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api } from '@/api/client';
import type { HealthPayload, MineruConfig, MineruVerifyResult, ParseEngine } from '@/api/types';
import {
  loadHealth, loadSettings, reportError, state, toast, withLoading,
} from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import AppIcon from '@/components/AppIcon.vue';
import KbIndexPanel from '@/components/settings/KbIndexPanel.vue';
import AgentModelPanel from '@/components/settings/AgentModelPanel.vue';
import ModelPanel from '@/components/settings/ModelPanel.vue';
import SearchHistoryPanel from '@/components/settings/SearchHistoryPanel.vue';
import ThemeAppearancePanel from '@/components/settings/ThemeAppearancePanel.vue';
import DisplayScalePanel from '@/components/settings/DisplayScalePanel.vue';
import TaskQueuePanel from '@/components/settings/TaskQueuePanel.vue';
import TrashPanel from '@/components/trash/TrashPanel.vue';

/* ---------------------------- 只读信息 ---------------------------- */

interface ReadonlyDef {
  key: string;
  label: string;
}

/** 只读信息卡片。 */
const READONLY_KEYS: ReadonlyDef[] = [
  { key: 'data_dir', label: '数据目录' },
  { key: 'db_backend', label: '数据库' },
  { key: 'api_prefix', label: '接口前缀' },
  { key: 'secrets_dir', label: '密钥目录' },
  { key: 'user_id', label: '用户标识' },
];
/* ---------------------------- 分区导航 ---------------------------- */

/** 分区：`id` 同时是锚点（`/app/settings#kb` 直接落在「索引管理」）。
 *
 * 版式是**贴左导航 + 自滚内容窗格**：一次只渲染一区，于是不需要"滚到第几屏"的锚点对齐
 * —— 深链只负责切区，滚动由窗格自己在切区时归零。
 */
const SECTIONS = [
  { id: 'appearance', label: '显示主题', icon: 'sparkle', group: '通用', desc: '换深浅配色与字号，点一下立即生效' },
  { id: 'models', label: '模型', icon: 'model', group: '智能与模型', desc: '选问答与生成用的模型，填密钥' },
  { id: 'agent', label: '论文问答', icon: 'chat', group: '智能与模型', desc: '调问答的技能、说话风格与次数上限' },
  { id: 'kb', label: '索引管理', icon: 'knowledge', group: '内容与索引', desc: '看论文入库情况，调检索范围' },
  { id: 'history', label: '搜索记录', icon: 'search', group: '内容与索引', desc: '看本机保存的搜索记录，可清空' },
  // 回收站并进「内容与索引」：它管的是论文数据本身（恢复 / 彻底删除），与索引管理同一档；
  // 放在组尾 —— 这是低频且不可逆的操作，不该排在日常入口前面。
  { id: 'trash', label: '回收站', icon: 'trash', group: '内容与索引', desc: '把删掉的论文恢复回来，或彻底删除' },
  { id: 'mineru', label: '在线解析', icon: 'bolt', group: '服务与系统', desc: '接在线解析服务，填密钥并验证' },
  { id: 'tasks', label: '任务队列', icon: 'task', group: '服务与系统', desc: '看导入、解析等后台任务的进度' },
  { id: 'about', label: '系统信息', icon: 'server', group: '服务与系统', desc: '看版本与环境，检查各服务是否正常' },
] as const;

/** 搜索用的同义词表：用户按"想改什么"找，不会按分区名找（要深色不会去敲"显示主题"）。 */
const SECTION_KEYWORDS: Record<string, string[]> = {
  appearance: ['主题', '皮肤', '字号', '深色', '浅色', '换肤'],
  models: ['模型', '密钥', '思考', '预设', '导入'],
  agent: ['技能', '对话风格', '联网搜索', '限额', '问答'],
  kb: ['索引', '向量', '分块', '检索', '论文'],
  history: ['记录', '历史', '搜索'],
  trash: ['回收站', '恢复', '删除'],
  mineru: ['token', '解析', '在线'],
  tasks: ['任务', '重试', '进度', '队列'],
  about: ['版本', '健康', '系统', '只读'],
};

/** 搜索词只活在组件里，**不进 hash** —— hash 只表示分区，深链才不会被搜索状态污染。 */
const query = ref('');
const needle = computed(() => query.value.trim().toLowerCase());

/**
 * 按标签 / 一句话说明 / 同义词表过滤。命中口径就是"用户说出口的词"，
 * 分组标题跟着剩下的项走，所以没命中的分组自然整段消失，不用另写一套过滤。
 */
const visibleSections = computed(() => {
  const k = needle.value;
  if (!k) return SECTIONS;
  return SECTIONS.filter((s) => s.label.toLowerCase().includes(k)
    || s.desc.toLowerCase().includes(k)
    || (SECTION_KEYWORDS[s.id] ?? []).some((w) => w.toLowerCase().includes(k)));
});

const route = useRoute();
const router = useRouter();
const active = ref<string>(SECTIONS[0].id);

/** 当前分区：窗格抬头用它，section 里不再重复写标题。 */
const activeSection = computed(() => SECTIONS.find((s) => s.id === active.value) ?? SECTIONS[0]);

/** 内容窗格：窗口不滚了，它自己就是滚动容器，切区后要把它归零。 */
const paneRef = ref<HTMLElement | null>(null);
/** 等 DOM 换完再归零 —— 新分区多高要到渲染后才定，早一步设会被旧内容的溢出量顶回去。 */
function resetPaneScroll() {
  void nextTick(() => {
    if (paneRef.value) paneRef.value.scrollTop = 0;
  });
}

/** 点分区：换窗格 + 把 hash 写进地址栏（用 replace，不堆历史，也不触发任何滚动）。 */
function go(id: string) {
  active.value = id;
  if (route.hash !== `#${id}`) void router.replace({ hash: `#${id}` });
  resetPaneScroll();
}

/** 回车跳到第一个命中：列表已被搜索词收窄，"第一个"就是最接近想要的那个。 */
function jumpToFirst() {
  const first = visibleSections.value[0];
  if (first) go(first.id);
}

/** Esc 清空搜索、恢复全部分区；当前分区不动，选中态因此照样在。 */
function clearQuery() {
  query.value = '';
}

/** 深链与浏览器前进/后退：只切区（窗格归零由 resetPaneScroll 负责）。 */
watch(() => route.hash, (hash) => {
  const id = String(hash || '').replace('#', '');
  if (SECTIONS.some((s) => s.id === id)) {
    active.value = id;
    resetPaneScroll();
  }
});

/** 导航会超出可视区（窄屏横排、或视口矮）：切区后把当前项滚进可视区；没溢出就直接跳过。 */
const navRef = ref<HTMLElement | null>(null);
function revealActive() {
  void nextTick(() => {
    const nav = navRef.value;
    if (!nav || nav.scrollWidth <= nav.clientWidth + 1) return;
    nav.querySelector<HTMLElement>('.settings-nav-item.active')
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
}
watch(active, () => revealActive());

const loadError = ref('');
const health = ref<HealthPayload | null>(null);
const healthError = ref('');

/** 首次进入用骨架提示，加载失败给明确错误而不是空白页。 */
const loading = computed(() => !!state.loading.settings && !state.settings && !loadError.value);

/* ---------------------------- 载入 ---------------------------- */

async function load() {
  loadError.value = '';
  try {
    await withLoading('settings', () => loadSettings(), { silent: true });
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
 * 与「索引管理」里那些检索参数的区别：这些**不进 `PUT /settings`**，而是各自的 `/mineru/config`。
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
  if (!(await askConfirm({
    title: '清除已保存的 MinerU token？',
    body: '清除后「在线解析」不可用，需要重新填入并验证。',
    danger: true,
    confirmLabel: '清除',
  }))) return;
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
  // 深链（/app/settings#kb）：只切到对应窗格 —— 一次只渲染一区，窗格一律从顶部开始。
  const id = String(route.hash || '').replace('#', '');
  if (id && SECTIONS.some((s) => s.id === id)) active.value = id;
  resetPaneScroll();
});
</script>

<template>
  <!-- 版式：贴左的导航栏 + 自己滚的内容窗格（模仿 Edge 设置页）。
       一次只渲染一区：切区把窗格滚回顶部，深链只负责选窗格。 -->
  <div class="settings-layout">
    <aside class="settings-rail">
      <h1 class="settings-rail-title">设置</h1>
      <!-- 搜索只筛左栏列表：窗格仍显示当前分区，回车才换区 -->
      <label class="settings-search">
        <AppIcon name="search" :size="14" />
        <input
          v-model="query"
          type="search"
          placeholder="搜索设置"
          aria-label="搜索设置"
          @keydown.enter.prevent="jumpToFirst"
          @keydown.esc.prevent="clearQuery"
        />
      </label>
      <nav class="settings-nav" ref="navRef" aria-label="设置分类">
        <template v-for="(s, i) in visibleSections" :key="s.id">
          <div v-if="i === 0 || visibleSections[i - 1].group !== s.group" class="settings-nav-group">{{ s.group }}</div>
          <button
            type="button"
            class="settings-nav-item"
            :class="{ active: active === s.id }"
            :aria-current="active === s.id ? 'page' : undefined"
            @click="go(s.id)"
          >
            <AppIcon :name="s.icon" :size="15" /><span class="settings-nav-label">{{ s.label }}</span>
          </button>
        </template>
      </nav>
      <p class="settings-empty" v-if="!visibleSections.length">没有匹配的设置</p>
    </aside>
    <div class="settings-pane" ref="paneRef">
      <!-- 页头：图标 + 标题 + 一句说明，三样都由 SECTIONS 里当前那条驱动 —— 9 个分节共用同一份模板 -->
      <header class="pane-head">
        <span class="pane-head-icon"><AppIcon :name="activeSection.icon" :size="17" /></span>
        <div class="pane-head-text">
          <h2>{{ activeSection.label }}</h2>
          <p class="pane-desc">{{ activeSection.desc }}</p>
        </div>
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
      <!-- ① 模型：原先独立成页，"模型管理"本来也是设置，于是并进来当一区 -->
      <section id="models" class="sec" v-if="active === 'models'">
        <ModelPanel />
      </section>

      <!-- ② 索引管理（管理/诊断，即时生效，没有"保存"） -->
      <section id="kb" class="sec" v-if="active === 'kb'">
        <KbIndexPanel />
      </section>

      <!-- ③ 论文问答：技能、对话风格与运行限额（模型本体在「模型」区） -->
      <section id="agent" class="sec" v-if="active === 'agent'">
        <AgentModelPanel />
      </section>

      <!-- ④ 显示主题 + 显示大小：换肤与字号都即时生效，不放"保存" -->
      <section id="appearance" class="sec" v-if="active === 'appearance'">
        <ThemeAppearancePanel />
        <DisplayScalePanel />
      </section>

      <!-- ⑤ 在线解析（MinerU）：token 与默认参数，即时保存（没有"保存设置"那一步） -->
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

        <div class="cards-2">
          <div class="card">
            <div class="card-head">凭证</div>
            <div class="card-body">
              <label class="field">
                <span class="field-top">API token</span>
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
              <p class="tiny faint meta-line">
                <span>token 文件 <span class="mono">{{ mineruBase.token_file || '—' }}</span></span>
                <span v-if="mineruBase.base_url">服务地址 <span class="mono">{{ mineruBase.base_url }}</span></span>
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
                默认解析引擎
                <select v-model="mineruForm.engine_default">
                  <option value="local">本地解析（离线、快）</option>
                  <option value="mineru">在线解析（MinerU 云端）</option>
                </select>
              </label>
              <p class="tiny faint hint">仅作导入弹窗初值，导入时仍可逐篇改。</p>
            </div>
          </div>
        </div>
      </section>

      <!-- ⑥ 任务队列：原「任务」页的精简版（进度 / 取消 · 重试 · 删除 + 实时指示灯） -->
      <section id="tasks" class="sec" v-if="active === 'tasks'">
        <TaskQueuePanel />
      </section>

      <!-- ⑦ 搜索记录（本机 localStorage） -->
      <section id="history" class="sec" v-if="active === 'history'">
        <SearchHistoryPanel />
      </section>

      <!-- ⑧ 回收站：原独立页 /trash，老链接与老标签由路由重定向落到这一区 -->
      <section id="trash" class="sec" v-if="active === 'trash'">
        <TrashPanel />
      </section>

      <!-- ⑨ 系统信息（只读） -->
      <section id="about" class="sec" v-if="active === 'about'">
        <div class="cards-2">
          <div class="card">
            <div class="card-head">只读信息</div>
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
/* 字段下面那行提示：一行、小字、离字段 8px（不留段落式留白） */
.hint { margin: 8px 0 0; }
.health-row { margin-top: 8px; }
/* token 文件 / 服务地址：并成一行，中间 16px 分隔 */
.meta-line { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 8px 0 0; }

/**
 * 版式：**钉死的左栏 + 自己滚的内容列**（模仿 Edge 设置页，一次只渲染一区）。
 *
 * 这里是整页而不是弹窗：导航栏从内容区最左边起、208px 固定宽，只用一条 1px 发丝线与内容
 * 分隔 —— 圆角、边框、投影叠上去就是一张浮在页面里的卡片，与"整页设置"的定位不符。
 *
 * 整页**不滚窗口**：高度钉成"视口 − 顶栏 − .app-main 上下内边距"，滚动只发生在两栏内部。
 * 为什么必须钉：各分区高度不等，交给窗口滚就会随切区出现/消失一条滚动条，整页宽度跟着变，
 * 左栏于是横向抖一下 —— 用户报的正是这个。算式与 base.css:269 的内边距（18 / 64）绑死，
 * 那边改了这里必须跟着改；861–1024 段的内边距是 14 / 56，见下面单独的媒体查询。
 *
 * 两栏之间**没有分隔线**：分层交给留白（左栏右留白 28、内容列左留白 28）与分组标题，
 * 一根竖线反而把整页切成两个盒子。
 */
.settings-layout {
  --rail-col: 208px;
  /* 顶出去：.app-main 自带左右内边距（20px），左栏因此离内容区边缘还有一段；
     只挪左边 —— 右边缘仍落在 .app-main 的内边距里，横向不会多出滚动条。 */
  margin-left: -20px;
  display: grid;
  grid-template-columns: var(--rail-col) minmax(0, 1fr);
  /* 显式写行高：两栏都靠 height:100% 撑满，行若是 auto 就退回按内容算，窗格的滚动区会变矮 */
  grid-template-rows: minmax(0, 1fr);
  height: calc(100dvh - var(--header-h) - 18px - 64px);
  overflow: hidden;
}
.settings-rail {
  /* 不吸顶：容器已经钉住了，整栏跟着容器走即可（吸顶 + 定高反而会互相打架） */
  position: static;
  height: 100%; overflow-y: auto;
  /* 栏内各块的间距由下面各自的 margin 给，不叠 flex gap（两处都管会变成加倍的缝） */
  display: flex; flex-direction: column; gap: 0;
  /* 左 6px：描边画在元素内也留点余量；右 28px 顶替被删掉的那条竖发丝线 */
  padding: 2px 28px 12px 6px;
}
/* 左栏抬头：整栏唯一的大字，比分区项高一档；下留 14px 交给搜索框 */
.settings-rail-title {
  margin: 0 0 14px; padding: 0 12px;
  font-size: calc(22px * var(--ts, 1)); font-weight: 650; letter-spacing: -.01em;
}
/* 搜索框：与论文库那条同构（图标 + 无边框输入），整行铺满 */
.settings-search {
  display: flex; align-items: center; gap: 8px;
  height: 32px; padding: 0 10px; margin-bottom: 12px;
  border-radius: var(--radius-ctl);
  background: var(--bg-sunken); border: 1px solid var(--border); color: var(--text-faint);
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.settings-search:focus-within {
  border-color: var(--accent); box-shadow: var(--ring); background: var(--bg-elev);
}
.settings-search input {
  width: 100%; min-width: 0; height: 100%; padding: 0;
  border: 0; border-radius: 0; background: transparent; box-shadow: none;
  font-size: calc(13px * var(--ts, 1));
}
.settings-search input:hover,
.settings-search input:focus { background: transparent; border: 0; box-shadow: none; }
.settings-empty { margin: 0; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint); }
.settings-nav { display: flex; flex-direction: column; gap: 2px; }
/* 分组小标题：把 8 个分区切成「通用 / 智能与模型 / 内容与索引 / 服务与系统」四段，
   一眼能看出"要改的是行为、模型，还是数据"。大写 + 字距 + 降一档色 = 只当路标，
   不跟可点项抢注意力。 */
.settings-nav-group {
  margin: 10px 0 2px; padding: 0 12px;
  font-size: calc(10.5px * var(--ts, 1)); font-weight: 600; letter-spacing: .08em;
  color: var(--text-faint); text-transform: uppercase;
}
.settings-nav-group:first-child { margin-top: 0; }
/* 可点项：高与圆角走既有控制变量（--control-h / --radius-ctl）；
   当前项 = 强调色浅底 + 左侧 3px 竖条（::before，不占布局），hover 走统一的 --bg-hover。 */
.settings-nav-item {
  position: relative;
  display: flex; align-items: center; gap: 9px; width: 100%;
  height: var(--control-h); padding: 0 12px; border-radius: var(--radius-ctl);
  background: transparent; color: var(--text-dim);
  font-family: inherit; font-size: calc(13px * var(--ts, 1)); font-weight: 500; text-align: left; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.settings-nav-item::before {
  content: ''; position: absolute; left: 4px; top: 50%;
  width: 3px; height: 14px; border-radius: var(--radius-ctl);
  background: var(--accent); opacity: 0; transform: translateY(-50%);
  transition: opacity var(--dur) var(--ease);
}
.settings-nav-item:hover { background: var(--bg-hover); color: var(--text); }
.settings-nav-item.active { background: var(--accent-soft); color: var(--accent-text); font-weight: 600; }
.settings-nav-item.active::before { opacity: 1; }
/* 描边必须画在元素内：左栏与横排的 nav 都是滚动容器，画在外面的那圈会被裁掉（用户截图里的黑边） */
.settings-nav-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.settings-nav-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.settings-pane {
  min-width: 0; display: flex; flex-direction: column; gap: 18px;
  /* 左栏钉死之后，滚动全部落在这里 */
  height: 100%; overflow-y: auto; overscroll-behavior: contain;
  /* 滚动条槽常驻：分区一高一矮，滚动条一会儿有、一会儿没有，不留槽内容宽度就会跟着跳 */
  scrollbar-gutter: stable;
  /* 两档留白是**下限**：离分隔线 28、右侧（含滚动条槽）不贴边 24 */
  padding-left: 28px; padding-right: 24px;
}
/* 内容列在剩余空间里水平居中、宽度自适应：宽屏封顶 1040（够放两列卡片），窄屏铺满。
   居中只能交给子项的 auto 外边 —— padding 的百分比算的是栅格区宽度，把常驻的滚动条槽
   也算进去了，那样内容会整体左偏半个滚动条宽。 */
.settings-pane > * { width: 100%; max-width: 1040px; margin-inline: auto; }
/* 页头：图标底 + 标题 + 一句说明，底下一根细分隔线收尾 —— 「页头 / 内容」两段节奏。
   近-远：页头到第一张卡 ~34px，卡与卡之间 12px（base.css 的 `.card + .card`）。 */
.pane-head {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 2px 0 16px; border-bottom: 1px solid var(--divider);
}
.pane-head-icon {
  display: grid; place-items: center; flex: none;
  width: 34px; height: 34px; border-radius: var(--radius-ctl);
  background: var(--accent-soft); color: var(--accent-text);
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
/* 三档层次：标题（600）/ 一行副标题（faint）/ 字段标签（dim 12px，在分节里） */
.pane-head-text { display: grid; gap: 3px; min-width: 0; }
.pane-head h2 { margin: 0; font-size: calc(18px * var(--ts, 1)); font-weight: 600; line-height: 1.3; letter-spacing: -.01em; }
.pane-desc { margin: 0; font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }

/* 861–1024：.app-main 的内边距收成 14 / 56（base.css），高度算式得换一档，
   否则差 16px 就又在窗口上长出一条滚动条 —— 那正是要消灭的东西。 */
@media (max-width: 1024px) {
  .settings-layout {
    height: calc(100dvh - var(--header-h) - 14px - 56px);
    /* 这一档 .app-main 的内边距是 12px，负外边距跟着换，否则左栏反而缩回去 8px */
    margin-left: -12px;
  }
}

/* 窄屏：导航折到内容上方、横排可滚（不挤成两列，也不把内容压到 200px 宽）。
   横向滚动交给 nav 自己 —— "设置"那一格始终留在左边；当前项由 revealActive 滚进可视区。 */
@media (max-width: 860px) {
  /* 手机回到窗口滚动：一屏内的滚动区很难用 —— 手势总被内层吃掉，滚到底也换不了区 */
  .settings-layout {
    grid-template-columns: minmax(0, 1fr); row-gap: 14px;
    height: auto; overflow: visible;
  }
  .settings-pane {
    padding-left: 0; padding-right: 0;
    height: auto; overflow: visible;
  }
  /* 折成两行："设置" + 搜索框一行，导航自己占满下一行（横排要整条宽度才滚得顺） */
  .settings-rail {
    position: static; height: auto; flex-direction: row; flex-wrap: wrap; align-items: center;
    gap: 10px; padding: 0 0 10px; overflow: visible;
    border-bottom: 1px solid var(--divider);
  }
  /* 不画竖线：横排的「设置」与导航之间靠 rail 的 gap 10 分开，留白左右对称，别在右边空出 10px */
  .settings-rail-title { flex: none; margin: 0; padding: 0; }
  .settings-search { flex: 1 1 140px; min-width: 0; height: 30px; margin-bottom: 0; }
  .settings-empty { flex: 1 0 100%; }
  .settings-nav {
    flex: 1 0 100%; min-width: 0; flex-direction: row; gap: 4px;
    overflow-x: auto; scrollbar-width: none;
  }
  .settings-nav::-webkit-scrollbar { display: none; }
  /* flex: none 是关键：不然项会被挤扁，横排就变成"每项都省略号" */
  .settings-nav-item { width: auto; flex: none; white-space: nowrap; }
  /* 横排时左侧竖条会顶到文字：当前项由浅底 + 强调色表达，竖条关掉 */
  .settings-nav-item::before { display: none; }
  /* 横排时分组标题只剩噪声（一段一段断开反而更难扫），直接不显示 */
  .settings-nav-group { display: none; }
  /* 页头收紧留白，但不贴边（左右留白由 .app-main 的 12px 兜着） */
  .pane-head { padding: 0 0 12px; }
  .pane-head h2 { font-size: calc(17px * var(--ts, 1)); }
}

/* 分区：只放内容（标题在窗格抬头）。卡片间距交给 base.css 的 `.card + .card`，
   这里不叠 gap —— 两处都管会出现 22px 的"缝"，看着像两组内容。 */
.sec { min-width: 0; scroll-margin-top: calc(var(--header-h) + 62px); }
.sec-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }

/* 卡片两列（短卡并排；长表要整宽就自己加 grid-column: 1 / -1）。
   间距只由 gap 给 —— base.css 的 .card + .card 在这里会把第二张顶下去 12px，要抹掉。 */
.cards-2 {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px; align-items: start;
}
.cards-2 > .card + .card { margin-top: 0; }
@media (max-width: 1100px) {
  .cards-2 { grid-template-columns: minmax(0, 1fr); }
}

/* 表单项：标签一行 */
.field-top { display: flex; align-items: baseline; gap: 8px; width: 100%; }
.field-top.inline { align-items: center; }

/* 只读值 / 健康信息的长路径：占一半宽度并省略 */
.ro-value { max-width: 56%; }

/* 在线解析（MinerU）：卡片内的行间距与验证结论的语义色 */
.row-gap { margin-top: 8px; }
/* token 输入框不跟着 `.field > input` 的 340px 上限走：token 很长，宽一点更好核对 */
.field input[type="password"] { max-width: 100%; width: 100%; font-family: var(--mono); }
.field-block { margin-top: 8px; }
/* 验证结论：成功走 --ok、失败走 --danger（与徽章同一套语义色，不用新色） */
.verify-msg { margin: 8px 0 0; line-height: 1.6; }
.verify-msg.ok { color: var(--ok); }
.verify-msg.bad { color: var(--danger); }
</style>
