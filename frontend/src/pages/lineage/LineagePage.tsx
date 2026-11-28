// 血缘：画布模式 —— 列级血缘画布（@xyflow/react + dagre 自动布局）| 检查器。
// 选中表或字段后按方向 / 深度沿数据流追溯（trace.ts），可只看两端；另有表格视图（EdgeTable.tsx）。
// 节点按 Avelis UI 的 av-node 渲染；取值边为实线，过滤 / 关联键 / 分组这类影响关系为警告色虚线，
// 外部表与真实表是“同一张表”（点线 + 链条标记），导入的设计时表为虚线占位节点。点击列高亮列级路径。
import dagre from "@dagrejs/dagre";
import {
  BaseEdge, EdgeLabelRenderer, getBezierPath, Handle, Position, ReactFlow, ReactFlowProvider, useReactFlow, useStore,
  type Edge, type EdgeProps, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { useAsset, useDomains, useGraph, type Graph } from "../../api/hooks";
import { Select } from "../../components/controls";
import { ErrorBoundary } from "../../components/ErrorBoundary";
import {
  Dot, Empty, Hint, Icon, KIND_LABEL, Plat, QueryState, RELATION_LABEL, Seg, Status, SWATCHES, Switch, statusOf, shortName,
} from "../../components/ui";
import { useOverlay } from "../../components/overlay";
import { t } from "../../i18n";
import { EdgeTable } from "./EdgeTable";
import { HealthPanel } from "./HealthPanel";
import { BADGE_ORDER, BADGE_SYMBOL, badgeTip, edgeBadge, type BadgeKind, type EdgeBadge } from "./edgeBadges";
import { buildFlowIndex, endsGraph, trace, TRACE_RELATION, type Direction, type TraceResult } from "./trace";

type GNode = Graph["nodes"][number];
type ColKey = string; // `${nodeId}::${column}`

const MAX_COLS = 6;
const VALUE = new Set(["value_direct", "value_computed", "value_aggregated", "table", TRACE_RELATION]);

interface AssetData extends Record<string, unknown> {
  node: GNode;
  columns: { name: string; relation: string | null; influence: boolean }[];
  domainSwatch: string | null;
  selected: boolean;
  pathCols: Set<string>;
  link: { status: string; target: string } | null;
  density: "overview" | "columns";
  onColumn: (col: string) => void;
  onAllColumns: () => void;
}

function nodeHeight(cols: number, hasLink: boolean, hasGrain: boolean, density: AssetData["density"]): number {
  if (density === "overview") return 54;
  return 54 + (cols ? Math.min(cols, MAX_COLS) * 27 + 9 : 0) + (cols > MAX_COLS ? 36 : 0) + (hasGrain ? 27 : 0) + (hasLink ? 31 : 0);
}

function graphColumns(graph: Graph): Map<string, AssetData["columns"]> {
  const columns = new Map<string, Map<string, { relation: string | null; influence: boolean }>>();
  const touch = (nodeId: string, name: string | null | undefined, relation: string | null, influence: boolean) => {
    if (!name) return;
    const cols = columns.get(nodeId) ?? new Map();
    const prev = cols.get(name);
    cols.set(name, { relation: prev?.relation ?? relation, influence: (prev?.influence ?? false) || influence });
    columns.set(nodeId, cols);
  };
  for (const edge of graph.edges) {
    for (const col of edge.columns) {
      touch(edge.target, col.target_column, VALUE.has(col.relation) ? col.relation : null, false);
      touch(edge.source, col.source_column, null, !VALUE.has(col.relation));
    }
  }
  return new Map([...columns].map(([id, cols]) => [id, [...cols].map(([name, detail]) => ({ name, ...detail })).sort((a, b) => Number(!!b.relation) - Number(!!a.relation))]));
}

function AssetNode({ data }: NodeProps<Node<AssetData>>) {
  const { node, columns, domainSwatch, selected, pathCols, link, density } = data;
  const placeholder = node.kind === "design_time";
  const grain = node.grains[0];
  const [tone] = statusOf(grain?.status);
  const parts = node.qualified.split(".");
  const schema = parts.slice(-3, -1).join(".");
  return (
    <div className={`av-node${selected ? " is-selected" : ""}${placeholder ? " is-placeholder" : ""}`}>
      <Handle type="target" position={Position.Left} className="av-lineage__handle" />
      <div className="av-node__hd">
        <span className="av-node__type"><Icon name={node.kind === "view" ? "view" : node.kind === "design_time" ? "upload" : "table"} /></span>
        <span className="av-node__id">
          <span className="av-node__path">
            {domainSwatch ? <span className={`av-swatch av-swatch--${domainSwatch}`} /> : null}
            {node.datasource ? <Plat kind={node.datasource.kind} /> : <span className="av-plat">{t("导入")}</span>}
            {/* 包裹库名以支持超长省略号并悬停展示完整限定名 */}
            {schema ? <span className="av-node__schema" data-tip={node.qualified}>{schema}</span> : null}
          </span>
          <span className="av-node__name">{parts[parts.length - 1]}</span>
        </span>
        <span className="av-tag">{KIND_LABEL[node.kind] ?? node.kind}</span>
      </div>
      {density === "columns" && columns.length ? (
        <div className="av-node__cols">
          {columns.slice(0, MAX_COLS).map((c) => (
            <button key={c.name} type="button" className={`av-col nodrag${pathCols.has(c.name) ? " is-path" : c.influence ? " is-influence" : ""}`}
              onClick={(e) => { e.stopPropagation(); data.onColumn(c.name); }}>
              <span>{c.name}</span><span>{c.relation ? RELATION_LABEL[c.relation] : ""}</span>
            </button>
          ))}
          {columns.length > MAX_COLS ? <button type="button" className="av-node__more av-lineage__all-columns nodrag" onClick={(event) => { event.stopPropagation(); data.onAllColumns(); }}>{t("查看全部 {{count}} 列", { count: columns.length })}</button> : null}
        </div>
      ) : null}
      {density === "columns" && grain ? <div className="av-node__grain">{t("粒度")} <b>{grain.columns.join(", ")}</b><Dot tone={tone} /></div> : null}
      {density === "columns" && link ? <div className="av-node__link"><Icon name="link" small />{t("同一张表")} <b>{link.target}</b><Status value={link.status} /></div> : null}
      <Handle type="source" position={Position.Right} className="av-lineage__handle" />
    </div>
  );
}

// 流动连线：数据沿边从上游流向下游。选中列的路径用渐变亮线和较快的光点，其余边变暗；
// 边不多时其余边也有慢而淡的光点。系统设置“减少动态效果”时光点隐藏（见 pages.css）。
type FlowData = {
  tone: "value" | "influence" | "identity" | "join";
  design: boolean;
  onPath: boolean;
  dim: boolean;
  ambient: boolean;
  labelDx?: number;
  labelDy?: number;
  /** 边类型徽章（计算 / 聚合 / 过滤 / 关联键 / 分组）；null 表示这条边不加徽章 */
  badge?: EdgeBadge | null;
};

// 徽章种类对应 RELATION_LABEL 的关系键，复用统一的读屏与图例译文
const BADGE_RELATION: Record<BadgeKind, string> = {
  aggregated: "value_aggregated", computed: "value_computed", filter: "filter", join_key: "join_key", group_by: "group_by",
};

function FlowEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, label, labelStyle,
  labelBgStyle }: EdgeProps<Edge<FlowData>>) {
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  const d = data!;
  const dots = d.onPath ? [0, 0.8] : [];
  const midX = labelX + (d.labelDx ?? 0);
  const midY = labelY + (d.labelDy ?? 0);
  return (
    <>
      {d.onPath ? <path d={path} className="av-flow__glow" /> : null}
      <BaseEdge id={id} path={path} label={label} labelX={midX} labelY={midY}
        labelStyle={labelStyle} labelBgStyle={labelBgStyle} labelBgPadding={[6, 3]} labelBgBorderRadius={4}
        className={`av-flow av-flow--${d.tone}${d.design ? " is-design" : ""}${d.onPath ? " is-path" : ""}${d.dim ? " is-dim" : ""}`}
        />
      {dots.length ? <g className={`av-flow__dots${d.onPath ? " is-path" : ""}`}>
        {dots.map((delay) => (
          <circle key={delay} r={d.onPath ? 3 : 1.8}>
            <animateMotion dur={d.onPath ? "1.6s" : "3.4s"} begin={`${delay}s`} repeatCount="indefinite" path={path} />
          </circle>
        ))}
      </g> : null}
      {/* 全局 data-tip 只作用于 HTML 元素，徽章经 EdgeLabelRenderer 挂到 HTML 层 */}
      {d.badge && !label ? (
        <EdgeLabelRenderer>
          <div
            className={`av-edge-badge nodrag nopan${d.onPath ? " is-path" : ""}${d.dim ? " is-dim" : ""}`}
            style={{ transform: `translate(-50%, -50%) translate(${midX}px, ${midY}px)` }}
            role="img"
            aria-label={d.badge.kinds.map((kind) => RELATION_LABEL[BADGE_RELATION[kind]]).join(t("、"))}
            data-tip={badgeTip(d.badge, t("（影响）").trim(), (count) => t("还有 {{count}} 条", { count }))}
          >
            {d.badge.kinds.map((kind) => (
              <span key={kind} className={`av-edge-badge__kind av-edge-badge__kind--${kind}`} aria-hidden="true">
                {BADGE_SYMBOL[kind]}
              </span>
            ))}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}

const nodeTypes = { asset: AssetNode };
const edgeTypes = { flow: FlowEdge };
const AMBIENT_LIMIT = 120;
const READABLE_ZOOM = 0.55;

export function LineagePage() {
  return <ReactFlowProvider><Canvas /></ReactFlowProvider>;
}

function Canvas() {
  const [params, setParams] = useSearchParams();
  const domains = useDomains();
  const domainIds = (params.get("domains") ?? "").split(",").filter(Boolean);
  const focusAsset = params.get("asset") ?? undefined;
  const graph = useGraph(domainIds, focusAsset);
  const [selected, setSelected] = useState<string | null>(focusAsset ?? null);
  const [column, setColumn] = useState<ColKey | null>(null);
  const [density, setDensity] = useState<AssetData["density"]>("overview");
  const [showColumns, setShowColumns] = useState(false);
  const [showJoins, setShowJoins] = useState(true);
  const [view, setView] = useState<"graph" | "table">("graph");
  const [direction, setDirection] = useState<Direction>("both");
  const [depth, setDepth] = useState<number | null>(null);
  const [ends, setEnds] = useState(false);
  const [domainOpen, setDomainOpen] = useState(false);
  const domainFilterRef = useRef<HTMLDivElement>(null);
  const domainTriggerRef = useRef<HTMLButtonElement>(null);
  // 业务域筛选弹层：点击外部（含画布捕获阶段）或按 Esc（走统一浮层栈）自动收起
  useOverlay(domainOpen, () => {
    const hadFocus = domainFilterRef.current?.contains(document.activeElement);
    setDomainOpen(false);
    if (hadFocus) domainTriggerRef.current?.focus();
  });
  useEffect(() => {
    if (!domainOpen) return;
    const onOutside = (e: Event) => {
      if (!domainFilterRef.current?.contains(e.target as globalThis.Node)) setDomainOpen(false);
    };
    window.addEventListener("pointerdown", onOutside, true);
    window.addEventListener("mousedown", onOutside, true);
    return () => {
      window.removeEventListener("pointerdown", onOutside, true);
      window.removeEventListener("mousedown", onOutside, true);
    };
  }, [domainOpen]);
  const [inspectorWidth, setInspectorWidth] = useState(readInspectorWidth);
  const resizing = useRef(false);
  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const box = (event.currentTarget.closest(".av-lineage") as HTMLElement).getBoundingClientRect();
    resizing.current = true;
    let next = inspectorWidth;
    const move = (e: PointerEvent) => {
      next = Math.round(Math.min(Math.max(box.right - e.clientX, INSPECTOR_MIN), Math.max(INSPECTOR_MIN, box.width * 0.6)));
      setInspectorWidth(next);
    };
    const up = () => {
      resizing.current = false;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      try { localStorage.setItem(INSPECTOR_KEY, String(next)); } catch { /* 拿不到存储时只在本次生效 */ }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const [nodeWidth] = useState(() => parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--size-node")));
  useEffect(() => { setSelected(focusAsset ?? null); setColumn(null); setShowColumns(false); }, [focusAsset]);
  const flow = useReactFlow();

  const swatchOf = useMemo(() => {
    const map = new Map<string, string>();
    (domains.data ?? []).forEach((d, i) => map.set(d.id, SWATCHES[i % SWATCHES.length]));
    return map;
  }, [domains.data]);

  // 选中路径：选了列时走列级数据流，只选了表时走表级上下游（含“同一张表”）；方向、深度由路径工具条控制。
  // 只沿数据流走（直接 / 计算 / 聚合 / 表级），过滤、关联键、分组不参与追溯（见 trace.ts）
  const flowIndex = useMemo(() => (graph.data ? buildFlowIndex(graph.data) : null), [graph.data]);
  const traced = useMemo<TraceResult | null>(() => {
    if (!flowIndex || !selected) return null;
    return column
      ? trace(flowIndex, column, { level: "column", direction, depth })
      : trace(flowIndex, selected, { level: "table", direction, depth });
  }, [flowIndex, selected, column, direction, depth]);
  const path = useMemo(() => ({ cols: traced?.cols ?? new Set<ColKey>(), edgeKeys: traced?.edgeKeys ?? new Set<string>() }), [traced]);
  // 检查器的字段清单始终取自完整图：只看两端时折叠图里的表级边没有字段名，设计时表又不另查资产字段
  const fullColumns = useMemo(() => (graph.data ? graphColumns(graph.data) : new Map<string, AssetData["columns"]>()), [graph.data]);
  // 只看两端：画布换成折叠了中间层的派生图（检查器仍按完整图显示真实上下游）
  const shown = useMemo<Graph | undefined>(() => {
    if (!graph.data || !ends || !traced || !selected) return graph.data;
    return endsGraph(graph.data, column ?? selected, traced, column ? "column" : "table");
  }, [graph.data, ends, traced, selected, column]);

  // dagre.layout 几何位置只依赖图结构、节点密度与宽度，不随选中列、悬停路径或关联路径显隐重算
  const layoutData = useMemo(() => {
    if (!shown) return null;
    const g = shown;
    const columnsByNode = graphColumns(g);
    const linkOf = new Map(g.identity_links.filter((l) => l.target).map((l) => [l.source, l]));
    const layout = new dagre.graphlib.Graph();
    layout.setGraph({ rankdir: "LR", nodesep: density === "overview" ? 16 : 28, ranksep: 96, marginx: 40, marginy: 40 });
    layout.setDefaultEdgeLabel(() => ({}));
    const heights = new Map<string, number>();
    const links = new Map<string, { status: string; target: string } | null>();
    for (const n of g.nodes) {
      const cols = columnsByNode.get(n.id) ?? [];
      const l = linkOf.get(n.qualified);
      const link = l ? { status: l.status, target: shortName(l.target?.qualified) } : null;
      links.set(n.id, link);
      const height = nodeHeight(cols.length, !!link, n.grains.length > 0, density);
      heights.set(n.id, height);
      layout.setNode(n.id, { width: nodeWidth, height });
    }
    const linked = new Set<string>();
    for (const e of g.edges) {
      layout.setEdge(e.source, e.target);
      linked.add(e.source);
      linked.add(e.target);
    }
    const idNodes = new Set(g.nodes.map((n) => n.id));
    const qualifiedNode = new Map(g.nodes.map((n) => [n.qualified, n]));
    for (const l of g.identity_links) {
      const src = qualifiedNode.get(l.source);
      if (!src || !l.target || !idNodes.has(l.target.id)) continue;
      layout.setEdge(l.target.id, src.id);
      linked.add(l.target.id);
      linked.add(src.id);
    }
    // 没有任何连线的表不参与分层，排成网格放在血缘图下方，避免把第一列拉成一长条
    const lonely = g.nodes.filter((n) => !linked.has(n.id));
    lonely.forEach((n) => layout.removeNode(n.id));
    dagre.layout(layout);
    const positions = new Map<string, { x: number; y: number }>();
    let bottom = 0;
    for (const n of g.nodes) {
      if (!linked.has(n.id)) continue;
      const p = layout.node(n.id);
      const h = heights.get(n.id) ?? 0;
      const pos = { x: p.x - nodeWidth / 2, y: p.y - h / 2 };
      positions.set(n.id, pos);
      bottom = Math.max(bottom, pos.y + h);
    }
    const perRow = 6;
    const rowHeight = Math.max(0, ...lonely.map((n) => heights.get(n.id) ?? 0)) + 16;
    lonely.forEach((n, i) => {
      positions.set(n.id, { x: 40 + (i % perRow) * (nodeWidth + 24), y: (linked.size ? bottom + 96 : 40) + Math.floor(i / perRow) * rowHeight });
    });
    return { columnsByNode, links, heights, positions, qualifiedNode, idNodes };
  }, [shown, density, nodeWidth]);

  const { nodes, edges } = useMemo(() => {
    if (!shown || !layoutData) return { nodes: [] as Node<AssetData>[], edges: [] as Edge[] };
    const g = shown;
    const { columnsByNode, links, heights, positions, qualifiedNode, idNodes } = layoutData;
    const built: Node<AssetData>[] = g.nodes.map((n) => ({
      id: n.id, type: "asset", position: positions.get(n.id) ?? { x: 0, y: 0 }, width: nodeWidth, height: heights.get(n.id),
      data: {
        node: n, columns: columnsByNode.get(n.id) ?? [], link: links.get(n.id) ?? null, selected: n.id === selected, density,
        domainSwatch: n.domain_id ? swatchOf.get(n.domain_id) ?? null : null,
        pathCols: new Set([...path.cols].filter((k) => k.startsWith(`${n.id}::`)).map((k) => k.split("::")[1])),
        onColumn: (col: string) => { setSelected(n.id); setColumn(`${n.id}::${col}`); },
        onAllColumns: () => { setSelected(n.id); setColumn(null); setShowColumns(true); },
      },
    }));
    const builtEdges: Edge<FlowData>[] = [];
    const hasPath = path.edgeKeys.size > 0;
    const ambient = g.edges.length <= AMBIENT_LIMIT;
    for (const e of g.edges) {
      const isTrace = e.relations.includes(TRACE_RELATION);
      const hops = isTrace && e.columns.length
        ? Math.min(...e.columns.map((c) => Number(c.attributes?.hops ?? 1)))
        : 1;
      const value = isTrace || e.relations.some((r) => VALUE.has(r));
      // 徽章：边不多时全部显示，边多时只在选中路径上显示，避免画布拥挤
      const badge = isTrace ? null : edgeBadge(e.columns);
      const designTime = !isTrace && e.origins.every((o) => o !== "parsed" && o !== "manual");
      const key = `${e.source}->${e.target}`;
      const onPath = isTrace || path.edgeKeys.has(key);
      builtEdges.push({
        id: key, source: e.source, target: e.target, type: "flow",
        // 直接相连（1 层）时中间没有折叠任何表，不加标签
        label: isTrace && hops > 1 ? t("经 {{count}} 层", { count: hops }) : undefined,
        labelStyle: { fill: isTrace ? "var(--flow)" : "var(--warn)", fontSize: 11, fontFamily: "var(--font-mono)" },
        labelBgStyle: { fill: "var(--bg-overlay)" },
        data: { tone: value ? "value" : "influence", design: designTime, onPath, dim: hasPath && !onPath, ambient,
          badge: badge && (ambient || onPath) ? badge : null },
      });
    }
    for (const l of g.identity_links) {
      const src = qualifiedNode.get(l.source);
      if (!src || !l.target || !idNodes.has(l.target.id)) continue;
      const onPath = path.edgeKeys.has(`id:${l.id}`);
      builtEdges.push({
        id: `id:${l.id}`, source: l.target.id, target: src.id, type: "flow", label: t("同一张表"),
        labelStyle: { fill: "var(--text-tertiary)", fontSize: 11 }, labelBgStyle: { fill: "var(--bg-overlay)" },
        data: { tone: "identity", design: false, onPath, dim: hasPath && !onPath, ambient },
      });
    }
    // 治理里的关联路径：画成独立的虚线，不参与布局（不改变血缘的上下游位置）；待审核的更淡
    if (showJoins) {
      for (const j of g.joins ?? []) {
        if (!idNodes.has(j.left_asset_id) || !idNodes.has(j.right_asset_id)) continue;
        const touches = selected === j.left_asset_id || selected === j.right_asset_id;
        builtEdges.push({
          id: `join:${j.id}`, source: j.left_asset_id, target: j.right_asset_id, type: "flow",
          label: `${j.status === "pending" ? t("待审核 · ") : ""}${j.left_columns.join(t("、"))}`,
          labelStyle: { fill: "var(--accent)", fontSize: 11, fontFamily: "var(--font-mono)" },
          labelBgStyle: { fill: "var(--bg-overlay)" },
          data: { tone: "join", design: j.status === "pending", onPath: false, dim: hasPath && !touches, ambient: false },
        });
      }
    }
    const placed: { x: number; y: number }[] = [];
    for (const edge of builtEdges) {
      if (!edge.label || !edge.data) continue;
      const sp = positions.get(edge.source);
      const tp = positions.get(edge.target);
      if (!sp || !tp) continue;
      const sh = heights.get(edge.source) ?? 80;
      const th = heights.get(edge.target) ?? 80;
      const mx = (sp.x + tp.x + nodeWidth) / 2;
      const my = (sp.y + sh / 2 + tp.y + th / 2) / 2;
      let chosenDy = 0;
      for (const cand of [0, -22, 22, -44, 44, -66, 66, -88, 88]) {
        if (placed.every((p) => Math.abs(p.x - mx) >= 110 || Math.abs(p.y - (my + cand)) >= 22)) {
          chosenDy = cand;
          break;
        }
      }
      edge.data.labelDy = chosenDy;
      placed.push({ x: mx, y: my + chosenDy });
    }
    return { nodes: built, edges: builtEdges };
  }, [layoutData, shown, selected, path, swatchOf, density, nodeWidth, showJoins]);

  const clearFilters = () => { setParams(new URLSearchParams()); setSelected(null); setColumn(null); setShowColumns(false); };
  const toggleDomain = (id: string) => {
    const next = domainIds.includes(id) ? domainIds.filter((d) => d !== id) : [...domainIds, id];
    const nextParams = new URLSearchParams();
    if (next.length) nextParams.set("domains", next.join(","));
    setParams(nextParams);
    setSelected(null);
    setColumn(null);
    setShowColumns(false);
  };
  const selectedNode = graph.isSuccess ? graph.data.nodes.find((node) => node.id === selected) : undefined;
  // 表格视图占满整个工作区，检查器只在图视图里显示
  const inspectorOpen = !!selectedNode && view === "graph";
  useEffect(() => {
    if (!shown?.nodes.length) return;
    // 表很多时整图缩到看不清没有意义：最小缩到能读出表名，其余靠搜索和拖动。切换“只看两端”时画布换了一张图，同样重新适配
    const frame = requestAnimationFrame(() => { void flow.fitView({ padding: 0.16, minZoom: READABLE_ZOOM }); });
    return () => cancelAnimationFrame(frame);
  }, [shown, density, flow]);
  // 选中表后，把它和高亮的上下游一起动画移到可见区域；属性栏刚打开时画布会变窄，等画布宽度更新后再定位
  const latest = useRef({ edges, path });
  latest.current = { edges, path };
  const width = useStore((st) => st.width);
  const height = useStore((st) => st.height);
  const zoom = useStore((st) => st.transform[2]);
  const pick = (id: string) => { setSelected(id); setColumn(null); setShowColumns(false); };
  useEffect(() => {
    if (!selected || resizing.current) return;
    const timer = setTimeout(() => {
      const ids = new Set([selected]);
      for (const e of latest.current.edges) {
        if (latest.current.path.edgeKeys.has(e.id)) { ids.add(e.source); ids.add(e.target); }
      }
      const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      // 整条路径放得下且看得清就整条显示；路径太长时以选中的表为中心，保持能读出表名
      const bounds = flow.getNodesBounds([...ids]);
      const fits = Math.min(width / (bounds.width * 1.4), height / (bounds.height * 1.4)) >= READABLE_ZOOM;
      void flow.fitView({ nodes: (fits ? [...ids] : [selected]).map((id) => ({ id })), padding: 0.2,
        maxZoom: fits ? 1.1 : 0.8, duration: still ? 0 : 500 });
    }, 60);
    return () => clearTimeout(timer);
  }, [selected, width, flow]);

  return (
    <div className="av-lineage" data-inspector-open={inspectorOpen} style={{ "--lineage-inspector-w": `${inspectorWidth}px` } as CSSProperties}>
      <div className="av-lineage__workspace">
        <div className="av-lineage__toolbar" aria-label={t("血缘工具栏")}>
          <div ref={domainFilterRef} className="av-lineage__domain-filter">
            <button ref={domainTriggerRef} type="button" className="av-btn av-btn--secondary av-btn--sm" aria-expanded={domainOpen}
              onClick={() => setDomainOpen((open) => !open)}>{t("业务域")}{domainIds.length ? ` · ${domainIds.length}` : t(" · 全部")}<Icon name="chevron" small /></button>
            {domainOpen ? <div className="av-lineage__domain-menu">
              <QueryState query={domains} isEmpty={(items) => !items.length} emptyText={t("暂无业务域")} compact rows={3}>
                {(items) => <fieldset><legend className="av-section-label">{t("筛选业务域")}</legend>{items.map((domain) => <label key={domain.id}>
                  <input type="checkbox" checked={domainIds.includes(domain.id)} onChange={() => toggleDomain(domain.id)} />
                  <span className={"av-swatch av-swatch--" + swatchOf.get(domain.id)} />{domain.name}
                </label>)}</fieldset>}
              </QueryState>
              <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={!domainIds.length && !focusAsset} onClick={clearFilters}>{t("清除筛选")}</button>
            </div> : null}
          </div>
          {graph.isSuccess && graph.data.nodes.length ? <NodeSearch nodes={graph.data.nodes} onPick={pick} /> : null}
          {/* 关联路径显隐改用 Switch 控件替代原生复选框 */}
          <label className="av-lineage__toggle"><Switch checked={showJoins} onChange={setShowJoins} label={t("关联路径")} /><span>{t("关联路径")}</span></label>
          <Seg value={density} label={t("节点显示")} options={[{ value: "overview", label: t("只看表") }, { value: "columns", label: t("显示字段") }]} onChange={setDensity} />
          <Seg value={view} label={t("视图")} options={[{ value: "graph", label: t("图") }, { value: "table", label: t("表格") }]} onChange={setView} />
          {focusAsset ? <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={clearFilters}>{t("查看全部血缘")}</button> : null}
          <Link className="av-btn av-btn--sm av-btn--primary av-lineage__import" to="/lineage/import"><Icon name="upload" small />{t("导入")}</Link>
        </div>
        <div className="av-canvas av-lineage__canvas">
          <QueryState query={graph} isEmpty={(data) => !data.nodes.length}
            emptyText={domainIds.length || focusAsset ? t("没有匹配的血缘") : t("暂无血缘")}
            emptyAction={domainIds.length || focusAsset ? <button type="button" className="av-btn av-btn--secondary" onClick={clearFilters}>{t("清除筛选")}</button> : <Link className="av-btn av-btn--primary" to="/lineage/import">{t("导入血缘")}</Link>}>
            {(data) => view === "table" ? (
              <EdgeTable graph={data} pathEdges={traced ? traced.edgeKeys : null} pathCols={traced ? traced.cols : null} originLabel={(origin) => ORIGIN[origin] ?? origin}
                onLocate={(id, col) => { setView("graph"); setSelected(id); setColumn(col ? `${id}::${col}` : null); setShowColumns(false); }} />
            ) : (
              <ErrorBoundary compact label={t("血缘画布渲染异常")}>
                <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} minZoom={0.1} maxZoom={1.6}
                  proOptions={{ hideAttribution: true }} nodesDraggable={false} nodesConnectable={false}
                  onNodeClick={(_, node) => pick(node.id)}
                  onPaneClick={() => { setSelected(null); setColumn(null); setShowColumns(false); }} />
              </ErrorBoundary>
            )}
          </QueryState>
          {view === "graph" && traced && selectedNode ? <PathBar level={column ? "column" : "table"} direction={direction} onDirection={setDirection}
            depth={depth} onDepth={setDepth} ends={ends} onEnds={setEnds} result={traced} /> : null}
          {view === "graph" && graph.isSuccess && graph.data.nodes.length ? <>
            <details className="av-float av-disclosure av-lineage__legend">
              <summary>{t("图例")}</summary>
              <div className="av-lineage__legend-items">
                <Legend tone="value" label={t("取值")} />
                <Legend tone="influence" label={t("过滤 / 关联 / 分组")} />
                {BADGE_ORDER.map((kind) => (
                  <span key={kind} className="av-caption av-lineage__legend-item">
                    <span className="av-edge-badge av-edge-badge--legend" aria-hidden="true">
                      <span className={`av-edge-badge__kind av-edge-badge__kind--${kind}`}>{BADGE_SYMBOL[kind]}</span>
                    </span>
                    {RELATION_LABEL[BADGE_RELATION[kind]]}
                  </span>
                ))}
                <Legend tone="identity" label={t("同一张表")} />
                <Legend tone="join" label={t("关联路径（治理）")} />
                <Legend tone="design" label={t("导入（设计时）")} />
                <Legend tone="path" label={t("选中路径")} />
              </div>
            </details>
            <div className="av-float av-lineage__zoom" role="group" aria-label={t("画布缩放")}>
              <button type="button" className="av-btn av-btn--sm av-btn--ghost av-btn--icon" aria-label={t("缩小")}
                onClick={() => flow.zoomOut({ duration: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 200 })}><Icon name="zoomout" /></button>
              <button type="button" className="av-btn av-btn--sm av-btn--ghost av-lineage__zoom-value" title={t("恢复到 100%")}
                aria-label={t("当前缩放 {{pct}}%，恢复到 100%", { pct: Math.round(zoom * 100) })}
                onClick={() => flow.zoomTo(1, { duration: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 200 })}>{Math.round(zoom * 100)}%</button>
              <button type="button" className="av-btn av-btn--sm av-btn--ghost av-btn--icon" aria-label={t("放大")}
                onClick={() => flow.zoomIn({ duration: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 200 })}><Icon name="zoomin" /></button>
              <button type="button" className="av-btn av-btn--sm av-btn--ghost"
                onClick={() => flow.fitView({ padding: 0.16, duration: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : undefined })}><Icon name="fit" />{t("适应画布")}</button>
            </div>
          </> : null}
        </div>
      </div>
      {inspectorOpen && selectedNode && graph.data ? <aside className="av-inspector av-lineage__inspector" aria-label={t("检查器")}>
        <div className="av-lineage__resize" role="separator" aria-orientation="vertical" aria-label={t("拖动调整检查器宽度")}
          title={t("拖动调整宽度，双击恢复默认")} onPointerDown={startResize}
          onDoubleClick={() => { setInspectorWidth(INSPECTOR_DEFAULT); try { localStorage.removeItem(INSPECTOR_KEY); } catch { /* 忽略 */ } }} />
        <Inspector key={selectedNode.id} node={selectedNode} graph={graph.data}
          graphColumnNames={(fullColumns.get(selectedNode.id) ?? []).map((item) => item.name)}
          column={column?.split("::")[1] ?? null}
          showColumns={showColumns} onShowColumns={() => setShowColumns(true)} onColumn={(name) => setColumn(selectedNode.id + "::" + name)}
          onPick={pick} onPickColumn={(id, col) => { setSelected(id); setColumn(`${id}::${col}`); setShowColumns(false); }}
          onClose={() => { setSelected(null); setColumn(null); setShowColumns(false); }} />
      </aside> : null}
    </div>
  );
}

const INSPECTOR_KEY = "avelis.lineage.inspector";
const INSPECTOR_MIN = 300;
const INSPECTOR_DEFAULT = 360;

function readInspectorWidth(): number {
  try {
    const saved = Number(localStorage.getItem(INSPECTOR_KEY));
    return Number.isFinite(saved) && saved >= INSPECTOR_MIN ? saved : INSPECTOR_DEFAULT;
  } catch {
    return INSPECTOR_DEFAULT;
  }
}

/** 按表名找表：输入时列出匹配项，回车或点选后选中并定位到画布上。 */
function NodeSearch({ nodes, onPick }: { nodes: GNode[]; onPick: (id: string) => void }) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const q = text.trim().toLocaleLowerCase();
  const hits = q ? nodes.filter((n) => n.qualified.toLocaleLowerCase().includes(q)).slice(0, 8) : [];
  const choose = (n: GNode) => { onPick(n.id); setText(""); setOpen(false); };
  return (
    <div className="av-lineage__search" onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false); }}>
      <Icon name="search" small />
      <input type="search" value={text} placeholder={t("搜索表（共 {{count}} 张）", { count: nodes.length })} aria-label={t("搜索表")}
        onChange={(e) => { setText(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && hits[0]) choose(hits[0]);
          if (e.key === "Escape") { setText(""); setOpen(false); }
        }} />
      {open && q ? <div className="av-menu av-lineage__search-menu" role="listbox">
        {hits.length ? hits.map((n) => (
          <button key={n.id} type="button" role="option" aria-selected={false} className="av-menu__item" onClick={() => choose(n)}>
            <span className="av-mono">{shortName(n.qualified)}</span>
            <span className="av-caption">{KIND_LABEL[n.kind] ?? n.kind}</span>
          </button>
        )) : <span className="av-caption av-lineage__search-empty">{t("没有匹配的表")}</span>}
      </div> : null}
    </div>
  );
}

/** 路径工具条：浮在画布左上角，只在选中表或字段时出现。 */
function PathBar({ level, direction, onDirection, depth, onDepth, ends, onEnds, result }: {
  level: "table" | "column"; direction: Direction; onDirection: (v: Direction) => void;
  depth: number | null; onDepth: (v: number | null) => void; ends: boolean; onEnds: (v: boolean) => void; result: TraceResult;
}) {
  const summary = [
    ...(direction !== "down" ? [t("上游 {{count}} 层", { count: result.reach.up })] : []),
    ...(direction !== "up" ? [t("下游 {{count}} 层", { count: result.reach.down })] : []),
    ...(direction !== "down" ? [t("来源 {{count}}", { count: result.sources.size })] : []),
    ...(direction !== "up" ? [t("去向 {{count}}", { count: result.sinks.size })] : []),
  ].join(" · ");
  return (
    <div className="av-float av-lineage__pathbar" role="group" aria-label={t("追溯路径")}>
      <span className="av-sr">{level === "column" ? t("当前按字段追溯") : t("当前按表追溯")}</span>
      <Seg<Direction>
        value={direction}
        label={t("追溯方向")}
        options={[
          { value: "up", label: t("上游") },
          { value: "both", label: t("双向") },
          { value: "down", label: t("下游") },
        ]}
        onChange={onDirection}
      />
      <Select
        className="av-lineage__depth"
        value={depth === null ? "" : String(depth)}
        label={t("追溯深度")}
        onChange={(v) => onDepth(v === "" ? null : Number(v))}
      >
        <option value="">{t("全部层级")}</option>
        {[1, 2, 3, 5].map((n) => (
          <option key={n} value={String(n)}>{t("{{count}} 层", { count: n })}</option>
        ))}
      </Select>
      <label className="av-lineage__toggle" data-tip={t("只保留起点与最上游来源、最终去向，中间流经的表折叠")}>
        <Switch checked={ends} onChange={onEnds} label={t("只看两端")} />
        <span>{t("只看两端")}</span>
      </label>
      <span className="av-caption av-lineage__pathbar-meta">{summary}</span>
    </div>
  );
}

function Legend({ tone, label }: { tone: "value" | "influence" | "identity" | "join" | "design" | "path"; label: string }) {
  return <span className={"av-caption av-lineage__legend-item av-lineage__legend-item--" + tone}>
    <svg width="24" height="6" aria-hidden="true">
      <line x1="0" y1="3" x2="24" y2="3" />
    </svg>{label}
  </span>;
}

function Inspector({ node, graph, graphColumnNames, column, showColumns, onShowColumns, onColumn, onPick, onPickColumn, onClose }: {
  node: GNode; graph: Graph; graphColumnNames: string[]; column: string | null; showColumns: boolean; onShowColumns: () => void;
  onColumn: (column: string) => void; onPick: (id: string) => void; onPickColumn: (id: string, column: string) => void; onClose: () => void;
}) {
  const [search, setSearch] = useState("");
  const asset = useAsset(node.kind === "design_time" ? undefined : node.id);
  const incoming = graph.edges.filter((e) => e.target === node.id);
  const outgoing = graph.edges.filter((e) => e.source === node.id);
  const name = (id: string) => graph.nodes.find((n) => n.id === id)?.qualified ?? id.replace(/^ref:/, "");
  const colIn = column ? incoming.flatMap((e) => e.columns.filter((c) => c.target_column === column).map((c) => ({ ...c, from: name(e.source), fromId: e.source }))) : [];
  // 影响条件：过滤、关联键、分组；表级关系已经在“上游”里，没有列名的不重复列出。按上游表分组
  const influences = incoming.flatMap((e) => e.columns.filter((c) => !c.target_column && c.source_column && !VALUE.has(c.relation))
    .map((c) => ({ ...c, from: name(e.source), fromId: e.source })));
  const influenceGroups = [...influences.reduce((map, c) => map.set(c.from, [...(map.get(c.from) ?? []), c]),
    new Map<string, typeof influences>())];
  const colOut = column ? outgoing.flatMap((e) => e.columns.filter((c) => c.source_column === column).map((c) => ({ ...c, to: name(e.target), toId: e.target }))) : [];
  const col = asset.data?.columns.find((c) => c.name === column);
  const columns = [...new Set([...(asset.data?.columns.map((item) => item.name) ?? []), ...graphColumnNames])];
  const visibleColumns = columns.filter((name) => name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  return (
    <>
      <div className="av-inspector__hd">
        <div className="av-lineage__inspector-actions">
          <span className="av-section-label">{KIND_LABEL[node.kind] ?? node.kind}{node.datasource ? ` · ${node.datasource.name}` : ""}</span>
          <div className="av-actions">
            {node.kind !== "design_time" ? <Link className="av-btn av-btn--secondary av-btn--sm" to={`/catalog/assets/${node.id}`}>{t("资产")}</Link> : null}
            <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" aria-label={t("关闭检查器")} onClick={onClose}><Icon name="close" /></button>
          </div>
        </div>
        <span className="av-mono av-lineage__qualified">{node.kind !== "design_time" ? <Link to={`/catalog/assets/${node.id}`}>{node.qualified}</Link> : node.qualified}{column ? <span className="av-lineage__column-name">.{column}</span> : null}</span>
        {node.note ? <span className="av-caption">{node.note}</span> : null}
      </div>
      <div className="av-inspector__bd">
        {showColumns ? <section className="av-lineage__column-browser" aria-label={t("全部字段")}>
          <label className="av-section-label" htmlFor="lineage-column-search">{t("字段")} {asset.isSuccess || node.kind === "design_time" ? `· ${columns.length}` : ""}</label>
          <input id="lineage-column-search" className="av-input" type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t("搜索字段")} autoFocus />
          {node.kind !== "design_time" && asset.isPending ? <span className="av-caption" role="status">{t("正在加载完整字段…")}</span> : null}
          {node.kind !== "design_time" && asset.isError ? <div className="av-lineage__column-error" role="alert"><span className="av-caption">{t("完整字段加载失败，当前仅显示血缘字段")}</span><button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => asset.refetch()}>{t("重试")}</button></div> : null}
          <div className="av-lineage__column-list">{visibleColumns.map((name) => <button key={name} type="button" className="av-btn av-btn--ghost av-lineage__column-item" aria-pressed={column === name} onClick={() => onColumn(name)}><span className="av-mono">{name}</span><span className="av-caption">{asset.data?.columns.find((item) => item.name === name)?.data_type}</span></button>)}</div>
          {!visibleColumns.length && columns.length ? <Empty text={t("没有匹配字段")} compact action={<button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setSearch("")}>{t("清除搜索")}</button>} /> : null}
          {!columns.length && (asset.isSuccess || node.kind === "design_time") ? <Empty text={t("暂无字段")} compact /> : null}
        </section> : <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={onShowColumns}>{t("查看全部字段")}</button>}
        {column ? (
          <>
            {col ? <dl className="av-kv"><dt>{t("类型")}</dt><dd className="av-mono">{col.data_type}</dd>
              <dt>{t("推导")}</dt><dd><span data-tip={col.fingerprint ? t("加工方式校验码 {{v1}}", { v1: col.fingerprint.slice(0, 10) }) : undefined}><Status value={col.fingerprint_status ?? "unknown"} /></span></dd>
              {col.fingerprint_reason ? <><dt>{t("原因")}</dt><dd>{col.fingerprint_reason}</dd></> : null}
              <dt>{t("业务含义")}</dt><dd>{col.confirmations.find((c) => c.valid)?.meaning ?? <span className="av-muted">{t("未确认")}</span>}</dd></dl> : null}
            <Flow title={t("来源")} onPick={(k) => { const c = colIn.find((x) => `${x.fromId}::${x.source_column}` === k); if (c?.source_column) onPickColumn(c.fromId, c.source_column); }} items={colIn.map((c) => ({ key: `${c.from}.${c.source_column}`, target: `${c.fromId}::${c.source_column}`, left: `${shortName(c.from)}.${c.source_column}`, rel: c.relation, origins: [c.origin], expr: c.expression }))} />
            <Flow title={t("下游")} onPick={(k) => { const [id, col] = k.split("::"); if (col) onPickColumn(id, col); else onPick(id); }} items={colOut.map((c) => ({ key: `${c.to}.${c.target_column}`, target: `${c.toId}::${c.target_column ?? ""}`, left: `${shortName(c.to)}.${c.target_column ?? t("（影响）")}`, rel: c.relation, origins: [c.origin], expr: c.expression }))} />
          </>
        ) : (
          <>
            {/* 粒度列与核验状态拆分为弹性换行子元素，去掉折行后悬空的孤立间隔点 */}
            {node.grains.length ? <dl className="av-kv"><dt>{t("粒度")}</dt><dd>{node.grains.map((g) => <span key={g.columns.join()} className="av-tag av-lineage__grain-tag"><span>{g.columns.join(", ")}</span><Status value={g.status} /></span>)}</dd></dl> : null}
            <Flow title={t("上游")} onPick={onPick} items={[...new Map(incoming.map((e) => [e.source, e])).values()].map((e) => ({ key: e.source, target: e.source, left: shortName(name(e.source)), rel: e.relations.find((r) => VALUE.has(r)) ?? e.relations[0], origins: e.origins, expr: t("{{count}} 条列级边", { count: e.columns.length }) }))} />
            <Flow title={t("下游")} onPick={onPick} items={[...new Map(outgoing.map((e) => [e.target, e])).values()].map((e) => ({ key: e.target, target: e.target, left: shortName(name(e.target)), rel: e.relations.find((r) => VALUE.has(r)) ?? e.relations[0], origins: e.origins, expr: t("{{count}} 条列级边", { count: e.columns.length }) }))} />
          </>
        )}
        <NodeJoins node={node} graph={graph} name={name} onPick={onPick} />
        {/* 血缘体检：只对已采集的表（设计时表没有资产记录） */}
        {node.kind !== "design_time" ? <HealthPanel key={node.id} assetId={node.id} onPickAsset={onPick} /> : null}
        {influenceGroups.length ? (
          <details className="av-fold av-lineage__influences">
            <summary>{t("影响条件")} <span className="av-caption">{t("{{count}} 项", { count: influences.length })}</span>
              <Hint text={t("过滤、关联键、分组决定哪些行参与，但值不复制到输出")} /></summary>
            {influenceGroups.map(([from, items]) => (
              <div key={from} className="av-lineage__stack">
                <span className="av-caption av-mono" title={from}>{shortName(from)}</span>
                {items.map((c, i) => <button key={i} type="button" className="av-chain__item av-lineage__chain-link" title={t("查看 {{v1}}.{{source_column}}", { v1: shortName(c.from), source_column: c.source_column })}
                  onClick={() => onPickColumn(c.fromId, c.source_column!)}><span className="av-tag">{RELATION_LABEL[c.relation] ?? c.relation}</span><span>{c.source_column}</span></button>)}
              </div>
            ))}
          </details>
        ) : null}
      </div>
    </>
  );
}

/** 这张表参与的关联路径（来自治理，已批准或待审核）；在目录里审核和维护。 */
function NodeJoins({ node, graph, name, onPick }: { node: GNode; graph: Graph; name: (id: string) => string; onPick: (id: string) => void }) {
  const joins = (graph.joins ?? []).filter((j) => j.left_asset_id === node.id || j.right_asset_id === node.id);
  return (
    <div className="av-lineage__stack">
      <span className="av-section-label">{t("关联路径")} <span className="av-caption">{joins.length}</span>
        <Hint text={t("表之间怎么关联由治理维护：分析里确认的关联会生成待审核的关联路径，负责人批准后所有人都能用")} /></span>
      {joins.map((j) => {
        const self = j.left_asset_id === node.id;
        const other = self ? j.right_asset_id : j.left_asset_id;
        const keys = (self ? j.left_columns : j.right_columns).map((c, i) => `${c} = ${(self ? j.right_columns : j.left_columns)[i]}`).join(t("，"));
        return (
          <button key={j.id} type="button" className="av-lineage__flow-item is-link" onClick={() => onPick(other)} title={t("定位到 {{v1}}", { v1: name(other) })}>
            <span className="av-chain__item"><span className="av-lineage__flow-name">{shortName(name(other))}</span>
              <Status value={j.status} /></span>
            <span className="av-caption av-mono">{keys}</span>
          </button>
        );
      })}
      <Link className="av-btn av-btn--ghost av-btn--sm" to="/catalog/joins">{joins.length ? t("审核与维护关联路径") : t("在目录里维护关联路径")}</Link>
    </div>
  );
}

const ORIGIN: Record<string, string> = { parsed: t("解析", { context: "origin" }), manual: t("人工"), import: t("导入解析"), import_manual: t("导入·人工"), model_confirmed: t("模型·已确认") };

function Flow({ title, items, onPick }: {
  title: string; onPick?: (id: string) => void;
  items: { key: string; target?: string; left: string; rel: string; origins: string[]; expr?: string | null }[];
}) {
  if (!items.length) return null;
  return (
    <div className="av-lineage__stack">
      <span className="av-section-label">{title} <span className="av-caption">{items.length}</span></span>
      {items.map((it) => {
        const body = <>
          <span className="av-chain__item"><span className="av-lineage__flow-name" title={it.left}>{it.left}</span>
            <span className="av-tag av-lineage__relation-tag">{RELATION_LABEL[it.rel] ?? it.rel}</span></span>
          <span className="av-caption">{it.origins.map((o) => ORIGIN[o] ?? o).join(t("、"))}{it.expr ? ` · ${it.expr}` : ""}</span>
        </>;
        return it.target && onPick
          ? <button key={it.key} type="button" className="av-lineage__flow-item is-link" title={t("定位到 {{left}}", { left: it.left })} onClick={() => onPick(it.target!)}>{body}</button>
          : <div key={it.key} className="av-lineage__flow-item">{body}</div>;
      })}
    </div>
  );
}
