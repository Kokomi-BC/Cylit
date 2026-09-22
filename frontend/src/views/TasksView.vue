<script setup lang="ts">
/**
 * 任务中心：状态筛选、进度、暂停 / 取消 / 继续 / 重试、日志与实验指标。
 *
 * 实时性：优先 SSE（task_progress 事件流）；连续失败后降级为轮询，SSE 恢复后自动回切。
 * `useEventStream` 在组件卸载时自动关闭 SSE 与轮询。
 */

import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import { api, sseUrl } from '@/api/client';
import type { TaskInfo } from '@/api/types';
import { useEventStream, type StreamStatus } from '@/composables/useEventStream';
import {
  loadPapers, loadTasks, paperTitle, reportError, state, toast, withLoading,
} from '@/stores/app';
import {
  TASK_TYPE_LABEL, fmtRelative, pct, taskStatus, truncate,
} from '@/utils/format';
import Modal from '@/components/Modal.vue';
import ProgressBar from '@/components/ProgressBar.vue';
import AppIcon from '@/components/AppIcon.vue';

/** 筛选项（顺序即界面顺序）。 */
interface FilterOption {
  value: string;
  label: string;
}

/** 状态筛选段（顺序即界面顺序）。 */
const STATUS_SEGMENTS: FilterOption[] = [
  { value: '', label: '全部' },
  { value: 'pending', label: '排队中' },
  { value: 'processing', label: '执行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'done', label: '已完成' },
  { value: 'failed', label: '失败' },
];

const QUEUE_OPTIONS: FilterOption[] = [
  { value: 'system', label: 'system 系统' },
  { value: 'md', label: 'md 翻译' },
  { value: 'pdf', label: 'pdf 版面' },
  { value: 'knowledge', label: 'knowledge 知识库' },
];

/** 降级轮询间隔。 */
const POLL_MS = 3000;
/** 统计合并窗口：SSE 流式进度时避免高频请求。 */
const STATS_DEBOUNCE_MS = 2500;

const status = ref('');
const type = ref('');
const queue = ref('');
const busyKey = ref('');
const reconciling = ref(false);

/** 实时性三态：sse / poll / offline。 */
const live = ref<'sse' | 'poll' | 'offline'>('sse');
const logOpen = ref(false);
const logLoading = ref(false);
const logError = ref('');
const logTask = ref<TaskInfo | null>(null);

let statsTimer: ReturnType<typeof setTimeout> | null = null;

const tasks = computed<TaskInfo[]>(() => state.tasks);
const total = computed<number>(() => state.taskStats?.total || 0);
const byStatus = computed<Record<string, number>>(() => state.taskStats?.by_status || {});
const firstLoad = computed<boolean>(() => !!state.loading.tasks && !tasks.value.length);

/** 每个状态筛选按钮上的计数徽章（全部 = stats.total）。 */
const segments = computed(() => STATUS_SEGMENTS.map((s) => ({
  ...s,
  count: s.value ? (byStatus.value[s.value] || 0) : total.value,
})));

/** 类型下拉：值为后端枚举，显示中文名。 */
const typeOptions = computed<FilterOption[]>(() => Object.entries(TASK_TYPE_LABEL)
  .map(([value, label]) => ({ value, label })));

function typeLabel(t?: string | null): string {
  const key = String(t ?? '');
  return TASK_TYPE_LABEL[key] || key || '未知';
}

function statusCount(key: string): number {
  return byStatus.value[key] || 0;
}

/** 可操作状态集合（SFC 里模板是真正的表达式，条件直接写 `&&` / includes 即可）。 */
const CANCELLABLE_STATUS: string[] = ['pending', 'processing', 'paused'];
const RETRYABLE_STATUS: string[] = ['failed', 'cancelled', 'paused'];

function progressOf(t: TaskInfo): number {
  return t.progress || pct(t.done_items, t.total_items);
}

/* ---------------------- 统计 / 断点值的呈现 ---------------------- */

function formatStatValue(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'number') {
    return Number.isInteger(v) ? String(v) : String(Math.round(v * 1000) / 1000);
  }
  if (Array.isArray(v)) return v.length ? v.join(', ') : '—';
  if (typeof v === 'object') {
    const inner = flattenStats(v);
    return inner.length ? inner.map((kv) => `${kv[0]}=${kv[1]}`).join(' / ') : '—';
  }
  return String(v);
}

/** 把可能嵌套的统计对象摊平成 [路径, 值]（如 cache.hit_rate / scheduler.window）。 */
function flattenStats(obj: any, prefix = '', depth = 0): Array<[string, string]> {
  if (!obj || typeof obj !== 'object' || depth > 4) return [];
  const out: Array<[string, string]> = [];
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      out.push(...flattenStats(v, key, depth + 1));
    } else {
      out.push([key, formatStatValue(v)]);
    }
  }
  return out;
}

function oneValue(v: unknown): string {
  return formatStatValue(v);
}

const logStats = computed<Array<[string, string]>>(() => flattenStats(logTask.value?.stats));
const hasLogStats = computed<boolean>(() => logStats.value.length > 0);
const logCursorKeys = computed<string[]>(() => Object.keys(logTask.value?.cursor || {}));
const logParamKeys = computed<string[]>(() => Object.keys(logTask.value?.params || {}));
const logTail = computed<string>(() => (logTask.value?.log_tail || []).join('\n'));

/* ------------------------------ 加载 ------------------------------ */

async function refresh(silent = false) {
  const params: Record<string, unknown> = {
    status: status.value || undefined,
    type: type.value || undefined,
    queue: queue.value || undefined,
    limit: 200,
  };
  try {
    await withLoading('tasks', () => loadTasks(params), { silent });
  } catch {
    // withLoading 非静默时已提示；静默（轮询降级）时不打断界面
  }
}

/** 顶部「刷新」：非静默，失败会弹错。 */
function goRefresh() {
  return refresh(false);
}

async function refreshStats() {
  try {
    state.taskStats = await api.get<{ by_status: Record<string, number>; total: number }>('/tasks/stats');
  } catch {
    // 统计非关键路径：静默失败，避免降级轮询时刷屏
  }
}

function scheduleStats() {
  if (statsTimer) return;
  statsTimer = setTimeout(() => {
    statsTimer = null;
    refreshStats();
  }, STATS_DEBOUNCE_MS);
}

/* ------------------------------ SSE ------------------------------ */

function onTaskEvent(_name: string, frame: any) {
  const task: TaskInfo | null = frame?.payload ?? frame;
  if (!task || typeof task !== 'object' || !task.id) return;
  const list = state.tasks;
  const i = list.findIndex((x) => x.id === task.id);
  const matches = (!status.value || task.status === status.value)
    && (!type.value || task.type === type.value)
    && (!queue.value || task.queue === queue.value);
  if (i >= 0) {
    if (matches) list.splice(i, 1, { ...list[i], ...task });
    else list.splice(i, 1);   // 状态变化后不再符合筛选
  } else if (matches) {
    list.unshift(task);
  }
  if (logTask.value && logTask.value.id === task.id) {
    logTask.value = { ...logTask.value, ...task };
  }
  scheduleStats();
}

/** SSE 状态 → 界面徽章（open=实时；polling/failed=降级或离线）。 */
function onStreamStatus(s: StreamStatus) {
  if (s === 'open') live.value = 'sse';
  else if (s === 'polling') live.value = 'poll';
  else if (s === 'failed') live.value = 'offline';
}

useEventStream({
  url: sseUrl('/tasks/stream'),
  events: ['task_progress'],
  onEvent: onTaskEvent,
  onStatus: onStreamStatus,
  // 降级轮询：与 SSE 恢复互斥，组件卸载时自动停止
  poll: () => refresh(true),
  pollInterval: POLL_MS,
});

/* ---------------------------- 任务操作 ---------------------------- */

async function act(task: TaskInfo, action: string, label: string, body: Record<string, unknown> = {}) {
  busyKey.value = `${action}:${task.id}`;
  try {
    const data = await api.post<any>(`/tasks/${task.id}/${action}`, body);
    toast(`${label}：${data?.message || task.id}`, 'ok');
    await refresh(true);
    refreshStats();
  } catch (err) {
    reportError(err, `${label}失败`);
  } finally {
    busyKey.value = '';
  }
}

const pause = (t: TaskInfo) => act(t, 'pause', '已请求暂停');
const cancel = (t: TaskInfo) => act(t, 'cancel', '已请求取消');
const resume = (t: TaskInfo) => act(t, 'resume', '已恢复');
const retry = (t: TaskInfo) => act(t, 'retry', '已重新排队');

async function reconcile() {
  reconciling.value = true;
  try {
    const task = await api.post<any>('/tasks/reconcile', {});
    toast(`已创建对账任务：${task?.id || ''}`, 'ok');
    await refresh(true);
    refreshStats();
  } catch (err) {
    reportError(err, '触发对账失败');
  } finally {
    reconciling.value = false;
  }
}

/** 打开日志抽屉：拉取详情（log_tail / cursor / stats / params）。 */
async function openLog(task: TaskInfo | null) {
  if (!task) return;
  logOpen.value = true;
  logLoading.value = true;
  logError.value = '';
  logTask.value = task;
  try {
    logTask.value = await api.get<TaskInfo>(`/tasks/${task.id}`);
  } catch (err: any) {
    logError.value = `任务详情加载失败：${err?.message || err}`;
    reportError(err, '加载任务详情失败');
  } finally {
    logLoading.value = false;
  }
}

/* ---------------------------- 生命周期 ---------------------------- */

onMounted(async () => {
  loadPapers().catch(() => {});          // 仅为把 paper_id 显示成标题
  await refresh();
  await refreshStats();
});

onUnmounted(() => {
  // SSE 与降级轮询由 useEventStream 负责关闭，这里只清统计合并定时器
  if (statsTimer) { clearTimeout(statsTimer); statsTimer = null; }
});

watch([status, type, queue], () => { refresh(true); });
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>任务中心</h1>
        <div class="sub">
          共 {{ total }} 个任务 · 进度推送 · 暂停 / 取消在块边界生效
        </div>
      </div>
      <div class="grow"></div>
      <div class="page-actions">
        <span class="badge" :class="live === 'sse' ? 'ok' : (live === 'poll' ? 'warn' : '')">
          {{ live === 'sse' ? '实时' : (live === 'poll' ? '轮询' : '离线') }}
        </span>
        <select v-model="queue" title="按队列筛选">
          <option value="">全部队列</option>
          <option v-for="q in QUEUE_OPTIONS" :key="q.value" :value="q.value">{{ q.label }}</option>
        </select>
        <select v-model="type" title="按任务类型筛选">
          <option value="">全部类型</option>
          <option v-for="t in typeOptions" :key="t.value" :value="t.value">{{ t.label }}</option>
        </select>
        <button class="btn sm" :disabled="state.loading.tasks" @click="goRefresh">
          <AppIcon name="refresh" :size="14" />{{ state.loading.tasks ? '刷新中…' : '刷新' }}
        </button>
        <button class="btn primary sm" :disabled="reconciling" @click="reconcile">
          <AppIcon name="layers" :size="14" />{{ reconciling ? '触发中…' : '触发对账' }}
        </button>
      </div>
    </div>

    <div class="toolbar">
      <div class="seg">
        <button v-for="s in segments" :key="s.value || 'all'"
                :class="{ active: status === s.value }" @click="status = s.value">
          {{ s.label }} <span class="badge">{{ s.count }}</span>
        </button>
      </div>
      <span class="spacer"></span>
      <span class="tiny faint">
        排队 {{ statusCount('pending') }} · 执行中 {{ statusCount('processing') }} ·
        暂停 {{ statusCount('paused') }} · 完成 {{ statusCount('completed') + statusCount('done') }} ·
        失败 {{ statusCount('failed') }} · 取消 {{ statusCount('cancelled') }}
      </span>
    </div>

    <div class="card">
      <div class="empty" v-if="firstLoad">正在加载任务…</div>

      <div class="empty" v-else-if="!tasks.length">
        <p>没有符合条件的任务。</p>
      </div>

      <table class="data" v-else>
        <thead>
          <tr>
            <th class="w-type">类型</th>
            <th class="w-paper">论文</th>
            <th class="w-status">状态</th>
            <th class="w-progress">进度</th>
            <th>消息</th>
            <th class="w-time">更新时间</th>
            <th class="w-ops">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in tasks" :key="t.id">
            <td>
              <div>{{ typeLabel(t.type) }}</div>
              <div class="tiny faint">队列 {{ t.queue || '—' }}<span v-if="t.queue_code"> ({{ t.queue_code }})</span></div>
              <div class="tiny faint mono">{{ t.id }}</div>
            </td>
            <td>
              <RouterLink v-if="t.paper_id"
                          :to="{ name: 'reader', params: { paperId: t.paper_id ?? '' } }"
                          :title="paperTitle(t.paper_id)">{{ paperTitle(t.paper_id) }}</RouterLink>
              <span v-else class="faint">—</span>
              <div class="tiny faint mono" v-if="t.paper_id">{{ t.paper_id }}</div>
            </td>
            <td>
              <span class="badge" :class="taskStatus(t.status).cls">{{ taskStatus(t.status).label }}</span>
              <div class="tiny warn" v-if="t.pause_requested && t.status === 'processing'">待暂停</div>
              <div class="tiny danger" v-if="t.cancel_requested">待取消</div>
            </td>
            <td>
              <div class="row tiny faint">
                <span>{{ t.done_items || 0 }} / {{ t.total_items || 0 }}</span>
                <span class="spacer"></span>
                <span>{{ progressOf(t) }}%</span>
              </div>
              <ProgressBar :value="progressOf(t)" :ok-at="100" />
            </td>
            <td>
              <div class="small" :title="t.message || ''">{{ truncate(t.message || '—', 90) }}</div>
              <div class="tiny danger" v-if="t.error" :title="t.error">{{ truncate(t.error, 90) }}</div>
            </td>
            <td class="tiny faint nowrap">{{ fmtRelative(t.updated_at || t.created_at) }}</td>
            <td>
              <div class="row wrap">
                <button class="btn sm" v-if="t.status === 'processing' && !t.pause_requested"
                        :disabled="busyKey === 'pause:' + t.id" @click="pause(t)">
                  <AppIcon name="pause" :size="14" />暂停
                </button>
                <button class="btn sm" v-if="t.status === 'paused'"
                        :disabled="busyKey === 'resume:' + t.id" @click="resume(t)">
                  <AppIcon name="play" :size="14" />继续
                </button>
                <button class="btn sm danger" v-if="CANCELLABLE_STATUS.includes(t.status) && !t.cancel_requested"
                        :disabled="busyKey === 'cancel:' + t.id" @click="cancel(t)">
                  <AppIcon name="stop" :size="14" />取消
                </button>
                <button class="btn sm" v-if="RETRYABLE_STATUS.includes(t.status)"
                        :disabled="busyKey === 'retry:' + t.id" @click="retry(t)">
                  <AppIcon name="refresh" :size="14" />重试
                </button>
                <button class="btn sm ghost" @click="openLog(t)">
                  <AppIcon name="document" :size="14" />日志
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <Modal :open="logOpen" wide
           :title="logTask ? ('任务详情 · ' + typeLabel(logTask.type) + ' · ' + logTask.id) : '任务详情'"
           @close="logOpen = false">
      <div class="empty" v-if="logLoading">正在加载详情…</div>
      <div class="empty log-error" v-else-if="logError">{{ logError }}</div>

      <template v-else-if="logTask">
        <div class="row wrap small gap-10">
          <span class="badge" :class="taskStatus(logTask?.status).cls">{{ taskStatus(logTask?.status).label }}</span>
          <span class="badge">队列 {{ logTask?.queue || '—' }}</span>
          <span class="badge">{{ logTask?.done_items || 0 }} / {{ logTask?.total_items || 0 }}</span>
          <span class="badge">{{ logTask?.progress || 0 }}%</span>
          <span class="badge danger" v-if="logTask?.cancel_requested">已请求取消</span>
          <span class="badge warn" v-if="logTask?.pause_requested">已请求暂停</span>
        </div>

        <div class="small muted" v-if="logTask?.message">{{ logTask?.message }}</div>
        <pre class="mono tiny log-pre" v-if="logTask?.error">{{ logTask?.error }}</pre>
        <div class="tiny faint">
          创建 {{ fmtRelative(logTask?.created_at) }} · 更新 {{ fmtRelative(logTask?.updated_at) }}
          <span v-if="logTask?.finished_at"> · 结束 {{ fmtRelative(logTask?.finished_at) }}</span>
        </div>

        <h3 class="gap-top">统计</h3>
        <div class="grid cols-2" v-if="hasLogStats">
          <div class="row small" v-for="kv in logStats" :key="kv[0]">
            <span class="mono faint truncate" :title="kv[0]">{{ kv[0] }}</span>
            <span class="spacer"></span>
            <b class="mono">{{ kv[1] }}</b>
          </div>
        </div>
        <p class="tiny faint" v-else>暂无统计。</p>

        <h3 class="gap-top">断点 cursor</h3>
        <div class="grid cols-2" v-if="logCursorKeys.length">
          <div class="row small" v-for="k in logCursorKeys" :key="k">
            <span class="mono faint truncate">{{ k }}</span>
            <span class="spacer"></span>
            <b class="mono">{{ oneValue(logTask?.cursor?.[k]) }}</b>
          </div>
        </div>
        <p class="tiny faint" v-else>无断点信息。</p>

        <h3 class="gap-top">参数 params</h3>
        <div class="grid cols-2" v-if="logParamKeys.length">
          <div class="row small" v-for="k in logParamKeys" :key="k">
            <span class="mono faint truncate">{{ k }}</span>
            <span class="spacer"></span>
            <b class="mono">{{ oneValue(logTask?.params?.[k]) }}</b>
          </div>
        </div>
        <p class="tiny faint" v-else>无参数。</p>

        <h3 class="gap-top">日志尾部</h3>
        <pre class="mono tiny log-pre tall">{{ logTail || '（暂无日志）' }}</pre>
      </template>

      <template #footer>
        <button class="btn sm" @click="logOpen = false">
          <AppIcon name="close" :size="14" />关闭
        </button>
        <button class="btn primary sm" v-if="logTask" @click="openLog(logTask)">
          <AppIcon name="refresh" :size="14" />刷新详情
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* 筛选条（新版全局样式未包含 .toolbar，这里补齐旧实现的行为） */
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.w-type { width: 132px; }
.w-paper { width: 190px; }
.w-status { width: 96px; }
.w-progress { width: 210px; }
.w-time { width: 104px; }
.w-ops { width: 236px; }
.gap-top { margin-top: 14px; }
.gap-10 { margin-top: 10px; }
.log-error { color: var(--danger); }
.log-pre {
  margin: 6px 0;
  padding: 8px 10px;
  background: var(--bg-sunken);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  overflow: auto;
}
.log-pre.tall { max-height: 280px; }
</style>
