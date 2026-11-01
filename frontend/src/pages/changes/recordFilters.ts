// 变更记录的筛选：日期按业务时区解释（与列表显示同一日界），资产按所选数据源联动。

export type RecordFilters = {
  datasource_id: string; asset_id: string; column: string; kind: string; actor: string; attribution_status: string;
  since: string; until: string;
};

/** 业务时区某天的第一个真实存在的时刻（通常是 00:00）对应的 UTC 时刻（ISO）。浏览器时区不参与：筛选与列表显示用同一个业务时区。
 * 当地 00:00 因夏令时跳过不存在时（如 America/Santiago 2026-09-06 直接从 00:00 跳到 01:00），取当天第一个存在的时刻；
 * 结果一定落在所选日期，且前一毫秒属于前一天。 */
export function zonedDayStart(day: string, timeZone: string): string {
  const [y, m, d] = day.split("-").map(Number);
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: "2-digit",
  });
  const local = (ms: number) => {
    const parts = format.formatToParts(new Date(ms));
    const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
    return { date: `${get("year")}-${String(get("month")).padStart(2, "0")}-${String(get("day")).padStart(2, "0")}`,
      wall: Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second")) };
  };
  const target = Date.UTC(y, m - 1, d);
  let ms = target;
  for (let i = 0; i < 3; i++) ms -= local(ms).wall - target; // 按偏移反推当地零点
  // 零点不存在或反推落在前后一天时：在 ±1 天内二分，找“当地日期 ≥ 所选日期”的第一个毫秒
  if (local(ms).date !== day || local(ms - 1).date === day) {
    let lo = target - 36 * 3600_000, hi = target + 36 * 3600_000;
    while (hi - lo > 1) {
      const mid = Math.floor((lo + hi) / 2);
      if (local(mid).date >= day) hi = mid; else lo = mid;
    }
    ms = hi;
  }
  if (local(ms).date !== day || local(ms - 1).date === day) {
    throw new Error(`无法确定 ${timeZone} 中 ${day} 的起始时刻`);
  }
  return new Date(ms).toISOString();
}

/** 请求参数：空值不带；起止日期换成业务时区当天第一个时刻的 UTC 时刻（截止日不含）。
 * 业务时区里整天不存在的日期（如 Pacific/Apia 2011-12-30）不发出，列在 invalid 里由日期字段提示。 */
export function recordQuery(filters: RecordFilters, timeZone: string): { query: { [k: string]: string }; invalid: string[] } {
  const query: { [k: string]: string } = {};
  const invalid: string[] = [];
  for (const [k, v] of Object.entries(filters)) {
    if (!v) continue;
    if (k !== "since" && k !== "until") {
      query[k] = v;
      continue;
    }
    try {
      query[k] = zonedDayStart(v, timeZone);
    } catch {
      invalid.push(k);
    }
  }
  return { query, invalid };
}

/** 某个数据源下的资产（/api/assets 返回 datasource: {id, name, …}）；未选数据源时返回全部。 */
export function assetsOfDatasource<T extends { datasource: { id: string } }>(assets: T[], datasourceId: string): T[] {
  return assets.filter((a) => !datasourceId || a.datasource.id === datasourceId);
}
