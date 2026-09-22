/**
 * SSE 订阅（规划 §3.1 A1 / §22 关键点 2：LiveUpdater 只刷新变化的块）。
 *
 * - 断线由 EventSource 自动重连；连续失败达阈值 → 通知调用方降级 3s 轮询，恢复后回切；
 * - `?nosse=1` 强制走降级路径：既服务于禁用长连接的环境（反向代理 buffering），
 *   也让无头浏览器自检能拿到 DOM（长连接会让 virtual-time 永不推进）。
 */

import { onUnmounted } from 'vue';

export type StreamStatus = 'connecting' | 'open' | 'error' | 'reconnecting' | 'polling' | 'failed';

export interface EventStreamOptions {
  url: string;
  events?: string[];
  onEvent?: (name: string, frame: any) => void;
  onStatus?: (status: StreamStatus) => void;
  onFallback?: () => void;
  onRecover?: () => void;
  fallbackAfter?: number;
}

export function sseDisabled(): boolean {
  const query = `${location.search}${location.hash}`;
  return /(?:^|[?&])nosse=1(?:[&#]|$)/.test(query);
}

export function createEventStream(opts: EventStreamOptions) {
  const {
    url, events = [], onEvent = () => {}, onStatus = () => {},
    onFallback = null, onRecover = null, fallbackAfter = 2,
  } = opts;

  let source: EventSource | null = null;
  let errors = 0;
  let degraded = false;
  let closed = false;

  const notifyStatus = (s: StreamStatus) => onStatus(s);

  if (sseDisabled()) {
    degraded = true;
    notifyStatus('polling');
    onFallback?.();
    return { close() { closed = true; }, get degraded() { return true; } };
  }

  const handle = (name: string) => (ev: MessageEvent) => {
    let payload: any = null;
    try { payload = JSON.parse(ev.data); } catch { payload = ev.data; }
    onEvent(name, payload);
  };

  function connect() {
    if (closed) return;
    try {
      source = new EventSource(url);
    } catch (err) {
      notifyStatus('failed');
      degraded = true;
      onFallback?.();
      return;
    }
    source.onopen = () => {
      errors = 0;
      notifyStatus('open');
      if (degraded) {
        degraded = false;
        onRecover?.();
      }
    };
    source.onerror = () => {
      errors += 1;
      notifyStatus(errors === 1 ? 'error' : 'reconnecting');
      if (!degraded && errors >= fallbackAfter) {
        degraded = true;
        onFallback?.();
      }
      if (source && source.readyState === EventSource.CLOSED && !closed) {
        setTimeout(connect, 2000);
      }
    };
    source.onmessage = handle('message');
    for (const name of events) source.addEventListener(name, handle(name) as EventListener);
  }

  connect();

  return {
    close() {
      closed = true;
      source?.close();
    },
    get degraded() { return degraded; },
  };
}

/** 轮询降级：`fn` 返回 false 时停止。 */
export function startPolling(fn: () => Promise<boolean | void> | boolean | void, intervalMs = 3000) {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const tick = async () => {
    if (stopped) return;
    try {
      const keep = await fn();
      if (keep === false) { stopped = true; return; }
    } catch (err) {
      console.warn('[poll] 轮询失败', err);
    }
    timer = setTimeout(tick, intervalMs);
  };
  timer = setTimeout(tick, intervalMs);
  return {
    stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
      timer = null;
    },
  };
}

/**
 * 组件内使用的 SSE：组件卸载时自动关闭连接与轮询。
 * 返回 `fallbackTo`，由调用方在 `onFallback` 里挂上轮询回调。
 */
export function useEventStream(opts: Omit<EventStreamOptions, 'onFallback' | 'onRecover'> & {
  poll?: () => Promise<void> | void;
  pollInterval?: number;
}) {
  const { poll, pollInterval = 3000, ...rest } = opts;
  let polling: ReturnType<typeof startPolling> | null = null;

  const stream = createEventStream({
    ...rest,
    onFallback: () => { if (poll) polling = startPolling(async () => { await poll(); }, pollInterval); },
    onRecover: () => { polling?.stop(); polling = null; },
  });

  onUnmounted(() => {
    stream.close();
    polling?.stop();
    polling = null;
  });

  return stream;
}
