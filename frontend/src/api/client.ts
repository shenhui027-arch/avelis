// 类型由后端 OpenAPI 生成（pnpm gen:api），请求只走 /api。
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

const TOKEN_KEY = "avelis.token";

export const token = {
  get: () => sessionStorage.getItem(TOKEN_KEY),
  set: (value: string) => sessionStorage.setItem(TOKEN_KEY, value),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
};

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
  }
}

const auth: Middleware = {
  onRequest({ request }) {
    const value = token.get();
    if (value) request.headers.set("Authorization", `Bearer ${value}`);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401) {
      token.clear();
      if (!location.pathname.startsWith("/login")) location.assign("/login");
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

export async function login(username: string, password: string): Promise<void> {
  const body = new URLSearchParams({ username, password });
  const response = await fetch("/api/auth/token", { method: "POST", body });
  const json = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, json.detail ?? "登录失败");
  token.set(json.access_token);
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST", headers: { Authorization: `Bearer ${token.get()}` } });
  token.clear();
}
