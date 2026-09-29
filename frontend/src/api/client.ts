import { useAuth } from '../store/auth';

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const BASE = '/api/v1';

type Query = Record<string, string | number | boolean | null | undefined>;

function qs(params?: Query): string {
  if (!params) return '';
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '');
  return entries.length ? '?' + new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString() : '';
}

export async function api<T = unknown>(path: string, init: RequestInit & { query?: Query } = {}): Promise<T> {
  const token = useAuth.getState().token;
  const res = await fetch(BASE + path + qs(init.query), {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  });
  if (res.status === 401 && token) {
    useAuth.getState().logout('Your session expired. Please sign in again.');
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d: { loc?: string[]; msg: string }) => `${d.loc?.slice(1).join('.')}: ${d.msg}`).join('; ')
      : detail || data?.message || res.statusText;
    throw new ApiError(res.status, message);
  }
  return data as T;
}

export const get = <T,>(path: string, query?: Query) => api<T>(path, { query });
export const post = <T,>(path: string, body?: unknown, query?: Query) =>
  api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body), query });
export const put = <T,>(path: string, body?: unknown, query?: Query) =>
  api<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}), query });
export const patch = <T,>(path: string, body?: unknown) => api<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) });
export const del = <T,>(path: string, query?: Query) => api<T>(path, { method: 'DELETE', query });
