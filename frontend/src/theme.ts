import { useSyncExternalStore } from "react";

// 主题偏好：跟随系统 / 深色 / 浅色。偏好只存在本机浏览器；解析结果写到 <html data-theme>，tokens.css 按它取值。
export type ThemePref = "system" | "dark" | "light";

const KEY = "avelis.theme";
const media = window.matchMedia("(prefers-color-scheme: light)");
const listeners = new Set<() => void>();

function read(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === "dark" || v === "light" || v === "system" ? v : "light";
  } catch {
    return "light";
  }
}

let pref = read();

function apply() {
  document.documentElement.dataset.theme = pref === "system" ? (media.matches ? "light" : "dark") : pref;
  listeners.forEach((l) => l());
}

media.addEventListener("change", () => { if (pref === "system") apply(); });
apply();

export function setThemePref(next: ThemePref) {
  pref = next;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    // 无痕模式等拿不到存储时只在本次生效
  }
  apply();
}

export function useThemePref(): ThemePref {
  return useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, () => pref);
}
