/**
 * 站内确认框的**请求端**（与 `components/ConfirmHost.vue` 配对）。
 *
 * 为什么用一个模块级单例而不是 composable + provide：调用点分布在**工具函数**里
 * （`utils/understanding.ts` 这种没有组件上下文的地方），provide/inject 够不着；
 * 而它本来就只可能有一个（同一时刻只弹一个确认框）。
 *
 * 用法：
 * ```ts
 * if (!(await askConfirm({ title: '删除这条笔记？', body: '不可恢复。', danger: true }))) return;
 * ```
 */

import { reactive } from 'vue';

export interface ConfirmOptions {
  title?: string;
  body?: string;
  /** 被操作对象的名字 / 摘要，弹窗里用浅底卡片强调 */
  target?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

export const confirmState = reactive<Required<ConfirmOptions> & { open: boolean }>({
  open: false,
  title: '确认操作',
  body: '',
  target: '',
  confirmLabel: '确认',
  cancelLabel: '取消',
  danger: false,
});

let pending: ((ok: boolean) => void) | null = null;

/** 弹一个确认框并**等用户选择**（返回 `true` = 确认）。 */
export function askConfirm(opts: ConfirmOptions = {}): Promise<boolean> {
  // 上一次还没回答就被新的顶掉：旧的那次按"取消"结算，别留下永不 resolve 的 Promise
  if (pending) {
    pending(false);
    pending = null;
  }
  confirmState.title = opts.title ?? '确认操作';
  confirmState.body = opts.body ?? '';
  confirmState.target = opts.target ?? '';
  confirmState.confirmLabel = opts.confirmLabel ?? '确认';
  confirmState.cancelLabel = opts.cancelLabel ?? '取消';
  confirmState.danger = Boolean(opts.danger);
  confirmState.open = true;
  return new Promise<boolean>((resolve) => { pending = resolve; });
}

/** 由 `ConfirmHost` 在用户点确认 / 取消时调用。 */
export function resolveConfirm(ok: boolean): void {
  confirmState.open = false;
  const done = pending;
  pending = null;
  done?.(ok);
}
