// 上下文窗口的输入与显示：支持直接写数字，或带单位 k（千）、M（百万），如 200k、1M、1.5M。
export const CONTEXT_MIN = 1024;
export const CONTEXT_MAX = 10_000_000;

/** 解析输入：空串返回 null（不填）；写法不对或超出范围返回 undefined。 */
export function parseContextWindow(text: string): number | null | undefined {
  const raw = text.trim().replace(/[,，\s]/g, "");
  if (!raw) return null;
  const m = /^(\d+(?:\.\d+)?)([kKmM]?)$/.exec(raw);
  if (!m) return undefined;
  const unit = m[2].toLowerCase();
  const value = Math.round(Number(m[1]) * (unit === "m" ? 1_000_000 : unit === "k" ? 1000 : 1));
  return value >= CONTEXT_MIN && value <= CONTEXT_MAX ? value : undefined;
}

/** 显示：整百万写成 M、整千写成 k，其余原样，保证再解析能得到同一个数。 */
export function formatContextWindow(value: number | null | undefined): string {
  if (value == null) return "";
  if (value % 1_000_000 === 0) return `${value / 1_000_000}M`;
  if (value % 100_000 === 0 && value > 1_000_000) return `${value / 1_000_000}M`;
  if (value % 1000 === 0) return `${value / 1000}k`;
  return String(value);
}
