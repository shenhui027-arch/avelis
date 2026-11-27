// 血缘追溯（纯函数，无 React 依赖）：按方向 / 深度沿数据流遍历、最上游来源与最终去向、“只看两端”的派生图、
// 表格视图的行、筛选排序与 CSV。数据全部来自 GET /api/lineage/graph（已是种子资产所在的整个连通分量），不另调接口。
// 单测：node src/pages/lineage/trace.test.mjs（from frontend/），契约以测试为准。
import type { Graph } from "../../api/hooks";

export type Direction = "up" | "down" | "both";
export type Level = "table" | "column";
/** 字段键：`${nodeId}::${column}`（与 LineagePage 的 ColKey 一致） */
export type ColKey = string;

/** 只有这些关系是数据流，参与方向追溯；关联键、过滤、分组只是影响条件，仅作展示（与 Esther 的“条件连线不参与追溯”一致）。 */
export const FLOW_RELATIONS: ReadonlySet<string> = new Set(["value_direct", "value_computed", "value_aggregated", "table"]);
/** “只看两端”里折叠了中间层的派生连线的关系名；attributes.hops 记录折叠前的层数 */
export const TRACE_RELATION = "trace";
/** 表格里“同一张表”（外部表 ↔ 源表的身份映射）一行的关系名 */
export const IDENTITY_RELATION = "identity";

/** 一步：走到哪里、经过哪条画布边。边键：血缘边 `${source}->${target}`，身份映射 `id:${link.id}`（与画布边 id 一致） */
export interface Step { to: string; edge: string }

export interface FlowIndex {
  tableUp: Map<string, Step[]>;
  tableDown: Map<string, Step[]>;
  colUp: Map<ColKey, Step[]>;
  colDown: Map<ColKey, Step[]>;
}

function addStep(map: Map<string, Step[]>, from: string, to: string, edge: string): void {
  const list = map.get(from);
  if (!list) {
    map.set(from, [{ to, edge }]);
    return;
  }
  if (!list.some((s) => s.to === to)) list.push({ to, edge });
}

function splitColKey(colKey: ColKey): [string, string] {
  const at = colKey.indexOf("::");
  return at >= 0 ? [colKey.slice(0, at), colKey.slice(at + 2)] : [colKey, ""];
}

/**
 * 建立上下游邻接表。
 * - 表级：一条血缘边只要 relations 含任一 FLOW_RELATIONS 就算一步（只有过滤 / 关联键 / 分组的边不算）。
 * - 字段级：边内每个 column 需 source_column、target_column 都有值且 relation ∈ FLOW_RELATIONS。
 * - 身份映射：identity_links 里 target 非空、且 graph.nodes 中存在 qualified === link.source 的节点时，
 *   数据从 target（真实表）流向该节点（外部表），边键 `id:${link.id}`；字段级按 column_match.matched 里的同名列连接。
 * - 同一对端点的重复步只记一次。
 */
export function buildFlowIndex(graph: Graph): FlowIndex {
  const tableUp = new Map<string, Step[]>();
  const tableDown = new Map<string, Step[]>();
  const colUp = new Map<ColKey, Step[]>();
  const colDown = new Map<ColKey, Step[]>();

  for (const e of graph.edges) {
    const edgeKey = `${e.source}->${e.target}`;
    if (e.relations.some((r) => FLOW_RELATIONS.has(r))) {
      addStep(tableDown, e.source, e.target, edgeKey);
      addStep(tableUp, e.target, e.source, edgeKey);
    }
    for (const c of e.columns) {
      if (c.source_column && c.target_column && FLOW_RELATIONS.has(c.relation)) {
        const srcCol = `${e.source}::${c.source_column}`;
        const dstCol = `${e.target}::${c.target_column}`;
        addStep(colDown, srcCol, dstCol, edgeKey);
        addStep(colUp, dstCol, srcCol, edgeKey);
      }
    }
  }

  const nodeIds = new Set(graph.nodes.map((n) => n.id));
  const byQualified = new Map(graph.nodes.map((n) => [n.qualified, n]));
  for (const link of graph.identity_links) {
    const extNode = byQualified.get(link.source);
    if (!link.target || !extNode || !nodeIds.has(link.target.id)) continue;
    const edgeKey = `id:${link.id}`;
    addStep(tableDown, link.target.id, extNode.id, edgeKey);
    addStep(tableUp, extNode.id, link.target.id, edgeKey);
    const matched = (link.column_match as { matched?: unknown })?.matched;
    if (Array.isArray(matched)) {
      for (const col of matched) {
        if (typeof col === "string" && col) {
          const srcCol = `${link.target.id}::${col}`;
          const dstCol = `${extNode.id}::${col}`;
          addStep(colDown, srcCol, dstCol, edgeKey);
          addStep(colUp, dstCol, srcCol, edgeKey);
        }
      }
    }
  }

  return { tableUp, tableDown, colUp, colDown };
}

export interface TraceOptions {
  level: Level;
  direction: Direction;
  /** 最多走几层；null 表示走到底 */
  depth: number | null;
}

export interface TraceResult {
  /** 路径上的节点 id（含起点）。字段级时为路径上字段所属的节点 */
  nodes: Set<string>;
  /** 字段级时路径上的字段（含起点）；表级时为空 */
  cols: Set<ColKey>;
  /** 路径经过的画布边键 */
  edgeKeys: Set<string>;
  /** 不受 depth 限制时实际可达的最大层数（按最短层数计），用于提示“上游 3 层 · 下游 2 层”；未请求的方向为 0 */
  reach: { up: number; down: number };
  /**
   * 最上游来源 / 最终去向：不受 depth 限制走到头、再没有下一步的节点或字段（表级为节点 id，字段级为 ColKey），
   * 值为距起点的最短层数。不含起点；只计请求的方向（direction=up 时 sinks 为空，反之亦然）。
   */
  sources: Map<string, number>;
  sinks: Map<string, number>;
}

function bfsWalk(adj: Map<string, Step[]>, start: string, depth: number | null) {
  const dist = new Map<string, number>([[start, 0]]);
  const kept = new Set<string>([start]);
  const edges = new Set<string>();
  const ends = new Map<string, number>();
  const queue: string[] = [start];
  let maxReach = 0;

  for (let i = 0; i < queue.length; i++) {
    const cur = queue[i];
    const d = dist.get(cur)!;
    const steps = adj.get(cur) ?? [];
    // 只有真正走到头（无任何下一步）才算来源或去向，环上的节点仍可继续走因此不算端点
    if (cur !== start && steps.length === 0) {
      ends.set(cur, d);
    }
    const withinDepth = depth === null || d < depth;
    for (const step of steps) {
      if (!dist.has(step.to)) {
        const nextDist = d + 1;
        dist.set(step.to, nextDist);
        if (nextDist > maxReach) maxReach = nextDist;
        queue.push(step.to);
      }
      if (withinDepth) {
        kept.add(step.to);
        edges.add(step.edge);
      }
    }
  }
  return { kept, edges, maxReach, ends };
}

/**
 * 从 start（表级为节点 id，字段级为 ColKey）出发按方向做广度优先遍历；“双向”为上、下游各自遍历的并集。
 * depth 只裁剪 nodes / cols / edgeKeys，不影响 reach、sources、sinks。图里可能有环，必须用 visited 防止死循环。
 */
export function trace(index: FlowIndex, start: string, options: TraceOptions): TraceResult {
  const { level, direction, depth } = options;
  const upAdj = level === "column" ? index.colUp : index.tableUp;
  const downAdj = level === "column" ? index.colDown : index.tableDown;

  const up = direction !== "down" ? bfsWalk(upAdj, start, depth) : null;
  const down = direction !== "up" ? bfsWalk(downAdj, start, depth) : null;

  const items = new Set<string>([start]);
  const edgeKeys = new Set<string>();
  for (const part of [up, down]) {
    if (!part) continue;
    for (const item of part.kept) items.add(item);
    for (const edge of part.edges) edgeKeys.add(edge);
  }

  const cols = level === "column" ? items : new Set<ColKey>();
  const nodes = level === "column" ? new Set([...items].map((k) => splitColKey(k)[0])) : items;

  return {
    nodes,
    cols,
    edgeKeys,
    reach: { up: up?.maxReach ?? 0, down: down?.maxReach ?? 0 },
    sources: up?.ends ?? new Map(),
    sinks: down?.ends ?? new Map(),
  };
}

/**
 * “只看两端”的派生图：只保留起点所在节点与 sources / sinks 所在节点，中间流经全部折叠。
 * - 每个来源节点 → 起点节点、起点节点 → 每个去向节点各一条边，relations / origins 均为 [TRACE_RELATION]，source_ref 为空串。
 * - 字段级时 columns 为端点字段与起点字段的配对（relation = TRACE_RELATION、expression = null、origin = TRACE_RELATION、
 *   attributes = { hops }），同一对节点的多个字段合并到一条边；表级时 columns 为 [{ source_column: null, target_column: null, … }] 一项，用来承载 hops。
 * - identity_links、joins 置空；nodes 保留原节点对象与原顺序；edges 先列来源边、再列去向边，各自按 graph.nodes 顺序。
 */
export function endsGraph(graph: Graph, start: string, result: TraceResult, level: Level): Graph {
  const [startNodeId, startCol] = level === "column" ? splitColKey(start) : [start, null];
  type GCol = Graph["edges"][number]["columns"][number];

  const srcColsByNode = new Map<string, GCol[]>();
  const sinkColsByNode = new Map<string, GCol[]>();

  if (level === "column") {
    for (const [colKey, hops] of result.sources) {
      const [nodeId, colName] = splitColKey(colKey);
      const list = srcColsByNode.get(nodeId) ?? [];
      list.push({
        source_column: colName,
        target_column: startCol,
        relation: TRACE_RELATION,
        expression: null,
        origin: TRACE_RELATION,
        attributes: { hops },
      });
      srcColsByNode.set(nodeId, list);
    }
    for (const [colKey, hops] of result.sinks) {
      const [nodeId, colName] = splitColKey(colKey);
      const list = sinkColsByNode.get(nodeId) ?? [];
      list.push({
        source_column: startCol,
        target_column: colName,
        relation: TRACE_RELATION,
        expression: null,
        origin: TRACE_RELATION,
        attributes: { hops },
      });
      sinkColsByNode.set(nodeId, list);
    }
  } else {
    for (const [nodeId, hops] of result.sources) {
      srcColsByNode.set(nodeId, [{
        source_column: null,
        target_column: null,
        relation: TRACE_RELATION,
        expression: null,
        origin: TRACE_RELATION,
        attributes: { hops },
      }]);
    }
    for (const [nodeId, hops] of result.sinks) {
      sinkColsByNode.set(nodeId, [{
        source_column: null,
        target_column: null,
        relation: TRACE_RELATION,
        expression: null,
        origin: TRACE_RELATION,
        attributes: { hops },
      }]);
    }
  }

  const keptIds = new Set<string>([startNodeId, ...srcColsByNode.keys(), ...sinkColsByNode.keys()]);
  const nodes = graph.nodes.filter((n) => keptIds.has(n.id));
  const edges: Graph["edges"] = [];

  for (const n of graph.nodes) {
    const columns = srcColsByNode.get(n.id);
    if (!columns) continue;
    edges.push({
      source: n.id,
      source_ref: "",
      target: startNodeId,
      columns,
      relations: [TRACE_RELATION],
      origins: [TRACE_RELATION],
    });
  }
  for (const n of graph.nodes) {
    const columns = sinkColsByNode.get(n.id);
    if (!columns) continue;
    edges.push({
      source: startNodeId,
      source_ref: "",
      target: n.id,
      columns,
      relations: [TRACE_RELATION],
      origins: [TRACE_RELATION],
    });
  }

  return { nodes, edges, identity_links: [], joins: [] };
}

/** 表格视图的一行：每条列级边一行；没有列的表级边一行；身份映射一行（relation = IDENTITY_RELATION，列为空）。 */
export interface EdgeRow {
  /** 行内唯一键 */
  key: string;
  /** 所属画布边键，用于“只看选中路径” */
  edge: string;
  sourceId: string;
  source: string;
  sourceColumn: string | null;
  targetId: string;
  target: string;
  targetColumn: string | null;
  relation: string;
  expression: string | null;
  origin: string;
}

/**
 * 由图生成表格行。表名取节点 qualified；设计时节点（id 以 "ref:" 开头）去掉前缀。
 * onlyEdges 给出时只保留 edge 在集合内的行。onlyCols（字段级追溯结果）非空时再按字段过滤：列级行要求源、目标字段都在集合内，
 * 没有字段的行（表级关系、影响条件）不保留，身份映射行仍按 onlyEdges 保留——同一对表之间与所选字段无关的映射不算在路径里。
 * 顺序：按 graph.edges 原顺序，列按原顺序，身份映射排在最后。
 */
export function edgeRows(graph: Graph, onlyEdges?: ReadonlySet<string>, onlyCols?: ReadonlySet<ColKey>): EdgeRow[] {
  const byColumn = !!onlyCols && onlyCols.size > 0;
  const onColumnPath = (sourceId: string, source: string | null | undefined, targetId: string, target: string | null | undefined) =>
    !!source && !!target && onlyCols!.has(`${sourceId}::${source}`) && onlyCols!.has(`${targetId}::${target}`);
  const nodeById = new Map(graph.nodes.map((n) => [n.id, n]));
  const byQualified = new Map(graph.nodes.map((n) => [n.qualified, n]));
  const tableName = (id: string): string => {
    const raw = nodeById.get(id)?.qualified ?? id;
    return id.startsWith("ref:") ? raw.replace(/^ref:/, "") : raw;
  };
  const usedKeys = new Set<string>();
  const allocKey = (base: string): string => {
    if (!usedKeys.has(base)) {
      usedKeys.add(base);
      return base;
    }
    const next = `${base}#${usedKeys.size}`;
    usedKeys.add(next);
    return next;
  };

  const rows: EdgeRow[] = [];
  for (const e of graph.edges) {
    const edgeKey = `${e.source}->${e.target}`;
    if (onlyEdges && !onlyEdges.has(edgeKey)) continue;
    const source = tableName(e.source);
    const target = tableName(e.target);
    if (e.columns.length) {
      e.columns.forEach((c, idx) => {
        if (byColumn && !onColumnPath(e.source, c.source_column, e.target, c.target_column)) return;
        rows.push({
          key: allocKey(`${edgeKey}:${idx}`),
          edge: edgeKey,
          sourceId: e.source,
          source,
          sourceColumn: c.source_column ?? null,
          targetId: e.target,
          target,
          targetColumn: c.target_column ?? null,
          relation: c.relation,
          expression: c.expression ?? null,
          origin: c.origin,
        });
      });
    } else if (!byColumn) {
      rows.push({
        key: allocKey(edgeKey),
        edge: edgeKey,
        sourceId: e.source,
        source,
        sourceColumn: null,
        targetId: e.target,
        target,
        targetColumn: null,
        relation: e.relations[0] ?? "table",
        expression: null,
        origin: e.origins[0] ?? "",
      });
    }
  }

  for (const link of graph.identity_links) {
    const extNode = byQualified.get(link.source);
    if (!link.target || !extNode || !nodeById.has(link.target.id)) continue;
    const edgeKey = `id:${link.id}`;
    if (onlyEdges && !onlyEdges.has(edgeKey)) continue;
    rows.push({
      key: allocKey(edgeKey),
      edge: edgeKey,
      sourceId: link.target.id,
      source: tableName(link.target.id),
      sourceColumn: null,
      targetId: extNode.id,
      target: tableName(extNode.id),
      targetColumn: null,
      relation: IDENTITY_RELATION,
      expression: null,
      origin: link.method,
    });
  }

  return rows;
}

/** 文本筛选：不分大小写匹配源表、源字段、目标表、目标字段、表达式；relation 为空串表示全部。 */
export function filterRows(rows: readonly EdgeRow[], filter: { text: string; relation: string }): EdgeRow[] {
  const q = filter.text.trim().toLocaleLowerCase();
  const rel = filter.relation;
  return rows.filter((row) => {
    if (rel && row.relation !== rel) return false;
    if (!q) return true;
    return [row.source, row.sourceColumn, row.target, row.targetColumn, row.expression]
      .some((v) => v !== null && v.toLocaleLowerCase().includes(q));
  });
}

export type SortKey = "source" | "sourceColumn" | "target" | "targetColumn" | "relation" | "origin";

/** 稳定排序，localeCompare 比较；null 永远排在最后（升降序都一样）。不修改入参。 */
export function sortRows(rows: readonly EdgeRow[], key: SortKey, dir: "asc" | "desc"): EdgeRow[] {
  return [...rows].sort((a, b) => {
    const va = a[key];
    const vb = b[key];
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    const cmp = va.localeCompare(vb);
    return dir === "desc" ? -cmp : cmp;
  });
}

/**
 * 表格行转导出用的二维表（第一行为表头，由调用方传入已翻译的文字）。列顺序：源表、源字段、目标表、目标字段、关系、表达式、来源；
 * 关系、来源用 label 函数转成显示文字；null 输出为空串。CSV 转义与下载由 src/csv.ts 负责（本模块保持无依赖，便于单测）。
 */
export function rowsToMatrix(
  rows: readonly EdgeRow[],
  headers: readonly [string, string, string, string, string, string, string],
  label: { relation: (relation: string) => string; origin: (origin: string) => string },
): string[][] {
  return [
    [...headers],
    ...rows.map((r) => [
      r.source,
      r.sourceColumn ?? "",
      r.target,
      r.targetColumn ?? "",
      label.relation(r.relation),
      r.expression ?? "",
      label.origin(r.origin),
    ]),
  ];
}
