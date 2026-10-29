// 变更记录的筛选：日期按业务时区解释（与列表显示同一日界），资产按所选数据源联动。

export type RecordFilters = {
  datasource_id: string; asset_id: string; column: string; kind: string; actor: string; attribution_status: string;
  since: string; until: string;
};

/** 业务时区某天 00:00 对应的 UTC 时刻（ISO）。浏览器时区不参与：筛选与列表显示用同一个业务时区。 */
export function zonedDayStart(day: string, timeZone: string): string {
  const [y, m, d] = day.split("-").map(Number);
  const target = Date.UTC(y, m - 1, d);
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: "2-digit",
  });
  let guess = target;
  for (let i = 0; i < 2; i++) { // 第二轮修正夏令时切换日
    const parts = format.formatToParts(new Date(guess));
    const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
    const shown = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
    guess -= shown - target;
  }
  return new Date(guess).toISOString();
}

/** 请求参数：空值不带；起止日期换成业务时区当天零点的 UTC 时刻（截止日不含）。 */
export function recordQuery(filters: RecordFilters, timeZone: string): { [k: string]: string } {
  const q: { [k: string]: string } = {};
  for (const [k, v] of Object.entries(filters)) {
    if (!v) continue;
    q[k] = k === "since" || k === "until" ? zonedDayStart(v, timeZone) : v;
  }
  return q;
}

/** 某个数据源下的资产（/api/assets 返回 datasource: {id, name, …}）；未选数据源时返回全部。 */
export function assetsOfDatasource<T extends { datasource: { id: string } }>(assets: T[], datasourceId: string): T[] {
  return assets.filter((a) => !datasourceId || a.datasource.id === datasourceId);
}
