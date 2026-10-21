/**
 * REST 客户端（规划 §6.1：前端只谈 REST，不直连任何存储）。
 *
 * 后端统一信封：`{data, error, trace_id, message}`。
 * 成功返回 `data`，失败抛 `ApiError`（带 code / status / detail / traceId）。
 * 前端**不硬编码模型名**、不拼 SQL、不读文件系统。
 */

export const API_ORIGIN: string =
  typeof location === 'undefined' || location.protocol === 'file:'
    ? 'http://127.0.0.1:8710'
    : location.origin;

export const API_BASE = `${API_ORIGIN}/api/v1`;

export class ApiError extends Error {
  code: string;
  status: number;
  detail: unknown;
  traceId: string;

  constructor(message: string, opts: { code?: string; status?: number; detail?: unknown; traceId?: string } = {}) {
    super(message || '请求失败');
    this.name = 'ApiError';
    this.code = opts.code ?? 'ERROR';
    this.status = opts.status ?? 0;
    this.detail = opts.detail ?? null;
    this.traceId = opts.traceId ?? '';
  }
}

/** 后端返回的资源路径（`/papers/...`）→ 可访问 URL（开发模式下经 Vite 代理）。 */
export function assetUrl(path?: string | null): string {
  if (!path) return '';
  if (/^(https?:|data:|blob:)/.test(path)) return path;
  return `${API_ORIGIN}${path.startsWith('/') ? '' : '/'}${path}`;
}

type Query = Record<string, unknown>;

function buildQuery(query?: Query): string {
  if (!query) return '';
  const parts: string[] = [];
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    if (Array.isArray(v)) {
      for (const item of v) parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(item))}`);
    } else {
      parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
    }
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  query?: Query;
  form?: FormData;
  signal?: AbortSignal;
}

/** 会话过期 → 回登录页（见下方 `toLoginIfExpired`）。 */
let kickedToLogin = false;

/**
 * `/cylit/`、`/api/v1/`、`/papers/` 三处在 nginx 上挂了 `auth_request`
 * （见 `deploy/nginx/cylit.conf`）：cookie 失效时**不会**把请求转到后端，
 * 而是直接回 401 + `{error:{code:"UNAUTHORIZED"}}`。前端遇到它就把人送回登录页 ——
 * 否则 12 小时一到，整个 SPA 还开着，每个请求都只在角落里弹一个红字提示。
 *
 * ⚠️ 只认信封里的 `code`：**Cylit 后端自己也返回 401**（`/agent/*` 的"内部令牌不正确"），
 * 那是接口用法问题，不该把人踢出应用。
 */
function toLoginIfExpired(status: number, code: string): void {
  if (kickedToLogin || status !== 401 || code !== 'UNAUTHORIZED') return;
  kickedToLogin = true;               // 一次就够，别让并发的请求把页面刷来刷去
  if (typeof location !== 'undefined') location.href = '/Login';
}

/**
 * **可重试的瞬时失败**：nginx 连不上上游（重启窗口）时回 502/503/504。
 *
 * 为什么需要：部署时 `pm2 restart cylit-api` 有 1~2 秒没有上游，nginx 立刻回 502。
 * 阅读器是**逐页**拉 `/annotations/image-marks?key=page:N-bilingual`，正好撞进这个窗口的那一页
 * 就会弹一次「批注读取错误」，刷新又好了（用户 2026-12-04 报的正是这个）。
 * 只重试**幂等**的 GET，且最多两次（400ms / 900ms）—— 真故障时只多花 1.3 秒，不会把请求堆起来。
 */
const RETRY_STATUS = new Set([502, 503, 504]);
const RETRY_DELAYS = [400, 900];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** 一个请求要拿到的 `data` 的形状由调用方用泛型声明（后端是动态 JSON，不做运行时校验）。 */
export async function request<T = any>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, form, signal } = opts;
  const url = `${API_BASE}/${path.replace(/^\/+/, '')}${buildQuery(query)}`;
  const init: RequestInit = { method, signal, headers: {} };
  if (form) {
    init.body = form;
  } else if (body !== undefined) {
    (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  const retriable = method === 'GET';
  let res: Response | null = null;
  for (let attempt = 0; ; attempt += 1) {
    try {
      res = await fetch(url, init);
    } catch (err: any) {
      if (err?.name === 'AbortError') throw err;
      if (retriable && !signal?.aborted && attempt < RETRY_DELAYS.length) {
        await sleep(RETRY_DELAYS[attempt]);
        continue;
      }
      throw new ApiError(`无法连接后端（${API_BASE}）：${err?.message ?? err}`, { code: 'NETWORK_ERROR' });
    }
    if (retriable && RETRY_STATUS.has(res.status) && !signal?.aborted
        && attempt < RETRY_DELAYS.length) {
      try { await res.body?.cancel(); } catch { /* 丢掉这一响应体即可 */ }
      await sleep(RETRY_DELAYS[attempt]);
      continue;
    }
    break;
  }

  const text = await res.text();
  let payload: any = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = null; }
  }

  if (!res.ok) {
    const err = payload?.error ?? {};
    toLoginIfExpired(res.status, err.code ?? '');
    throw new ApiError(err.message ?? `HTTP ${res.status}`, {
      code: err.code ?? `HTTP_${res.status}`,
      status: res.status,
      detail: err.detail ?? payload ?? text,
      traceId: payload?.trace_id ?? res.headers.get('x-trace-id') ?? '',
    });
  }
  if (payload && typeof payload === 'object' && 'data' in payload) {
    const data = payload.data;
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      Object.defineProperty(data, '__meta', {
        value: { message: payload.message ?? null, traceId: payload.trace_id ?? '' },
        enumerable: false, configurable: true,
      });
    }
    return data as T;
  }
  return payload as T;
}

export const api = {
  get: <T = any>(path: string, query?: Query, opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'GET', query }),
  post: <T = any>(path: string, body?: unknown, query?: Query, opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'POST', body: body ?? {}, query }),
  put: <T = any>(path: string, body?: unknown, query?: Query, opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'PUT', body: body ?? {}, query }),
  del: <T = any>(path: string, query?: Query, opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'DELETE', query }),
  upload: <T = any>(path: string, form: FormData, opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'POST', form }),
};

export function sseUrl(path: string, query?: Query): string {
  return `${API_BASE}/${path.replace(/^\/+/, '')}${buildQuery(query)}`;
}

/**
 * 下载接口产物（Markdown / zip 这类非 JSON 响应）→ Blob。
 *
 * 不走 `request()`：那条路会把响应当 JSON 解析（`res.text()` + `JSON.parse`），二进制包会坏掉。
 * 失败照样抛 `ApiError`，调用方能把错误提示出来（比直接开新标签页"悄悄失败"强）。
 */
export async function downloadBlob(path: string, query?: Query): Promise<Blob> {
  const url = `${API_BASE}/${path.replace(/^\/+/, '')}${buildQuery(query)}`;
  let res: Response;
  try {
    res = await fetch(url);
  } catch (err: any) {
    throw new ApiError(`无法连接后端（${API_BASE}）：${err?.message ?? err}`, { code: 'NETWORK_ERROR' });
  }
  if (!res.ok) {
    // 出错时后端仍然回 JSON 信封，尽量把 message 取出来
    const text = await res.text().catch(() => '');
    let payload: any = null;
    try { payload = text ? JSON.parse(text) : null; } catch { payload = null; }
    const err = payload?.error ?? {};
    toLoginIfExpired(res.status, err.code ?? '');
    throw new ApiError(err.message ?? `HTTP ${res.status}`, {
      code: err.code ?? `HTTP_${res.status}`,
      status: res.status,
      detail: err.detail ?? payload ?? text,
      traceId: payload?.trace_id ?? res.headers.get('x-trace-id') ?? '',
    });
  }
  return res.blob();
}
