// 类型由后端 OpenAPI 生成（pnpm gen:api），请求只走 /api。
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

const TOKEN_KEY = "avelis.token";
const EXPIRY_KEY = "avelis.token.expiry"; // {issued, expires}：毫秒时间戳，用来决定何时续期

type Issued = { access_token: string; expires_at: string; expires_in?: number };

export const token = {
  get: () => sessionStorage.getItem(TOKEN_KEY),
  set: (value: string) => sessionStorage.setItem(TOKEN_KEY, value),
  /** 登录、改密码、续期拿到新凭证时一起记下有效期。 */
  issue: (out: Issued) => {
    sessionStorage.setItem(TOKEN_KEY, out.access_token);
    // 用相对时长 expires_in 算本地到期时间，不受浏览器与服务器时钟差影响
    const now = Date.now();
    const expires = out.expires_in != null ? now + out.expires_in * 1000 : Date.parse(out.expires_at);
    sessionStorage.setItem(EXPIRY_KEY, JSON.stringify({ issued: now, expires }));
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
let refreshing: Promise<void> | null = null;

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
    refreshing = (async () => {
      const response = await fetch("/api/auth/refresh", { method: "POST", headers: { Authorization: `Bearer ${value}` } });
      if (response.ok && token.get() === value) token.issue(await response.json());
    })().catch(() => undefined).finally(() => { refreshing = null; });
    await refreshing;
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
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
  }
}

const retries = new WeakMap<Request, Request>(); // 发出前的副本：续期期间用旧凭证撞上 401 时换新凭证重发一次

const auth: Middleware = {
  async onRequest({ request }) {
    if (refreshing) await refreshing;
    const value = token.get();
    if (value) request.headers.set("Authorization", `Bearer ${value}`);
    retries.set(request, request.clone());
    return request;
  },
  async onResponse({ response, request }) {
    // 发出时用的凭证已被续期替换：换上新凭证重发一次，不当作会话过期
    const sent = request.headers.get("Authorization")?.replace(/^Bearer /, "");
    const copy = retries.get(request);
    if (response.status === 401 && copy && sent && token.get() && sent !== token.get()) {
      copy.headers.set("Authorization", `Bearer ${token.get()}`);
      return fetch(copy);
    }
    // 会话过期或被注销（改密码、被停用）：回到登录页并带上原因与原页面；登录、改密码接口自己处理 401/403
    if (response.status === 401 && !new URL(request.url).pathname.startsWith("/api/auth/")) {
      token.clear();
      if (!location.pathname.startsWith("/login")) {
        const from = encodeURIComponent(location.pathname + location.search);
        location.assign(`/login?reason=expired&from=${from}`);
      }
    }
    return response;
  },
};

export const api = createClient<paths>({ baseUrl: "" });
api.use(auth);

/** 把 openapi-fetch 的结果变成“成功返回数据，失败抛 ApiError（带后端的中文原因）”。 */
export async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await promise;
  if (error !== undefined || !response.ok) {
    const detail = (error as { detail?: unknown })?.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail) ? "输入不合法" : `请求失败（${response.status}）`;
    throw new ApiError(response.status, message, error);
  }
  return data as T;
}

export type LoginResult = { mustChangePassword: boolean; expiresAt: string };

export async function login(username: string, password: string): Promise<LoginResult> {
  const body = new URLSearchParams({ username, password });
  const response = await fetch("/api/auth/token", { method: "POST", body });
  const json = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, json.detail ?? "登录失败");
  token.issue(json);
  return { mustChangePassword: !!json.must_change_password, expiresAt: json.expires_at };
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST", headers: { Authorization: `Bearer ${token.get()}` } });
  token.clear();
}
