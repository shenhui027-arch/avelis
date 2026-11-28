// 采集 / 测试连接错误的显示：后端给出按请求语言翻译的说明（error_message）时优先显示说明，
// 驱动原文（error）保留供排查；认不出的错误没有说明，照旧显示原文。纯函数，不依赖 i18n，便于测试。

export type CollectError = { error?: string | null; error_message?: string | null };

/** 原文在提示里最多显示的字数，超出截断并以“…”结尾（完整原文放在 title 里） */
export const RAW_LIMIT = 160;

/**
 * 拆出主文字与原文：
 * - 有 error_message（去空白后非空）→ { text: error_message, raw: error 去空白后（为空则 null） }；
 *   若原文与说明相同，raw 为 null。
 * - 没有说明、有原文 → { text: 原文, raw: null }
 * - 都没有 → { text: fallback, raw: null }
 */
export function errorParts(e: CollectError | null | undefined, fallback: string): { text: string; raw: string | null } {
  const message = typeof e?.error_message === "string" ? e.error_message.trim() : "";
  const raw = typeof e?.error === "string" ? e.error.trim() : "";
  if (message) return { text: message, raw: raw && raw !== message ? raw : null };
  if (raw) return { text: raw, raw: null };
  return { text: fallback, raw: null };
}

/** 截断原文：长度不超过 RAW_LIMIT 原样返回；否则取前 RAW_LIMIT 个字符（去掉末尾空白）后加“…” */
export function shortRaw(raw: string): string {
  return raw.length <= RAW_LIMIT ? raw : `${raw.slice(0, RAW_LIMIT).trimEnd()}…`;
}

/**
 * 提示条（Banner）用的一行文字：有原文时为 withRaw(text, shortRaw(raw))，否则为 text。
 * withRaw 由调用方传入，负责翻译与括注格式，如 `(text, raw) => t("{{text}}（原文：{{raw}}）", { text, raw })`。
 */
export function bannerText(
  e: CollectError | null | undefined,
  fallback: string,
  withRaw: (text: string, raw: string) => string,
): string {
  const { text, raw } = errorParts(e, fallback);
  return raw ? withRaw(text, shortRaw(raw)) : text;
}
