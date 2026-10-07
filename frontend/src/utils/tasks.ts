/**
 * 任务类型 / 状态的**界面口径**（阅读器左侧「任务」页签的清单用）。
 *
 * 为什么集中在一个文件：同一条流水线在两处都要说人话（任务页签的清单、取消任务的确认框），
 * 类型名各写一份必然慢慢走音（"翻译" / "逐页翻译" / "中文翻译"）。
 *
 * ⚠️ `TRANSLATE_MD` 与 `TRANSLATE_ZH` 在界面上**是同一件事**（逐页翻译的两条路：英文转录 + 中文翻译），
 * 所以都映射成"翻译"——列两种叫法只会让人以为跑了两套翻译。
 * ⚠️ `UNDERSTAND_A` 在界面上就叫"解读"（前端没有"路"这个概念；`UNDERSTAND_B`
 * ——原「阅读地图」——已随该功能一起删除）。
 */
import type { TaskInfo } from '@/api/types';
import { pct } from '@/utils/format';

/** 任务类型 → 中文名（后端新增类型时回落到原始 type，不显示空白）。 */
export const TASK_TYPE_LABELS: Record<string, string> = {
  PARSE: '解析',
  TRANSCRIBE_EN: '转录',
  TRANSLATE_MD: '翻译',
  TRANSLATE_ZH: '翻译',
  UNDERSTAND_A: '解读',
  INDEX: '索引',
};

/** 任务状态 → 中文名（`cancelled` / `canceled` 两种拼法后端历史上都出现过，都认）。 */
export const TASK_STATUS_LABELS: Record<string, string> = {
  completed: '已完成',
  processing: '进行中',
  pending: '排队中',
  paused: '已暂停',
  failed: '已失败',
  cancelled: '已取消',
  canceled: '已取消',
};

/**
 * "还在跑"的三个状态：`paused` 也算 —— 暂停的任务随时可以继续，
 * 界面若把它当"结束"，用户就看不到自己那条暂停的翻译了。
 */
export const TASK_RUNNING_STATUSES = ['processing', 'pending', 'paused'];

export function isTaskRunning(t: TaskInfo | null | undefined): boolean {
  return Boolean(t) && TASK_RUNNING_STATUSES.includes(String(t?.status));
}

export function taskTypeLabel(type: string): string {
  return TASK_TYPE_LABELS[type] ?? type;
}

export function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] ?? status;
}

/** 清单里类型出现的顺序（与流水线一致）。顺序固定，刷新时行不会跳来跳去。 */
const TYPE_ORDER = ['翻译', '转录', '解读', '索引', '解析'];

/** 「任务」页签里的一行 = 一类任务（不是一条任务）。 */
export interface TaskTypeRow {
  /** 行的 key（= 中文类型名，一类一行） */
  key: string;
  label: string;
  /** 原始状态（给样式选色用） */
  status: string;
  statusLabel: string;
  running: boolean;
  /** 这一类里代表行的那条任务（取消按钮取它） */
  task: TaskInfo | null;
  /**
   * `done/total` 数字；空串 = **不显示数字**（三种情况）。
   *
   * 用户口径（第三轮返工）：**已完成的行不显示数字**，只留"已完成"徽章 + 满格条。
   * 理由是它自相矛盾：后端偶尔给的是"这一批里的完成量"（实测 TRANSCRIBE_EN 有
   * `done=1/total=3` 却已 completed 的记录），`转录 已完成 1/3 + 满格条` 看起来就像出错了 ——
   * 这种"任务提前收尾"的事实用数字表达反而是错的，徽章 + 满格条已经说清楚了。
   * 另外两种：`total <= 1`（"1/1" 是噪声）、非完成态又没有总数（本来就没数字可给）。
   */
  progressLabel: string;
  /**
   * 进度条的百分比；`null` = **这一类没进度可算，不画条**（只留状态徽章）。
   *
   * 用户口径（第二轮返工）：能显示进度的地方要把进度条加上。判据分三种：
   * - `total > 1`（翻译 22 页 / 索引 203 块 / 解析 900 块…）：有进度条。
   *   已完成的**一律按 100%** 画 —— 后端偶尔给的是"这一批里的完成量"（实测 TRANSCRIBE_EN
   *   有 `done=1/total=3` 却已 completed 的记录），按数字画会留一截看起来像没跑完；
   * - `total <= 0` 但有 0~100 的 `progress`（有些任务只报百分比、不给计数）：也画；
   * - 其余（**解读** `total=1`、排队中的 0%）：不画 —— 一条永远停在 0 或满格的条只是噪声。
   */
  progressValue: number | null;
  /** 代表行的 message；**失败时才在界面上显示**（成功时那是"用时 432s"这类流水账） */
  message: string;
}

/**
 * 每篇论文按**类型**汇总成一行：
 * `翻译：进行中 · 9/22` / `转录：已完成`（满格条，不报数字）/ `解读：排队中`（用户口径）。
 *
 * 为什么不把十几条历史任务全列出来：同一类型往往是几十次重跑，全列是一面墙，
 * 而读者关心的只是"这条流水线现在什么状态"。代表行取**在跑的那条**（没有才取最新一条，
 * 接口按 created_at 倒序）：重跑时旧任务的"已完成"不该盖住正在进行的现状。
 */
export function groupPaperTasks(tasks: TaskInfo[]): TaskTypeRow[] {
  const groups = new Map<string, TaskInfo[]>();
  for (const t of tasks) {
    const label = taskTypeLabel(String(t.type));
    const list = groups.get(label);
    if (list) list.push(t);
    else groups.set(label, [t]);
  }
  // 已知类型按流水线顺序；后端将来新增的类型排在后面，按首次出现顺序
  const labels = [
    ...TYPE_ORDER.filter((l) => groups.has(l)),
    ...[...groups.keys()].filter((l) => !TYPE_ORDER.includes(l)),
  ];
  return labels.map((label) => {
    const list = groups.get(label) ?? [];
    const task = list.find(isTaskRunning) ?? list[0] ?? null;
    const status = String(task?.status ?? '');
    const total = Number(task?.total_items ?? 0);
    const done = Number(task?.done_items ?? 0);
    const raw = Number(task?.progress);
    const hasPct = Number.isFinite(raw) && raw >= 0 && raw <= 100;
    /* 有总数就是"可计量的任务"；没有总数但报了百分比（且真的动过）也算。
       解读那种 `total=1` 两边都不满足 → 不画条（用户口径）。 */
    const drawable = total > 1 || (total <= 0 && hasPct && raw > 0);
    const progressValue = drawable
      ? (status === 'completed' ? 100 : (hasPct ? Math.round(raw) : pct(done, total)))
      : null;
    return {
      key: label,
      label,
      status,
      statusLabel: task ? taskStatusLabel(status) : '',
      running: isTaskRunning(task),
      task,
      /* 已完成不报数字（见 `progressLabel` 的说明）：徽章 + 满格条已经说清楚，
         数字反而会与"已完成"打架。其余状态有总数就报，方便看还剩多少。 */
      progressLabel: (total > 1 && status !== 'completed') ? `${done}/${total}` : '',
      progressValue,
      message: String(task?.message ?? ''),
    };
  });
}
