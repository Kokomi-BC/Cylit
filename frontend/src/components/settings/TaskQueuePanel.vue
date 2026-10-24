<script setup lang="ts">
/**
 * 设置页「任务队列」：一张可搜索 / 可筛选 / 可排序 / 可分页的任务表。
 *
 * 取数**复用** `@/stores/app` 的 `loadTasks`（一次写回 `state.tasks` 与 `state.taskStats`，limit 200），
 * 实时**复用** `useEventStream`（SSE 连续失败自动降级 3s 轮询，组件卸载即停）。
 *
 * **为什么搜索/筛选/排序/分页都在前端做**：后端 `GET /tasks` 只支持 status/type/queue/paper_id 与
 * limit，没有 q / offset / order。而实时推送是往 `state.tasks` 里**就地打补丁**的 ——
 * 一旦换成服务端分页，每帧补丁都得重新问一次"当前页"，实时性会被翻页逻辑拖散。
 * 所以口径是：**在完整任务集合上做搜索 → 筛选 → 排序 → 最后才分页**，
 * 任何一层的条件变化都把页码归 1（避免停在超出结果范围的那一页）。
 *
 * 状态计数按"搜索 + 类型 + 仅我创建"过滤后统计（**不含**状态筛选本身，
 * 否则每个分段都会显示成它自己那一份数量，看不出全局分布）；这条口径写在工具条的提示里。
 */
import { computed, onMounted, ref, watch } from 'vue';
import { api, sseUrl } from '@/api/client';
import type { TaskInfo } from '@/api/types';
import { useEventStream, type StreamStatus } from '@/composables/useEventStream';
import { loadTasks, paperTitle, reportError, state, toast, withLoading } from '@/stores/app';
import { TASK_TYPE_LABEL, fmtRelative, pct, taskStatus } from '@/utils/format';
import { askConfirm } from '@/stores/confirm';
import ProgressBar from '@/components/ProgressBar.vue';
import AppIcon from '@/components/AppIcon.vue';

/** 状态分段（顺序即界面顺序）。 */
const STATUS_SEGMENTS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '排队中' },
  { value: 'processing', label: '执行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
];

/** 降级轮询间隔。 */
const POLL_MS = 3000;

/** 每页条数：只影响"当前看多少行"，不改变取数范围（200 条一次拿全）。 */
const PAGE_SIZES = [10, 20, 50] as const;

const status = ref('');
const keyword = ref('');
const typeFilter = ref('');
const sortDesc = ref(true);
const page = ref(1);
const pageSize = ref<number>(10);
const busyKey = ref('');
/** 实时三态：压成一枚小圆点，不占一行字。 */
const live = ref<'sse' | 'poll' | 'offline'>('sse');

const tasks = computed<TaskInfo[]>(() => state.tasks);
const firstLoad = computed<boolean>(() => !!state.loading.tasks && !tasks.value.length);
const liveText = computed(() => ({ sse: '实时推送', poll: '轮询中', offline: '已离线' }[live.value]));

/** 任务类型的可读名：比 TASK_TYPE_LABEL 更贴"这一行在干什么"，没收录的退回原文。 */
const TYPE_READABLE: Record<string, string> = {
  IMPORT: '文献导入',
  PARSE: '文献解析',
  TRANSLATE: '翻译处理',
  TRANSLATE_MD: 'Markdown 翻译',
  INDEX: '索引构建',
  RECONCILE: '索引对账',
  ANALYSIS: 'AI 分析',
  PDF_TRANSLATE: '版面翻译',
  EXPORT: '导出',
  META_FILL: '元数据补全',
  TRANSCRIBE_EN: '英文转写',
  TRANSLATE_EN: '英文翻译',
  TRANSLATE_ZH: '中文翻译',
  UNDERSTAND_A: '论文理解 A',
  UNDERSTAND_B: '论文理解 B',
  FOLDER_SYNC: '分类同步',
};

function typeLabel(t?: string | null): string {
  const key = String(t ?? '');
  return TYPE_READABLE[key] || TASK_TYPE_LABEL[key] || key || '—';
}

/** 类型筛选的选项：只列当前真的存在的类型（不摆一排空选项），按可读名排序。 */
const typeOptions = computed(() => {
  const seen = new Map<string, string>();
  for (const t of tasks.value) {
    const key = String(t.type ?? '');
    if (key && !seen.has(key)) seen.set(key, typeLabel(key));
  }
  return [...seen.entries()]
    .map(([value, label]) => ({ value, label }))
    .sort((a, b) => a.label.localeCompare(b.label, 'zh-Hans-CN'));
});

function titleOf(t: TaskInfo): string {
  return t.paper_id ? paperTitle(t.paper_id) : typeLabel(t.type);
}

/** 次级说明：这一行作用的对象（论文 id / 任务 id）。类型已在「类型」列，不重复。 */
function subOf(t: TaskInfo): string {
  return t.paper_id ? `论文 ${t.paper_id}` : `任务 ${t.id}`;
}

/** 类型列的技术标识：只有可读名与原始值不一致时才补（否则同一串会出现两次）。 */
function rawType(t: TaskInfo): string {
  const key = String(t.type ?? '');
  return key && key !== typeLabel(key) ? key : '';
}

/** 绝对时间：`YYYY-MM-DD HH:MM`（本地时区，与论文库/笔记的写法一致）。拿不到就给空串。 */
function fmtAbsolute(value?: string | null): string {
  const ts = Date.parse(String(value || ''));
  if (!Number.isFinite(ts)) return '';
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function progressOf(t: TaskInfo): number {
  return t.progress || pct(t.done_items, t.total_items);
}

/** 搜索命中的是"任务名 / 文件名 / 任务 ID"，与占位提示一致（都按小写子串比）。 */
function matches(t: TaskInfo, needle: string): boolean {
  if (!needle) return true;
  return titleOf(t).toLowerCase().includes(needle)
    || String(t.paper_id ?? '').toLowerCase().includes(needle)
    || String(t.type ?? '').toLowerCase().includes(needle)
    || typeLabel(t.type).toLowerCase().includes(needle)
    || t.id.toLowerCase().includes(needle);
}

/** 搜索 + 类型 + （没有创建者字段，故无"仅我创建"）—— 状态计数就看这一份。 */
const base = computed<TaskInfo[]>(() => {
  const needle = keyword.value.trim().toLowerCase();
  return tasks.value.filter((t) => matches(t, needle)
    && (!typeFilter.value || String(t.type ?? '') === typeFilter.value));
});

/** 状态计数：在 base 上数，全部 = base 的长度。 */
const segments = computed(() => STATUS_SEGMENTS.map((s) => ({
  ...s,
  count: s.value ? base.value.filter((t) => t.status === s.value).length : base.value.length,
})));

/** 排序：更新时间（缺失时退回创建时间），同值时用 id 稳定。 */
const sorted = computed<TaskInfo[]>(() => {
  const list = base.value.filter((t) => !status.value || t.status === status.value);
  const at = (t: TaskInfo) => Date.parse(String(t.updated_at || t.created_at || '')) || 0;
  return list.slice().sort((a, b) => (sortDesc.value ? at(b) - at(a) : at(a) - at(b)));
});

const total = computed(() => sorted.value.length);
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const paged = computed<TaskInfo[]>(() => {
  const start = (page.value - 1) * pageSize.value;
  return sorted.value.slice(start, start + pageSize.value);
});

/** 条件一变就把页码归 1：否则容易停在一个已经没有内容的页上。 */
watch([keyword, typeFilter, status, sortDesc, pageSize], () => { page.value = 1; });
/** 结果变少时也把页码夹回范围内（实时刷新删任务时会遇到）。 */
watch(pageCount, (n) => { if (page.value > n) page.value = n; });

const filtered = computed(() => !!keyword.value.trim() || !!typeFilter.value || !!status.value);
function clearFilters(): void {
  keyword.value = '';
  typeFilter.value = '';
  status.value = '';
}

/* 可操作状态集合（与后端状态机对齐）。 */
const CANCELLABLE = ['pending', 'processing', 'paused'];
const RETRYABLE = ['failed', 'cancelled', 'paused'];
const TERMINAL = ['completed', 'failed', 'cancelled'];

async function refresh(silent = false) {
  try {
    await withLoading('tasks', () => loadTasks(), { silent });
  } catch {
    // withLoading 非静默时已经提示过；轮询降级时保持安静
  }
}

async function act(t: TaskInfo, action: string, label: string) {
  busyKey.value = `${action}:${t.id}`;
  try {
    await api.post(`/tasks/${t.id}/${action}`, {});
    toast(label, 'ok', 2400);
    await refresh(true);
  } catch (err) {
    reportError(err, `${label}失败`);
  } finally {
    busyKey.value = '';
  }
}

async function remove(t: TaskInfo) {
  if (!(await askConfirm({
    title: `删除任务记录 ${t.id}？`,
    body: '只删这条记录，已产出的译文与索引不受影响。',
    danger: true,
    confirmLabel: '删除',
  }))) return;
  busyKey.value = `del:${t.id}`;
  try {
    await api.del(`/tasks/${t.id}`);
    toast('任务记录已删除', 'ok', 2400);
    await refresh(true);
  } catch (err) {
    reportError(err, '删除任务记录失败');
  } finally {
    busyKey.value = '';
  }
}

/* ------------------------------ 实时 ------------------------------ */

/** SSE 帧：正常进度是**一整行** `TaskInfo`；删除时后端只推一枚「墓碑」
 *  —— `{ id, deleted: true }`（见 `backend/services/task/task_service.py` 的 `delete()`）。 */
type TaskEvent = Partial<TaskInfo> & { id?: string; deleted?: boolean };

function onTaskEvent(_name: string, frame: any) {
  const task: TaskEvent | null = frame?.payload ?? frame;
  if (!task || typeof task !== 'object' || !task.id) return;
  const list = state.tasks;
  const i = list.findIndex((x) => x.id === task.id);
  // ① 墓碑 = 这条记录被删了：在列表里就移除，不在就什么都不做。
  //    ⚠️ 绝不能当新任务 unshift 进去 —— 墓碑只有 id，插进去渲染出来就是一行
  //    「任务 — / 类型 — / 0 / 0 / 未知」的空任务（用户报的 bug 正是这个）。
  if (task.deleted) {
    if (i >= 0) list.splice(i, 1);
    return;
  }
  // ② 只有**整行**（带 type 与 status）才允许插到最前面：残缺帧插进去同样是空白行。
  if (i >= 0) list.splice(i, 1, { ...list[i], ...task } as TaskInfo);
  else if (task.type && task.status) list.unshift(task as TaskInfo);
}

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
  poll: () => refresh(true),
  pollInterval: POLL_MS,
});

onMounted(() => { void refresh(); });
</script>

<template>
  <div class="tq">
    <!-- 工具栏：搜索 → 状态 → 类型 → 排序 → 实时与刷新，一行排完（窄了自动换行） -->
    <div class="tq-bar">
      <label class="tq-search">
        <AppIcon name="search" :size="14" />
        <input
          v-model="keyword"
          type="search"
          autocomplete="off"
          spellcheck="false"
          placeholder="搜索任务名称、文件名或任务 ID"
          data-task-search
        />
        <button v-if="keyword" type="button" class="tq-clear" title="清空搜索" @click="keyword = ''">
          <AppIcon name="close" :size="12" />
        </button>
      </label>

      <div class="seg tq-seg" role="group" aria-label="按状态筛选">
        <button v-for="s in segments" :key="s.value || 'all'"
                :class="{ active: status === s.value }" @click="status = s.value">
          {{ s.label }}<span class="tq-n tnum">{{ s.count }}</span>
        </button>
      </div>

      <div class="tq-right">
        <label class="tq-select">
          <AppIcon name="filter" :size="13" />
          <select v-model="typeFilter" aria-label="任务类型" data-task-type>
            <option value="">任务类型</option>
            <option v-for="o in typeOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </label>
        <label class="tq-select">
          <AppIcon name="clock" :size="13" />
          <select v-model="sortDesc" aria-label="更新时间排序" data-task-sort>
            <option :value="true">更新时间：新→旧</option>
            <option :value="false">更新时间：旧→新</option>
          </select>
        </label>
        <span class="tq-live-wrap" :title="liveText">
          <span class="tq-live" :class="live" aria-hidden="true" />
          <span class="tq-live-text">{{ liveText }}</span>
        </span>
        <button class="btn sm" :disabled="state.loading.tasks" @click="refresh(false)">
          <AppIcon name="refresh" :size="15" />刷新
        </button>
      </div>
    </div>

    <!-- 表：主体是这张表，行不套卡片；行间只留一条发丝线 -->
    <div class="tq-table-wrap">
      <table class="data fixed tq-table" v-if="firstLoad">
        <tbody><tr><td><p class="empty">正在加载任务…</p></td></tr></tbody>
      </table>
      <div class="tq-empty" v-else-if="!filtered && !total">
        <p class="empty">暂无任务。</p>
      </div>
      <div class="tq-empty" v-else-if="!total">
        <p class="empty">没有符合条件的任务。</p>
        <button class="btn sm" @click="clearFilters">清除筛选</button>
      </div>
      <table class="data fixed tq-table" v-else>
        <thead>
          <tr>
            <th>任务</th>
            <th class="w-type">类型</th>
            <th class="w-progress">进度</th>
            <th class="w-status">状态</th>
            <th class="w-time">更新时间</th>
            <th class="w-ops">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in paged" :key="t.id">
            <td>
              <div class="tq-name">
                <span class="truncate" :title="titleOf(t)">{{ titleOf(t) }}</span>
                <span class="tq-sub truncate" :title="subOf(t)">{{ subOf(t) }}</span>
              </div>
            </td>
            <td>
              <span class="tq-type">
                <span class="tq-type-label">{{ typeLabel(t.type) }}</span>
                <span class="tq-type-raw mono" v-if="rawType(t)" :title="t.type">{{ t.type }}</span>
              </span>
            </td>
            <td>
              <div class="tq-prog">
                <span class="tiny faint tnum">{{ t.done_items || 0 }} / {{ t.total_items || 0 }}</span>
                <span class="spacer"></span>
                <span class="tiny tnum">{{ progressOf(t) }}%</span>
              </div>
              <ProgressBar :value="progressOf(t)" :ok-at="100" />
            </td>
            <td>
              <span class="badge" :class="taskStatus(t.status).cls">{{ taskStatus(t.status).label }}</span>
            </td>
            <td class="tq-time">
              <!-- 两行：粗读用相对时间，核对用绝对时间（全站统一 YYYY-MM-DD HH:MM） -->
              <span class="tq-rel">{{ fmtRelative(t.updated_at || t.created_at) }}</span>
              <span class="tq-abs tnum">{{ fmtAbsolute(t.updated_at || t.created_at) }}</span>
            </td>
            <td class="ops">
              <div class="row nowrap" style="gap: 4px">
                <button class="btn icon sm" title="取消任务"
                      v-if="CANCELLABLE.includes(t.status) && !t.cancel_requested"
                      :disabled="busyKey === 'cancel:' + t.id" @click="act(t, 'cancel', '已请求取消')">
                  <AppIcon name="stop" :size="14" />
                </button>
                <button class="btn icon sm" title="重新排队"
                      v-if="RETRYABLE.includes(t.status)"
                      :disabled="busyKey === 'retry:' + t.id" @click="act(t, 'retry', '已重新排队')">
                  <AppIcon name="refresh" :size="14" />
                </button>
                <button class="btn icon sm" title="删除记录"
                      v-if="TERMINAL.includes(t.status)"
                      :disabled="busyKey === 'del:' + t.id" @click="remove(t)">
                  <AppIcon name="trash" :size="14" />
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 页脚：总数 + 每页条数 + 翻页 -->
    <footer class="tq-foot" v-if="total">
      <span class="tq-count">共 {{ total }} 条<template v-if="filtered">（已筛选）</template></span>
      <span class="spacer"></span>
      <label class="tq-size">
        每页
        <select v-model.number="pageSize" data-task-page-size>
          <option v-for="n in PAGE_SIZES" :key="n" :value="n">{{ n }}</option>
        </select>
        条
      </label>
      <div class="tq-pager">
        <button class="btn icon sm" title="上一页" :disabled="page <= 1" @click="page -= 1">
          <AppIcon name="chevron-left" :size="14" />
        </button>
        <span class="tq-page tnum">{{ page }} / {{ pageCount }}</span>
        <button class="btn icon sm" title="下一页" :disabled="page >= pageCount" @click="page += 1">
          <AppIcon name="chevron-right" :size="14" />
        </button>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.tq { display: flex; flex-direction: column; gap: 12px; }

/* ---------- 工具栏：一行装完，装不下才换行（不堆成三行大块） ---------- */
.tq-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

.tq-search {
  display: flex; align-items: center; gap: 8px;
  box-sizing: border-box; flex: 0 1 300px; min-width: 200px; height: var(--control-h);
  padding: 0 10px; border-radius: var(--r-sm);
  border: 1px solid var(--border); background: var(--bg-elev); color: var(--text-faint);
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.tq-search:focus-within { border-color: var(--accent); box-shadow: var(--ring); }
.tq-search input {
  flex: 1; min-width: 0; height: 100%; padding: 0; border: 0; background: transparent;
  border-radius: 0; box-shadow: none; font-size: calc(12.5px * var(--ts, 1)); color: var(--text);
}
.tq-search input:hover, .tq-search input:focus { background: transparent; border: 0; box-shadow: none; }
.tq-clear {
  flex: none; display: grid; place-items: center; width: 16px; height: 16px; padding: 0;
  border: 0; border-radius: 999px; background: var(--bg-hover); color: var(--text-faint); cursor: pointer;
}
.tq-clear:hover { color: var(--text); }

/* 状态分段：与全站 .seg 同档，计数比文字淡一档 */
.tq-seg { height: var(--control-h); flex: 0 1 auto; }
.tq-seg button { font-size: calc(12px * var(--ts, 1)); padding: 0 10px; }
.tq-n { margin-left: 5px; color: var(--text-faint); font-weight: 500; }
.seg button.active .tq-n { color: inherit; opacity: .65; }

.tq-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-left: auto; }
.tq-select {
  display: flex; align-items: center; gap: 6px; height: var(--control-h); padding: 0 8px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text-faint);
}
.tq-select select {
  height: 100%; padding: 0; border: 0; background: transparent; box-shadow: none;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); max-width: 150px;
}
.tq-select select:hover, .tq-select select:focus { background: transparent; border: 0; box-shadow: none; }

.tq-live-wrap { display: inline-flex; align-items: center; gap: 6px; }
.tq-live {
  flex: none; width: 7px; height: 7px; border-radius: 999px; background: var(--text-faint);
}
.tq-live.sse { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 16%, transparent); }
.tq-live.poll { background: var(--warn); }
.tq-live-text { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }

/* ---------- 表格 ---------- */
.tq-table-wrap { min-width: 0; }
.tq-table { margin: 0; }
.tq-table th, .tq-table td { vertical-align: middle; }
.tq-name { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.tq-name .truncate { display: block; font-weight: 500; }
.tq-sub {
  display: block; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}
.tq-type { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.tq-type-label { font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim); }
.tq-type-raw {
  font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); opacity: .8;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tq-prog { display: flex; align-items: baseline; gap: 6px; margin-bottom: 4px; color: var(--text-dim); }
.tq-time { white-space: nowrap; }
.tq-rel { display: block; font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.tq-abs { display: block; font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); opacity: .8; }
.w-type { width: 116px; }
.w-progress { width: 148px; }
.w-status { width: 96px; }
.w-time { width: 116px; }
.w-ops { width: 104px; }
td .truncate { display: block; }

.tq-empty { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 32px 0; }
.tq-empty .empty { margin: 0; }

/* ---------- 页脚 ---------- */
.tq-foot { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding-top: 10px; border-top: 1px solid var(--divider); }
.tq-count { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.tq-size { display: inline-flex; align-items: center; gap: 6px; font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.tq-size select {
  height: 26px; padding: 0 6px; border-radius: var(--r-sm);
  border: 1px solid var(--border); background: var(--bg-elev);
  font-size: calc(12px * var(--ts, 1)); color: var(--text-dim);
}
.tq-pager { display: inline-flex; align-items: center; gap: 4px; }
.tq-page { font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); min-width: 44px; text-align: center; }

/* 窄屏：工具栏三块纵向排，搜索框占满一行 */
@media (max-width: 900px) {
  .tq-search { flex: 1 0 100%; }
  .tq-seg { flex-wrap: wrap; height: auto; }
  .tq-right { margin-left: 0; flex: 1 0 100%; }
}
/* 更窄：更新时间那一列让位（绝对时间仍挂在 title 上），任务名与进度才有宽度 */
@media (max-width: 720px) {
  .tq-table th.w-time, .tq-table td.tq-time { display: none; }
}
</style>
