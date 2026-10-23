/**
 * POST + SSE 的流式客户端。
 *
 * 为什么不用 `EventSource`：它只能 GET。而问答要 POST —— 问题、划词选区、会话 id 都在
 * body 里，塞进 URL 会立刻遇到换行、引号、LaTeX 的转义问题。`fetch` + `ReadableStream`
 * 手解 SSE 帧只有几十行，换来的是"请求体就是正常的 JSON"。
 *
 * 帧格式与后端一致：`event: <type>\n data: <json>\n\n`（`data` 一定是单行 JSON）。
 */

import { API_BASE, ApiError } from './client';

export interface SseOptions {
  /** 中止（切会话 / 组件卸载 / 用户点停止） */
  signal?: AbortSignal;
  onEvent: (name: string, data: any) => void;
}

function parseFrame(raw: string): { name: string; data: any } | null {
  let name = '';
  const dataLines: string[] = [];
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) name = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
    // 其它行（`:` 心跳注释等）忽略
  }
  if (!name) return null;
  const text = dataLines.join('\n');
  let data: any = null;
  if (text) { try { data = JSON.parse(text); } catch { data = text; } }
  return { name, data };
}

/** POST 一个 JSON，按 SSE 逐事件回调；函数返回时流已经正常结束。 */
export async function postSse(path: string, body: unknown, opts: SseOptions): Promise<void> {
  const url = `${API_BASE}/${path.replace(/^\/+/, '')}`;
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
      signal: opts.signal,
    });
  } catch (err: any) {
    if (err?.name === 'AbortError') throw err;
    throw new ApiError(`无法连接后端（${API_BASE}）：${err?.message ?? err}`, { code: 'NETWORK_ERROR' });
  }

  // 校验失败这类是**普通 JSON 响应**（不是流），按 REST 的错误口径抛出去
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '');
    let payload: any = null;
    try { payload = text ? JSON.parse(text) : null; } catch { payload = null; }
    const err = payload?.error ?? {};
    throw new ApiError(err.message ?? `HTTP ${res.status}`, {
      code: err.code ?? `HTTP_${res.status}`, status: res.status,
      detail: err.detail ?? text, traceId: payload?.trace_id ?? '',
    });
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    for (;;) {
      /* 读流失败要**分类**（只改这一处，SSE 解析语义一个字没动）。
         为什么：`reader.read()` 抛出来的东西以前会以裸 `TypeError: network error` 冒到界面上，
         而这时**响应头早就 200 回来了**、正文也落了一半 —— 用户看到的却像"连不上后端"，
         于是他能做的动作全错（重装 / 换网 / 再点一次）。
         现在包成 `ApiError(STREAM_INTERRUPTED, status=200)`：错误卡据此说"回答流被截断了"。
         **用户主动中止原样抛**（AbortError）：否则点"停止生成"会长出一张错误卡。 */
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (err: any) {
        if (err?.name === 'AbortError' || opts.signal?.aborted) throw err;
        throw new ApiError(`回答流中断了：${err?.message ?? err}`, {
          code: 'STREAM_INTERRUPTED',
          status: 200,
          detail: { reason: String(err?.message ?? err) },
        });
      }
      const { done, value } = chunk;
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut = buffer.indexOf('\n\n');
      while (cut >= 0) {
        const frame = parseFrame(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        if (frame) opts.onEvent(frame.name, frame.data);
        cut = buffer.indexOf('\n\n');
      }
    }
    // 收尾：最后可能有一帧没有以空行结束
    const tail = parseFrame(buffer);
    if (tail) opts.onEvent(tail.name, tail.data);
  } finally {
    reader.releaseLock?.();
  }
}
