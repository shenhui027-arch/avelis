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
  // TODO(agy): 按上面的规则实现
  void e;
  return { text: fallback, raw: null };
}

/** 截断原文：长度不超过 RAW_LIMIT 原样返回；否则取前 RAW_LIMIT 个字符（去掉末尾空白）后加“…” */
export function shortRaw(raw: string): string {
  // TODO(agy): 实现
  return raw;
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
  // TODO(agy): 用 errorParts 与 shortRaw 实现
  void withRaw;
  return errorParts(e, fallback).text;
}
