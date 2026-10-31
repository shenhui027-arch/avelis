// 时间统一按业务时区显示，精确到分钟。
export function formatTime(iso: string | null | undefined, timeZone = "Asia/Shanghai"): string {
  if (!iso) return "";
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date(iso));
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
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
