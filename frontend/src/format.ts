// 时间统一按业务时区显示，精确到分钟。
export function formatTime(iso: string | null | undefined, timeZone = "Asia/Shanghai"): string {
  if (!iso) return "";
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date(iso));
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
}
