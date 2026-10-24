<script setup lang="ts">
/**
 * 设置页「系统信息」：一块**只读**面板，四张卡纵向排（运行环境 / 存储空间 / 服务健康 / 索引存储状态）。
 *
 * 取数自己来，两块并发：`GET /settings/health`（环境 + 磁盘 + 三个服务的健康）与
 * `GET /knowledge/index/status`（全库对账）。**两块各自 try/catch**：一块挂了只降级自己那张卡，
 * 另一张照常显示 —— 系统信息页最不该做的事就是"后端少一个字段，整页空白"。
 *
 * `system` / `disk` 是后端**并行开发中**的新字段：没有、为 null、或整块是空对象时，
 * 对应卡片只留一行 .tiny.faint「后端未返回…」，不报错、不占位、不猜数。
 *
 * 「索引存储状态」直接复用 `KbStorageCard`（props + reconcile 事件与它在「索引管理」里时一模一样）。
 * 校验 / 清理的确认框、请求与 toast 都留在本组件：卡片只显示现状、发意图，不自己发请求。
 *
 * 设计口径：颜色只用 tokens 变量；字号 calc(Npx * var(--ts, 1))；数字 tabular-nums；
 * 断点只用 560（两列键值网格降到一列，占用条本来就在卡内）。hover 只改背景 / 边框。
 */
import { computed, onMounted, ref } from 'vue';
import { api } from '@/api/client';
import type { HealthPayload } from '@/api/types';
import { isLoading, reportError, toast, withLoading } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import AppIcon from '@/components/AppIcon.vue';
import KbStorageCard from './KbStorageCard.vue';

/** `health.system`：运行环境（后端为动态 JSON，字段全部可选）。 */
interface SystemInfo {
  os?: string | null;
  os_detail?: string | null;
  kernel?: string | null;
  arch?: string | null;
  python?: string | null;
  cpu_count?: number | null;
  hostname?: string | null;
  uptime_s?: number | null;
}

/** `health.disk`：磁盘占用（字节；data_dir_bytes / db_bytes 可能拿不到）。 */
interface DiskInfo {
  path?: string | null;
  total_bytes?: number | null;
  used_bytes?: number | null;
  free_bytes?: number | null;
  percent?: number | null;
  data_dir_bytes?: number | null;
  db_bytes?: number | null;
}

/** 健康响应的完整视角：在共享类型上加两块新字段。 */
interface HealthFull extends HealthPayload {
  system?: SystemInfo | null;
  disk?: DiskInfo | null;
}

/** 向量库状态（`index_status.vector`）。 */
interface VectorStatus { available: boolean; count?: number | null; error?: string | null }

/** `GET /knowledge/index/status`（不带 paper_id，即全库）里本面板用到的字段。 */
interface IndexStatus {
  chunks: number;
  chunks_with_hash: number;
  index_versions: Record<string, number>;
  vector: VectorStatus | null;
  model_dim?: number | null;
  store_dim?: number | null;
  dim_match?: boolean | null;
  orphan_chunks?: number | null;
  missing_vectors?: number | null;
  consistent?: boolean | null;
}

/** `POST /knowledge/reconcile` 的返回（backend knowledge_service.reconcile）。 */
interface ReconcileResult {
  orphans?: number;
  missing?: number;
  removed_metadata?: number;
  removed_vectors?: number;
  consistent?: boolean;
}

const health = ref<HealthFull | null>(null);
const healthError = ref('');
const indexStatus = ref<IndexStatus | null>(null);
/** 最近一次读取索引状态的时间（本地时钟），交给存储卡展示。 */
const checkedAt = ref('');
/** 行级动作键：目前只有 `reconcile`（存储卡的两个按钮共用）。 */
const busyKey = ref('');

const healthLoading = computed<boolean>(() => isLoading('sysinfo-health'));
const statusLoading = computed<boolean>(() => isLoading('sysinfo-status'));

/** 本地时钟 HH:MM:SS（不做时区换算：这是"刚才几点刷的"，不是服务端时间戳）。 */
function clockNow(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/* -------------------------------- 取数 -------------------------------- */

/** 健康：失败只写自己那块错误行，不 toast、不牵连索引状态。 */
async function loadHealth(): Promise<void> {
  healthError.value = '';
  try {
    await withLoading('sysinfo-health', async () => {
      const res = await api.get<HealthFull>('/settings/health');
      health.value = res;
      healthCheckedAt.value = clockNow();
      if (res?.error) healthError.value = String(res.error);
    }, { silent: true });
  } catch (err) {
    healthError.value = (err as Error)?.message ?? String(err);
  }
}

/** 索引状态：同上，失败只让存储卡显示"暂无索引状态"。 */
async function loadStatus(): Promise<void> {
  try {
    await withLoading('sysinfo-status', async () => {
      indexStatus.value = await api.get<IndexStatus>('/knowledge/index/status');
      checkedAt.value = clockNow();
    }, { silent: true });
  } catch { /* 存储卡自己会显示"暂无索引状态" */ }
}

async function refreshAll(): Promise<void> {
  if (busyKey.value === 'reconcile') return;
  await Promise.allSettled([loadHealth(), loadStatus()]);
}

onMounted(() => {
  void Promise.allSettled([loadHealth(), loadStatus()]);
});

/* ------------------------------ 健康派生 ------------------------------ */

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

/** 任务池里正在跑的任务数（数组给长度，数字直接用，缺失给 0）。 */
const runningCount = computed<number>(() => {
  const r = runner.value.running;
  if (Array.isArray(r)) return r.length;
  const n = Number(r);
  return Number.isFinite(n) ? n : 0;
});

const runningText = computed(() => {
  const r = runner.value.running;
  if (Array.isArray(r)) return `${r.length} 个`;
  if (r === null || r === undefined) return '—';
  return `${r} 个`;
});

/* ------------------------------ 环境 / 磁盘 ------------------------------ */

/** 空对象也算"没返回"：后端字段还在并行开发，给 `{}` 与给 null 一个待遇。 */
function meaningful(obj: Record<string, unknown> | null | undefined): boolean {
  if (!obj || typeof obj !== 'object') return false;
  return Object.values(obj).some((v) => v !== null && v !== undefined && v !== '');
}

const system = computed<SystemInfo | null>(
  () => (meaningful(health.value?.system) ? health.value!.system! : null),
);
const disk = computed<DiskInfo | null>(
  () => (meaningful(health.value?.disk) ? health.value!.disk! : null),
);

/** 内核 + 架构：两者都在才拼中缀，缺一个就只给有的那个。 */
const kernelText = computed<string>(() => {
  const s = system.value;
  if (!s) return '—';
  const kernel = s.kernel ? String(s.kernel) : '';
  const arch = s.arch ? String(s.arch) : '';
  if (kernel && arch) return `${kernel} · ${arch}`;
  return kernel || arch || '—';
});

const cpuText = computed<string>(() => {
  const n = Number(system.value?.cpu_count);
  return Number.isFinite(n) && n > 0 ? `${n} 核` : '—';
});

/** 「本进程已运行」：天 / 小时 / 分 / 秒四档，最多给两级（"3 天 4 小时"、"41 分"）。 */
function formatUptime(v: number | null | undefined): string {
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return '—';
  const total = Math.floor(n);
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小时`;
  if (h > 0) return `${h} 小时 ${m} 分`;
  if (m > 0) return `${m} 分`;
  return `${total} 秒`;
}

const uptimeText = computed<string>(() => formatUptime(system.value?.uptime_s));

/** 字节格式化：B / KB / MB / GB，一位小数（自己写，不引库；负数与非数给 —）。 */
function formatBytes(v: number | null | undefined): string {
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let x = n / 1024;
  let i = 0;
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1; }
  return `${x.toFixed(1)} ${units[i]}`;
}

/** 占用百分比：优先用后端的 percent，没有就按 used / total 自己算，两者都没有给 0。 */
const diskPct = computed<number>(() => {
  const d = disk.value;
  if (!d) return 0;
  const p = Number(d.percent);
  if (Number.isFinite(p)) return Math.min(100, Math.max(0, p));
  const total = Number(d.total_bytes);
  const used = Number(d.used_bytes);
  if (Number.isFinite(total) && total > 0 && Number.isFinite(used)) {
    return Math.min(100, Math.max(0, (used / total) * 100));
  }
  return 0;
});

/** 占用条的颜色档：<70 强调色，70~90 警告色，>90 危险色。 */
const diskTone = computed<string>(
  () => (diskPct.value > 90 ? 'danger' : diskPct.value >= 70 ? 'warn' : 'accent'),
);

/** MySQL 库占用：null / 缺失 / 非数都不出这一行。 */
const dbBytes = computed<number | null>(() => {
  const v = disk.value?.db_bytes;
  if (v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
});

/* ------------------------------- 对账 ------------------------------- */

/**
 * 对账：`fix: false` 只报告，`fix: true` 才动手清（先确认）。
 * 只有 fix 会改数据 —— 清完两块的数字都会变，所以哪一支都重读索引状态。
 */
async function reconcileNow(fix: boolean): Promise<void> {
  if (fix) {
    const orphans = Number(indexStatus.value?.orphan_chunks || 0);
    const missing = Number(indexStatus.value?.missing_vectors || 0);
    if (!orphans && !missing) { toast('没有需要清理的孤儿数据', 'ok'); return; }
    const ok = await askConfirm({
      title: `清理 ${orphans} 条孤儿元数据？`,
      body: `同时会清掉向量库里 ${missing} 条没有元数据的向量。清理后不可恢复，`
        + '需要时对相关论文重建索引。',
      confirmLabel: '清理',
      danger: true,
    });
    if (!ok) return;
  }
  busyKey.value = 'reconcile';
  try {
    const res = await api.post<ReconcileResult>('/knowledge/reconcile', { fix });
    if (fix) {
      toast(`已清理 ${res?.removed_metadata ?? 0} 条孤儿元数据、${res?.removed_vectors ?? 0} 条孤儿向量`,
        'ok', 3600);
    } else {
      const bad = Number(res?.orphans || 0) || Number(res?.missing || 0);
      toast(`对账完成：孤儿元数据 ${res?.orphans ?? 0} 条、孤儿向量 ${res?.missing ?? 0} 条`,
        bad ? 'warn' : 'ok', 4200);
    }
    await loadStatus();
  } catch (err) {
    reportError(err, '对账失败');
  } finally {
    busyKey.value = '';
  }
}

/* ------------------------------ 小动作 ------------------------------ */

/** 复制一段长文本（路径为主）。WebView / 非安全上下文里 clipboard 可能不可用，失败就说清楚。 */
async function copyText(text?: string | null): Promise<void> {
  const value = String(text ?? '').trim();
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast('已复制', 'ok', 1600);
  } catch {
    toast('这个环境不允许复制，手动选中文本即可', 'warn', 2600);
  }
}

/** 最近一次健康检测的本地时间；没测过就是空串。 */
const healthCheckedAt = ref('');

</script>

<template>
  <div class="sysinfo">
    <!-- 顶部工具行：只读说明 + 上次检测时间 + 重新检测 -->
    <div class="si-bar">
      <span class="si-bar-note">只读信息 · 环境 / 存储 / 服务状态</span>
      <span class="si-bar-time tnum" v-if="healthCheckedAt">上次检测 {{ healthCheckedAt }}</span>
      <span class="spacer"></span>
      <button class="btn sm" type="button" :disabled="healthLoading" title="重新检测各服务" @click="refreshAll">
        <AppIcon name="refresh" :size="14" />{{ healthLoading ? '检测中…' : '重新检测' }}
      </button>
    </div>

    <!-- 四张卡：2×2 排布（窄屏一列）。每张都是同一套 .card + .card-head + .card-body -->
    <div class="si-cards">
      <!-- ① 运行环境 -->
      <section class="card si-card">
        <header class="card-head">
          <AppIcon name="server" :size="15" />
          <h3 class="si-title">运行环境</h3>
        </header>
        <div class="card-body">
          <p class="si-empty" v-if="!system">后端未返回运行环境信息</p>
          <dl class="kv" v-else>
            <div class="kv-row"><dt>操作系统</dt>
              <dd :title="system.os_detail || system.os || ''">{{ system.os || '—' }}</dd></div>
            <div class="kv-row"><dt>内核</dt><dd :title="kernelText">{{ kernelText }}</dd></div>
            <div class="kv-row"><dt>Python</dt><dd>{{ system.python || '—' }}</dd></div>
            <div class="kv-row"><dt>CPU</dt><dd>{{ cpuText }}</dd></div>
            <div class="kv-row"><dt>主机名</dt><dd :title="system.hostname || ''">{{ system.hostname || '—' }}</dd></div>
            <div class="kv-row"><dt>已运行</dt><dd>{{ uptimeText }}</dd></div>
          </dl>
        </div>
      </section>

      <!-- ② 存储空间 -->
      <section class="card si-card">
        <header class="card-head">
          <AppIcon name="layers" :size="15" />
          <h3 class="si-title">存储空间</h3>
          <span class="spacer"></span>
          <span class="disk-pct tnum" :class="diskTone" v-if="disk">{{ diskPct.toFixed(1) }}%</span>
        </header>
        <div class="card-body">
          <p class="si-empty" v-if="!disk">后端未返回磁盘信息</p>
          <template v-else>
            <div class="disk-head">
              <span class="disk-usage">已用 <b>{{ formatBytes(disk.used_bytes) }}</b> / 共 {{ formatBytes(disk.total_bytes) }}</span>
            </div>
            <div
              class="bar"
              role="progressbar"
              aria-label="磁盘占用"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-valuenow="Math.round(diskPct)"
            >
              <span class="bar-fill" :class="diskTone" :style="{ width: diskPct + '%' }"></span>
            </div>
            <p class="disk-line">可用 {{ formatBytes(disk.free_bytes) }} · data 目录 {{ formatBytes(disk.data_dir_bytes) }}<template v-if="dbBytes !== null"> · MySQL {{ formatBytes(dbBytes) }}</template></p>
            <div class="path-line" v-if="disk.path">
              <span class="path-k">卷</span>
              <span class="mono path-v" :title="disk.path">{{ disk.path }}</span>
              <button class="btn icon sm ghost" type="button" title="复制路径" @click="copyText(disk.path)">
                <AppIcon name="copy" :size="13" />
              </button>
            </div>
          </template>
        </div>
      </section>

      <!-- ③ 服务与任务池 -->
      <section class="card si-card">
        <header class="card-head">
          <AppIcon name="gauge" :size="15" />
          <h3 class="si-title">服务与任务池</h3>
        </header>
        <div class="card-body">
          <p class="si-empty" v-if="healthError">健康检查失败：{{ healthError }}</p>
          <p class="si-empty" v-else-if="!health">检测中…</p>
          <ul class="srv" v-else>
            <li class="srv-row">
              <span class="srv-name">MySQL</span>
              <span class="srv-detail mono truncate" :title="String(db.database ?? '')">{{ db.backend || '—' }} · {{ db.database || '—' }}</span>
              <span class="badge" :class="dbOk ? 'ok' : 'danger'">{{ dbOk ? '正常' : (db.error ? '异常' : '未探测') }}</span>
            </li>
            <li class="srv-row">
              <span class="srv-name">LanceDB</span>
              <span class="srv-detail mono truncate" :title="String(vector.path ?? vector.error ?? '')">
                {{ vector.path || vector.error || '—' }}<template v-if="vector.chunks !== undefined && vector.chunks !== null"> · {{ vector.chunks }} 条向量</template>
              </span>
              <span class="badge" :class="vectorOk ? 'ok' : 'danger'">{{ vectorOk ? '可用' : '不可用' }}</span>
            </li>
            <li class="srv-row">
              <span class="srv-name">任务池</span>
              <span class="srv-detail">workers {{ runner.workers ?? '—' }} · 运行中 {{ runningText }} · 队列 {{ queueText }}</span>
              <span class="badge" :class="runningCount > 0 ? 'ok' : 'dim'">{{ runningCount > 0 ? `运行中 ${runningCount}` : '空闲' }}</span>
            </li>
            <li class="srv-row">
              <span class="srv-name">数据目录</span>
              <span class="srv-detail mono truncate" :title="health.data_dir || ''">{{ health.data_dir || '—' }}</span>
              <button class="btn icon sm ghost" type="button" title="复制路径" @click="copyText(health.data_dir)">
                <AppIcon name="copy" :size="13" />
              </button>
            </li>
          </ul>
        </div>
      </section>

      <!-- ④ 索引存储：复用索引管理那张卡，外壳对齐到同一套 .card（头/体在原组件里覆盖成卡片版式） -->
      <section class="card si-card si-card--kb">
        <KbStorageCard :status="indexStatus" :loading="statusLoading" :busy="busyKey"
                       :checked-at="checkedAt" :actions="false" @reconcile="reconcileNow" />
      </section>
    </div>
  </div>
</template>

<style scoped>
/* 只读面板：一条工具行 + 2×2 的卡片。卡片用全站同一套 .card / .card-head / .card-body，
   不另立一套「标题在块外」的写法（用户口径 2026-12：系统信息用多卡片布局）。 */
.sysinfo { display: flex; flex-direction: column; gap: 12px; min-width: 0; }

.si-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-height: 30px; }
.si-bar-note { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.si-bar-time { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }

/* 卡片网格：两列等宽、按行顶对齐；窄屏一列。
   .card + .card 的上边距在这里清零 —— 间距只由 gap 给，否则同排两张卡会差 12px。 */
.si-cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; align-items: start; }
.si-cards > .card + .card { margin-top: 0; }
.si-card { min-width: 0; }
.si-card .card-head { gap: 8px; }
.si-card .card-head :deep(svg) { color: var(--text-faint); }
.si-title { margin: 0; font-size: calc(13px * var(--ts, 1)); font-weight: 650; line-height: 1.35; color: var(--text); }
.si-empty { margin: 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.6; color: var(--text-faint); }

/* ① 运行环境：定宽标签 + 值，标签右对齐成一条竖线 */
.kv { display: grid; grid-template-columns: minmax(0, 1fr); gap: 7px; margin: 0; }
.kv-row { display: grid; grid-template-columns: 68px minmax(0, 1fr); align-items: baseline; gap: 10px; min-width: 0; }
.kv dt { font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
.kv dd {
  margin: 0; font-size: calc(12.5px * var(--ts, 1)); color: var(--text); font-variant-numeric: tabular-nums;
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* ② 存储：用量行 + 占用条 + 细目 + 路径（可复制） */
.disk-head { display: flex; align-items: baseline; gap: 8px; }
.disk-usage { font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim); font-variant-numeric: tabular-nums; }
.disk-usage b { font-weight: 600; color: var(--text); }
.disk-pct { font-size: calc(12px * var(--ts, 1)); font-weight: 650; color: var(--accent); }
.disk-pct.warn { color: var(--warn); }
.disk-pct.danger { color: var(--danger); }
.bar { height: 6px; margin: 8px 0; border-radius: var(--r-pill); background: var(--bg-sunken); overflow: hidden; }
.bar-fill { display: block; height: 100%; border-radius: var(--r-pill); background: var(--accent); transition: width var(--dur) var(--ease); }
.bar-fill.warn { background: var(--warn); }
.bar-fill.danger { background: var(--danger); }
.disk-line { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.7; color: var(--text-faint); font-variant-numeric: tabular-nums; }
.path-line { display: flex; align-items: center; gap: 6px; margin-top: 6px; min-width: 0; }
.path-k { flex: none; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.path-v { min-width: 0; overflow-wrap: anywhere; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim); }

/* ③ 服务与任务池：一行一个服务，发丝线分隔 */
.srv { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
.srv-row { display: flex; align-items: center; gap: 10px; min-height: 30px; min-width: 0; }
.srv-row + .srv-row { border-top: 1px solid var(--divider); }
.srv-name { flex: none; width: 68px; font-size: calc(12.5px * var(--ts, 1)); color: var(--text); }
.srv-detail { flex: 1 1 auto; min-width: 0; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.srv-row .badge { flex: none; }

/* ④ 索引存储：复用组件自带的 head/body，把它覆盖成同一张卡的版式 */
.si-card--kb { overflow: hidden; }
.si-card--kb :deep(.storage-head) {
  margin: 0; padding: 11px 16px; border-bottom: 1px solid var(--divider);
}
.si-card--kb :deep(.ttl) { font-size: calc(13px * var(--ts, 1)); font-weight: 650; }
.si-card--kb :deep(.storage-body) { padding: 16px; border-radius: 0; background: transparent; }

@media (max-width: 900px) {
  .si-cards { grid-template-columns: minmax(0, 1fr); }
}
</style>
