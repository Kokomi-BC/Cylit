/** 全局状态：跨页缓存 + toast + 共享加载器（够用即可，不引额外状态库）。 */

import { reactive } from 'vue';
import { api } from '@/api/client';
import type {
  ModelConfig, ModelTypeDefault, ModelPreset, Paper, SettingsPayload, TaskInfo,
} from '@/api/types';

export interface Toast {
  id: number;
  message: string;
  kind: 'info' | 'ok' | 'warn' | 'error';
}

interface AppState {
  papers: Paper[];
  papersTotal: number;
  models: ModelConfig[];
  modelTypes: { types: string[]; defaults: Record<string, ModelTypeDefault | null> } | null;
  secrets: string[];
  secretsDir: string;
  presets: ModelPreset[];
  tasks: TaskInfo[];
  taskStats: { by_status: Record<string, number>; total: number };
  settings: SettingsPayload | null;
  health: Record<string, any> | null;
  toasts: Toast[];
  loading: Record<string, boolean>;
}

export const state = reactive<AppState>({
  papers: [],
  papersTotal: 0,
  models: [],
  modelTypes: null,
  secrets: [],
  secretsDir: '',
  presets: [],
  tasks: [],
  taskStats: { by_status: {}, total: 0 },
  settings: null,
  health: null,
  toasts: [],
  loading: {},
});

let toastSeq = 0;

/** 提示停留时长：**按级别递加**（用户口径 2026-12）——
 *  `info` / `ok` 3.5 秒 → `warn` 5 秒 → `error` 6.1 秒。
 *  为什么这样分：越严重的提示往往越长（出错那句常带「为什么 + 怎么办」），读不完等于没提示；
 *  而一句「已保存」挂太久会挡住右上角。以后要调就改这张表，别再让调用点各写一个 ttl。
 *
 *  ⚠️ 调用点里那 73 处显式 `ttl` **不再生效**（形参改名 _ttl 保留，只为不动那些调用点）：
 *  停留时间只由级别决定，没有常驻提示这种用法（要常驻就用别的东西）。
 *  ⚠️ 退场还有 260ms 的滑出淡出动画（`.toast-leave-active`），肉眼看到的总时长比这个数再长一点。 */
export const TOAST_TTL: Record<Toast['kind'], number> = {
  info: 3500,
  ok: 3500,
  warn: 5000,
  error: 6100,
};

export function toast(message: string, kind: Toast['kind'] = 'info', _ttl = 0): number {
  const id = ++toastSeq;
  state.toasts.push({ id, message: String(message ?? ''), kind });
  const ms = TOAST_TTL[kind] ?? TOAST_TTL.info;   // 级别决定停留时长（表在文件上方）
  setTimeout(() => dismissToast(id), ms);
  return id;
}

export function dismissToast(id: number): void {
  const i = state.toasts.findIndex((t) => t.id === id);
  if (i >= 0) state.toasts.splice(i, 1);
}

/** 统一错误提示；返回错误对象，便于调用方决定后续流程。 */
export function reportError(err: unknown, prefix = '操作失败'): unknown {
  const message = (err as any)?.message ?? String(err);
  toast(`${prefix}：${message}`, 'error', 6000);
  console.warn(prefix, err);
  return err;
}

export function isLoading(key: string): boolean {
  return !!state.loading[key];
}

export async function withLoading<T>(
  key: string, fn: () => Promise<T>, opts: { silent?: boolean } = {},
): Promise<T> {
  state.loading[key] = true;
  try {
    return await fn();
  } catch (err) {
    if (!opts.silent) reportError(err);
    throw err;
  } finally {
    state.loading[key] = false;
  }
}

/* ------------------------------ 共享加载器 ------------------------------ */

export async function loadPapers(params: Record<string, unknown> = {}): Promise<{ items: Paper[]; total: number }> {
  const data = await api.get<{ items: Paper[]; total: number }>('/papers', { page_size: 100, ...params });
  state.papers = data.items ?? [];
  state.papersTotal = data.total ?? 0;
  return data;
}

export async function loadModels(): Promise<ModelConfig[]> {
  const data = await api.get<{ items: ModelConfig[] }>('/models');
  state.models = data.items ?? [];
  return state.models;
}

export async function loadModelTypes() {
  state.modelTypes = await api.get('/models/types');
  return state.modelTypes;
}

export async function loadTasks(params: Record<string, unknown> = {}): Promise<{ items: TaskInfo[] }> {
  const data = await api.get<{ items: TaskInfo[]; stats: AppState['taskStats'] }>('/tasks', { limit: 200, ...params });
  state.tasks = data.items ?? [];
  state.taskStats = data.stats ?? { by_status: {}, total: 0 };
  return data;
}

export async function loadSettings(): Promise<SettingsPayload> {
  state.settings = await api.get<SettingsPayload>('/settings');
  return state.settings;
}

export async function loadHealth(): Promise<Record<string, any>> {
  try {
    state.health = await api.get('/settings/health');
  } catch (err: any) {
    state.health = { error: err?.message ?? String(err) };
  }
  return state.health!;
}

export function paperTitle(paperId?: string | null): string {
  if (!paperId) return '—';
  return state.papers.find((p) => p.id === paperId)?.title ?? paperId;
}
