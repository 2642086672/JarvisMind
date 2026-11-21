import { getActiveSpace, getOwnerToken, notifyUnauthorized } from "../session";

/**
 * Typed HTTP client for the JarvisMind API.
 *
 * Every error the server returns uses one envelope, ``{"error": {"code", "message",
 * "details"}}``. It is normalised into ``ApiError`` so pages can show the server's
 * own message. Request bodies and tokens are never logged or stored here.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export type QueryValue = string | number | boolean | null | undefined;
export type QueryParams = Record<string, QueryValue>;

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  query?: QueryParams;
  /** Send the owner credential (default true). */
  auth?: boolean;
  /** Use this credential instead of the signed-in one. Used only by sign-in. */
  token?: string;
  /** Send the active memory space header (default true). */
  space?: boolean;
  signal?: AbortSignal;
}

export function buildQuery(query?: QueryParams): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.append(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "validation_error" && Array.isArray(error.details)) {
      const messages = error.details
        .map((item) =>
          typeof item === "object" && item !== null && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : "",
        )
        .filter((text) => text.length > 0);
      if (messages.length > 0) return `${error.message}：${messages.join("；")}`;
    }
    return error.message;
  }
  if (error instanceof Error) return error.message;
  return "发生未知错误";
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function parseBody(text: string): unknown {
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

function toApiError(status: number, payload: unknown, fallback: string): ApiError {
  const envelope = payload as { error?: { code?: unknown; message?: unknown; details?: unknown } } | undefined;
  const error = envelope?.error;
  if (error && typeof error.code === "string" && typeof error.message === "string") {
    return new ApiError(status, error.code, error.message, error.details);
  }
  return new ApiError(status, "http_error", fallback || `请求失败（HTTP ${status}）`);
}

export function buildHeaders(options: RequestOptions, accept: string): Headers {
  const headers = new Headers({ Accept: accept });
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (options.auth === false) return headers;
  const token = options.token ?? getOwnerToken();
  if (!token) throw new ApiError(401, "unauthorized", "请先使用所有者令牌登录");
  headers.set("Authorization", `Bearer ${token}`);
  if (options.space !== false) headers.set("X-JarvisMind-Space", getActiveSpace());
  return headers;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const init: RequestInit = {
    method: options.method ?? "GET",
    headers: buildHeaders(options, "application/json"),
    cache: "no-store",
    signal: options.signal,
  };
  if (options.body !== undefined) init.body = JSON.stringify(options.body);

  let response: Response;
  try {
    response = await fetch(`${path}${buildQuery(options.query)}`, init);
  } catch (cause) {
    if (isAbortError(cause)) throw cause;
    throw new ApiError(0, "network_error", "无法连接 JarvisMind API，请确认服务正在运行");
  }

  const text = await response.text();
  const payload = parseBody(text);
  if (!response.ok) {
    if (response.status === 401 && options.auth !== false && options.token === undefined) {
      notifyUnauthorized();
    }
    throw toApiError(response.status, payload, response.statusText);
  }
  return payload as T;
}

/**
 * Download an authenticated file (JSON or CSV export). A plain link cannot carry
 * the owner credential, so the bytes are fetched with the header and saved locally.
 */
export async function downloadFile(path: string, query: QueryParams, filename: string): Promise<void> {
  const response = await fetch(`${path}${buildQuery(query)}`, {
    headers: buildHeaders({}, "*/*"),
    cache: "no-store",
  });
  if (!response.ok) {
    const payload = parseBody(await response.text());
    if (response.status === 401) notifyUnauthorized();
    throw toApiError(response.status, payload, response.statusText);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
