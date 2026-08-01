// 轻量 API 客户端:fetch + Bearer。access(15min)过期时用 refresh_token 静默续期(旋转式)并重试一次;
// 并发 401 共享同一 refresh promise(单飞),避免旋转竞态把彼此的新 refresh 作废;
// 二次 401 才清会话回登录(带 ?next=当前路径)。API 路径以 output/TravelFlow AI-architecture.md §10 为准。

import {
  clearSession,
  getRefreshToken,
  getToken,
  saveSession,
  type Role,
} from "@/lib/auth";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
  ) {
    super(message);
  }
}

interface StructuredApiErrorDetail {
  code: string;
  message: string;
}

function isStructuredApiErrorDetail(
  detail: unknown,
): detail is StructuredApiErrorDetail {
  if (typeof detail !== "object" || detail === null) return false;
  const value = detail as Record<string, unknown>;
  return typeof value.code === "string" && typeof value.message === "string";
}

// 后端 POST /auth/refresh 契约(auth.py TokenResponse):旋转式,旧 refresh 立即作废,须存新 access+refresh。
interface RefreshResponse {
  access_token: string;
  refresh_token: string;
  org: { id: string; slug: string; name: string; role: string };
}

// 单飞:并发 401 共享同一个 refresh promise,防止旋转竞态把彼此的新 refresh 作废。
let refreshInFlight: Promise<boolean> | null = null;

async function doRefresh(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;
  try {
    const resp = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!resp.ok) return false;
    const data = (await resp.json()) as RefreshResponse;
    // 登录、退出或另一轮刷新可能已替换会话，旧响应不得覆盖新 token。
    if (getRefreshToken() !== refreshToken) return Boolean(getToken());
    saveSession({
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
      org: { ...data.org, role: data.org.role as Role },
    });
    return true;
  } catch {
    return false;
  }
}

function refreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = doRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

function redirectToLogin() {
  if (
    typeof window !== "undefined" &&
    !location.pathname.startsWith("/login")
  ) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.href = `/login?next=${next}`;
  }
}

// 统一错误抛出:401(续期兜底仍失败)清会话跳登录;其余解析后端 detail。
async function raiseForStatus(
  resp: Response,
  requestToken?: string | null,
): Promise<void> {
  if (resp.status === 401) {
    if (requestToken !== undefined) {
      // 只允许使用当前 token 发出的请求清会话，避免旧请求踢掉刚登录的新会话。
      if (getToken() === requestToken) {
        clearSession();
        redirectToLogin();
      }
      throw new ApiError(401, "登录已过期");
    }
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    let code: string | undefined;
    try {
      const body = (await resp.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (isStructuredApiErrorDetail(body.detail)) {
        detail = body.detail.message;
        code = body.detail.code;
      } else if (body.detail) detail = JSON.stringify(body.detail);
    } catch {
      // 保持 statusText
    }
    throw new ApiError(resp.status, detail, code);
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
  retry = true,
  authenticated = true,
): Promise<T> {
  const token = authenticated ? getToken() : null;
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const resp = await fetch(`${API_BASE}/api/v1${path}`, { ...init, headers });

  // 401:静默续期成功则用新 token 重试一次;失败落到 raiseForStatus 清会话跳登录。
  if (resp.status === 401 && authenticated && retry) {
    // 请求在途期间若已有新会话，直接用新 access token 重试，不再刷新旧会话。
    if (getToken() !== token || (await refreshSession())) {
      return request<T>(path, init, false, true);
    }
    // refresh 失败期间也可能刚完成新登录。
    if (getToken() !== token) return request<T>(path, init, false, true);
  }
  await raiseForStatus(resp, authenticated ? token : undefined);
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

/**
 * 带鉴权的文件流下载:解析 Content-Disposition 文件名并触发浏览器保存。
 * Content-Disposition 不可用时,用调用方传入的 fallbackName 或响应类型补齐扩展名。
 */
function extFromFilename(name?: string): string | null {
  const base = name?.split(/[\\/]/).pop() ?? "";
  const dot = base.lastIndexOf(".");
  return dot > 0 && dot < base.length - 1 ? base.slice(dot + 1) : null;
}

function extFromContentType(contentType: string): string | null {
  const normalized = contentType.split(";")[0]?.trim().toLowerCase();
  if (normalized === "application/pdf") return "pdf";
  if (normalized === "application/zip") return "zip";
  if (
    normalized ===
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
  ) {
    return "docx";
  }
  if (
    normalized ===
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
  ) {
    return "pptx";
  }
  return null;
}

function fallbackDownloadName(
  path: string,
  fallbackName: string | undefined,
  contentType: string,
): string {
  const ext =
    extFromFilename(fallbackName) ??
    extFromFilename(path) ??
    extFromContentType(contentType) ??
    "bin";
  const name = fallbackName ?? "download";
  return extFromFilename(name) ? name : `${name}.${ext}`;
}

export async function downloadFile(
  path: string,
  fallbackName?: string,
  retry = true,
): Promise<void> {
  const token = getToken();
  const headers = new Headers();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(`${API_BASE}/api/v1${path}`, { headers });

  if (resp.status === 401 && retry) {
    if (getToken() !== token || (await refreshSession())) {
      return downloadFile(path, fallbackName, false);
    }
    if (getToken() !== token) return downloadFile(path, fallbackName, false);
  }
  await raiseForStatus(resp, token);

  const blob = await resp.blob();
  const disposition = resp.headers.get("content-disposition") ?? "";
  const utf8Match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  const plainMatch = /filename="?([^";]+)"?/i.exec(disposition);
  const filename = utf8Match
    ? decodeURIComponent(utf8Match[1])
    : (plainMatch?.[1] ??
      fallbackDownloadName(
        path,
        fallbackName,
        resp.headers.get("content-type") ?? blob.type,
      ));

  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/**
 * 带鉴权取二进制资源并返回 object URL(生成图片内联预览等场景:
 * <img src> 带不了 Bearer,只能 fetch blob 再转)。调用方负责 revokeObjectURL。
 */
export async function fetchBlob(
  path: string,
  retry = true,
  signal?: AbortSignal,
): Promise<Blob> {
  const token = getToken();
  const headers = new Headers();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(`${API_BASE}/api/v1${path}`, { headers, signal });

  if (resp.status === 401 && retry) {
    if (getToken() !== token || (await refreshSession())) {
      return fetchBlob(path, false, signal);
    }
    if (getToken() !== token) return fetchBlob(path, false, signal);
  }
  await raiseForStatus(resp, token);
  return resp.blob();
}

export async function fetchObjectUrl(
  path: string,
  retry = true,
  signal?: AbortSignal,
): Promise<string> {
  return URL.createObjectURL(await fetchBlob(path, retry, signal));
}

/**
 * 带鉴权的 SSE POST:逐条解析 `data:` 行 JSON 并回调。
 * EventSource 不支持自定义 header,故用 fetch + ReadableStream 消费。
 * 401 续期与重试只在流建立前做;流一旦开始读取即不再重试(流中断不重放)。
 */
export async function postSse(
  path: string,
  body: unknown,
  onEvent: (data: unknown) => void,
  signal?: AbortSignal,
  retry = true,
  onOpen?: (response: Response) => void,
  onJson?: (status: number, data: unknown) => void,
): Promise<void> {
  const token = getToken();
  const headers = new Headers({ "Content-Type": "application/json" });
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const resp = await fetch(`${API_BASE}/api/v1${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal,
  });

  if (resp.status === 401 && retry) {
    if (getToken() !== token || (await refreshSession())) {
      return postSse(path, body, onEvent, signal, false, onOpen, onJson);
    }
    if (getToken() !== token) {
      return postSse(path, body, onEvent, signal, false, onOpen, onJson);
    }
  }
  await raiseForStatus(resp, token);
  // 非流式 JSON 应答(如 202 入队回执):交给 onJson,不进入 SSE 读取
  const contentType = resp.headers.get("content-type") ?? "";
  if (onJson && !contentType.includes("text/event-stream")) {
    onJson(resp.status, await resp.json().catch(() => null));
    return;
  }
  onOpen?.(resp);
  if (!resp.body) throw new ApiError(resp.status, "响应不含流式 body");

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
      }
    }
  }
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  publicGet: <T>(path: string) => request<T>(path, undefined, false, false),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  postWithHeaders: <T>(path: string, body: unknown, headers: HeadersInit) =>
    request<T>(path, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    }),
  postAbortable: <T>(path: string, body: unknown, signal: AbortSignal) =>
    request<T>(path, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  publicPost: <T>(path: string, body?: unknown) =>
    request<T>(
      path,
      {
        method: "POST",
        body: body === undefined ? undefined : JSON.stringify(body),
      },
      false,
      false,
    ),
  postForm: <T>(path: string, form: FormData) =>
    request<T>(path, { method: "POST", body: form }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  delete: <T = void>(path: string) => request<T>(path, { method: "DELETE" }),
  deleteWithHeaders: <T = void>(path: string, headers: HeadersInit) =>
    request<T>(path, { method: "DELETE", headers }),
};
