import { currentLanguage } from "./i18n";

// 时间统一按业务时区显示，精确到分钟；写法跟随界面语言（中文 2026-11-01 14:30，英文 Nov 1, 2026, 14:30）。
function zoned(iso: string, timeZone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormatPart[] {
  return new Intl.DateTimeFormat(currentLanguage() === "en" ? "en-US" : "zh-CN", { timeZone, hourCycle: "h23", ...options })
    .formatToParts(new Date(iso));
}

function pick(parts: Intl.DateTimeFormatPart[], type: string): string {
  return parts.find((p) => p.type === type)?.value ?? "";
}

/** 业务时区的日期与时间（到分钟）。 */
export function formatTime(iso: string | null | undefined, timeZone = "Asia/Shanghai"): string {
  if (!iso) return "";
  if (currentLanguage() === "en") {
    return new Intl.DateTimeFormat("en-US", { timeZone, hourCycle: "h23", year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
  }
  const p = zoned(iso, timeZone, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  return `${pick(p, "year")}-${pick(p, "month")}-${pick(p, "day")} ${pick(p, "hour")}:${pick(p, "minute")}`;
}

/** 业务时区的日期（不含时间）。 */
export function formatDate(iso: string | null | undefined, timeZone = "Asia/Shanghai"): string {
  if (!iso) return "";
  if (currentLanguage() === "en") {
    return new Intl.DateTimeFormat("en-US", { timeZone, year: "numeric", month: "short", day: "numeric" }).format(new Date(iso));
  }
  const p = zoned(iso, timeZone, { year: "numeric", month: "2-digit", day: "2-digit" });
  return `${pick(p, "year")}-${pick(p, "month")}-${pick(p, "day")}`;
}

/** 业务时区的月日与时间（列表里省掉年份）。 */
export function formatShortTime(iso: string | null | undefined, timeZone = "Asia/Shanghai"): string {
  if (!iso) return "";
  if (currentLanguage() === "en") {
    return new Intl.DateTimeFormat("en-US", { timeZone, hourCycle: "h23", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
  }
  const p = zoned(iso, timeZone, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  return `${pick(p, "month")}-${pick(p, "day")} ${pick(p, "hour")}:${pick(p, "minute")}`;
}

/** 业务时区的时刻 HH:mm（与语言无关）。 */
export function formatClock(iso: string | null | undefined, timeZone = "Asia/Shanghai"): string {
  if (!iso) return "";
  const p = zoned(iso, timeZone, { hour: "2-digit", minute: "2-digit" });
  return `${pick(p, "hour")}:${pick(p, "minute")}`;
}

/** 业务时区的日期键 YYYY-MM-DD，用于分组与比较，不随语言变化。 */
export function dayKey(iso: string, timeZone = "Asia/Shanghai"): string {
  const p = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(iso));
  return `${pick(p, "year")}-${pick(p, "month")}-${pick(p, "day")}`;
}

/** 相对时间（“3 分钟前” / “3 minutes ago”），超过 30 天回到日期。 */
export function formatRelative(iso: string | null | undefined, timeZone = "Asia/Shanghai", now = Date.now()): string {
  if (!iso) return "";
  const diff = (new Date(iso).getTime() - now) / 1000;
  const rtf = new Intl.RelativeTimeFormat(currentLanguage(), { numeric: "auto" });
  const abs = Math.abs(diff);
  if (abs < 45) return rtf.format(0, "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 30 * 86400) return rtf.format(Math.round(diff / 86400), "day");
  return formatDate(iso, timeZone);
}

/** 数字按语言加千分位；digits 为最多小数位。 */
export function formatNumber(n: number, digits = 2): string {
  return new Intl.NumberFormat(currentLanguage(), { maximumFractionDigits: digits }).format(n);
}

/** 大数缩写：中文用 万 / 亿（繁体 萬 / 億），英文用 K / M / B。 */
export function formatCompact(n: number): string {
  const abs = Math.abs(n);
  if (abs < 1e4) return formatNumber(n);
  return new Intl.NumberFormat(currentLanguage(), { notation: "compact", maximumFractionDigits: abs >= 1e8 ? 2 : 1 }).format(n);
}

/** 业务时区的上个自然月与再上个月（YYYY-MM-DD）：指定日期的初始值按今天推算，不写死某个期间。 */
export function lastMonths(timeZone: string, now = new Date()): { start: string; end: string; cs: string; ce: string } {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone, year: "numeric", month: "numeric" }).formatToParts(now);
  const year = Number(parts.find((p) => p.type === "year")?.value);
  const month = Number(parts.find((p) => p.type === "month")?.value); // 1–12
  const ymd = (d: Date) => d.toISOString().slice(0, 10);
  const first = (offset: number) => new Date(Date.UTC(year, month - 1 + offset, 1));
  const last = (offset: number) => new Date(Date.UTC(year, month + offset, 0));
  return { start: ymd(first(-1)), end: ymd(last(-1)), cs: ymd(first(-2)), ce: ymd(last(-2)) };
}
