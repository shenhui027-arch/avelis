// 血缘：画布模式 —— 列级血缘画布（@xyflow/react + dagre 自动布局）| 检查器。
// 节点按 Avelis UI 的 av-node 渲染；取值边为实线，过滤 / 关联键 / 分组这类影响关系为警告色虚线，
// 外部表与真实表是“同一张表”（点线 + 链条标记），导入的设计时表为虚线占位节点。点击列高亮列级路径。
import dagre from "@dagrejs/dagre";
import {
  BaseEdge, getBezierPath, Handle, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type Edge, type EdgeProps, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { useAsset, useDomains, useGraph, type Graph } from "../../api/hooks";
import {
  Dot, Empty, Hint, Icon, KIND_LABEL, Plat, QueryState, RELATION_LABEL, Seg, Status, statusOf, shortName,
} from "../../components/ui";
import { SWATCHES } from "../catalog/CatalogPage";

type GNode = Graph["nodes"][number];
type ColKey = string; // `${nodeId}::${column}`

const MAX_COLS = 6;
const VALUE = new Set(["value_direct", "value_computed", "value_aggregated", "table"]);

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
  if (density === "overview") return 54 + (cols ? 36 : 0);
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
  return (
    <div className={`av-node${selected ? " is-selected" : ""}${placeholder ? " is-placeholder" : ""}`}>
      <Handle type="target" position={Position.Left} className="av-lineage__handle" />
      <div className="av-node__hd">
        <span className="av-node__type"><Icon name={node.kind === "view" ? "view" : node.kind === "design_time" ? "upload" : "table"} /></span>
        <span className="av-node__id">
          <span className="av-node__path">
            {domainSwatch ? <span className={`av-swatch av-swatch--${domainSwatch}`} /> : null}
            {node.datasource ? <Plat kind={node.datasource.kind} /> : <span className="av-plat">导入</span>}
            {parts.slice(-3, -1).join(".")}
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
          {columns.length > MAX_COLS ? <button type="button" className="av-node__more av-lineage__all-columns nodrag" onClick={(event) => { event.stopPropagation(); data.onAllColumns(); }}>查看全部 {columns.length} 列</button> : null}
        </div>
      ) : null}
      {density === "overview" && columns.length ? <button type="button" className="av-lineage__all-columns nodrag" onClick={(event) => { event.stopPropagation(); data.onAllColumns(); }}>查看全部 {columns.length} 列</button> : null}
      {density === "columns" && grain ? <div className="av-node__grain">粒度 <b>{grain.columns.join(", ")}</b><Dot tone={tone} /></div> : null}
      {density === "columns" && link ? <div className="av-node__link"><Icon name="link" small />同一张表 <b>{link.target}</b><Status value={link.status} /></div> : null}
      <Handle type="source" position={Position.Right} className="av-lineage__handle" />
    </div>
  );
}

// 流动连线：数据沿边从上游流向下游。选中列的路径用渐变亮线和较快的光点，其余边变暗；
// 边不多时其余边也有慢而淡的光点。系统设置“减少动态效果”时光点隐藏（见 pages.css）。
type FlowData = { tone: "value" | "influence" | "identity"; design: boolean; onPath: boolean; dim: boolean; ambient: boolean };

function FlowEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, label, labelStyle,
  labelBgStyle }: EdgeProps<Edge<FlowData>>) {
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  const d = data!;
  const gid = `flow-${id}`;
  const dots = d.onPath ? [0, 0.8] : d.ambient && !d.dim && d.tone !== "identity" ? [0] : [];
  return (
    <>
      {d.onPath ? <>
        <defs>
          <linearGradient id={gid} gradientUnits="userSpaceOnUse" x1={sourceX} y1={sourceY} x2={targetX} y2={targetY}>
            <stop offset="0" stopColor="#3d6cf0" /><stop offset=".6" stopColor="#6d5cff" /><stop offset="1" stopColor="#22d3ee" />
          </linearGradient>
        </defs>
        <path d={path} className="av-flow__glow" />
      </> : null}
      <BaseEdge id={id} path={path} label={label} labelX={labelX} labelY={labelY} labelStyle={labelStyle} labelBgStyle={labelBgStyle}
        className={`av-flow av-flow--${d.tone}${d.design ? " is-design" : ""}${d.onPath ? " is-path" : ""}${d.dim ? " is-dim" : ""}`}
        style={d.onPath ? { stroke: `url(#${gid})` } : undefined} />
      {dots.length ? <g className={`av-flow__dots${d.onPath ? " is-path" : ""}`}>
        {dots.map((delay) => (
          <circle key={delay} r={d.onPath ? 3 : 1.8}>
            <animateMotion dur={d.onPath ? "1.6s" : "3.4s"} begin={`${delay}s`} repeatCount="indefinite" path={path} />
          </circle>
        ))}
      </g> : null}
    </>
  );
}

const nodeTypes = { asset: AssetNode };
const edgeTypes = { flow: FlowEdge };
const AMBIENT_LIMIT = 120;

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
  const [nodeWidth] = useState(() => parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--size-node")));
  useEffect(() => { setSelected(focusAsset ?? null); setColumn(null); setShowColumns(false); }, [focusAsset]);
  const flow = useReactFlow();

  const swatchOf = useMemo(() => {
    const map = new Map<string, string>();
    (domains.data ?? []).forEach((d, i) => map.set(d.id, SWATCHES[i % SWATCHES.length]));
    return map;
  }, [domains.data]);

  // 列级路径：从选中的列向上游、下游各走一遍（只走取值关系）
  const path = useMemo(() => {
    const cols = new Set<ColKey>();
    const edgeKeys = new Set<string>();
    if (!column || !graph.data) return { cols, edgeKeys };
    const up = new Map<ColKey, { from: ColKey; edge: string }[]>();
    const down = new Map<ColKey, { to: ColKey; edge: string }[]>();
    for (const e of graph.data.edges) {
      for (const c of e.columns) {
        if (!c.target_column || !c.source_column || !VALUE.has(c.relation)) continue;
        const s = `${e.source}::${c.source_column}`;
        const t = `${e.target}::${c.target_column}`;
        const key = `${e.source}->${e.target}`;
        up.set(t, [...(up.get(t) ?? []), { from: s, edge: key }]);
        down.set(s, [...(down.get(s) ?? []), { to: t, edge: key }]);
      }
    }
    for (const l of graph.data.identity_links) {
      if (!l.target) continue;
      const src = graph.data.nodes.find((n) => n.qualified === l.source);
      if (!src) continue;
      for (const m of ((l.column_match as { matched?: string[] }).matched ?? [])) {
        const a = `${src.id}::${m}`;
        const b = `${l.target.id}::${m}`;
        up.set(a, [...(up.get(a) ?? []), { from: b, edge: `id:${l.id}` }]);
        down.set(b, [...(down.get(b) ?? []), { to: a, edge: `id:${l.id}` }]);
      }
    }
    const walk = (start: ColKey, next: Map<ColKey, { from?: ColKey; to?: ColKey; edge: string }[]>) => {
      const stack = [start];
      const visited = new Set<ColKey>();
      while (stack.length) {
        const k = stack.pop()!;
        if (visited.has(k)) continue;
        visited.add(k);
        cols.add(k);
        for (const n of next.get(k) ?? []) {
          edgeKeys.add(n.edge);
          stack.push((n.from ?? n.to)!);
        }
      }
    };
    walk(column, up);
    walk(column, down as unknown as Map<ColKey, { from?: ColKey; edge: string }[]>);
    return { cols, edgeKeys };
  }, [column, graph.data]);

  const { nodes, edges } = useMemo(() => {
    if (!graph.data) return { nodes: [] as Node<AssetData>[], edges: [] as Edge[] };
    const g = graph.data;
    const columnsByNode = graphColumns(g);
    const linkOf = new Map(g.identity_links.filter((l) => l.target).map((l) => [l.source, l]));
    const layout = new dagre.graphlib.Graph();
    layout.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 96, marginx: 40, marginy: 40 });
    layout.setDefaultEdgeLabel(() => ({}));
    const built: Node<AssetData>[] = g.nodes.map((n) => {
      const cols = columnsByNode.get(n.id) ?? [];
      const l = linkOf.get(n.qualified);
      const link = l ? { status: l.status, target: shortName(l.target?.qualified) } : null;
      const height = nodeHeight(cols.length, !!link, n.grains.length > 0, density);
      layout.setNode(n.id, { width: nodeWidth, height });
      return {
        id: n.id, type: "asset", position: { x: 0, y: 0 }, width: nodeWidth, height,
        data: {
          node: n, columns: cols, link, selected: n.id === selected, density,
          domainSwatch: n.domain_id ? swatchOf.get(n.domain_id) ?? null : null,
          pathCols: new Set([...path.cols].filter((k) => k.startsWith(`${n.id}::`)).map((k) => k.split("::")[1])),
          onColumn: (col: string) => { setSelected(n.id); setColumn(`${n.id}::${col}`); },
          onAllColumns: () => { setSelected(n.id); setColumn(null); setShowColumns(true); },
        },
      };
    });
    const builtEdges: Edge<FlowData>[] = [];
    const hasPath = path.edgeKeys.size > 0;
    const ambient = g.edges.length <= AMBIENT_LIMIT;
    for (const e of g.edges) {
      const value = e.relations.some((r) => VALUE.has(r));
      const designTime = e.origins.every((o) => o !== "parsed" && o !== "manual");
      const key = `${e.source}->${e.target}`;
      const onPath = path.edgeKeys.has(key);
      layout.setEdge(e.source, e.target);
      builtEdges.push({
        id: key, source: e.source, target: e.target, type: "flow",
        label: !value && e.relations.includes("join_key") ? "关联" : undefined,
        labelStyle: { fill: "var(--warn)", fontSize: 11, fontFamily: "var(--font-mono)" },
        labelBgStyle: { fill: "var(--bg-overlay)" },
        data: { tone: value ? "value" : "influence", design: designTime, onPath, dim: hasPath && !onPath, ambient },
      });
    }
    for (const l of g.identity_links) {
      const src = g.nodes.find((n) => n.qualified === l.source);
      if (!src || !l.target || !g.nodes.some((n) => n.id === l.target!.id)) continue;
      layout.setEdge(l.target.id, src.id);
      const onPath = path.edgeKeys.has(`id:${l.id}`);
      builtEdges.push({
        id: `id:${l.id}`, source: l.target.id, target: src.id, type: "flow", label: "同一张表",
        labelStyle: { fill: "var(--text-tertiary)", fontSize: 11 }, labelBgStyle: { fill: "var(--bg-overlay)" },
        data: { tone: "identity", design: false, onPath, dim: hasPath && !onPath, ambient },
      });
    }
    dagre.layout(layout);
    for (const n of built) {
      const p = layout.node(n.id);
      n.position = { x: p.x - nodeWidth / 2, y: p.y - (n.height ?? 0) / 2 };
    }
    return { nodes: built, edges: builtEdges };
  }, [graph.data, selected, path, swatchOf, density, nodeWidth]);

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
  const inspectorOpen = !!selectedNode;
  useEffect(() => {
    if (!graph.isSuccess || !graph.data.nodes.length) return;
    const frame = requestAnimationFrame(() => { void flow.fitView({ padding: 0.16 }); });
    return () => cancelAnimationFrame(frame);
  }, [graph.data, graph.isSuccess, density, flow]);

  return (
    <div className="av-lineage" data-inspector-open={inspectorOpen}>
      <div className="av-lineage__workspace">
        <div className="av-lineage__toolbar" aria-label="血缘工具栏">
          <details className="av-lineage__domain-filter">
            <summary className="av-btn av-btn--secondary av-btn--sm">业务域{domainIds.length ? ` · ${domainIds.length}` : " · 全部"}<Icon name="chevron" small /></summary>
            <div className="av-lineage__domain-menu">
              <QueryState query={domains} isEmpty={(items) => !items.length} emptyText="暂无业务域" compact rows={3}>
                {(items) => <fieldset><legend className="av-section-label">筛选业务域</legend>{items.map((domain) => <label key={domain.id}>
                  <input type="checkbox" checked={domainIds.includes(domain.id)} onChange={() => toggleDomain(domain.id)} />
                  <span className={"av-swatch av-swatch--" + swatchOf.get(domain.id)} />{domain.name}
                </label>)}</fieldset>}
              </QueryState>
              <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={!domainIds.length && !focusAsset} onClick={clearFilters}>清除筛选</button>
            </div>
          </details>
          <Seg value={density} label="节点密度" options={[{ value: "overview", label: "表级总览" }, { value: "columns", label: "字段" }]} onChange={setDensity} />
          {focusAsset ? <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={clearFilters}>查看全部血缘</button> : null}
          <Link className="av-btn av-btn--sm av-btn--primary av-lineage__import" to="/lineage/import"><Icon name="upload" small />导入</Link>
        </div>
        <div className="av-canvas av-lineage__canvas">
          <QueryState query={graph} isEmpty={(data) => !data.nodes.length}
            emptyText={domainIds.length || focusAsset ? "没有匹配的血缘" : "暂无血缘"}
            emptyAction={domainIds.length || focusAsset ? <button type="button" className="av-btn av-btn--secondary" onClick={clearFilters}>清除筛选</button> : <Link className="av-btn av-btn--primary" to="/lineage/import">导入血缘</Link>}>
            {() => <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} fitView minZoom={0.1} maxZoom={1.6}
              proOptions={{ hideAttribution: true }} nodesDraggable={false} nodesConnectable={false}
              onNodeClick={(_, node) => { setSelected(node.id); setColumn(null); setShowColumns(false); }}
              onPaneClick={() => { setSelected(null); setColumn(null); setShowColumns(false); }} />}
          </QueryState>
          {graph.isSuccess && graph.data.nodes.length ? <>
            <details className="av-float av-lineage__legend">
              <summary>图例</summary>
              <div className="av-lineage__legend-items">
                <Legend tone="value" label="取值" />
                <Legend tone="influence" label="过滤 / 关联 / 分组" />
                <Legend tone="identity" label="同一张表" />
                <Legend tone="design" label="导入（设计时）" />
                <Legend tone="path" label="选中路径" />
              </div>
            </details>
            <div className="av-float av-lineage__zoom" role="group" aria-label="画布缩放">
              <button type="button" className="av-btn av-btn--sm av-btn--ghost av-btn--icon" aria-label="缩小" onClick={() => flow.zoomOut()}><Icon name="zoomout" /></button>
              <button type="button" className="av-btn av-btn--sm av-btn--ghost" onClick={() => flow.zoomTo(1)}>100%</button>
              <button type="button" className="av-btn av-btn--sm av-btn--ghost av-btn--icon" aria-label="放大" onClick={() => flow.zoomIn()}><Icon name="zoomin" /></button>
              <button type="button" className="av-btn av-btn--sm av-btn--ghost" onClick={() => flow.fitView({ padding: 0.16 })}><Icon name="fit" />适应画布</button>
            </div>
          </> : null}
        </div>
      </div>
      {selectedNode && graph.data ? <aside className="av-inspector av-lineage__inspector" aria-label="检查器">
        <Inspector key={selectedNode.id} node={selectedNode} graph={graph.data} column={column?.split("::")[1] ?? null}
          showColumns={showColumns} onShowColumns={() => setShowColumns(true)} onColumn={(name) => setColumn(selectedNode.id + "::" + name)}
          onClose={() => { setSelected(null); setColumn(null); setShowColumns(false); }} />
      </aside> : null}
    </div>
  );
}

function Legend({ tone, label }: { tone: "value" | "influence" | "identity" | "design" | "path"; label: string }) {
  return <span className={"av-caption av-lineage__legend-item av-lineage__legend-item--" + tone}>
    <svg width="24" height="6" aria-hidden="true">
      {tone === "path" ? <defs><linearGradient id="av-legend-flow" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="24" y2="0">
        <stop offset="0" stopColor="#3d6cf0" /><stop offset="1" stopColor="#22d3ee" /></linearGradient></defs> : null}
      <line x1="0" y1="3" x2="24" y2="3" />
    </svg>{label}
  </span>;
}

function Inspector({ node, graph, column, showColumns, onShowColumns, onColumn, onClose }: {
  node: GNode; graph: Graph; column: string | null; showColumns: boolean; onShowColumns: () => void;
  onColumn: (column: string) => void; onClose: () => void;
}) {
  const [search, setSearch] = useState("");
  const asset = useAsset(node.kind === "design_time" ? undefined : node.id);
  const incoming = graph.edges.filter((e) => e.target === node.id);
  const outgoing = graph.edges.filter((e) => e.source === node.id);
  const name = (id: string) => graph.nodes.find((n) => n.id === id)?.qualified ?? id.replace(/^ref:/, "");
  const colIn = column ? incoming.flatMap((e) => e.columns.filter((c) => c.target_column === column).map((c) => ({ ...c, from: name(e.source) }))) : [];
  const influences = incoming.flatMap((e) => e.columns.filter((c) => !c.target_column).map((c) => ({ ...c, from: name(e.source) })));
  const colOut = column ? outgoing.flatMap((e) => e.columns.filter((c) => c.source_column === column).map((c) => ({ ...c, to: name(e.target) }))) : [];
  const col = asset.data?.columns.find((c) => c.name === column);
  const graphColumnNames = useMemo(() => (graphColumns(graph).get(node.id) ?? []).map((item) => item.name), [graph, node.id]);
  const columns = [...new Set([...(asset.data?.columns.map((item) => item.name) ?? []), ...graphColumnNames])];
  const visibleColumns = columns.filter((name) => name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  return (
    <>
      <div className="av-inspector__hd">
        <div className="av-lineage__inspector-actions">
          <span className="av-section-label">{KIND_LABEL[node.kind] ?? node.kind}{node.datasource ? ` · ${node.datasource.name}` : ""}</span>
          <div className="av-actions">
            {node.kind !== "design_time" ? <Link className="av-btn av-btn--secondary av-btn--sm" to={`/catalog/assets/${node.id}`}>资产</Link> : null}
            <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" aria-label="关闭检查器" onClick={onClose}><Icon name="close" /></button>
          </div>
        </div>
        <span className="av-mono av-lineage__qualified">{node.qualified}{column ? <span className="av-lineage__column-name">.{column}</span> : null}</span>
        {node.note ? <span className="av-caption">{node.note}</span> : null}
      </div>
      <div className="av-inspector__bd">
        {showColumns ? <section className="av-lineage__column-browser" aria-label="全部字段">
          <label className="av-section-label" htmlFor="lineage-column-search">字段 {asset.isSuccess || node.kind === "design_time" ? `· ${columns.length}` : ""}</label>
          <input id="lineage-column-search" className="av-input" type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索字段" autoFocus />
          {node.kind !== "design_time" && asset.isPending ? <span className="av-caption" role="status">正在加载完整字段…</span> : null}
          {node.kind !== "design_time" && asset.isError ? <div className="av-lineage__column-error" role="alert"><span className="av-caption">完整字段加载失败，当前仅显示血缘字段</span><button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => asset.refetch()}>重试</button></div> : null}
          <div className="av-lineage__column-list">{visibleColumns.map((name) => <button key={name} type="button" className="av-btn av-btn--ghost av-lineage__column-item" aria-pressed={column === name} onClick={() => onColumn(name)}><span className="av-mono">{name}</span><span className="av-caption">{asset.data?.columns.find((item) => item.name === name)?.data_type}</span></button>)}</div>
          {!visibleColumns.length && columns.length ? <Empty text="没有匹配字段" compact action={<button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setSearch("")}>清除搜索</button>} /> : null}
          {!columns.length && (asset.isSuccess || node.kind === "design_time") ? <Empty text="暂无字段" compact /> : null}
        </section> : <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={onShowColumns}>查看全部字段</button>}
        {column ? (
          <>
            {col ? <dl className="av-kv"><dt>类型</dt><dd className="av-mono">{col.data_type}</dd>
              <dt>推导</dt><dd><Status value={col.fingerprint_status ?? "unknown"} /> <span className="av-mono av-muted">{col.fingerprint?.slice(0, 10)}</span></dd>
              {col.fingerprint_reason ? <><dt>原因</dt><dd>{col.fingerprint_reason}</dd></> : null}
              <dt>业务含义</dt><dd>{col.confirmations.find((c) => c.valid)?.meaning ?? <span className="av-muted">未确认</span>}</dd></dl> : null}
            <Flow title="来源" items={colIn.map((c) => ({ key: `${c.from}.${c.source_column}`, left: `${shortName(c.from)}.${c.source_column}`, rel: c.relation, origin: c.origin, expr: c.expression }))} />
            <Flow title="下游" items={colOut.map((c) => ({ key: `${c.to}.${c.target_column}`, left: `${shortName(c.to)}.${c.target_column ?? "（影响）"}`, rel: c.relation, origin: c.origin, expr: c.expression }))} />
          </>
        ) : (
          <>
            {node.grains.length ? <dl className="av-kv"><dt>粒度</dt><dd>{node.grains.map((g) => <span key={g.columns.join()} className="av-tag av-lineage__grain-tag">{g.columns.join(", ")} · <Status value={g.status} /></span>)}</dd></dl> : null}
            <Flow title="上游" items={[...new Map(incoming.map((e) => [e.source, e])).values()].map((e) => ({ key: e.source, left: shortName(name(e.source)), rel: e.relations.find((r) => VALUE.has(r)) ?? e.relations[0], origin: e.origins.join("、"), expr: `${e.columns.length} 条列级边` }))} />
            <Flow title="下游" items={[...new Map(outgoing.map((e) => [e.target, e])).values()].map((e) => ({ key: e.target, left: shortName(name(e.target)), rel: e.relations.find((r) => VALUE.has(r)) ?? e.relations[0], origin: e.origins.join("、"), expr: `${e.columns.length} 条列级边` }))} />
          </>
        )}
        {influences.length ? (
          <div className="av-lineage__stack">
            <span className="av-section-label">影响条件 <Hint text="过滤、关联键、分组决定哪些行参与，但值不复制到输出" /></span>
            {influences.map((c, i) => <span key={i} className="av-chain__item"><span className="av-tag">{RELATION_LABEL[c.relation]}</span><span>{shortName(c.from)}.{c.source_column}</span></span>)}
          </div>
        ) : null}
        <span className="av-caption">点击节点中的列，高亮该列的上下游路径。</span>
      </div>
    </>
  );
}

const ORIGIN: Record<string, string> = { parsed: "解析", manual: "人工", import: "导入解析", import_manual: "导入·人工", model_confirmed: "模型·已确认" };

function Flow({ title, items }: { title: string; items: { key: string; left: string; rel: string; origin: string; expr?: string | null }[] }) {
  if (!items.length) return null;
  return (
    <div className="av-lineage__stack">
      <span className="av-section-label">{title}</span>
      {items.map((it) => (
        <div key={it.key} className="av-lineage__flow-item">
          <span className="av-chain__item"><span>{it.left}</span><span className="av-tag av-lineage__relation-tag">{RELATION_LABEL[it.rel] ?? it.rel}</span></span>
          <span className="av-caption">{it.origin.split("、").map((o) => ORIGIN[o] ?? o).join("、")}{it.expr ? ` · ${it.expr}` : ""}</span>
        </div>
      ))}
    </div>
  );
}
