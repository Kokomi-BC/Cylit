<script setup lang="ts">
/**
 * 设置页「任务队列」面板：任务中心的精简版 —— 进度、状态、取消 / 重试 / 删除，
 * 外加一枚实时指示灯。日志、对账、清理、类型与队列双筛选都没搬进来。
 *
 * 取数**复用** `@/stores/app` 的 `loadTasks`（一次写回 `state.tasks` 与 `state.taskStats`），
 * 实时**复用** `useEventStream`（SSE 连续失败自动降级 3s 轮询，组件卸载即停）：
 * 挂载拉一次，之后全交给事件流与轮询，这里不另起一套请求。
 */
import { computed, onMounted, ref } from 'vue';
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
  { value: 'done', label: '已完成' },
  { value: 'failed', label: '失败' },
];

/** 降级轮询间隔。 */
const POLL_MS = 3000;

const status = ref('');
const busyKey = ref('');
/** 实时三态：压成一枚小圆点，不占一行字。 */
const live = ref<'sse' | 'poll' | 'offline'>('sse');

const tasks = computed<TaskInfo[]>(() => state.tasks);
const firstLoad = computed<boolean>(() => !!state.loading.tasks && !tasks.value.length);
/** 计数直接数**已加载的这一批**：SSE 打补丁后立刻跟着变，不必再单独请求统计接口。 */
const segments = computed(() => STATUS_SEGMENTS.map((s) => ({
  ...s,
  count: s.value ? tasks.value.filter((t) => t.status === s.value).length : tasks.value.length,
})));
const shown = computed<TaskInfo[]>(() => (status.value
  ? tasks.value.filter((t) => t.status === status.value)
  : tasks.value));
const liveText = computed(() => ({ sse: '实时推送', poll: '轮询中', offline: '已离线' }[live.value]));

function typeLabel(t?: string | null): string {
  const key = String(t ?? '');
  return TASK_TYPE_LABEL[key] || key || '—';
}

function titleOf(t: TaskInfo): string {
  return t.paper_id ? paperTitle(t.paper_id) : typeLabel(t.type);
}

function progressOf(t: TaskInfo): number {
  return t.progress || pct(t.done_items, t.total_items);
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
    <p class="panel-note">看任务跑到哪儿，也能取消或重试。</p>

    <div class="grid tq-grid">
      <!-- 状态筛选：六枚分段，短卡 -->
      <div class="card">
        <div class="card-head">按状态看</div>
        <div class="card-body">
          <div class="seg tq-seg">
            <button v-for="s in segments" :key="s.value || 'all'"
                    :class="{ active: status === s.value }" @click="status = s.value">
              {{ s.label }} <span class="tq-n tnum">{{ s.count }}</span>
            </button>
          </div>
        </div>
      </div>

      <!-- 实时与刷新：短卡 -->
      <div class="card">
        <div class="card-head">实时</div>
        <div class="card-body tq-live-card">
          <p class="tq-hint">
            <span class="tq-live" :class="live" :title="liveText" :aria-label="liveText" />
            {{ liveText }}
          </p>
          <button class="btn sm" :disabled="state.loading.tasks" @click="refresh(false)">
            <AppIcon name="refresh" :size="15" />刷新
          </button>
        </div>
      </div>

      <!-- 任务列表：长内容，整宽 -->
      <div class="card tq-card-wide" v-if="firstLoad">
        <p class="empty">正在加载任务…</p>
      </div>
      <div class="card tq-card-wide" v-else-if="!shown.length">
        <p class="empty">{{ status ? '这个状态下没有任务。' : '暂无任务。' }}</p>
      </div>
      <div class="card tq-card-wide" v-else>
        <table class="data fixed">
          <thead>
            <tr>
              <th>任务</th>
              <th class="w-type">类型</th>
              <th class="w-progress">进度</th>
              <th class="w-status">状态</th>
              <th class="w-time">更新</th>
              <th class="w-ops">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in shown" :key="t.id">
              <td><span class="truncate" :title="titleOf(t)">{{ titleOf(t) }}</span></td>
              <td class="tiny muted">{{ typeLabel(t.type) }}</td>
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
              <td class="tiny faint nowrap">{{ fmtRelative(t.updated_at || t.created_at) }}</td>
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
    </div>
  </div>
</template>

<style scoped>
.tq { display: flex; flex-direction: column; gap: 12px; }

/* 顶部一句话：一行说完这一块能干什么，不解释做法 */
.panel-note {
  margin: 0; font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
}

/* 卡片网格：短卡两列并排，长内容整宽。间距只由 `.grid` 的 gap 负责，卡片不再加 margin。 */
.tq-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; }
.tq-card-wide { grid-column: 1 / -1; }

/* 六枚分段放进半宽卡：允许换行，否则会被挤出卡片 */
.tq-seg { width: 100%; flex-wrap: wrap; height: auto; }
/* 分段里的计数：比按钮正文淡一档，不跟标签抢注意力 */
.tq-n { color: var(--text-faint); font-weight: 500; }
.seg button.active .tq-n { color: inherit; opacity: .65; }

/* 实时卡：一盏灯 + 一句状态，下面一枚刷新 */
.tq-live-card { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }
.tq-hint {
  display: flex; align-items: center; gap: 7px; margin: 0;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-dim);
}
/* 实时指示灯：三态只用颜色表达（--ok / --warn / 灰），文字进 title */
.tq-live {
  flex: none; width: 7px; height: 7px; border-radius: 999px;
  background: var(--text-faint);
}
.tq-live.sse { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 16%, transparent); }
.tq-live.poll { background: var(--warn); }

.tq-prog {
  display: flex; align-items: baseline; gap: 6px;
  margin-bottom: 4px; color: var(--text-dim);
}
.w-type { width: 96px; }
.w-progress { width: 132px; }
.w-status { width: 92px; }
.w-time { width: 84px; }
.w-ops { width: 108px; }
/* 固定列宽的表里，单元格内容要能被截断才不顶破列 */
td .truncate { display: block; }

/* 窄一档：卡片回落单列，短卡也竖着排 */
@media (max-width: 1100px) {
  .tq-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
