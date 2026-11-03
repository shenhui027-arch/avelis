// 数值显示与后端 grounding.display 同一套规则（ADR 0009、0014）：格式由量纲决定，不按列名或数值猜。
import { t } from "./i18n";

export type NumberStyle = "cn" | "intl";
export type UnitMeta = { kind?: string | null; unit?: string | null; scale?: number | string | null; of_ratio?: boolean | null };

const KINDS = new Set(["level", "delta", "ratio", "rate_change", "share", "count", "unknown"]);

/** 有限数值；字符串数字可以，空串、NaN、文字都不是。 */
export function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string" || value.trim() === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// 四舍五入（ROUND_HALF_UP）到 places 位；strip 去掉多余的零。
function rounded(value: number, places: number, strip = false): string {
  const factor = 10 ** places;
  const r = Math.sign(value) * Math.round(Math.abs(value) * factor + 1e-9) / factor;
  const text = (Object.is(r, -0) ? 0 : r).toFixed(places);
  return strip && text.includes(".") ? text.replace(/\.?0+$/, "") : text;
}

/** 两种数字体系都从绝对值 1 万起缩写：cn 用万、亿，intl 用 K、M、B。 */
export function compact(value: number, places: number, style: NumberStyle, strip = false): string {
  const scales: [number, string][] = style === "intl" ? [[1e9, "B"], [1e6, "M"], [1e3, "K"]] : [[1e8, "亿"], [1e4, "万"]];
  if (Math.abs(value) >= 1e4) for (const [scale, suffix] of scales) if (Math.abs(value) >= scale) return rounded(value / scale, places, strip) + suffix;
  return rounded(value, places, strip);
}

function unitOf(meta: UnitMeta | null | undefined) {
  const kind = meta?.kind && KINDS.has(meta.kind) ? meta.kind : "unknown";
  const scale = toNumber(meta?.scale) ?? 1;
  return { kind, unit: meta?.unit ?? "", scale, ofRatio: !!meta?.of_ratio };
}

/** 按量纲显示一个值：差额、变化率带符号；负号用 U+2212。不是数字时原样返回。 */
export function formatValue(value: unknown, meta: UnitMeta | null | undefined, style: NumberStyle, precision?: number): string {
  if (value == null) return "—";
  const n = toNumber(value);
  if (n === null) return String(value);
  const { kind, unit, scale, ofRatio } = unitOf(meta);
  const fixed = precision !== undefined;
  const places = (fallback: number) => (fixed ? precision : fallback);
  const sign = n < 0 ? "−" : (kind === "delta" || kind === "rate_change") && n > 0 ? "+" : "";
  const v = Math.abs(n);
  if (kind === "rate_change" || kind === "share" || (kind === "ratio" && !unit)) return sign + rounded(v * 100, places(1)) + "%";
  if (kind === "delta" && ofRatio && !unit) return sign + rounded(v * 100, places(1)) + t("个百分点");
  if (kind === "ratio") return sign + rounded(v, places(2), !fixed) + unit;
  const base = v * scale;
  const abbreviated = base >= 1e4;
  const fallback = kind === "count" ? 0 : kind === "unknown" ? 4 : 2;
  return sign + compact(base, places(abbreviated ? 2 : fallback), style, !fixed && !abbreviated) + (kind !== "unknown" ? unit : "");
}

/** 坐标轴刻度：同样按量纲换算，但去掉多余的零、不带单位（单位写在轴名或提示里）。 */
export function formatAxis(value: number, meta: UnitMeta | null | undefined, style: NumberStyle): string {
  const { kind, unit, scale, ofRatio } = unitOf(meta);
  const sign = value < 0 ? "−" : "";
  const v = Math.abs(value);
  if (kind === "delta" && ofRatio && !unit) return sign + rounded(v * 100, 1, true) + t("个百分点");
  if (kind === "rate_change" || kind === "share" || (kind === "ratio" && !unit)) return sign + rounded(v * 100, 1, true) + "%";
  if (kind === "ratio") return sign + rounded(v, 2, true);
  return sign + compact(v * scale, 2, style, true);
}

/** 量纲的单位文字（轴名、图例用）；百分比类与未知量纲没有单位。 */
export function unitLabel(meta: UnitMeta | null | undefined): string {
  const { kind, unit } = unitOf(meta);
  if (kind === "unknown" || kind === "rate_change" || kind === "share" || (kind === "ratio" && !unit)) return "";
  return unit;
}
