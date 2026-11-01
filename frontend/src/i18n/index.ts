import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { useSyncExternalStore } from "react";
import { initReactI18next } from "react-i18next";

import en from "./locales/en.json";
import zhTW from "./locales/zh-TW.json";

// 界面语言：简体中文（默认）、繁体中文、英文（ADR 0015）。
// 键就是简体原文（gettext 风格，与后端 .po 的 msgid 一致）：写 t("保存")，简体直接显示原文，繁体与英文查语言包。
// 选择只存在本机浏览器；没选过时按浏览器语言，匹配不上用简体中文。用户与模型产出的内容不翻译。
export const LANGUAGES = [
  { code: "zh-CN", label: "简体中文" },
  { code: "zh-TW", label: "繁體中文" },
  { code: "en", label: "English" },
] as const;
export type Language = (typeof LANGUAGES)[number]["code"];

const KEY = "avelis.lang";
export const DEFAULT_LANGUAGE: Language = "zh-CN";

/** 浏览器语言标签归到支持的三种之一：港澳台与 Hant 用繁体，其余中文用简体，en-* 用英文。 */
export function normalizeLanguage(tag: string | null | undefined): Language | undefined {
  const lower = (tag ?? "").toLowerCase();
  if (!lower) return undefined;
  if (/^zh-(tw|hk|mo)\b|^zh-hant\b/.test(lower)) return "zh-TW";
  if (lower === "zh" || lower.startsWith("zh-")) return "zh-CN";
  if (lower === "en" || lower.startsWith("en-")) return "en";
  return undefined;
}

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: { "zh-CN": { translation: {} }, "zh-TW": { translation: zhTW }, en: { translation: en } },
    supportedLngs: LANGUAGES.map((l) => l.code),
    fallbackLng: DEFAULT_LANGUAGE,
    keySeparator: false, // 键是原文，里面的“.”“：”不是层级
    nsSeparator: false,
    initAsync: false, // 语言包是静态打包的：同步初始化，模块级常量里的 t() 也能取到当前语言
    // 只在用户主动切换时写入本机（setLanguage），自动检测的结果不落盘，浏览器语言变了还会跟着变
    detection: { order: ["localStorage", "navigator"], lookupLocalStorage: KEY, caches: [],
      convertDetectedLanguage: (lng: string) => normalizeLanguage(lng) ?? lng },
    interpolation: { escapeValue: false }, // React 自己转义
    returnNull: false,
  });

/** 当前界面语言（已归一到支持的三种）。 */
export function currentLanguage(): Language {
  return normalizeLanguage(i18n.resolvedLanguage) ?? DEFAULT_LANGUAGE;
}

document.documentElement.lang = currentLanguage();
i18n.on("languageChanged", () => { document.documentElement.lang = currentLanguage(); });

/** 组件、工具函数与模块级常量共用的翻译函数。 */
export const t = i18n.t.bind(i18n) as (key: string, options?: Record<string, unknown>) => string;

/** 切换语言：记在本机后整页重新加载，模块级的标签表与已格式化的内容都按新语言重新生成。 */
export function setLanguage(next: Language) {
  if (next === currentLanguage()) return;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    // 无痕模式等拿不到存储时只在本次生效
    void i18n.changeLanguage(next);
    return;
  }
  window.location.reload();
}

const subscribe = (l: () => void) => { i18n.on("languageChanged", l); return () => i18n.off("languageChanged", l); };

/** 当前语言（拿不到本机存储、只在本次切换时，LanguageRoot 靠它重新挂载界面）。 */
export function useLanguage(): Language {
  return useSyncExternalStore(subscribe, currentLanguage);
}

export default i18n;
