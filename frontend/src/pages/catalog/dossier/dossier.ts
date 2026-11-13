// 资产档案页的纯函数：不依赖 React 与语言包，便于单测（dossier.test.mjs）。文案在组件里经 t() 给出。

/** 说明的状态：没有说明时是 none。 */
export type NoteState = "confirmed" | "draft" | "stale" | "none";

type NoteLike = { status: "draft" | "confirmed" | "stale" } | null | undefined;

export function noteState(note: NoteLike): NoteState {
  return note ? note.status : "none";
}

/** 草稿与过期都需要有人确认。 */
export function needsConfirm(note: NoteLike): boolean {
  const s = noteState(note);
  return s === "draft" || s === "stale";
}

/** 表的写法（库.表、目录.库.表）取最后一段作为表名；空写法原样返回。 */
export function tableName(ref: string | null | undefined): string {
  if (!ref) return "";
  const parts = ref.split(".").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : ref;
}

/** 限定名去掉最后一段，即库名（或目录.库）；只有一段时为空。 */
export function schemaOf(qualified: string): string {
  const parts = qualified.split(".");
  return parts.length > 1 ? parts.slice(0, -1).join(".") : "";
}

/** 来源字段的显示名：表名.字段（没有字段时只有表名）。 */
export function sourceLabel(source: { ref: string; column?: string | null }): string {
  const name = tableName(source.ref);
  return source.column ? `${name}.${source.column}` : name;
}

/** 列表默认只显示前 limit 个，其余收起；展开后全部显示。 */
export function visible<T>(items: T[], limit: number, expanded: boolean): { shown: T[]; hidden: number } {
  if (expanded || items.length <= limit) return { shown: items, hidden: 0 };
  return { shown: items.slice(0, limit), hidden: items.length - limit };
}

/** 字段说明计数：已确认、待确认（草稿 + 过期）。 */
export function noteCounts(fields: { note?: NoteLike }[]): { confirmed: number; pending: number } {
  let confirmed = 0, pending = 0;
  for (const f of fields) {
    const s = noteState(f.note);
    if (s === "confirmed") confirmed += 1;
    else if (s === "draft" || s === "stale") pending += 1;
  }
  return { confirmed, pending };
}

/** 名字的头像字：取第一个字符（中文名取姓，英文名取首字母大写）。 */
export function initialOf(name: string | null | undefined): string {
  const trimmed = (name ?? "").trim();
  if (!trimmed) return "?";
  return Array.from(trimmed)[0].toUpperCase();
}

/** 最近一次提问的人：按 last_asked_at 取最新。 */
export function latestAsker<T extends { last_asked_at: string }>(people: T[]): T | null {
  let best: T | null = null;
  for (const p of people) if (!best || Date.parse(p.last_asked_at) > Date.parse(best.last_asked_at)) best = p;
  return best;
}

/** 一句话说明在顶部怎么显示：注释直接显示；说明区分已确认 / 草稿 / 过期；都没有时为 none。 */
export type SummaryState = "comment" | NoteState;

export function summaryState(summary: { text?: string | null; source?: "comment" | "note" | null; note?: NoteLike }): SummaryState {
  if (!summary.text) return "none";
  if (summary.source === "comment") return "comment";
  return summary.note ? summary.note.status : "confirmed";
}

/** 流向带右栏：各项为 0 的不显示；三项都没有时 any 为 false。 */
export function usage(d: { metrics?: unknown[]; asked?: { total?: number }; downstream?: unknown[] }): {
  metrics: number; asked: number; downstream: number; any: boolean;
} {
  const metrics = d.metrics?.length ?? 0;
  const asked = d.asked?.total ?? 0;
  const downstream = d.downstream?.length ?? 0;
  return { metrics, asked, downstream, any: metrics + asked + downstream > 0 };
}
