/**
 * 任务类型 / 状态的**界面口径**（阅读器左侧「任务」页签的清单用）。
 *
 * 为什么集中在一个文件：同一条流水线在两处都要说人话（任务页签的清单、取消任务的确认框），
 * 类型名各写一份必然慢慢走音（"翻译" / "逐页翻译" / "中文翻译"）。
 *
 * ⚠️ `TRANSLATE_MD` 与 `TRANSLATE_ZH` 在界面上**是同一件事**（逐页翻译的两条路：英文转录 + 中文翻译），
 * 所以都映射成"翻译"——列两种叫法只会让人以为跑了两套翻译。
 * ⚠️ `UNDERSTAND_A` / `UNDERSTAND_B` 同理：前端没有"两路"这个概念（用户口径），统一叫"解读"。
 */
import type { TaskInfo } from '@/api/types';

/** 任务类型 → 中文名（后端新增类型时回落到原始 type，不显示空白）。 */
export const TASK_TYPE_LABELS: Record<string, string> = {
  PARSE: '解析',
  TRANSCRIBE_EN: '转录',
  TRANSLATE_MD: '翻译',
  TRANSLATE_ZH: '翻译',
  UNDERSTAND_A: '解读',
  UNDERSTAND_B: '解读',
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
  /** `done/total`；`total <= 1` 时为空串（"1/1" 是噪声） */
  progressLabel: string;
  /** 代表行的 message；**失败时才在界面上显示**（成功时那是"用时 432s"这类流水账） */
  message: string;
}

/**
 * 每篇论文按**类型**汇总成一行：
 * `翻译：已完成 · 22/22` / `转录：已完成` / `解读：进行中`（用户口径）。
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
    const total = Number(task?.total_items ?? 0);
    const done = Number(task?.done_items ?? 0);
    return {
      key: label,
      label,
      status: String(task?.status ?? ''),
      statusLabel: task ? taskStatusLabel(String(task.status)) : '',
      running: isTaskRunning(task),
      task,
      progressLabel: total > 1 ? `${done}/${total}` : '',
      message: String(task?.message ?? ''),
    };
  });
}
