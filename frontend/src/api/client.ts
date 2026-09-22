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

  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err: any) {
    if (err?.name === 'AbortError') throw err;
    throw new ApiError(`无法连接后端（${API_BASE}）：${err?.message ?? err}`, { code: 'NETWORK_ERROR' });
  }

  const text = await res.text();
  let payload: any = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = null; }
  }

  if (!res.ok) {
    const err = payload?.error ?? {};
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
