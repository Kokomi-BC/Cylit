/**
 * 「论文解读」的**数据源与轮询** —— 阅读器左栏页签 + 整页路由共用。
 *
 * 为什么必须共用：同一份解读有两个出口（左栏「论文解读」页签、整页 `/understand/:paperId`）。
 * 两处各写一遍 fetch + 轮询，会出现"切个页签又把同一个接口打一遍"，
 * 而且各自对"缺内容"的判据会慢慢漂移。这里按 `paperId` 缓存整包，并发请求只发一次（in-flight 去重）。
 *
 * 缓存 TTL：30s。同一篇论文在两个出口之间来回切是**零请求**；隔久了再打开会重新取一次，
 * 免得"论文库里重跑过解读、这边还显示旧内容"。轮询那一轮走 `force`，绕过 TTL。
 *
 * 轮询口径（沿用前几轮）：**只在有解读任务在跑时**轮询，5s 一轮、48 轮上限。
 * 任务的判据由调用方给（阅读器按自己的任务列表算，整页这边自己拉 `/tasks`）。
 *
 * ⚠️ 2026-10-02：原「阅读地图」（B 路 + 相关概念）整体删除，这里只剩论文精读这一路。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch, type Ref } from 'vue';
import { api } from '@/api/client';
import { isTaskRunning } from '@/utils/tasks';
import type { TaskInfo, UnderstandingPayload } from '@/api/types';

/** `GET /papers/{id}/understanding` 的整包形状（`a` = 论文精读）。 */
export interface UnderstandingBundle {
  paper_id?: string;
  a?: UnderstandingPayload | null;
  /** 已经生成过的那几路（现在只有 `['a']`） */
  done?: string[];
}

/** 缓存里的一篇论文（`reactive`：两个出口共享同一份，谁刷新了别处立刻跟着变）。 */
export interface UnderstandingEntry {
  bundle: UnderstandingBundle;
  loading: boolean;
  /** 已经成功取回过一次（配合 `at` 决定缓存还能不能用） */
  loaded: boolean;
  /** 最后一次成功取回的时间戳（缓存 TTL 用） */
  at: number;
  /** 后端 `error.message`（原样显示：400 那句"先建索引"是可执行指引） */
  error: string;
}

const CACHE_TTL_MS = 30_000;
const POLL_MS = 5000;
const POLL_MAX = 48;

const cache = new Map<string, UnderstandingEntry>();
const inflight = new Map<string, Promise<void>>();

function entryFor(paperId: string): UnderstandingEntry {
  let e = cache.get(paperId);
  if (!e) {
    e = reactive<UnderstandingEntry>({
      bundle: { a: null }, loading: false, loaded: false, at: 0, error: '',
    });
    cache.set(paperId, e);
  }
  return e;
}

/** 取这篇论文的解读缓存（没有就建一个空壳；**不发请求**）。 */
export function understandingEntry(paperId: string): UnderstandingEntry {
  return entryFor(paperId);
}

/**
 * 取整包解读。`force` 绕过 TTL（轮询那一轮用）；`silent` 时不置 loading、失败也不覆盖 error
 * （网络抖一下不该让已经显示出来的解读变空）。
 */
export async function loadUnderstanding(
  paperId: string, opts: { silent?: boolean; force?: boolean } = {},
): Promise<void> {
  const entry = entryFor(paperId);
  if (!opts.force && entry.loaded && Date.now() - entry.at < CACHE_TTL_MS) return;
  const running = inflight.get(paperId);
  if (running) return running;                     // 并发去重：同一篇论文同一时刻只发一次
  if (!opts.silent) entry.loading = true;
  const task = (async () => {
    try {
      const d = await api.get<UnderstandingBundle>(`/papers/${paperId}/understanding`);
      entry.bundle = { a: d?.a ?? null, done: d?.done ?? [] };
      entry.error = '';
      entry.loaded = true;
      entry.at = Date.now();
    } catch (err) {
      if (!opts.silent) entry.error = (err as Error)?.message ?? String(err);
    } finally {
      entry.loading = false;
      inflight.delete(paperId);
    }
  })();
  inflight.set(paperId, task);
  return task;
}

/**
 * 某个出口（页签或整页）用的解读状态。
 *
 * `running`：任务是否在跑。阅读器那边有自己的任务列表（更准、更省），传进来；
 * 整页没有任务列表，就留空 —— 本组合式自己每轮拉一次 `/tasks` 判断。
 */
export function useUnderstanding(paperId: Ref<string>, opts: { running?: Ref<boolean> } = {}) {
  const entry = computed(() => entryFor(paperId.value));
  const ownRunning = ref(false);
  const running = computed(() => (opts.running ? opts.running.value : ownRunning.value));

  const starting = ref(false);
  /** 本出口刚发起过解读：任务列表还没反映出来时也要继续轮询 */
  const started = ref(false);
  const timedOut = ref(false);

  const payload = computed(() => entry.value.bundle.a ?? null);
  const markdown = computed(() => String(payload.value?.markdown ?? ''));
  /** 已经拿到解读正文（轮询的终点；空状态与"还没好"由它区分） */
  const hasContent = computed(() => Boolean(markdown.value.trim()));

  /** 自己拉任务列表（只有整页需要；阅读器那边由宿主给 `running`，不重复请求） */
  async function syncRunning() {
    if (opts.running) return;
    try {
      const d = await api.get<{ items: TaskInfo[] }>('/tasks', { paper_id: paperId.value, limit: 20 });
      ownRunning.value = (d.items ?? []).some(
        (t) => t.type === 'UNDERSTAND_A' && isTaskRunning(t),
      );
    } catch {
      ownRunning.value = false;
    }
  }

  let timer: number | undefined;
  let rounds = 0;

  function stopPoll() {
    if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
  }
  function shouldPoll(): boolean {
    return !hasContent.value && !timedOut.value && (running.value || started.value);
  }
  function schedule() {
    if (timer !== undefined || !shouldPoll()) return;
    timer = window.setTimeout(async () => {
      timer = undefined;
      rounds += 1;
      await Promise.all([
        loadUnderstanding(paperId.value, { silent: true, force: true }),
        syncRunning(),
      ]);
      if (hasContent.value) { started.value = false; rounds = 0; return; }
      if (rounds >= POLL_MAX) { timedOut.value = true; return; }
      schedule();
    }, POLL_MS);
  }

  /* 任务在跑 → 开始轮询；任务收尾 → 已经有结果就停手（再问只是白打接口），
     什么都还没有就再等一会儿（任务列表可能只慢了一拍）。 */
  watch(running, (r) => {
    if (r) {
      started.value = true;
      rounds = 0;
      timedOut.value = false;
      schedule();
      return;
    }
    if (hasContent.value) { started.value = false; stopPoll(); return; }
    schedule();
  });

  /**
   * 「开始解读」：`POST /papers/{id}/understand`（**不带 force**，后端幂等，已有结果就不重跑）。
   * 返回是否发起成功；失败把后端 `error.message` 原样放进 `entry.error`。
   */
  async function start(): Promise<boolean> {
    starting.value = true;
    entry.value.error = '';
    timedOut.value = false;
    try {
      await api.post(`/papers/${paperId.value}/understand`, {});
      started.value = true;
      rounds = 0;
      await loadUnderstanding(paperId.value, { silent: true, force: true });
      await syncRunning();
      schedule();
      return true;
    } catch (err) {
      entry.value.error = (err as Error)?.message ?? String(err);
      return false;
    } finally {
      starting.value = false;
    }
  }

  onMounted(() => {
    void loadUnderstanding(paperId.value);
    void syncRunning();
    schedule();
  });
  onUnmounted(stopPoll);
  watch(paperId, () => {
    started.value = false;
    timedOut.value = false;
    rounds = 0;
    stopPoll();
    void loadUnderstanding(paperId.value);
    void syncRunning();
  });

  return {
    entry, payload, markdown, hasContent,
    starting, started, timedOut, running, start, stopPoll,
  };
}
