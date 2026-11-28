// 血缘画布的边类型徽章（纯函数，无 React 依赖）：从一条表间边的列级关系里归纳出要标在连线上的徽章，
// 以及悬停提示里逐条列出的字段映射。直接传值（value_direct）、表级（table）与“只看两端”的折叠边不加徽章。
// 单测：node src/pages/lineage/edgeBadges.test.mjs（from frontend/），契约以测试为准。
import type { Graph } from "../../api/hooks";

type ColumnEdge = Graph["edges"][number]["columns"][number];

export type BadgeKind = "aggregated" | "computed" | "filter" | "join_key" | "group_by";

/** 徽章顺序：先取值类（聚合、计算），再影响条件（过滤、关联键、分组） */
export const BADGE_ORDER: readonly BadgeKind[] = ["aggregated", "computed", "filter", "join_key", "group_by"];

/** relation → 徽章种类；不在表里的关系不加徽章 */
export const BADGE_OF: Readonly<Record<string, BadgeKind>> = {
  value_aggregated: "aggregated", value_computed: "computed", filter: "filter", join_key: "join_key", group_by: "group_by",
};

/** 徽章符号（与 Esther 手册的约定一致，读屏文字由调用方用 RELATION_LABEL 提供） */
export const BADGE_SYMBOL: Readonly<Record<BadgeKind, string>> = {
  aggregated: "Σ", computed: "ƒ", filter: "⊳", join_key: "⋈", group_by: "▦",
};

export interface BadgeLine {
  kind: BadgeKind;
  source: string | null;
  target: string | null;
  expression: string | null;
}

export interface EdgeBadge {
  /** 出现的徽章种类，按 BADGE_ORDER 去重排序 */
  kinds: BadgeKind[];
  /** 悬停提示的明细：只含有徽章的列级关系，先按 BADGE_ORDER 再按原顺序，最多 max 条 */
  lines: BadgeLine[];
  /** 超出 max 未列出的条数 */
  more: number;
  /** 是否只有影响条件（过滤 / 关联键 / 分组），没有取值关系——画布据此保持警告色虚线 */
  influenceOnly: boolean;
}

/** 取值类关系：只要存在任一此类列级关系，表间边就不属于纯影响条件边 */
const VALUE_RELATIONS = new Set(["value_direct", "value_computed", "value_aggregated", "table"]);

/**
 * 归纳一条边的徽章。columns 为该表间边的全部列级关系；没有任何可加徽章的关系时返回 null。
 * influenceOnly：columns 里没有任何取值关系（value_direct / value_computed / value_aggregated / table）时为 true。
 */
export function edgeBadge(columns: readonly ColumnEdge[], max = 6): EdgeBadge | null {
  const buckets = new Map<BadgeKind, BadgeLine[]>();
  let hasValue = false;
  for (const col of columns) {
    if (VALUE_RELATIONS.has(col.relation)) hasValue = true;
    if (!Object.hasOwn(BADGE_OF, col.relation)) continue;
    const kind = BADGE_OF[col.relation];
    const line: BadgeLine = {
      kind,
      source: col.source_column ?? null,
      target: col.target_column ?? null,
      expression: col.expression ?? null,
    };
    const list = buckets.get(kind);
    if (list) list.push(line);
    else buckets.set(kind, [line]);
  }
  if (!buckets.size) return null;
  const kinds: BadgeKind[] = [];
  const ordered: BadgeLine[] = [];
  for (const kind of BADGE_ORDER) {
    const list = buckets.get(kind);
    if (!list) continue;
    kinds.push(kind);
    ordered.push(...list);
  }
  const limit = Math.max(0, max);
  const lines = ordered.slice(0, limit);
  return {
    kinds,
    lines,
    more: ordered.length - lines.length,
    influenceOnly: !hasValue,
  };
}

/**
 * 悬停提示文字：每条一行“徽章符号 源字段 → 目标字段：表达式”，没有目标字段时目标写 noTarget（如“（影响）”），
 * 没有源字段写“—”，没有表达式时省略“：表达式”；more > 0 时末行为 moreText(more)。行之间用 \n 连接（提示框按行显示）。
 */
export function badgeTip(badge: EdgeBadge, noTarget: string, moreText: (count: number) => string): string {
  const rows = badge.lines.map((line) => {
    const src = line.source || "—";
    const dst = line.target || noTarget;
    const expr = line.expression ? `：${line.expression}` : "";
    return `${BADGE_SYMBOL[line.kind]} ${src} → ${dst}${expr}`;
  });
  if (badge.more > 0) rows.push(moreText(badge.more));
  return rows.join("\n");
}
