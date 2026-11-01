import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { useSyncExternalStore } from "react";
import { initReactI18next } from "react-i18next";

import en from "./locales/en.json";
import zhCN from "./locales/zh-CN.json";
import zhTW from "./locales/zh-TW.json";

// 界面语言：简体中文（默认）、繁体中文、英文（ADR 0015）。
// 选择只存在本机浏览器；没选过时按浏览器语言，匹配不上用简体中文。用户与模型产出的内容不翻译。
export const LANGUAGES = [
  { code: "zh-CN", label: "简体中文" },
  { code: "zh-TW", label: "繁體中文" },
  { code: "en", label: "English" },
] as const;
export type Language = (typeof LANGUAGES)[number]["code"];

const KEY = "avelis.lang";
export const DEFAULT_LANGUAGE: Language = "zh-CN";

/** 浏览器语言标签归到支持的三种之一：港台与 Hant 用繁体，其余中文用简体，en-* 用英文。 */
export function normalizeLanguage(tag: string | null | undefined): Language | undefined {
  const t = (tag ?? "").toLowerCase();
  if (!t) return undefined;
  if (/^zh-(tw|hk|mo)\b|^zh-hant\b/.test(t)) return "zh-TW";
  if (t === "zh" || t.startsWith("zh-")) return "zh-CN";
  if (t === "en" || t.startsWith("en-")) return "en";
  return undefined;
}

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: { "zh-CN": { translation: zhCN }, "zh-TW": { translation: zhTW }, en: { translation: en } },
    supportedLngs: LANGUAGES.map((l) => l.code),
    fallbackLng: DEFAULT_LANGUAGE,
    // 只在用户主动切换时写入本机（setLanguage），自动检测的结果不落盘，浏览器语言变了还会跟着变
    detection: { order: ["localStorage", "navigator"], lookupLocalStorage: KEY, caches: [],
      convertDetectedLanguage: (lng: string) => normalizeLanguage(lng) ?? lng },
    interpolation: { escapeValue: false }, // React 自己转义
    returnNull: false,
  });

function applyDocument(lng: string) {
  document.documentElement.lang = lng;
}
applyDocument(i18n.resolvedLanguage ?? DEFAULT_LANGUAGE);
i18n.on("languageChanged", applyDocument);

/** 组件与工具函数共用的翻译函数。切换语言时整棵界面重新挂载（见 LanguageRoot），不需要每个组件订阅。 */
export const t = i18n.t.bind(i18n) as typeof i18n.t;

/** 动态键（状态码、枚举值）：语言包里有就翻译，没有就返回 fallback（通常是原始值）。 */
export function tx(key: string, fallback: string, options?: Record<string, unknown>): string {
  return i18n.exists(key) ? String((i18n.t as (k: string, o?: object) => unknown)(key, options)) : fallback;
}

/** 枚举值 → 当前语言标签的表：取值时才翻译，模块级常量也会跟着语言变。用法与普通对象一样（[k]、Object.entries）。 */
export function labelTable(prefix: string, keys: readonly string[]): Record<string, string> {
  const table: Record<string, string> = {};
  for (const k of keys) Object.defineProperty(table, k, { enumerable: true, get: () => tx(`${prefix}.${k}`, k) });
  return table;
}

/** 当前界面语言（已归一到支持的三种）。 */
export function currentLanguage(): Language {
  return (normalizeLanguage(i18n.resolvedLanguage) ?? DEFAULT_LANGUAGE);
}

export function setLanguage(next: Language) {
  try {
    localStorage.setItem(KEY, next);
  } catch {
    // 无痕模式等拿不到存储时只在本次生效
  }
  void i18n.changeLanguage(next);
}

const subscribe = (l: () => void) => { i18n.on("languageChanged", l); return () => i18n.off("languageChanged", l); };

export function useLanguage(): Language {
  return useSyncExternalStore(subscribe, currentLanguage);
}

export default i18n;
