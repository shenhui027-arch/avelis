// 类型由后端 OpenAPI 生成（pnpm gen:api），请求只走 /api。
import createClient, { type Middleware } from "openapi-fetch";
import { currentLanguage, t } from "../i18n";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

const TOKEN_KEY = "avelis.token";
const EXPIRY_KEY = "avelis.token.expiry"; // {issued, expires}：毫秒时间戳，用来决定何时续期

type Issued = { access_token: string; expires_at: string; expires_in?: number };

let hadSession = (() => {
  try {
    return !!sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return false;
  }
})();

export const token = {
  get: () => sessionStorage.getItem(TOKEN_KEY),
  set: (value: string) => {
    sessionStorage.setItem(TOKEN_KEY, value);
    hadSession = true;
  },
  /** 登录、改密码、续期拿到新凭证时一起记下有效期。 */
  issue: (out: Issued) => {
    sessionStorage.setItem(TOKEN_KEY, out.access_token);
    // 用相对时长 expires_in 算本地到期时间，不受浏览器与服务器时钟差影响
    const now = Date.now();
    const expires = out.expires_in != null ? now + out.expires_in * 1000 : Date.parse(out.expires_at);
    sessionStorage.setItem(EXPIRY_KEY, JSON.stringify({ issued: now, expires }));
    hadSession = true;
  },
  expiry: (): { issued: number; expires: number } | null => {
    try {
      return JSON.parse(sessionStorage.getItem(EXPIRY_KEY) ?? "null");
    } catch {
      return null;
    }
  },
  clear: () => {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(EXPIRY_KEY);
  },
};

// 正在续期时的那次请求：其他请求先等它完成再带上新凭证，避免拿着刚被注销的旧凭证收到 401
let refreshing: Promise<boolean> | null = null;

async function refreshSession(): Promise<boolean> {
  if (refreshing) return refreshing;
  const value = token.get();
  refreshing = (async () => {
    const response = await fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "include",
      headers: value ? { Authorization: `Bearer ${value}` } : undefined,
    });
    if (!response.ok) return false;
    const json = await response.json();
    token.issue(json);
    hadSession = true;
    return true;
  })().catch(() => false).finally(() => { refreshing = null; });
  return refreshing;
}

/** 使用中自动续期：凭证用过一半、且这段时间里用户有过操作时，换一张新凭证；一直闲置则照常到期回登录页。
 *  检查间隔按凭证有效期定（四分之一，5 秒到 1 分钟之间），再短的有效期也不会错过续期窗口。 */
export function keepSessionAlive(): () => void {
  let active = 0;
  let timer = 0;
  const mark = () => { active = Date.now(); };
  const check = async () => {
    const value = token.get(), expiry = token.expiry();
    if (!value || !expiry || refreshing || active < expiry.issued) return;
    const now = Date.now();
    if (now >= expiry.expires || now < expiry.issued + (expiry.expires - expiry.issued) / 2) return;
    await refreshSession();
  };
  const schedule = () => {
    const expiry = token.expiry();
    const span = expiry ? expiry.expires - expiry.issued : 60_000;
    timer = window.setTimeout(() => { void check().finally(schedule); }, Math.min(60_000, Math.max(5_000, span / 4)));
  };
  const events = ["pointerdown", "keydown", "wheel"] as const;
  events.forEach((e) => window.addEventListener(e, mark, { passive: true }));
  schedule();
  const onVisible = () => { if (document.visibilityState === "visible") void check(); };
  document.addEventListener("visibilitychange", onVisible);
  return () => {
    events.forEach((e) => window.removeEventListener(e, mark));
    window.clearTimeout(timer);
    document.removeEventListener("visibilitychange", onVisible);
  };
}

export class ApiError extends Error {
  public code?: string;
  public retryAfter?: number;
  public challengeToken?: string;
  public totpRequired?: boolean;
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
    const detail = (body as { detail?: unknown } | undefined)?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const obj = detail as Record<string, unknown>;
      if (typeof obj.code === "string") this.code = obj.code;
      if (typeof obj.retry_after === "number") this.retryAfter = obj.retry_after;
      if (typeof obj.challenge_token === "string") this.challengeToken = obj.challenge_token;
      if (typeof obj.totp_required === "boolean") this.totpRequired = obj.totp_required;
    }
  }
}

export function parseApiError(status: number, body: unknown, fallback: string): ApiError {
  const obj = body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : undefined;
  const detail = obj?.detail;
  let message = fallback;
  if (typeof detail === "string" && detail) {
    message = detail;
  } else if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const nested = (detail as Record<string, unknown>).detail ?? (detail as Record<string, unknown>).message;
    if (typeof nested === "string" && nested) message = nested;
  } else if (Array.isArray(detail)) {
    message = t("输入不合法");
  } else if (typeof obj?.message === "string" && obj.message) {
    message = obj.message;
  } else if (status === 413) {
    message = t("上传文件过大，超出服务端限制");
  }
  return new ApiError(status, message, body);
}

const retries = new WeakMap<Request, Request>(); // 发出前的副本：续期期间用旧凭证撞上 401 时换新凭证重发一次
const retriedOnce = new WeakSet<Request>();

const auth: Middleware = {
  async onRequest({ request }) {
    if (refreshing) await refreshing;
    const value = token.get();
    if (value) request.headers.set("Authorization", `Bearer ${value}`);
    // 后端按界面语言返回错误与提示（ADR 0015）
    request.headers.set("Accept-Language", currentLanguage());
    if (!retriedOnce.has(request)) retries.set(request, request.clone());
    return request;
  },
  async onResponse({ response, request }) {
    const path = new URL(request.url).pathname;
    if (response.ok && !path.startsWith("/api/auth/")) {
      hadSession = true;
      return response;
    }
    if (response.status !== 401 || path.startsWith("/api/auth/")) return response;

    // 发出时用的凭证已被续期替换或可通过 HttpOnly refresh cookie 续期：换新凭证重发一次
    if (refreshing) await refreshing;
    const sent = request.headers.get("Authorization")?.replace(/^Bearer /, "");
    const copy = retries.get(request);
    if (copy && !retriedOnce.has(request)) {
      let canRetry = !!(sent && token.get() && sent !== token.get());
      if (!canRetry) canRetry = await refreshSession();
      if (canRetry) {
        const nextToken = token.get();
        if (nextToken) copy.headers.set("Authorization", `Bearer ${nextToken}`);
        else copy.headers.delete("Authorization");
        retriedOnce.add(copy);
        return fetch(copy);
      }
    }

    // 会话过期或被注销（改密码、被停用）：只有曾经建立过会话时才提示 reason=expired
    const wasActive = hadSession || !!token.get();
    token.clear();
    hadSession = false;
    if (wasActive && !location.pathname.startsWith("/login")) {
      const from = encodeURIComponent(location.pathname + location.search);
      location.assign(`/login?reason=expired&from=${from}`);
    }
    return response;
  },
};

export const api = createClient<paths>({ baseUrl: "", credentials: "include" });
api.use(auth);

/** 把 openapi-fetch 的结果变成“成功返回数据，失败抛 ApiError（带后端按界面语言给出的原因）”。 */
export async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await promise;
  if (error !== undefined || !response.ok) {
    throw parseApiError(response.status, error, t("请求失败（{{status}}）", { status: response.status }));
  }
  return data as T;
}

export type LoginResult = { mustChangePassword: boolean; expiresAt: string };

export async function login(
  username: string,
  password: string,
  options?: { rememberMe?: boolean; totpCode?: string },
): Promise<LoginResult> {
  const body = new URLSearchParams({
    username,
    password,
    remember_me: options?.rememberMe ? "true" : "false",
  });
  if (options?.totpCode) body.set("totp_code", options.totpCode);
  const response = await fetch("/api/auth/token", {
    method: "POST",
    body,
    credentials: "include",
    headers: { "Accept-Language": currentLanguage() },
  });
  const json = await response.json().catch(() => ({}));
  if (!response.ok) throw parseApiError(response.status, json, t("登录失败"));
  token.issue(json);
  return { mustChangePassword: !!json.must_change_password, expiresAt: json.expires_at };
}

export async function verifyTotpLogin(challengeToken: string, code: string): Promise<LoginResult> {
  const response = await fetch("/api/auth/totp/verify", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", "Accept-Language": currentLanguage() },
    body: JSON.stringify({ challenge_token: challengeToken, code }),
  });
  const json = await response.json().catch(() => ({}));
  if (!response.ok) throw parseApiError(response.status, json, t("登录失败"));
  token.issue(json);
  return { mustChangePassword: !!json.must_change_password, expiresAt: json.expires_at };
}

export async function logout(): Promise<void> {
  const value = token.get();
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "include",
      headers: value ? { Authorization: `Bearer ${value}` } : undefined,
    });
  } finally {
    token.clear();
    hadSession = false;
  }
}

/** 统一底层 fetch 封装：携带 cookie/Bearer/语言头，401 单飞续期并跳转登录，默认解析 ApiError。 */
export async function apiFetch(
  input: string,
  init: RequestInit & { throwOnError?: boolean } = {},
): Promise<Response> {
  const { throwOnError = true, ...reqInit } = init;
  if (refreshing) await refreshing;
  const buildHeaders = (authOverride?: string | null) => {
    const headers = new Headers(reqInit.headers);
    const tok = authOverride !== undefined ? authOverride : token.get();
    if (tok && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${tok}`);
    if (!headers.has("Accept-Language")) headers.set("Accept-Language", currentLanguage());
    return headers;
  };
  let response = await fetch(input, { ...reqInit, credentials: "include", headers: buildHeaders() });
  const path = new URL(input, location.origin).pathname;
  if (response.ok && !path.startsWith("/api/auth/")) {
    hadSession = true;
    return response;
  }
  if (response.status === 401 && path.startsWith("/api/") && !path.startsWith("/api/auth/")) {
    const sent = token.get();
    if (refreshing) await refreshing;
    let canRetry = !!(sent && token.get() && sent !== token.get());
    if (!canRetry) canRetry = await refreshSession();
    if (canRetry) {
      response = await fetch(input, {
        ...reqInit,
        credentials: "include",
        headers: buildHeaders(token.get()),
      });
      if (response.ok) {
        hadSession = true;
        return response;
      }
    }
    if (response.status === 401) {
      const wasActive = hadSession || !!token.get();
      token.clear();
      hadSession = false;
      if (wasActive && !location.pathname.startsWith("/login")) {
        const from = encodeURIComponent(location.pathname + location.search);
        location.assign(`/login?reason=expired&from=${from}`);
      }
    }
  }
  if (!response.ok && throwOnError) {
    const body = await response.clone().json().catch(() => undefined);
    throw parseApiError(response.status, body, t("请求失败（{{status}}）", { status: response.status }));
  }
  return response;
}
