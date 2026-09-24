<script setup lang="ts">
/**
 * 设置：**分区**的参数编辑 + 只读系统信息与健康检查。
 *
 * 分区（顶部悬浮药丸导航可跳，支持 `/app/settings#kb` 这类深链）：
 * 1. **常规参数** —— 任务线程数 / 渲染 / RAG 参数 / 其他；**只有这一区有"保存设置"**
 *    （数据面 `PUT /settings`，仅提交变更项）。
 * 2. **知识库与索引** —— 概览统计、论文索引表、索引状态与对账、chunk 元数据。
 *    这些原本长在「知识库」页上，用户口径是"那页全是设置"，于是整体搬到这里；
 *    它们都是**即时操作**，没有"保存"这一步。
 * 3. **搜索记录** —— 知识库搜索历史的本地管理（localStorage）。
 * 4. **系统信息** —— 只读信息 + 健康检查（`GET /settings/health`）。
 *
 * 数据面：`GET /settings`（values + stored + editable 白名单）、`PUT /settings`、
 * `GET /settings/health`。设置写入 setting 表，重启后仍生效。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api } from '@/api/client';
import type { HealthPayload } from '@/api/types';
import {
  loadHealth, loadSettings, reportError, state, toast, withLoading,
} from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';
import KbIndexPanel from '@/components/settings/KbIndexPanel.vue';
import SearchHistoryPanel from '@/components/settings/SearchHistoryPanel.vue';

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

/** 分区：`id` 同时是锚点（`/app/settings#kb` 直接落到"知识库与索引"）。 */
const SECTIONS = [
  { id: 'general', label: '常规参数', icon: 'settings' },
  { id: 'kb', label: '知识库与索引', icon: 'knowledge' },
  { id: 'history', label: '搜索记录', icon: 'search' },
  { id: 'about', label: '系统信息', icon: 'server' },
];

const route = useRoute();
const router = useRouter();
const active = ref<string>(SECTIONS[0].id);

/* ------------------------- 锚点对齐（异步内容会顶偏落点） ------------------------- */

/**
 * **为什么要一套对齐逻辑**：本页内容（统计数字、论文索引表、chunk 表）都是异步长出来的。
 * 只在跳转那一刻滚一次，滚动会发生在数据到达**之前** —— 上面那一节随后变高，把目标分区往下顶。
 * 实测（把网络拖慢到 400ms/请求、从其它页面深链到 `#history`）：
 * 滚完是可见的，等 50 行 chunk 表到达后文档高度从 2384 → 4893，
 * `#history` 被顶到视口下方 **3087px** —— 用户"跳到了搜索记录"，屏幕上却是别的分区。
 *
 * 这和阅读器"深链与恢复位置抢滚动"是同一类坑：**滚动必须发生在布局稳定之后**。
 * 做法：跳转后开一个**有界**的对齐循环（250ms 一次，最长 6 秒），
 * 位置偏了就补一次；用户任何真实手势（滚轮 / 触摸 / 按键 / 按下鼠标）立刻停手，绝不跟人抢。
 */
const ALIGN_INTERVAL = 250;    // 轮询间隔
const ALIGN_HOLD_MS = 6000;    // 最长对齐窗口：之后页面爱怎么长都不再动用户的滚动
const ALIGN_TOLERANCE = 6;     // 偏差容忍（px）
const pendingAlign = ref('');
let alignTimer: number | null = null;
let alignDeadline = 0;

/** 分区落点偏移：**取自样式**（`.sec` 的 `scroll-margin-top`），改导航高度不用回来改 JS。 */
function sectionOffset(el: HTMLElement): number {
  const raw = getComputedStyle(el).scrollMarginTop;
  const n = Number.parseFloat(raw);
  return Number.isFinite(n) ? n : 0;
}

/** 对齐到某分区的目标滚动位置（页面到底时**夹到最大可滚位置** —— 最后两节本来就够不到顶部）。 */
function alignTarget(el: HTMLElement): number {
  const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
  const raw = el.getBoundingClientRect().top + window.scrollY - sectionOffset(el);
  return Math.min(Math.max(0, raw), max);
}

function scrollToSection(id: string) {
  const el = document.getElementById(id);
  if (!el) return;
  // 瞬时跳转：平滑滚动会和"内容长出来后纠偏"互相打架（纠偏会打断动画），
  // 而页内锚点跳转本来就是瞬时的（浏览器原生 `#anchor` 也是）。
  window.scrollTo({ top: alignTarget(el), behavior: 'auto' });
}

function stopAlign() {
  if (alignTimer) { window.clearTimeout(alignTimer); alignTimer = null; }
  pendingAlign.value = '';
}

function alignTick() {
  alignTimer = null;
  const id = pendingAlign.value;
  if (!id || Date.now() > alignDeadline) { stopAlign(); return; }
  const el = document.getElementById(id);
  if (el) {
    const want = alignTarget(el);
    if (Math.abs(window.scrollY - want) > ALIGN_TOLERANCE) {
      window.scrollTo({ top: want, behavior: 'auto' });
    }
  }
  alignTimer = window.setTimeout(alignTick, ALIGN_INTERVAL);
}

/** 跳转后登记目标：接下来一段时间内持续纠偏，直到用户自己动手。 */
function markPending(id: string) {
  pendingAlign.value = id;
  alignDeadline = Date.now() + ALIGN_HOLD_MS;
  if (alignTimer) window.clearTimeout(alignTimer);
  alignTimer = window.setTimeout(alignTick, ALIGN_INTERVAL);
}

/** 用户的手势：停掉对齐，并从这一刻起让高亮跟滚动位置走。 */
function onUserGesture() {
  userScrolled = true;
  stopAlign();
}

/** 点分区：改地址栏的 hash（用 replace，不堆历史）并滚过去。 */
function go(id: string) {
  active.value = id;
  userScrolled = false;          // 这次跳转由我们接管高亮，别让滚动过程改写它
  if (route.hash !== `#${id}`) void router.replace({ hash: `#${id}` });
  scrollToSection(id);
  markPending(id);
}

/**
 * 高亮当前分区。
 *
 * **只在"用户自己滚"的时候重算**：点药丸 / 深链是**有明确意图**的跳转，滚动过程中
 * 经过谁就点亮谁的话，落点还没到、高亮已经跳到别人身上了；更糟的是最后两节
 * （搜索记录 / 系统信息）本来就不可能滚到顶部（后面没有内容了，最多滚到页面底部）——
 * 那种情况下按位置重算会把"我点的搜索记录"直接改成"系统信息"。
 */
let scrollRaf = 0;
let userScrolled = false;     // 用户自己滚动过才允许位置判定改写高亮

function onScroll() {
  if (!userScrolled) return;
  if (scrollRaf) return;
  scrollRaf = requestAnimationFrame(() => {
    scrollRaf = 0;
    const line = window.scrollY + 126;   // 顶栏 + 悬浮导航 + 一点余量
    let current = SECTIONS[0].id;
    for (const s of SECTIONS) {
      const el = document.getElementById(s.id);
      if (el && el.getBoundingClientRect().top + window.scrollY <= line) current = s.id;
    }
    // 滚到底时最后一节可能永远够不到那条线（页面已经不能再滚），直接认它
    const atBottom = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 4;
    if (atBottom) current = SECTIONS[SECTIONS.length - 1].id;
    active.value = current;
  });
}

watch(() => route.hash, (hash) => {
  const id = String(hash || '').replace('#', '');
  if (!SECTIONS.some((s) => s.id === id)) return;
  active.value = id;
  userScrolled = false;
  void nextTick(() => {
    scrollToSection(id);
    markPending(id);
  });
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

onMounted(async () => {
  await load();
  await loadHealthCard();
  // 深链（/app/settings#kb）：内容按锚点滚过去；首屏不做平滑滚动，避免刚进来就"滑一段"。
  // 数据还在路上，所以同时 markPending —— 面板 settled 时会再对一次。
  const id = String(route.hash || '').replace('#', '');
  if (id && SECTIONS.some((s) => s.id === id)) {
    active.value = id;
    userScrolled = false;
    await nextTick();
    scrollToSection(id);
    markPending(id);
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  // 用户一动，就停掉自动对齐（不跟人抢滚动条），高亮也从那一刻起跟位置走
  window.addEventListener('wheel', onUserGesture, { passive: true });
  window.addEventListener('touchstart', onUserGesture, { passive: true });
  window.addEventListener('pointerdown', onUserGesture, { passive: true });
  window.addEventListener('keydown', onUserGesture);
});

onUnmounted(() => {
  window.removeEventListener('scroll', onScroll);
  window.removeEventListener('wheel', onUserGesture);
  window.removeEventListener('touchstart', onUserGesture);
  window.removeEventListener('pointerdown', onUserGesture);
  window.removeEventListener('keydown', onUserGesture);
  if (scrollRaf) cancelAnimationFrame(scrollRaf);
  stopAlign();
});
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>设置</h1>
        <div class="sub">参数、知识库索引与搜索记录。带「保存设置」的只有<strong>常规参数</strong>，其余是即时操作。</div>
      </div>
    </div>

    <nav class="sec-nav" aria-label="设置分区">
      <button
        v-for="s in SECTIONS"
        :key="s.id"
        type="button"
        :class="{ active: active === s.id }"
        @click="go(s.id)"
      >
        <AppIcon :name="s.icon" :size="14" />{{ s.label }}
      </button>
    </nav>

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
      <section id="general" class="sec">
        <div class="sec-head">
          <h2 class="sec-title">常规参数</h2>
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

      <!-- ② 知识库与索引（管理/诊断，即时生效，没有"保存"） -->
      <section id="kb" class="sec">
        <div class="sec-head">
          <h2 class="sec-title">知识库与索引</h2>
          <span class="tiny faint">建索引、索引状态与 chunk 元数据；知识库页只负责检索与浏览。</span>
        </div>
        <KbIndexPanel />
      </section>

      <!-- ③ 搜索记录（本机 localStorage） -->
      <section id="history" class="sec">
        <div class="sec-head">
          <h2 class="sec-title">搜索记录</h2>
          <span class="tiny faint">知识库页每次检索记一条，只存在本机浏览器。</span>
        </div>
        <SearchHistoryPanel />
      </section>

      <!-- ④ 系统信息（只读） -->
      <section id="about" class="sec">
        <div class="sec-head">
          <h2 class="sec-title">系统信息</h2>
        </div>
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
</template>

<style scoped>
/* 设置页私有细节（其余样式复用 base.css 的通用类） */
.load-error { color: var(--danger); }
.hint { margin: 10px 0 0; }
.health-row { margin-top: 8px; }

/**
 * 分区药丸导航：**吸顶悬浮**（页面很长，滚到下面也要能跳回去）。
 * `top` 让开顶栏高度；底色必须不透明，否则正文会从底下透出来。
 */
.sec-nav {
  position: sticky; top: calc(var(--header-h) + 8px); z-index: 20;
  display: inline-flex; align-items: center; gap: 2px; padding: 4px;
  margin-bottom: 4px;
  background: var(--bg-elev); border: 1px solid var(--border);
  border-radius: var(--radius-ctl); box-shadow: var(--shadow-1);
}
.sec-nav button {
  display: inline-flex; align-items: center; gap: 6px;
  height: calc(var(--control-h-sm) + 2px); padding: 0 12px;
  border: 0; border-radius: var(--radius-ctl); background: transparent;
  color: var(--text-dim); font-family: inherit; font-size: 12.5px; font-weight: 500;
  cursor: pointer; white-space: nowrap;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.sec-nav button:hover { background: var(--bg-hover); color: var(--text); }
.sec-nav button.active { background: var(--accent-soft); color: var(--accent-text); font-weight: 600; }

/* 分区：标题行 + 内容；锚点落点用 scroll-margin 兜底（浏览器原生滚动也不会被顶栏盖住） */
.sec { margin-top: 22px; scroll-margin-top: calc(var(--header-h) + 62px); }
.sec-head { display: flex; align-items: center; gap: 8px; margin: 0 0 10px; flex-wrap: wrap; }
.sec-title { font-size: 15px; margin: 0; }

/* 表单项：标签与"后端的键名"同一行，键名右对齐靠灰，不再占一整行高度 */
.field-top { display: flex; align-items: baseline; gap: 8px; width: 100%; }
.field-top.inline { align-items: center; }
.field-key {
  margin-left: auto; font-family: var(--mono); font-size: 11px; font-weight: 400;
  color: var(--text-faint);
}

/* 只读值 / 健康信息的长路径：占一半宽度并省略 */
.ro-value { max-width: 56%; }
</style>
