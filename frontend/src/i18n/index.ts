import i18n from "i18next";
import { initReactI18next } from "react-i18next";

// 界面语言：简体中文（默认）、繁体中文、英文（ADR 0015）。
// 键就是简体原文（gettext 风格，与后端 .po 的 msgid 一致）：写 t("保存")，简体直接显示原文，繁体与英文按需异步分包加载。
// 选择只存在本机浏览器；没选过时按浏览器语言，匹配不上用简体中文。用户与模型产出的内容不翻译。
export const LANGUAGES = [
  { code: "zh-CN", label: "简体中文" },
  { code: "zh-TW", label: "繁體中文" },
  { code: "en", label: "English" },
] as const;
export type Language = (typeof LANGUAGES)[number]["code"];

const KEY = "avelis.lang";
const PARAM = "lang"; // 拿不到浏览器存储时，选择记在地址栏 ?lang=
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

/** 按 querystring -> localStorage -> sessionStorage -> navigator 顺序同步探测当前语言，仅按需加载对应语言包。 */
function detectInitialLanguage(): Language {
  try {
    const q = new URLSearchParams(window.location.search).get(PARAM);
    const fromQuery = normalizeLanguage(q);
    if (fromQuery) return fromQuery;
  } catch {
    // 忽略不可用 URL 环境
  }
  for (const getStore of [() => localStorage, () => sessionStorage]) {
    try {
      const hit = normalizeLanguage(getStore().getItem(KEY));
      if (hit) return hit;
    } catch {
      // 禁用存储时跳过
    }
  }
  try {
    const candidates = navigator.languages?.length ? navigator.languages : [navigator.language];
    for (const tag of candidates) {
      const hit = normalizeLanguage(tag);
      if (hit) return hit;
    }
  } catch {
    // 忽略不可用 navigator 环境
  }
  return DEFAULT_LANGUAGE;
}

const initialLang = detectInitialLanguage();
const initialResources: Record<string, { translation: Record<string, string> }> = {
  "zh-CN": { translation: {} },
};
if (initialLang === "zh-TW") {
  initialResources["zh-TW"] = { translation: (await import("./locales/zh-TW.json")).default };
} else if (initialLang === "en") {
  initialResources.en = { translation: (await import("./locales/en.json")).default };
}

void i18n
  .use(initReactI18next)
  .init({
    resources: initialResources,
    lng: initialLang,
    supportedLngs: LANGUAGES.map((l) => l.code),
    fallbackLng: DEFAULT_LANGUAGE,
    keySeparator: false, // 键是原文，里面的“.”“：”不是层级
    nsSeparator: false,
    initAsync: false, // 仅加载当前语言包后同步初始化，模块级常量里的 t() 也能立即可用
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

/**
 * 切换语言：记下选择后整页重新加载，模块级常量（导航、状态与角色标签表等）和已格式化的内容都按新语言重新求值，
 * 不需要每张表各自订阅语言变化。记录位置按可用性依次退：本机存储 → 会话存储 → 地址栏 ?lang=（无痕或禁用存储时）。
 */
export function setLanguage(next: Language) {
  if (next === currentLanguage()) return;
  const url = new URL(window.location.href);
  url.searchParams.delete(PARAM); // 地址栏的选择优先级最高，存进存储后要去掉，否则会盖住新选择
  const saved = [() => localStorage, () => sessionStorage].some((storage) => {
    try {
      storage().setItem(KEY, next);
      return true;
    } catch {
      return false;
    }
  });
  if (!saved) url.searchParams.set(PARAM, next);
  window.location.replace(url.toString());
}

/** 当前语言（切换后页面会重新加载，渲染期间不会变）。 */
export function useLanguage(): Language {
  return currentLanguage();
}

export default i18n;
